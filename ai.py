from public_prompts import required_prompt as _prompt
"""AI module — GPT-4o-mini responses."""
import os
import re
import json
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    timeout=20.0,
    max_retries=2,
)
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
INPUT_COST_PER_MILLION = float(os.getenv("AI_INPUT_COST_PER_MILLION", "0.15"))
OUTPUT_COST_PER_MILLION = float(os.getenv("AI_OUTPUT_COST_PER_MILLION", "0.60"))

# Только НАШИ данные — не трогаем данные владельца
_CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
_OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
_BOT_TOKEN = os.getenv("SHOPBOT_TOKEN", "")

def sanitize_knowledge(text: str) -> str:
    """Remove only OUR sensitive data before saving to DB."""
    # Конкретные наши токены
    for secret in [_CRYPTOBOT_TOKEN, _OPENAI_KEY, _BOT_TOKEN]:
        if secret and secret in text:
            text = text.replace(secret, '[ВИДАЛЕНО]')
    # Паттерн CryptoBot токена (число:строка)
    text = re.sub(r'\d{4,7}:[A-Za-z0-9_-]{30,}', '[ВИДАЛЕНО]', text)
    # OpenAI ключи
    text = re.sub(r'sk-[A-Za-z0-9]{20,}', '[ВИДАЛЕНО]', text)
    return text


SECURITY_RULES = ''


def system_prompt_admin(shop_name: str, knowledge: str):
    return _prompt('admin', shop_name=shop_name, knowledge=sanitize_knowledge(knowledge or ''))


def system_prompt_customer(shop_name: str, knowledge: str, settings: dict | None=None, catalog: list[dict] | None=None):
    return _prompt('customer', shop_name=shop_name, knowledge=sanitize_knowledge(knowledge or ''), settings=settings or {}, catalog=catalog or [])


def _usage_from_response(response) -> dict:
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    estimated_cost = (
        input_tokens * INPUT_COST_PER_MILLION
        + output_tokens * OUTPUT_COST_PER_MILLION
    ) / 1_000_000
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": estimated_cost,
    }


async def chat(system: str, history: list, user_msg: str, history_limit: int = 3) -> dict:
    messages = [{"role": "system", "content": system}]
    messages.extend(history[-history_limit:])
    messages.append({"role": "user", "content": user_msg})
    try:
        resp = await client.chat.completions.create(
            model=MODEL, messages=messages, max_tokens=500, temperature=0.2
        )
        return {
            "ok": True,
            "content": resp.choices[0].message.content,
            "error_type": None,
            "usage": _usage_from_response(resp),
        }
    except Exception as e:
        return {
            "ok": False,
            "content": "Вибачте, сталася тимчасова помилка. Спробуйте ще раз трохи пізніше 🙏",
            "error_type": type(e).__name__,
            "usage": {"input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0},
        }


CUSTOMER_RESPONSE_SCHEMA = {
    "name": "customer_service_response",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reply": {"type": "string"},
            "intent": {
                "type": "string",
                "enum": ["faq", "new_order", "update_order", "cancel_order", "status", "handoff"],
            },
            "needs_human": {"type": "boolean"},
            "order": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "customer_name": {"type": "string"},
                            "item": {"type": "string"},
                            "datetime_text": {"type": "string"},
                            "phone": {"type": "string"},
                            "details": {"type": "string"},
                        },
                        "required": ["customer_name", "item", "datetime_text", "phone", "details"],
                    },
                ]
            },
        },
        "required": ["reply", "intent", "needs_human", "order"],
    },
}


async def customer_chat(system: str, history: list, user_msg: str, history_limit: int = 10) -> dict:
    messages = [{"role": "system", "content": system}]
    messages.extend(history[-history_limit:])
    messages.append({"role": "user", "content": user_msg})
    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=600,
            temperature=0.1,
            response_format={"type": "json_schema", "json_schema": CUSTOMER_RESPONSE_SCHEMA},
        )
        parsed = json.loads(resp.choices[0].message.content or "{}")
        return {
            "ok": True,
            "content": str(parsed.get("reply") or "").strip(),
            "intent": parsed.get("intent") or "faq",
            "needs_human": bool(parsed.get("needs_human")),
            "order": parsed.get("order"),
            "error_type": None,
            "usage": _usage_from_response(resp),
        }
    except Exception as exc:
        return {
            "ok": False,
            "content": "",
            "intent": "faq",
            "needs_human": False,
            "order": None,
            "error_type": type(exc).__name__,
            "usage": {"input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0},
        }


async def detect_order_intent(history: list, user_msg: str, return_usage: bool = False):
    messages = [
        {
            "role": "system",
            _prompt('order_intent'): _prompt('order_intent'),
        }
    ]
    messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_msg})
    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=10,
            temperature=0,
        )
        content = (resp.choices[0].message.content or "").strip().lower()
        if content in {"cancel", "status", "update", "new", "other"}:
            if return_usage:
                return {"intent": content, "usage": _usage_from_response(resp)}
            return content
    except Exception:
        pass
    if return_usage:
        return {"intent": "other", "usage": {"input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}}
    return "other"


async def extract_and_merge_knowledge(admin_msg: str, existing: str) -> str:
    """Merge new info from admin into existing knowledge base."""
    messages = [
        {_prompt('knowledge_extraction'): _prompt('knowledge_extraction'), _prompt('knowledge_extraction'): _prompt('knowledge_extraction')},
        {"role": "user", "content": f"Існуюча база:\n{existing or '(порожньо)'}\n\nНове від власника:\n{admin_msg}"}
    ]
    try:
        resp = await client.chat.completions.create(
            model=MODEL, messages=messages, max_tokens=1000, temperature=0.1
        )
        choice = resp.choices[0]
        if getattr(choice, "finish_reason", None) != "stop":
            return existing or ""
        merged = sanitize_knowledge(choice.message.content or "").strip()
        return merged or (existing or "")
    except Exception:
        return existing or ""
