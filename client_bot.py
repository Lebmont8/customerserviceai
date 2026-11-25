"""
CustomerServiceAI — клієнтський бот (шаблон).
Запускається окремо для кожного магазину зі своїм токеном.
"""
import os
import re
import logging
import sys
import hashlib
import time
from datetime import datetime, timedelta, time as dt_time
from types import SimpleNamespace
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
import database as db
import ai
import payments
from logging_utils import configure_logging
from runtime_health import record_polling_conflict

configure_logging()
logger = logging.getLogger(__name__)
ADMIN_ID = int(__import__('os').environ.get('ADMIN_ID') or '0')
SUPPORT_INSTAGRAM = "@vexoraworkingspace"
MAX_USER_MESSAGE_LEN = 2000
MAX_AI_RESPONSE_LEN = 1200
DELAYED_MESSAGE_SECONDS = 120
PENDING_RETRY_LIMIT = 8
REPEAT_SPAM_THRESHOLD = 3
REPEAT_SPAM_SERIES_LIMIT = 3
REPEAT_SPAM_IGNORE_SECONDS = 72 * 60 * 60
BURST_WINDOW_SECONDS = 12
BURST_WARNING_LIMIT = 5
BURST_BLOCK_LIMIT = 9
BURST_BLOCK_SECONDS = 10 * 60
SPAM_BLOCK_SECONDS = 24 * 60 * 60
ORDER_CANCEL_PATTERNS = [
    r"\bскасувати заявку\b",
    r"\bскасувати замовлення\b",
    r"\bвідмінити заявку\b",
    r"\bвідмінити замовлення\b",
    r"\bхочу скасувати заявку\b",
    r"\bхочу скасувати замовлення\b",
    r"\bскасуй заявку\b",
    r"\bскасуй замовлення\b",
    r"\bзаявка не потрібна\b",
    r"\bзамовлення не потрібне\b",
    r"\bскасувати\b",
    r"\bвідміна\b",
    r"\bотменить заявк[уи]\b",
    r"\bотменить заказ\b",
    r"\bcancel (?:my )?(?:order|booking)\b",
    r"\b(?:bestellung|termin) stornieren\b",
]

SPAM_LINK_RE = re.compile(
    r"(?:https?://|www\.|t\.me/|telegram\.me/|bit\.ly/|tinyurl\.com|wa\.me/)",
    re.IGNORECASE,
)
PROMO_SPAM_RE = re.compile(
    r"\b(?:казино|casino|ставк[аи]|беттинг|1xbet|pin[- ]?up|слот[ыи]|viagra|кредит|займ|инвестируй|пассивный доход|airdrop|giveaway|free money)\b",
    re.IGNORECASE,
)
TOXIC_RE = re.compile(
    r"\b(?:идиот|дебил|тупой|тупая|сука|бляд|хуй|пизд|нахуй|fuck|shit)\b",
    re.IGNORECASE,
)
PROMPT_ATTACK_RE = re.compile(
    r"(?:забудь\s+(?:все\s+)?(?:инструкции|правила)|ignore\s+(?:all\s+)?(?:previous|system)|system\s+prompt|developer\s+message|покажи\s+(?:промпт|инструкции)|раскрой\s+(?:промпт|инструкции))",
    re.IGNORECASE,
)
OFFTOPIC_RE = re.compile(
    r"\b(?:анекдот|шутк[ауи]|погода|политик[аи]|новости|футбол|курс биткоина|биткоин|крипт[ауы]|python|javascript|код|сочинение|реферат|домашк[ауи]|переведи текст)\b",
    re.IGNORECASE,
)
ORDER_UPDATE_PATTERNS = [
    r"\bзмінити заявку\b",
    r"\bзмінити замовлення\b",
    r"\bзмінити запис\b",
    r"\bперенести заявку\b",
    r"\bперенести замовлення\b",
    r"\bперенести запис\b",
    r"\bпоміняти час\b",
    r"\bпоміняти дату\b",
    r"\bзмінити час\b",
    r"\bзмінити дату\b",
    r"\bизменить заявку\b",
    r"\bизменить заказ\b",
    r"\bизменить запись\b",
    r"\bперенести заявку\b",
    r"\bперенести заказ\b",
    r"\bперенести запись\b",
    r"\bпоменять время\b",
    r"\bпоменять дату\b",
    r"\bизменить время\b",
    r"\bизменить дату\b",
]
ORDER_STATUS_PATTERNS = [
    r"\bстатус заявки\b",
    r"\bстатус замовлення\b",
    r"\bмоя заявка\b",
    r"\bмоє замовлення\b",
    r"\bде моя заявка\b",
    r"\bде моє замовлення\b",
    r"\bзаявк[аи] .*статус\b",
    r"\bактивн[а-яіїє]* .*заявк[аи]\b",
    r"\bактивн[а-яіїє]* .*замовленн[яі]\b",
    r"\bчи є .*заявк[аи]\b",
    r"\bчи є .*замовленн[яі]\b",
    r"\bзаявка .*актуальн[а-яіїє]*\b",
    r"\bзамовлення .*актуальн[а-яіїє]*\b",
    r"\bзаявка .*в силі\b",
    r"\bзамовлення .*в силі\b",
]
HUMAN_REQUEST_RE = re.compile(
    r"\b(?:менеджер|оператор|людина|человек|живой сотрудник|human|mitarbeiter|berater)\b",
    re.IGNORECASE,
)


async def _shop_ai_context(shop_id: str) -> tuple[dict, list[dict]]:
    return await db.get_shop_settings(shop_id), await db.list_catalog_items(shop_id)


async def _record_ai_result(shop_id: str, ai_result: dict):
    usage = ai_result.get("usage") or {}
    await db.record_ai_usage(
        shop_id,
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        float(usage.get("estimated_cost_usd") or 0),
        count_request=False,
    )


def _classify_message(text: str) -> tuple[str, str | None]:
    normalized = re.sub(r"\s+", " ", (text or "").strip())
    lower = normalized.lower()

    if PROMPT_ATTACK_RE.search(normalized):
        return (
            "blocked",
            "Я не можу показувати внутрішні інструкції. Можу допомогти з питанням про цей бізнес.",
        )

    if TOXIC_RE.search(normalized):
        return (
            "toxic",
            "Я можу допомогти з товарами, послугами, доставкою, оплатою або оформленням заявки. Напишіть, будь ласка, по суті питання.",
        )

    link_count = len(SPAM_LINK_RE.findall(normalized))
    if link_count >= 2 or (link_count and PROMO_SPAM_RE.search(normalized)):
        return "spam", "Повідомлення відхилено як підозріле. Надішліть, будь ласка, одне питання без рекламних посилань."

    if PROMO_SPAM_RE.search(normalized):
        return "spam", "Повідомлення відхилено як підозріле. Напишіть, будь ласка, питання про цей бізнес."

    if OFFTOPIC_RE.search(lower) and not any(
        marker in lower for marker in ["доставка", "оплата", "замов", "заказ", "товар", "послуг", "заявк", "контакт", "адрес", "графік", "график"]
    ):
        return (
            "offtopic",
            "Я можу допомогти з товарами, послугами, доставкою, оплатою або оформленням заявки. Що саме підказати?",
        )

    return "client", None


async def _check_message_limits(shop_id: str, user_id: int, text: str) -> tuple[bool, str | None]:
    now = time.time()
    state = await db.get_spam_state(shop_id, user_id)

    if state["ignore_until"] > now:
        return True, None

    normalized_text = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
    last_text = state.get("last_text", "")
    repeat_count = state.get("repeat_count", 0)
    series_count = state.get("series_count", 0)
    burst_count = state.get("burst_count", 0)
    last_message_at = state.get("last_message_at", 0)
    ignore_until = state.get("ignore_until", 0)

    if last_text == normalized_text:
        repeat_count += 1
    else:
        last_text = normalized_text
        repeat_count = 1

    if now - last_message_at <= BURST_WINDOW_SECONDS:
        burst_count += 1
    else:
        burst_count = 1

    warning_text = None
    should_block = False

    if burst_count == BURST_WARNING_LIMIT:
        warning_text = "Будь ласка, пишіть трохи повільніше, щоб я встиг коректно обробити повідомлення 🙏"
    elif burst_count >= BURST_BLOCK_LIMIT:
        ignore_until = now + BURST_BLOCK_SECONDS
        should_block = True

    if repeat_count == REPEAT_SPAM_THRESHOLD:
        series_count += 1

        if series_count == 1:
            warning_text = "Будь ласка, не дублюйте одне й те саме повідомлення 🙏"
        elif series_count == 2:
            warning_text = "Це повторний спам однаковими повідомленнями. Якщо так продовжиться, бот перестане відповідати на 72 години."
        elif series_count >= REPEAT_SPAM_SERIES_LIMIT:
            ignore_until = now + REPEAT_SPAM_IGNORE_SECONDS
            should_block = True

    await db.save_spam_state(
        shop_id,
        user_id,
        last_text,
        repeat_count,
        series_count,
        ignore_until,
        burst_count,
        now,
    )

    return should_block, warning_text


async def _handle_ai_failure(context: ContextTypes.DEFAULT_TYPE, shop_id: str, shop_name: str, error_type: str):
    now = time.time()
    state = await db.get_ai_error_state(shop_id)
    consecutive_errors = state.get("consecutive_errors", 0) + 1
    last_notified_at = state.get("last_notified_at", 0)

    should_notify = consecutive_errors >= 2 and (now - last_notified_at > 600)
    if should_notify:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"⚠️ CustomerServiceAI: 2 AI-помилки підряд\n\n"
                    f"Магазин: {shop_name or 'без назви'}\n"
                    f"Shop ID: {shop_id}\n"
                    f"Помилка: {error_type}"
                )
            )
            last_notified_at = now
        except Exception as e:
            logger.error(f"Failed to notify admin about AI errors: {e}")

    await db.save_ai_error_state(shop_id, consecutive_errors, error_type, now, last_notified_at)


def _sanitize_ai_response(response: str) -> str:
    text = (response or "").strip()
    if not text:
        return ""

    if len(text) > MAX_AI_RESPONSE_LEN and "[ЗАЯВКА]" not in text:
        text = text[:MAX_AI_RESPONSE_LEN].rstrip()
        if "\n" in text:
            text = text.rsplit("\n", 1)[0].rstrip() or text
        text += "…"

    return text


def _fallback_safe_reply(shop: dict) -> str:
    knowledge = (shop.get("knowledge") or "").lower()
    if any(marker in knowledge for marker in ["контакти", "зв'язатися", "telegram", "instagram", "телефон"]):
        return "Зараз не можу коректно відповісти. Будь ласка, напишіть менеджеру напряму через контакти, вказані в цьому боті 🙏"
    return "Вибачте, я зараз не можу коректно відповісти. Спробуйте ще раз трохи пізніше 🙏"


def _looks_like_order_cancel(text: str) -> bool:
    normalized = text.strip().lower()
    if re.search(
        r"\b(?:не|not|don't|do not|nicht)\b.{0,30}\b(?:скасов|отмен|cancel|stornier)",
        normalized,
    ):
        return False
    return any(re.search(pattern, normalized) for pattern in ORDER_CANCEL_PATTERNS)


def _looks_like_order_update(text: str) -> bool:
    normalized = text.strip().lower()
    return any(re.search(pattern, normalized) for pattern in ORDER_UPDATE_PATTERNS)


def _looks_like_order_status_question(text: str) -> bool:
    normalized = text.strip().lower()
    return any(re.search(pattern, normalized) for pattern in ORDER_STATUS_PATTERNS)


def _parse_order_line(order_line: str) -> dict:
    parsed = {
        "customer_name": "",
        "item": "",
        "datetime_text": "",
        "phone": "",
        "details": "",
    }
    payload = order_line.replace("[ЗАЯВКА]", "").strip()
    for part in payload.split("|"):
        segment = part.strip()
        if ":" not in segment:
            continue
        key, value = [x.strip() for x in segment.split(":", 1)]
        low_key = key.lower()
        if "ім" in low_key:
            parsed["customer_name"] = value
        elif "послуга" in low_key or "товар" in low_key:
            parsed["item"] = value
        elif "дата" in low_key or "час" in low_key:
            parsed["datetime_text"] = value
        elif "телефон" in low_key:
            parsed["phone"] = value
        elif "детал" in low_key:
            parsed["details"] = value
    return parsed


def _missing_order_fields(order_data: dict, required_fields: list[str] | None = None) -> list[str]:
    labels = {
        "customer_name": "ім'я",
        "item": "товар/послуга",
        "datetime_text": "дата/час",
        "phone": "телефон",
        "details": "деталі",
    }
    required_fields = required_fields or ["item", "datetime_text", "phone"]
    return [labels[key] for key in required_fields if not (order_data.get(key) or "").strip()]


def _order_summary(order_data: dict) -> str:
    return (
        "Перевірте заявку:\n"
        f"Ім'я: {order_data.get('customer_name') or '—'}\n"
        f"Товар/Послуга: {order_data.get('item') or '—'}\n"
        f"Дата/Час: {order_data.get('datetime_text') or '—'}\n"
        f"Телефон: {order_data.get('phone') or '—'}\n"
        f"Деталі: {order_data.get('details') or '—'}"
    )


MONTHS = {
    "січня": 1, "января": 1, "january": 1,
    "лютого": 2, "февраля": 2, "february": 2,
    "березня": 3, "марта": 3, "march": 3,
    "квітня": 4, "апреля": 4, "april": 4,
    "травня": 5, "мая": 5, "may": 5,
    "червня": 6, "июня": 6, "june": 6,
    "липня": 7, "июля": 7, "july": 7,
    "серпня": 8, "августа": 8, "august": 8,
    "вересня": 9, "сентября": 9, "september": 9,
    "жовтня": 10, "октября": 10, "october": 10,
    "листопада": 11, "ноября": 11, "november": 11,
    "грудня": 12, "декабря": 12, "december": 12,
}

WEEKDAYS = {
    "понеділок": 0, "понедельник": 0,
    "вівторок": 1, "вторник": 1,
    "середу": 2, "середа": 2, "среду": 2, "среда": 2,
    "четвер": 3, "четверг": 3,
    "п'ятницю": 4, "пятницю": 4, "п'ятниця": 4, "пятница": 4,
    "суботу": 5, "субота": 5,
    "неділю": 6, "неділя": 6, "воскресенье": 6,
}


def _parse_order_datetime(datetime_text: str, now: datetime | None = None) -> datetime | None:
    text = re.sub(r"\s+", " ", (datetime_text or "").strip().lower())
    if not text:
        return None

    now = now or datetime.now()
    date_value = None

    if any(word in text for word in ["післязавтра", "послезавтра"]):
        date_value = (now + timedelta(days=2)).date()
    elif any(word in text for word in ["завтра"]):
        date_value = (now + timedelta(days=1)).date()
    elif any(word in text for word in ["сьогодні", "сегодня"]):
        date_value = now.date()
    else:
        numeric_date = re.search(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", text)
        if numeric_date:
            day = int(numeric_date.group(1))
            month = int(numeric_date.group(2))
            year = int(numeric_date.group(3)) if numeric_date.group(3) else now.year
            if year < 100:
                year += 2000
            try:
                candidate = datetime(year, month, day).date()
                if not numeric_date.group(3) and candidate < now.date():
                    candidate = datetime(now.year + 1, month, day).date()
                date_value = candidate
            except ValueError:
                date_value = None

    if date_value is None:
        month_names = "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True))
        named_date = re.search(rf"\b(\d{{1,2}})\s+({month_names})(?:\s+(\d{{2,4}}))?\b", text)
        if named_date:
            day = int(named_date.group(1))
            month = MONTHS[named_date.group(2)]
            year = int(named_date.group(3)) if named_date.group(3) else now.year
            if year < 100:
                year += 2000
            try:
                candidate = datetime(year, month, day).date()
                if not named_date.group(3) and candidate < now.date():
                    candidate = datetime(now.year + 1, month, day).date()
                date_value = candidate
            except ValueError:
                return None

    if date_value is None:
        for word, weekday in WEEKDAYS.items():
            if re.search(rf"\b{re.escape(word)}\b", text):
                days_ahead = (weekday - now.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                date_value = (now + timedelta(days=days_ahead)).date()
                break

    if date_value is None:
        date_value = now.date()

    time_match = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if not time_match:
        time_match = re.search(r"(?:^|\s)(?:о|об|в|у|на)\s*(\d{1,2})\.(\d{2})\b", text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
    else:
        time_match = re.search(
            r"(?:^|\s)(?:о|об|в|у|на|at)?\s*(\d{1,2})\s*(?:год(?:ині|ина|ин|ину)?|час(?:ов|а)?|h)\b",
            text,
        )
        if not time_match:
            time_match = re.search(r"(?:^|\s)(?:о|об|в|у|на)\s*(\d{1,2})\b", text)
        if not time_match:
            return None
        hour = int(time_match.group(1))
        minute = 0

    try:
        order_time = dt_time(hour=hour, minute=minute)
    except ValueError:
        return None

    candidate = datetime.combine(date_value, order_time)
    if candidate < now and date_value == now.date():
        return None
    return candidate


def _parse_auto_complete_at(datetime_text: str) -> str | None:
    order_dt = _parse_order_datetime(datetime_text)
    if not order_dt:
        return None

    return (order_dt + timedelta(minutes=10)).isoformat(sep=" ", timespec="seconds")


def _order_datetime_from_order(order: dict) -> datetime | None:
    parsed = _parse_order_datetime(order.get("datetime_text") or "")
    if parsed:
        return parsed

    auto_complete_at = order.get("auto_complete_at")
    if not auto_complete_at:
        return None
    try:
        return datetime.fromisoformat(auto_complete_at) - timedelta(minutes=10)
    except Exception:
        return None


def _is_within_24_hours(order: dict) -> bool:
    order_dt = _order_datetime_from_order(order)
    if not order_dt:
        return False
    delta = order_dt - datetime.now()
    return timedelta(0) <= delta < timedelta(hours=24)


def _extract_manager_contact(knowledge: str) -> str:
    lines = [line.strip() for line in (knowledge or "").splitlines() if line.strip()]
    contact_markers = [
        "контакти",
        "зв'язатися",
        "звязатися",
        "зв'язок",
        "зв'язку",
        "менеджер",
        "телефон",
        "telegram",
        "instagram",
    ]

    for line in lines:
        low = line.lower()
        if any(marker in low for marker in contact_markers):
            value = line.split(":", 1)[-1].strip() if ":" in line else line
            if value:
                return value

    phone = re.search(r"(?:\+?\d[\d\s().-]{7,}\d)", knowledge or "")
    if phone:
        return phone.group(0).strip()

    username = re.search(r"@[A-Za-z0-9_]{5,32}", knowledge or "")
    if username:
        return username.group(0)

    return ""


def _manager_contact_reply(shop: dict) -> str:
    contact = _extract_manager_contact(shop.get("knowledge") or "")
    if contact:
        return (
            "Щоб змінити або скасувати заявку менш ніж за 24 години до призначеного часу, "
            f"будь ласка, напишіть менеджеру напряму: {contact}"
        )
    return (
        "Щоб змінити або скасувати заявку менш ніж за 24 години до призначеного часу, "
        "потрібно написати менеджеру напряму. Контакт менеджера поки не вказаний у цьому боті."
    )


def _maintenance_reply(maintenance: dict) -> str:
    ends_at_raw = maintenance.get("ends_at") or ""
    try:
        ends_at = datetime.fromisoformat(ends_at_raw)
        until_text = ends_at.strftime("%d.%m.%Y %H:%M")
    except Exception:
        until_text = "після завершення технічних робіт"

    custom_message = (maintenance.get("message") or "").strip()
    if custom_message:
        return f"{custom_message}\n\nБот буде доступний з {until_text}."
    return (
        "Вибачте, зараз проводяться технічні роботи. "
        f"Бот буде доступний з {until_text}."
    )


async def _notify_order_event(context: ContextTypes.DEFAULT_TYPE, shop: dict, user_info, event_type: str, order_data: dict, order_id: int):
    username = f"@{user_info.username}" if user_info.username else ""
    display_name = user_info.full_name or "Клієнт"
    contact_line = display_name
    if username:
        contact_line += f" ({username})"
    await db.enqueue_outbox(
        event_key=f"order:{shop['shop_id']}:{order_id}:{event_type}",
        shop_id=shop["shop_id"],
        topic="owner_order_event",
        payload={
            "admin_id": int(shop["admin_id"]),
            "order_id": int(order_id),
            "event_type": event_type,
            "contact": contact_line,
            "order": order_data,
        },
    )


def _is_delayed_update(update: Update) -> bool:
    msg_date = getattr(update.message, "date", None)
    if not msg_date:
        return False
    now = datetime.now(msg_date.tzinfo) if msg_date.tzinfo else datetime.now()
    return (now - msg_date).total_seconds() > DELAYED_MESSAGE_SECONDS


def _with_delay_apology(response: str) -> str:
    return (
        "Вибачте за затримку, проводились технічні роботи. "
        "Відповідаю на ваше повідомлення:\n\n"
        f"{response}"
    )


def _pending_user_info(user_id: int, client: dict | None):
    first_name = (client or {}).get("first_name") or "Клієнт"
    username = (client or {}).get("username") or ""
    return SimpleNamespace(id=user_id, username=username, full_name=first_name)


async def _process_ai_response(
    context: ContextTypes.DEFAULT_TYPE,
    shop: dict,
    user_id: int,
    user_info,
    user_text: str,
    response: str,
    delayed: bool = False,
    source_update_id: int | None = None,
    order_data: dict | None = None,
):
    final_response = _with_delay_apology(response) if delayed else response

    if order_data is not None:
        settings = await db.get_shop_settings(shop["shop_id"])
        required_fields = list(settings["required_order_fields"])
        item_name = (order_data.get("item") or "").strip().casefold()
        for catalog_item in await db.list_catalog_items(shop["shop_id"]):
            if item_name and catalog_item["name"].strip().casefold() == item_name:
                required_fields = list(dict.fromkeys(required_fields + catalog_item["required_fields"]))
                break
        missing = _missing_order_fields(order_data, required_fields)
        if missing:
            reply = (
                "Щоб підготувати заявку, уточніть: " + ", ".join(missing) + ". "
                "Я не створюватиму її без цих даних."
            )
            await db.add_message(shop["shop_id"], user_id, "user", user_text)
            await db.add_message(shop["shop_id"], user_id, "assistant", reply)
            await context.bot.send_message(chat_id=user_id, text=reply)
            return

        order_id = await db.create_order_draft(
            shop["shop_id"], user_id,
            order_data["customer_name"], order_data["item"],
            order_data["datetime_text"], order_data["phone"],
            order_data["details"], source_update_id,
        )
        await db.add_analytics_event(shop["shop_id"], "order_draft")
        summary = _order_summary(order_data)
        await db.add_message(shop["shop_id"], user_id, "user", user_text)
        await db.add_message(shop["shop_id"], user_id, "assistant", summary)
        await context.bot.send_message(
            chat_id=user_id,
            text=_with_delay_apology(summary) if delayed else summary,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Підтвердити", callback_data=f"order_confirm_{order_id}"),
                    InlineKeyboardButton("✏️ Змінити", callback_data=f"order_edit_{order_id}"),
                ],
                [InlineKeyboardButton("❌ Скасувати", callback_data=f"order_reject_{order_id}")],
            ]),
        )
        return

    await db.add_message(shop["shop_id"], user_id, "user", user_text)
    await db.add_message(shop["shop_id"], user_id, "assistant", response)
    await context.bot.send_message(chat_id=user_id, text=final_response)


async def process_pending_messages_job(context: ContextTypes.DEFAULT_TYPE):
    shop_id = os.getenv("CLIENT_SHOP_ID", "")
    if not shop_id:
        return

    maintenance = await db.get_active_maintenance()
    if maintenance:
        return

    shop = await db.get_shop_by_id(shop_id)
    if not shop or not await payments.is_active(shop_id):
        return

    settings, catalog = await _shop_ai_context(shop_id)
    if not settings["ai_enabled"]:
        return
    pending_messages = await db.claim_pending_client_messages(shop_id, limit=PENDING_RETRY_LIMIT)
    for item in pending_messages:
        if not await db.reserve_ai_request(shop_id):
            await db.mark_pending_client_message_attempt(item["id"], "UsageLimit")
            continue
        user_id = int(item["user_id"])
        user_text = item["text"]
        history = await db.get_conversation(shop_id, user_id)
        system = ai.system_prompt_customer(shop["shop_name"], shop["knowledge"] or "", settings, catalog)
        ai_result = await ai.customer_chat(system, history, user_text, history_limit=10)
        await _record_ai_result(shop_id, ai_result)
        response = _sanitize_ai_response(ai_result["content"])

        if ai_result["ok"] and response:
            client = await db.get_client(shop_id, user_id)
            pending_user = _pending_user_info(user_id, client)
            if ai_result.get("needs_human"):
                await _request_human(shop, pending_user, user_text, int(item["id"]))
            await _process_ai_response(
                context,
                shop,
                user_id,
                pending_user,
                user_text,
                response,
                delayed=True,
                order_data=None if ai_result.get("needs_human") else ai_result.get("order"),
            )
            await db.mark_pending_client_message_done(item["id"])
            await db.reset_ai_error_state(shop_id)
            logger.info("Pending client message answered | shop_id=%s user_id=%s pending_id=%s", shop_id, user_id, item["id"])
        else:
            error_type = ai_result.get("error_type") or ("EmptyAIResponse" if ai_result["ok"] else "UnknownError")
            await db.mark_pending_client_message_attempt(item["id"], error_type)
            logger.warning("Pending client message retry failed | shop_id=%s user_id=%s pending_id=%s error=%s", shop_id, user_id, item["id"], error_type)


async def post_init(app):
    await db.init_db()
    await payments.init_payments_db()
    await app.bot.set_my_commands([
        BotCommand("start", "Почати"),
    ])
    app.job_queue.run_repeating(
        process_pending_messages_job,
        interval=60,
        first=20,
        name="process_pending_messages_job",
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    shop_id = os.getenv("CLIENT_SHOP_ID", "")
    if not shop_id:
        await update.message.reply_text("👋 Вітаю! Бот тимчасово недоступний. Спробуйте пізніше.")
        return

    shop = await db.get_shop_by_id(shop_id)
    if not shop:
        await update.message.reply_text("❌ Магазин не знайдено. Спробуйте пізніше.")
        return

    maintenance = await db.get_active_maintenance()
    if maintenance:
        await update.message.reply_text(_maintenance_reply(maintenance))
        return

    shop_name = shop["shop_name"] or "бізнес"

    await update.message.reply_text(
        f"👋 Вітаю! Я — AI-консультант {shop_name}.\n\n"
        f"Можете написати мені будь-яке питання про:\n"
        f"• товари або послуги\n"
        f"• графік роботи\n"
        f"• адресу\n"
        f"• доставку\n"
        f"• способи оплати\n"
        f"• акції та знижки\n"
        f"• гарантії, повернення або обмін\n"
        f"• наявність товарів\n"
        f"• особливі умови\n"
        f"• як зв'язатися з менеджером\n\n"
        f"Напишіть ваше питання, і я допоможу вам розібратися.\n\n"
        f"Бота створено за допомогою: @ownercustomerservicebot"
    )


async def _request_human(shop: dict, user_info, text: str, update_id: int):
    await db.set_human_pause(shop["shop_id"], user_info.id, True, "customer_request")
    await db.enqueue_outbox(
        event_key=f"handoff:{shop['shop_id']}:{update_id}",
        shop_id=shop["shop_id"],
        topic="owner_handoff",
        payload={
            "admin_id": int(shop["admin_id"]),
            "user_id": int(user_info.id),
            "display_name": user_info.full_name or "Клієнт",
            "username": user_info.username or "",
            "text": text,
        },
    )


async def handle_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    shop_id = os.getenv("CLIENT_SHOP_ID", "")
    if not shop_id or not await db.claim_update(shop_id, update.update_id):
        return
    shop = await db.get_shop_by_id(shop_id)
    if not shop:
        await query.edit_message_text("❌ Бізнес не знайдено.")
        return
    user_id = update.effective_user.id
    data = query.data or ""
    try:
        order_id = int(data.rsplit("_", 1)[-1])
    except ValueError:
        await query.edit_message_text("❌ Некоректна дія із заявкою.")
        return

    if data.startswith("order_confirm_"):
        confirmed, order = await db.confirm_order_draft(shop_id, user_id, order_id)
        if not confirmed or not order:
            await query.edit_message_text("Заявка вже оброблена або не належить цьому чату.")
            return
        await db.add_analytics_event(shop_id, "order")
        await _notify_order_event(context, shop, update.effective_user, "created", order, order_id)
        await db.add_audit_event(
            shop_id, "customer", user_id, "order_confirmed", "order", str(order_id)
        )
        await query.edit_message_text(
            "✅ Заявку підтверджено і передано бізнесу.\n\n" + _order_summary(order)
        )
        return

    if data.startswith("order_reject_"):
        rejected = await db.reject_order_draft(shop_id, user_id, order_id)
        await query.edit_message_text(
            "❌ Чернетку заявки скасовано." if rejected else "Заявка вже оброблена."
        )
        return

    if data.startswith("order_edit_"):
        order = await db.get_order(shop_id, order_id)
        if not order or order.get("user_id") != user_id or order.get("status") != "pending_confirmation":
            await query.edit_message_text("Заявка вже оброблена або не належить цьому чату.")
            return
        await query.edit_message_text(
            _order_summary(order) + "\n\nНапишіть одним повідомленням, що саме потрібно змінити."
        )
        return

    if data.startswith("order_cancel_confirm_"):
        order = await db.get_order(shop_id, order_id)
        if not order or order.get("user_id") != user_id:
            await query.edit_message_text("Заявка не знайдена у цьому чаті.")
            return
        cancelled = await db.cancel_order(shop_id, order_id)
        if cancelled:
            await _notify_order_event(context, shop, update.effective_user, "cancelled", order, order_id)
            await db.add_audit_event(
                shop_id, "customer", user_id, "order_cancelled", "order", str(order_id)
            )
        await query.edit_message_text(
            "✅ Заявку скасовано." if cancelled else "Заявка вже не активна."
        )
        return

    if data.startswith("order_cancel_keep_"):
        await query.edit_message_text("Добре, заявка залишається активною.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    incident_id = f"INC-{int(time.time())}-{id(context.error) % 10000:04d}"
    error = context.error
    error_info = (type(error), error, error.__traceback__) if error else None
    if type(error).__name__ == "Conflict":
        record_polling_conflict("client", os.getenv("CLIENT_SHOP_ID", ""))
    logger.error(
        "Unhandled client bot error | incident_id=%s error_type=%s",
        incident_id,
        type(error).__name__ if error else "Unknown",
        exc_info=error_info,
    )
    message = getattr(update, "effective_message", None)
    if message:
        try:
            await message.reply_text(
                f"Сталася технічна помилка. Спробуйте ще раз. Код звернення: {incident_id}"
            )
        except Exception:
            logger.warning("Could not deliver incident id | incident_id=%s", incident_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    if not text:
        return
    if len(text) > MAX_USER_MESSAGE_LEN:
        await update.message.reply_text("Будь ласка, надішліть коротше повідомлення одним запитом 🙏")
        return

    shop_id = os.getenv("CLIENT_SHOP_ID", "")
    if not shop_id:
        await update.message.reply_text("⏸ Бот тимчасово недоступний. Спробуйте пізніше.")
        return

    shop = await db.get_shop_by_id(shop_id)
    if not shop:
        await update.message.reply_text("❌ Магазин не знайдено.")
        return
    if not await db.claim_update(shop_id, update.update_id):
        logger.info("Duplicate Telegram update ignored | shop_id=%s update_id=%s", shop_id, update.update_id)
        return

    maintenance = await db.get_active_maintenance()
    if maintenance:
        await update.message.reply_text(_maintenance_reply(maintenance))
        return

    category, category_reply = _classify_message(text)
    if category == "spam":
        logger.info("Spam message rejected | shop_id=%s user_id=%s", shop_id, user_id)
    if category_reply:
        if category not in {"spam", "blocked"}:
            await db.add_message(shop_id, user_id, "user", text)
            await db.add_message(shop_id, user_id, "assistant", category_reply)
        await update.message.reply_text(category_reply)
        return

    is_blocked, warning_text = await _check_message_limits(shop_id, user_id, text)
    if warning_text:
        await update.message.reply_text(warning_text)
        return
    if is_blocked:
        return

    # Перевіряємо підписку
    if not await payments.is_active(shop_id):
        await update.message.reply_text("⏸ Бот тимчасово недоступний. Спробуйте пізніше.")
        return

    settings, catalog = await _shop_ai_context(shop_id)
    if not settings["ai_enabled"]:
        await update.message.reply_text("⏸ AI цього бізнесу ще не увімкнено. Спробуйте пізніше.")
        return
    await update.message.chat.send_action("typing")

    logger.info("Incoming client message | shop_id=%s user_id=%s length=%s", shop_id, user_id, len(text))

    # Зберігаємо ім'я/ник клієнта
    user_info = update.effective_user
    await db.save_client(
        shop_id,
        user_id,
        user_info.username or "",
        user_info.first_name or ""
    )
    await db.add_analytics_event(shop_id, "inquiry")

    if HUMAN_REQUEST_RE.search(text):
        await db.add_analytics_event(shop_id, "handoff")
        await _request_human(shop, user_info, text, update.update_id)
        reply = "Передаю діалог менеджеру. AI у цьому чаті поставлено на паузу до відповіді людини."
        await db.add_message(shop_id, user_id, "user", text)
        await db.add_message(shop_id, user_id, "assistant", reply)
        await update.message.reply_text(reply)
        return

    if await db.is_human_paused(shop_id, user_id):
        await _request_human(shop, user_info, text, update.update_id)
        await db.add_message(shop_id, user_id, "user", text)
        await update.message.reply_text(
            "Ваше повідомлення передано менеджеру. AI залишається на паузі."
        )
        return

    if not await db.reserve_ai_request(shop_id):
        await db.add_analytics_event(shop_id, "usage_limit")
        await update.message.reply_text("⏸ Місячний ліміт AI вичерпано. Менеджер бізнесу вже може це побачити в аналітиці.")
        return

    history = await db.get_conversation(shop_id, user_id)
    intent_result = await ai.detect_order_intent(history, text, return_usage=True)
    await _record_ai_result(shop_id, intent_result)
    intent = intent_result["intent"]
    logger.info(f"Detected intent | shop_id={shop_id} user_id={user_id} intent={intent}")

    if intent in {"cancel", "update"} or _looks_like_order_cancel(text) or _looks_like_order_update(text):
        active_order = await db.get_active_order(shop_id, user_id)
        if active_order and _is_within_24_hours(active_order):
            await _request_human(shop, user_info, text, update.update_id)
            manager_reply = _manager_contact_reply(shop) + "\n\nAI у цьому чаті поставлено на паузу."
            await db.add_message(shop_id, user_id, "user", text)
            await db.add_message(shop_id, user_id, "assistant", manager_reply)
            await update.message.reply_text(manager_reply)
            return

    if intent == "cancel" or _looks_like_order_cancel(text):
        logger.info("Cancel request detected | shop_id=%s user_id=%s", shop_id, user_id)
        active_order = await db.get_active_order(shop_id, user_id)
        if active_order:
            cancel_reply = "Підтвердіть скасування цієї заявки:\n\n" + _order_summary(active_order)
            await db.add_message(shop_id, user_id, "user", text)
            await db.add_message(shop_id, user_id, "assistant", cancel_reply)
            await update.message.reply_text(
                cancel_reply,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "❌ Так, скасувати",
                        callback_data=f"order_cancel_confirm_{active_order['order_id']}",
                    ),
                    InlineKeyboardButton(
                        "Залишити",
                        callback_data=f"order_cancel_keep_{active_order['order_id']}",
                    ),
                ]]),
            )
            return
        no_order_reply = "Активної заявки для скасування не знайдено."
        await db.add_message(shop_id, user_id, "user", text)
        await db.add_message(shop_id, user_id, "assistant", no_order_reply)
        await update.message.reply_text(no_order_reply)
        return

    if intent == "status" or _looks_like_order_status_question(text):
        latest_order = await db.get_latest_order(shop_id, user_id)
        if latest_order:
            status = latest_order.get("status")
            if status == "active":
                status_reply = "🕓 Ваша заявка зараз очікує виконання."
            elif status == "completed":
                status_reply = "✅ Ваша заявка вже виконана."
            elif status == "cancelled":
                status_reply = "❌ Ваша заявка скасована."
            else:
                status_reply = "ℹ️ Статус вашої заявки зараз недоступний."
            await db.add_message(shop_id, user_id, "user", text)
            await db.add_message(shop_id, user_id, "assistant", status_reply)
            await update.message.reply_text(status_reply)
            return

    system = ai.system_prompt_customer(shop["shop_name"], shop["knowledge"] or "", settings, catalog)
    ai_result = await ai.customer_chat(system, history, text, history_limit=10)
    await _record_ai_result(shop_id, ai_result)
    response = _sanitize_ai_response(ai_result["content"])

    if ai_result["ok"] and response:
        await db.reset_ai_error_state(shop_id)
    else:
        await db.add_analytics_event(shop_id, "ai_error")
        error_type = ai_result.get("error_type") or ("EmptyAIResponse" if ai_result["ok"] else "UnknownError")
        await _handle_ai_failure(context, shop_id, shop.get("shop_name", ""), error_type)
        await db.save_pending_client_message(shop_id, user_id, text, error_type)
        response = (
            "Вибачте, зараз не можу коректно відповісти через тимчасову технічну проблему. "
            "Я збережу ваше повідомлення і відповім, щойно робота відновиться 🙏"
        )
        await update.message.reply_text(response)
        logger.warning(f"Saved pending client message | shop_id={shop_id} user_id={user_id} error_type={error_type}")
        return

    if ai_result.get("needs_human"):
        await db.add_analytics_event(shop_id, "handoff")
        await _request_human(shop, user_info, text, update.update_id)
        if not response:
            response = "Точної інформації немає, тому я не буду вгадувати. Передаю питання менеджеру."

    await _process_ai_response(
        context,
        shop,
        user_id,
        user_info,
        text,
        response,
        delayed=_is_delayed_update(update),
        source_update_id=update.update_id,
        order_data=None if ai_result.get("needs_human") else ai_result.get("order"),
    )


async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот не приймає файли. Напишіть ваше питання текстом 🙏")


def run(token: str):
    """Запуск клієнтського бота з конкретним токеном."""
    app = (
        Application.builder()
        .token(token)
        .connect_timeout(5)
        .read_timeout(15)
        .write_timeout(15)
        .pool_timeout(5)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(handle_order_callback, pattern=r"^order_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(
        ~filters.TEXT & ~filters.COMMAND,
        handle_unsupported
    ))
    app.add_error_handler(error_handler)

    logger.info("Client bot started")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    token = os.getenv("CLIENT_BOT_TOKEN") or sys.stdin.readline().strip()
    if token:
        run(token)
    else:
        print("ERROR: CLIENT_BOT_TOKEN not set")
