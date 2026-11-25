"""
CustomerServiceAI — бот для власників бізнесу.
Реєстрація, налаштування, база знань, підписка, переписки.
"""
import os
import re
import logging
import json
import asyncio
from io import BytesIO
from html import escape
from datetime import datetime, timedelta, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, Bot, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from telegram.error import RetryAfter, BadRequest
from dotenv import load_dotenv
import database as db
import ai
import payments
import bot_runner
from logging_utils import configure_logging
from runtime_health import record_polling_conflict

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
BOT_TOKEN = os.getenv("MANAGER_BOT_TOKEN")
SUPPORT_INSTAGRAM = "@vexoraworkingspace"
SUPPORT_INSTAGRAM_URL = "https://example.invalid/support"
SUPPORT_INSTAGRAM_LINK = f'<a href="{SUPPORT_INSTAGRAM_URL}">Instagram</a>'
SUPPORT_INSTAGRAM_HANDLE_LINK = f'<a href="{SUPPORT_INSTAGRAM_URL}">{SUPPORT_INSTAGRAM}</a>'
ADMIN_ID = int(__import__('os').environ.get('ADMIN_ID') or '0')
configure_logging()
logger = logging.getLogger(__name__)

MAX_KNOWLEDGE_LEN = 12000
CHATS_PAGE_SIZE = 10
MAX_CLIENT_LABEL_LEN = 15
REPLY_MODE_TIMEOUT_SECONDS = 300
MAX_BROADCAST_TEXT_LEN = 3000
waiting_token: set[int] = set()
setup_mode: dict[int, dict] = {}
reply_mode: dict[int, dict] = {}
broadcast_mode: dict[int, dict] = {}
catalog_mode: dict[int, dict] = {}
owner_test_mode: dict[int, dict] = {}

KNOWLEDGE_QUESTIONS = [
    {
        "key": "business_name",
        "label": "Назва бізнесу",
        "question": "Як називається ваш бізнес?",
        "example": "Наприклад: Квіткова майстерня «Лея»",
        "aliases": ("Назва бізнесу", "Назва"),
    },
    {
        "key": "business_about",
        "label": "Про що ваш бізнес",
        "question": "Коротко опишіть, чим займається ваш бізнес.",
        "example": "Наприклад: Створюємо букети та доставляємо їх по Києву",
        "aliases": ("Про що ваш бізнес",),
    },
    {
        "key": "address",
        "label": "Адреса та самовивіз",
        "question": "Чи є у вас адреса або самовивіз?",
        "example": "Напишіть адресу й важливі умови або пропустіть цей крок",
        "aliases": ("Адреса та самовивіз", "Адреса"),
    },
    {
        "key": "delivery",
        "label": "Доставка",
        "question": "Як працює доставка?",
        "example": "Куди доставляєте, строки, вартість та обмеження",
        "aliases": ("Доставка",),
    },
    {
        "key": "payment",
        "label": "Способи оплати",
        "question": "Як клієнт може оплатити замовлення?",
        "example": "Наприклад: карткою, готівкою або переказом",
        "aliases": ("Способи оплати",),
    },
    {
        "key": "contacts",
        "label": "Контакти менеджера",
        "question": "Як клієнту зв'язатися з менеджером, якщо потрібна людина?",
        "example": "Телефон, Telegram або Instagram",
        "aliases": ("Контакти менеджера", "Контакти / як зв'язатися з менеджером"),
    },
    {
        "key": "promotions",
        "label": "Акції та знижки",
        "question": "Чи є зараз акції або знижки?",
        "example": "Якщо немає — просто пропустіть цей крок",
        "aliases": ("Акції та знижки",),
    },
    {
        "key": "returns",
        "label": "Гарантії, повернення та обмін",
        "question": "Які умови гарантії, повернення або обміну?",
        "example": "Напишіть тільки те, що справді діє у вашому бізнесі",
        "aliases": ("Гарантії, повернення та обмін", "Гарантії / повернення / обмін"),
    },
    {
        "key": "special_terms",
        "label": "Важливі умови",
        "question": "Що клієнту важливо знати до замовлення?",
        "example": "Наприклад: строки виготовлення, мінімальна сума або передоплата",
        "aliases": ("Важливі умови", "Особливі умови", "Особливі умови роботи або важливі нюанси"),
    },
    {
        "key": "faq",
        "label": "Часті питання",
        "question": "Які питання клієнти ставлять найчастіше і як правильно на них відповідати?",
        "example": "Напишіть конкретні питання та підтверджені відповіді",
        "aliases": ("Часті питання", "Все, що клієнти можуть у вас запитати"),
    },
]

AVAILABILITY_LABELS = {
    "in_stock": "Є в наявності",
    "out_of_stock": "Немає в наявності",
    "preorder": "Під замовлення",
    "service": "Послуга",
    "unknown": "Не вказано",
}

TONE_LABELS = {
    "friendly": "Дружній",
    "professional": "Професійний",
    "concise": "Короткий",
}

TONE_EXAMPLES = {
    "friendly": "Зазвичай доставляємо за 1–2 дні 😊 Напишіть ваше місто — уточню точніше.",
    "professional": "Термін доставки — 1–2 робочі дні. Для точного розрахунку вкажіть місто.",
    "concise": "Доставка: 1–2 дні. Напишіть місто для уточнення.",
}

MONTHS = {
    "січня": 1, "января": 1,
    "лютого": 2, "февраля": 2,
    "березня": 3, "марта": 3,
    "квітня": 4, "апреля": 4,
    "травня": 5, "мая": 5,
    "червня": 6, "июня": 6,
    "липня": 7, "июля": 7,
    "серпня": 8, "августа": 8,
    "вересня": 9, "сентября": 9,
    "жовтня": 10, "октября": 10,
    "листопада": 11, "ноября": 11,
    "грудня": 12, "декабря": 12,
}


def normalize_client_label(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip())
    if len(value) > MAX_CLIENT_LABEL_LEN:
        return value[:MAX_CLIENT_LABEL_LEN] + "…"
    return value


def clear_owner_modes(user_id: int, keep: str = "") -> None:
    reply_mode.pop(user_id, None)
    if keep != "token":
        waiting_token.discard(user_id)
    if keep != "setup":
        setup_mode.pop(user_id, None)
    if keep != "catalog":
        catalog_mode.pop(user_id, None)
    if keep != "test":
        owner_test_mode.pop(user_id, None)


async def cleanup_conversations_job(context):
    deleted = await db.cleanup_old_conversations(days=14)
    logger.info(f"Weekly cleanup done, deleted {deleted} old conversation rows")


async def autocomplete_orders_job(context):
    now = datetime.utcnow()
    orders = await db.get_orders_for_autocomplete()
    for order in orders:
        auto_complete_at = order.get("auto_complete_at")
        if not auto_complete_at:
            continue
        try:
            complete_at = datetime.fromisoformat(auto_complete_at)
        except Exception:
            continue
        if complete_at <= now:
            done = await db.complete_order(order["shop_id"], order["order_id"])
            if done:
                logger.info(f"Order #{order['order_id']} auto-completed")


async def reconcile_bots_job(context):
    try:
        result = await bot_runner.reconcile_bots()
        if result["started"] or result["stopped"] or result["running"] != result["expected"]:
            logger.warning(
                "Client bot reconciliation | expected=%s running=%s started=%s stopped=%s",
                result["expected"], result["running"], result["started"], result["stopped"],
            )
    except Exception:
        logger.exception("Client bot reconciliation failed")


async def process_outbox_job(context):
    events = await db.claim_outbox(limit=20)
    for event in events:
        try:
            payload = json.loads(event["payload"])
            if event["topic"] == "owner_order_event":
                event_type = payload.get("event_type")
                prefix = {
                    "created": "📬 Нова заявка",
                    "updated": "✏️ Заявку оновлено",
                    "cancelled": "❌ Заявку скасовано клієнтом",
                }.get(event_type, "📦 Подія заявки")
                order = payload.get("order") or {}
                contact = escape(str(payload.get("contact") or "Клієнт"))
                text = (
                    f"{prefix}\n\n🆔 Заявка #{int(payload['order_id'])}\n"
                    f"👤 {contact}\n\n"
                    f"Ім'я: {escape(str(order.get('customer_name') or '—'))}\n"
                    f"Товар/Послуга: {escape(str(order.get('item') or '—'))}\n"
                    f"Дата/Час: {escape(str(order.get('datetime_text') or '—'))}\n"
                    f"Телефон: {escape(str(order.get('phone') or '—'))}\n"
                    f"Деталі: {escape(str(order.get('details') or '—'))}"
                )
                await context.bot.send_message(
                    chat_id=int(payload["admin_id"]), text=text, parse_mode="HTML"
                )
            elif event["topic"] == "owner_handoff":
                username = str(payload.get("username") or "")
                contact = escape(str(payload.get("display_name") or "Клієнт"))
                if username:
                    contact += f" (@{escape(username)})"
                await context.bot.send_message(
                    chat_id=int(payload["admin_id"]),
                    text=(
                        "🧑‍💼 Потрібна відповідь людини\n\n"
                        f"Клієнт: {contact}\n"
                        f"ID: {int(payload['user_id'])}\n"
                        f"Повідомлення: {escape(str(payload.get('text') or ''))}\n\n"
                        "AI у цьому чаті поставлено на паузу. Відкрийте «Переписки», щоб відповісти."
                    ),
                    parse_mode="HTML",
                )
            elif event["topic"] == "client_message":
                target_shop = await db.get_shop_by_id(event["shop_id"])
                if not target_shop or not target_shop.get("client_bot_token"):
                    raise RuntimeError("client_bot_unavailable")
                client_bot = Bot(token=target_shop["client_bot_token"])
                await client_bot.send_message(
                    chat_id=int(payload["user_id"]), text=str(payload["text"])
                )
            else:
                raise RuntimeError("unknown_outbox_topic")
            await db.mark_outbox_done(event["id"])
        except Exception as exc:
            await db.mark_outbox_failed(event["id"], type(exc).__name__)
            logger.warning(
                "Outbox delivery failed | event_id=%s topic=%s error_type=%s",
                event["id"], event["topic"], type(exc).__name__,
            )


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def check_and_notify(context):
    shop_id = context.job.data["shop_id"]
    admin_id = context.job.data["admin_id"]
    is_last = context.job.data.get("last", False)

    invoice_paid = False
    pending_invoices = await payments.get_pending_invoices()
    for invoice in pending_invoices:
        if invoice["shop_id"] != shop_id:
            continue
        provider_invoice = await payments.check_invoice_details(invoice["invoice_id"])
        if provider_invoice and provider_invoice.get("status") == "paid":
            result = await payments.process_paid_invoice(
                invoice["invoice_id"], shop_id, provider_invoice
            )
            if result.get("processed") or result.get("reason") == "already_processed":
                paid_shop = await db.get_shop_by_id(shop_id)
                if paid_shop and paid_shop.get("client_bot_token"):
                    bot_runner.start_bot(shop_id, paid_shop["client_bot_token"])
                invoice_paid = True

    if invoice_paid:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text="✅ Оплату підтверджено!\n\nПідписку продовжено на 1 місяць. Дякуємо! 🎉"
            )
        except Exception as e:
            logger.error(f"Failed to notify paid: {e}")
    else:
        text = (
            "⏰ Пробний період закінчується через 1 хвилину!\n\n"
            "Після закінчення бот перестане відповідати вашим клієнтам.\n\n"
            "Оплатіть зараз: /myshop"
            if is_last else
            "⏰ Пробний період закінчується через 10 хвилин!\n\n"
            "Після закінчення бот перестане відповідати вашим клієнтам.\n\n"
            "Щоб продовжити роботу — оплатіть підписку: /myshop"
        )
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception as e:
            logger.error(f"Failed to send reminder: {e}")

    pending = await db.get_pending_reminders()
    for r in pending:
        if r["shop_id"] == shop_id:
            await db.mark_reminder_sent(r["id"])


def schedule_reminder(app, shop_id: str, admin_id: int, fire_at, is_last: bool):
    from datetime import timezone
    now = datetime.now(timezone.utc)
    fire_at_tz = fire_at.replace(tzinfo=timezone.utc) if fire_at.tzinfo is None else fire_at
    delay = (fire_at_tz - now).total_seconds()
    if delay < 0:
        delay = 0
    app.job_queue.run_once(
        check_and_notify,
        when=delay,
        data={"shop_id": shop_id, "admin_id": admin_id, "last": is_last},
        name=f"remind_{'1' if is_last else '10'}_{shop_id}"
    )


async def post_init(app: Application):
    await db.init_db()
    await payments.init_payments_db()
    deleted = await db.cleanup_old_conversations(days=14)
    logger.info(f"Startup cleanup done, deleted {deleted} old conversation rows")
    await app.bot.set_my_commands([
        BotCommand("start", "Головне меню"),
        BotCommand("settings", "Налаштування магазину"),
        BotCommand("knowledge", "База знань для AI"),
        BotCommand("orders", "Активні заявки"),
        BotCommand("chats", "Переписки з клієнтами"),
        BotCommand("link", "Посилання для клієнтів"),
        BotCommand("subscription", "Керування підпискою"),
        BotCommand("support", "Тех. підтримка"),
        BotCommand("privacy", "Приватність і зберігання даних"),
        BotCommand("export", "Експорт даних бізнесу"),
        BotCommand("deleteaccount", "Видалення бізнесу і даних"),
        BotCommand("reset", "Очистити історію розмов клієнтського бота"),
    ])
    pending = await db.get_pending_reminders()
    for r in pending:
        from datetime import datetime as dt
        fire_at = dt.fromisoformat(r["fire_at"])
        schedule_reminder(app, r["shop_id"], r["admin_id"], fire_at, bool(r["is_last"]))
    if pending:
        logger.info(f"Restored {len(pending)} pending reminders")

    app.job_queue.run_daily(
        cleanup_conversations_job,
        time=time(hour=4, minute=0)
    )
    app.job_queue.run_repeating(
        reconcile_bots_job,
        interval=30,
        first=10,
        name="reconcile_client_bots",
    )
    app.job_queue.run_repeating(
        process_outbox_job,
        interval=5,
        first=5,
        name="process_outbox",
    )

    logger.info("Manager bot started")


def owner_menu_keyboard(has_bot: bool = False, is_admin_user: bool = False):
    rows = [
        [InlineKeyboardButton("✅ Готовність", callback_data="owner_readiness"),
         InlineKeyboardButton("🧪 Перевірити AI", callback_data="owner_ai_test")],
        [InlineKeyboardButton("⚙️ Параметри AI", callback_data="owner_preferences"),
         InlineKeyboardButton("📝 Дані бізнесу", callback_data="owner_setup")],
        [InlineKeyboardButton("📋 База знань", callback_data="owner_info")],
        [InlineKeyboardButton("🛍 Каталог", callback_data="owner_catalog"),
         InlineKeyboardButton("📊 Аналітика", callback_data="owner_analytics")],
        [InlineKeyboardButton("🤖 Підключити бота" if not has_bot else "🔄 Змінити бота", callback_data="owner_connect_bot")],
        [InlineKeyboardButton("📦 Активні заявки", callback_data="owner_orders"),
         InlineKeyboardButton("💬 Переписки", callback_data="owner_chats")],
        [InlineKeyboardButton("🔗 Посилання", callback_data="owner_link"),
         InlineKeyboardButton("💳 Підписка", callback_data="owner_subscription")],
        [InlineKeyboardButton("🛠 Тех. підтримка", callback_data="owner_support")],
        [InlineKeyboardButton("🔐 Дані та приватність", callback_data="owner_privacy")],
    ]
    if is_admin_user:
        rows.append([InlineKeyboardButton("🛠 Адмінка", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def _checkmark(value: bool) -> str:
    return "✅" if value else "⬜"


def _extract_shop_name(knowledge: str) -> str:
    for line in (knowledge or "").splitlines():
        clean = line.replace("**", "").replace("#", "").strip()
        lower = clean.lower()
        if lower.startswith("назва бізнесу:") or lower.startswith("назва:") or lower.startswith("назва -"):
            name = clean.split(":", 1)[-1].split("-", 1)[-1].strip()
            if name and len(name) < 60 and "база знань" not in name.lower():
                return name
    return ""


def _knowledge_question_by_index(index: int) -> dict:
    return KNOWLEDGE_QUESTIONS[max(0, min(index, len(KNOWLEDGE_QUESTIONS) - 1))]


def _parse_knowledge_answers(knowledge: str) -> dict[str, str]:
    answers: dict[str, str] = {}
    alias_map = {
        alias.casefold(): question["key"]
        for question in KNOWLEDGE_QUESTIONS
        for alias in question["aliases"]
    }
    for raw_line in (knowledge or "").splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        label, value = line.split(":", 1)
        key = alias_map.get(label.strip().casefold())
        if key and value.strip():
            answers[key] = value.strip()
    return answers


def _compose_knowledge(existing: str, answers: dict[str, str]) -> str:
    managed_aliases = {
        alias.casefold()
        for question in KNOWLEDGE_QUESTIONS
        for alias in question["aliases"]
    }
    preserved = []
    for raw_line in (existing or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        label = stripped.split(":", 1)[0].strip().casefold() if ":" in stripped else ""
        if label not in managed_aliases:
            preserved.append(stripped)

    structured = [
        f"{question['label']}: {answers[question['key']].strip()}"
        for question in KNOWLEDGE_QUESTIONS
        if answers.get(question["key"], "").strip()
    ]
    return "\n".join(structured + preserved).strip()


def _knowledge_question_text(state: dict) -> str:
    index = int(state.get("index", 0))
    question = _knowledge_question_by_index(index)
    current = state.get("answers", {}).get(question["key"], "").strip()
    current_text = f"\n\nПоточна відповідь:\n{current}" if current else ""
    return (
        "📝 Дані бізнесу\n\n"
        f"Крок {index + 1} з {len(KNOWLEDGE_QUESTIONS)}\n"
        f"{question['question']}\n"
        f"{question['example']}"
        f"{current_text}\n\n"
        "Надішліть відповідь одним повідомленням."
    )


def _knowledge_question_keyboard(state: dict) -> InlineKeyboardMarkup:
    index = int(state.get("index", 0))
    question = _knowledge_question_by_index(index)
    current = state.get("answers", {}).get(question["key"], "").strip()
    rows = []
    if current:
        rows.append([InlineKeyboardButton("✅ Залишити без змін", callback_data="setup_keep")])
    else:
        rows.append([InlineKeyboardButton("⏭ Пропустити", callback_data="setup_skip")])
    rows.append([InlineKeyboardButton("⬅ Назад", callback_data="setup_back")])
    rows.append([InlineKeyboardButton("🏠 Головне меню", callback_data="back_menu")])
    return InlineKeyboardMarkup(rows)


def _catalog_step_text(state: dict) -> str:
    stages = ("name", "price", "availability", "description")
    stage = state.get("stage", "name")
    number = stages.index(stage) + 1
    values = state.get("values", {})
    prompts = {
        "name": ("Як називається товар або послуга?", "Наприклад: Букет із троянд"),
        "price": ("Яка ціна в гривнях?", "Наприклад: 850"),
        "availability": ("Який статус цієї позиції?", "Оберіть один варіант кнопкою нижче"),
        "description": ("Додайте короткий опис.", "Що саме отримає клієнт; цей крок можна пропустити"),
    }
    question, example = prompts[stage]
    current = values.get(stage, "")
    if stage == "availability" and current:
        current = AVAILABILITY_LABELS.get(current, "Не вказано")
    current_text = f"\n\nПоточне значення:\n{current}" if current else ""
    return (
        "🛍 Товар або послуга\n\n"
        f"Крок {number} з {len(stages)}\n{question}\n{example}"
        f"{current_text}"
    )


def _catalog_step_keyboard(state: dict) -> InlineKeyboardMarkup:
    stage = state.get("stage", "name")
    values = state.get("values", {})
    rows = []
    if stage == "availability":
        rows.extend([
            [InlineKeyboardButton("✅ Є в наявності", callback_data="catalog_avail_in_stock")],
            [InlineKeyboardButton("📦 Під замовлення", callback_data="catalog_avail_preorder")],
            [InlineKeyboardButton("🛠 Послуга", callback_data="catalog_avail_service")],
            [InlineKeyboardButton("❌ Немає в наявності", callback_data="catalog_avail_out_of_stock")],
        ])
    if values.get(stage):
        rows.append([InlineKeyboardButton("✅ Залишити без змін", callback_data="catalog_keep")])
    elif stage != "name":
        rows.append([InlineKeyboardButton("⏭ Пропустити", callback_data="catalog_skip")])
    rows.append([InlineKeyboardButton("⬅ Назад", callback_data="catalog_back")])
    rows.append([InlineKeyboardButton("🏠 Головне меню", callback_data="back_menu")])
    return InlineKeyboardMarkup(rows)


async def _save_catalog_state(shop_id: str, state: dict) -> tuple[int, str]:
    values = state.get("values", {})
    name = str(values.get("name") or "").strip()
    if not name:
        raise ValueError("catalog_name_required")
    item_id = await db.upsert_catalog_item(
        shop_id,
        name,
        str(values.get("description") or "").strip(),
        str(values.get("price") or "").strip(),
        "UAH",
        str(values.get("availability") or "unknown"),
        item_id=state.get("item_id"),
    )
    return item_id, name


async def build_catalog_view(shop_id: str) -> tuple[str, InlineKeyboardMarkup]:
    items = await db.list_catalog_items(shop_id)
    lines = []
    for item in items[:20]:
        price = f"{item.get('price')} грн" if item.get("price") else "ціна не вказана"
        availability = AVAILABILITY_LABELS.get(item.get("availability"), "Не вказано")
        lines.append(f"• {item['name']} — {price} — {availability}")
    text = "🛍 Каталог\n\n" + ("\n".join(lines) if lines else "Товарів або послуг ще немає.")
    buttons = [[InlineKeyboardButton("➕ Додати товар або послугу", callback_data="catalog_add")]]
    buttons.extend([
        [InlineKeyboardButton(f"✏️ {normalize_client_label(item['name'])}", callback_data=f"catalog_edit_{item['item_id']}")]
        for item in items[:20]
    ])
    buttons.append([InlineKeyboardButton("⬅ Назад", callback_data="back_menu")])
    return text, InlineKeyboardMarkup(buttons)


async def build_readiness_text(shop: dict) -> str:
    status = await db.get_onboarding_status(shop["shop_id"])
    return (
        "✅ Готовність до запуску\n\n"
        f"{_checkmark(status['bot_connected'])} 1. Клієнтський бот підключений\n"
        f"{_checkmark(status['knowledge_ready'])} 2. Дані бізнесу заповнені\n"
        f"{_checkmark(status['test_passed'])} 3. Відповіді AI перевірені\n"
        f"{_checkmark(status['ai_enabled'])} 4. AI відповідає клієнтам\n\n"
        "Після заповнення даних поговоріть зі своїм AI у тестовому режимі. "
        "Якщо відповіді вас влаштовують — підтвердьте перевірку та увімкніть AI для клієнтів."
    )


async def build_preferences_text(shop_id: str, notice: str = "") -> str:
    settings = await db.get_shop_settings(shop_id)
    tone = settings["tone"]
    notice_text = f"{notice}\n\n" if notice else ""
    return (
        f"{notice_text}⚙️ Параметри AI\n\n"
        f"Обраний стиль: {TONE_LABELS.get(tone, TONE_LABELS['friendly'])}\n\n"
        "Приклад для питання «Коли буде доставка?»:\n\n"
        f"🙂 Дружній:\n{TONE_EXAMPLES['friendly']}\n\n"
        f"💼 Професійний:\n{TONE_EXAMPLES['professional']}\n\n"
        f"✂️ Короткий:\n{TONE_EXAMPLES['concise']}\n\n"
        "AI автоматично відповідає мовою клієнта. Якщо мову не визначено — українською.\n\n"
        f"Ліміт AI-запитів/місяць: {settings['monthly_ai_request_limit']}\n"
        f"Ліміт витрат/місяць: ${settings['monthly_ai_cost_limit_usd']:.2f}"
    )


def preferences_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🙂 Дружній", callback_data="pref_tone_friendly"),
         InlineKeyboardButton("💼 Професійний", callback_data="pref_tone_professional")],
        [InlineKeyboardButton("✂️ Короткий", callback_data="pref_tone_concise")],
        [InlineKeyboardButton("⬅ Назад", callback_data="back_menu")],
    ])


def owner_test_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Очистити тестовий діалог", callback_data="test_clear")],
        [InlineKeyboardButton("⚙️ Змінити налаштування", callback_data="test_settings")],
        [InlineKeyboardButton("✅ Відповіді мене влаштовують", callback_data="test_finish")],
        [InlineKeyboardButton("⬅ Назад", callback_data="owner_readiness")],
    ])


def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("🏪 Бізнеси", callback_data="admin_businesses")],
        [InlineKeyboardButton("💸 Підписки", callback_data="admin_subscriptions")],
        [InlineKeyboardButton("📣 Розсилка", callback_data="admin_broadcasts")],
        [InlineKeyboardButton("⬅ Назад", callback_data="back_menu")],
    ])


def admin_broadcast_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Усім власникам", callback_data="broadcast_owners")],
        [InlineKeyboardButton("🛍 Клієнтам бізнесу", callback_data="broadcast_clients_select")],
        [InlineKeyboardButton("🛠 Технічні роботи", callback_data="broadcast_maintenance")],
        [InlineKeyboardButton("⬅ Назад", callback_data="admin_panel")],
    ])


def broadcast_preview_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Надіслати", callback_data="broadcast_confirm"),
            InlineKeyboardButton("❌ Скасувати", callback_data="broadcast_cancel"),
        ],
        [InlineKeyboardButton("⬅ Розсилка", callback_data="admin_broadcasts")],
    ])


def _broadcast_target_label(ctx: dict) -> str:
    target = ctx.get("target")
    if target == "owners":
        return "усім власникам"
    if target == "maintenance":
        return "усім власникам як повідомлення про технічні роботи"
    if target == "clients":
        return f"клієнтам бізнесу {ctx.get('shop_name') or ctx.get('shop_id') or ''}".strip()
    return "невідома аудиторія"


def _parse_date_part(date_text: str, now: datetime | None = None):
    now = now or datetime.now()
    text = re.sub(r"\s+", " ", (date_text or "").strip().lower())
    if not text or text in {"сьогодні", "сегодня"}:
        return now.date()
    if text == "завтра":
        return (now + timedelta(days=1)).date()
    if text in {"післязавтра", "послезавтра"}:
        return (now + timedelta(days=2)).date()

    numeric = re.search(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", text)
    if numeric:
        day = int(numeric.group(1))
        month = int(numeric.group(2))
        year = int(numeric.group(3)) if numeric.group(3) else now.year
        if year < 100:
            year += 2000
        candidate = datetime(year, month, day).date()
        if not numeric.group(3) and candidate < now.date():
            candidate = datetime(now.year + 1, month, day).date()
        return candidate

    month_names = "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True))
    named = re.search(rf"\b(\d{{1,2}})\s+({month_names})(?:\s+(\d{{2,4}}))?\b", text)
    if named:
        day = int(named.group(1))
        month = MONTHS[named.group(2)]
        year = int(named.group(3)) if named.group(3) else now.year
        if year < 100:
            year += 2000
        candidate = datetime(year, month, day).date()
        if not named.group(3) and candidate < now.date():
            candidate = datetime(now.year + 1, month, day).date()
        return candidate

    return None


def _parse_time_part(time_text: str):
    text = (time_text or "").strip().lower()
    match = re.search(r"\b(\d{1,2})[:.](\d{2})\b", text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
    else:
        match = re.search(r"\b(\d{1,2})\b", text)
        if not match:
            return None
        hour = int(match.group(1))
        minute = 0
    try:
        return time(hour=hour, minute=minute)
    except ValueError:
        return None


def parse_maintenance_period(text: str, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    now = now or datetime.now()
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not normalized:
        return None

    match = re.search(
        r"^(?P<date>.+?)\s+(?:з|с|from)?\s*(?P<start>\d{1,2}(?::|\.)?\d{0,2})\s*(?:до|-|—|по)\s*(?P<end>\d{1,2}(?::|\.)?\d{0,2})$",
        normalized,
    )
    if not match:
        return None

    date_value = _parse_date_part(match.group("date"), now)
    start_time = _parse_time_part(match.group("start"))
    end_time = _parse_time_part(match.group("end"))
    if not date_value or not start_time or not end_time:
        return None

    starts_at = datetime.combine(date_value, start_time)
    ends_at = datetime.combine(date_value, end_time)
    if ends_at <= starts_at:
        ends_at += timedelta(days=1)
    return starts_at, ends_at


def format_maintenance_period(starts_at: datetime, ends_at: datetime) -> str:
    return f"{starts_at.strftime('%d.%m.%Y %H:%M')} - {ends_at.strftime('%d.%m.%Y %H:%M')}"


async def build_returning_text(shop: dict | None) -> str:
    shop_name = escape(shop["shop_name"] if shop else "без назви")
    subscription_line = "💳 Підписка: статус недоступний"
    if shop:
        is_active_sub = await payments.is_active(shop["shop_id"])
        days = await payments.days_left(shop["shop_id"])
        if is_active_sub:
            subscription_line = f"💳 Підписка: активна, залишилось {days} дн."
        else:
            subscription_line = "💳 Підписка: не активна"

    readiness = await db.get_onboarding_status(shop["shop_id"]) if shop else None
    readiness_line = ""
    if readiness:
        completed = sum(bool(readiness[key]) for key in ("bot_connected", "knowledge_ready", "test_passed", "ai_enabled"))
        readiness_line = f"\n✅ Готовність: {completed}/4"
    return (
        f"👋 З поверненням! Ваш бізнес: {shop_name}\n\n"
        "Що означають кнопки нижче:\n\n"
        "• 📝 Дані бізнесу — відповісти на короткі питання про ваш бізнес\n"
        "• 🧪 Перевірити AI — поговорити зі своїм AI до запуску\n"
        "• ⚙️ Параметри AI — вибрати стиль відповідей і побачити приклади\n"
        "• 📋 База знань — подивитись, що бот уже знає\n"
        "• 🔄 Змінити бота / 🤖 Підключити бота — підключити або змінити клієнтського бота\n"
        "• 📦 Активні заявки — перегляд і керування поточними заявками\n"
        "• 💬 Переписки — подивитись діалоги з клієнтами\n"
        "• 🔗 Посилання — username і пряме посилання для клієнтів\n"
        "• 💳 Підписка — перевірити або продовжити підписку\n"
        "• 🛠 Тех. підтримка — якщо щось зламалось або потрібна допомога\n\n"
        "З чого почати:\n"
        "• підключити бота\n"
        "• заповнити дані бізнесу\n"
        "• поговорити з AI у тестовому режимі\n"
        "• підтвердити відповіді та увімкнути AI для клієнтів\n\n"
        "/start — повернутись в меню\n\n"
        "📚 Ресурси\n\n"
        "📖 Керівництво\n"
        f"📸 {SUPPORT_INSTAGRAM_LINK}\n\n"
        f"{subscription_line}{readiness_line}"
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clear_owner_modes(user_id)

    shop = await db.get_shop_by_admin(user_id)
    if shop:
        await update.message.reply_text(
            await build_returning_text(shop),
            parse_mode="HTML",
            reply_markup=owner_menu_keyboard(
                has_bot=bool(shop and shop.get("client_bot_token")),
                is_admin_user=is_admin(user_id)
            )
        )
        return

    shop_id = await db.create_shop(user_id)
    await payments.create_subscription(shop_id)

    from datetime import timezone
    sub = await payments.get_subscription(shop_id)
    trial_end = datetime.fromisoformat(sub["trial_end"]).replace(tzinfo=timezone.utc)

    fire_10 = trial_end - timedelta(minutes=10)
    fire_1 = trial_end - timedelta(minutes=1)

    await db.save_reminder(shop_id, user_id, fire_10, is_last=False)
    await db.save_reminder(shop_id, user_id, fire_1, is_last=True)

    schedule_reminder(context.application, shop_id, user_id, fire_10, is_last=False)
    schedule_reminder(context.application, shop_id, user_id, fire_1, is_last=True)

    await update.message.reply_text(
        "👋 Вітаю в CustomerServiceAI\n\n"
        "Це AI-менеджер для вашого бізнесу в Telegram.\n"
        "Він автоматично відповідає клієнтам, приймає заявки та допомагає вести бізнес.\n\n"
        "Що ви отримаєте:\n"
        "• автоматичні відповіді клієнтам у Telegram\n"
        "• прийом, оновлення та скасування заявок\n"
        "• список активних заявок для власника\n"
        "• перегляд переписок з клієнтами\n"
        "• можливість написати клієнту через AI manager bot\n"
        "• окремий кабінет для керування бізнесом\n"
        "• швидке підключення\n\n"
        "Чому саме CustomerServiceAI:\n"
        "• усе працює прямо в Telegram\n"
        "• немає складних та довгих налаштувань\n"
        "• клієнти пишуть у вашого бота напряму, ви отримуєте тільки заявки\n"
        "• AI відповідає 24/7 використовуючи вашу базу знань\n"
        "• заявки, переписки та керування — в одному місці\n"
        "• підключення займає лише кілька хвилин\n\n"
        "Як це працює:\n"
        "• у системі використовується 2 боти\n\n"
        "• перший бот — @ownercustomerservicebot (в ньому ви зараз знаходитесь), саме в ньому ви керуєте налаштуваннями, заявками, переписками та роботою AI\n\n"
        "• другий бот — це бот для ваших клієнтів, саме він відповідає на повідомлення, спілкується з клієнтами та приймає заявки\n\n"
        "• після того як ви надсилаєте токен вашого Telegram-бота, система автоматично підключає та налаштовує його для роботи з клієнтами\n\n"
        "• після підключення вам потрібно заповнити базу знань, щоб AI розумів ваш бізнес, товари, послуги та правила роботи\n\n"
        "• усе керування бізнесом відбувається у вашому боті для власника, а спілкування з клієнтами — у боті для клієнтів\n\n"
        "Як почати:\n"
        "1. Натисніть кнопку «🤖 Підключити бота»\n"
        "   (там буде інструкція, робіть по інструкції)\n\n"
        "2. Заповніть дані бізнесу у кнопці «📝 Дані бізнесу»\n\n"
        "3. Натисніть «🧪 Перевірити AI» і поставте кілька тестових запитань\n\n"
        "4. Підтвердьте відповіді та увімкніть AI для клієнтів\n\n"
        "🎁 7 днів безкоштовно\n"
        "Потім — $20/місяць",
        reply_markup=owner_menu_keyboard(is_admin_user=is_admin(user_id))
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    shop = await db.get_shop_by_admin(user_id)
    update_scope = shop["shop_id"] if shop else f"owner-{user_id}"
    if not await db.claim_update(update_scope, update.update_id, source="manager"):
        logger.info("Duplicate manager callback ignored | user_id=%s update_id=%s", user_id, update.update_id)
        return
    has_bot = bool(shop and shop.get("client_bot_token"))
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Назад", callback_data="back_menu")]])

    if data == "admin_panel" and is_admin(user_id):
        await query.edit_message_text("🛠 Адмінка\n\nОберіть розділ:", reply_markup=admin_panel_keyboard())
        return

    if data == "admin_stats" and is_admin(user_id):
        stats = await db.get_shop_stats()
        sub_stats = await payments.get_subscription_stats()
        text = (
            "📊 Статистика\n\n"
            f"Бізнесів всього: {stats['total']}\n"
            f"З підключеним ботом: {stats['with_bot']}\n"
            f"Без підключеного бота: {stats['without_bot']}\n"
            f"З заповненою базою знань: {stats['with_knowledge']}\n\n"
            f"Підписки активні: {sub_stats['paid']}\n"
            f"Тріал активний: {sub_stats['trial']}\n"
            f"Прострочені: {sub_stats['expired']}\n"
            f"Скоро закінчуються: {sub_stats['expiring_soon']}\n\n"
            f"Повідомлень у БД: {stats['conversations']}\n"
            f"Унікальних клієнтів: {stats['clients']}"
        )
        await query.edit_message_text(text, reply_markup=admin_panel_keyboard())
        return

    if data == "admin_businesses" and is_admin(user_id):
        shops = await db.get_all_shops()
        if not shops:
            await query.edit_message_text("🏪 Бізнесів поки немає.", reply_markup=admin_panel_keyboard())
            return

        sub_lines = []
        for shop_item in shops[:30]:
            sub = await payments.get_subscription(shop_item['shop_id'])
            status = "без підписки"
            if sub:
                if sub.get('paid_until'):
                    paid_until = datetime.fromisoformat(sub['paid_until'])
                    status = "active" if paid_until > datetime.utcnow() else "expired"
                elif sub.get('trial_end'):
                    trial_end = datetime.fromisoformat(sub['trial_end'])
                    status = "trial" if trial_end > datetime.utcnow() else "expired"
            bot_status = "бот ✅" if shop_item.get('has_client_bot') else "бот ❌"
            name = shop_item['shop_name'] or 'без назви'
            sub_lines.append(f"• {name} | {shop_item['shop_id']} | {status} | {bot_status}")

        text = "🏪 Бізнеси\n\n" + "\n".join(sub_lines)
        if len(text) > 3900:
            text = text[:3900] + "\n…"
        await query.edit_message_text(text, reply_markup=admin_panel_keyboard())
        return

    if data == "admin_subscriptions" and is_admin(user_id):
        stats = await payments.get_subscription_stats()
        text = (
            "💸 Підписки\n\n"
            f"Всього: {stats['total']}\n"
            f"Активні платні: {stats['paid']}\n"
            f"На тріалі: {stats['trial']}\n"
            f"Прострочені: {stats['expired']}\n"
            f"Скоро закінчуються: {stats['expiring_soon']}"
        )
        await query.edit_message_text(text, reply_markup=admin_panel_keyboard())
        return

    if data == "admin_broadcasts" and is_admin(user_id):
        await query.edit_message_text(
            "📣 Розсилка\n\n"
            "Оберіть аудиторію. Перед відправкою буде попередній перегляд і підтвердження.",
            reply_markup=admin_broadcast_keyboard()
        )
        return

    if data == "broadcast_owners" and is_admin(user_id):
        broadcast_mode[user_id] = {"stage": "await_text", "target": "owners"}
        await query.edit_message_text(
            "👥 Розсилка усім власникам\n\n"
            "Надішліть текст повідомлення одним повідомленням. Перед відправкою я покажу попередній перегляд.",
            reply_markup=admin_broadcast_keyboard()
        )
        return

    if data == "broadcast_maintenance" and is_admin(user_id):
        broadcast_mode[user_id] = {"stage": "await_maintenance_period", "target": "maintenance"}
        await query.edit_message_text(
            "🛠 Технічні роботи\n\n"
            "Надішліть період техробіт одним повідомленням.\n\n"
            "Приклади:\n"
            "завтра з 10 до 22\n"
            "25.07 10:00-18:00\n"
            "сьогодні з 23 до 2",
            reply_markup=admin_broadcast_keyboard()
        )
        return

    if data == "broadcast_clients_select" and is_admin(user_id):
        shops = await db.get_all_shops()
        shops = [s for s in shops if s.get("has_client_bot")]
        if not shops:
            await query.edit_message_text(
                "🛍 Немає бізнесів з підключеним клієнтським ботом.",
                reply_markup=admin_broadcast_keyboard()
            )
            return

        buttons = []
        for shop_item in shops[:30]:
            name = normalize_client_label(shop_item.get("shop_name") or "без назви")
            buttons.append([
                InlineKeyboardButton(
                    f"{name} | {shop_item['shop_id']}",
                    callback_data=f"broadcast_clients_shop_{shop_item['shop_id']}",
                )
            ])
        buttons.append([InlineKeyboardButton("⬅ Розсилка", callback_data="admin_broadcasts")])
        await query.edit_message_text(
            "🛍 Оберіть бізнес, клієнтам якого треба надіслати повідомлення:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("broadcast_clients_shop_") and is_admin(user_id):
        shop_id = data.replace("broadcast_clients_shop_", "", 1)
        target_shop = await db.get_shop_by_id(shop_id)
        if not target_shop:
            await query.edit_message_text("Бізнес не знайдено.", reply_markup=admin_broadcast_keyboard())
            return
        broadcast_mode[user_id] = {
            "stage": "await_text",
            "target": "clients",
            "shop_id": shop_id,
            "shop_name": target_shop.get("shop_name") or "без назви",
        }
        await query.edit_message_text(
            f"🛍 Розсилка клієнтам бізнесу: {target_shop.get('shop_name') or 'без назви'}\n\n"
            "Надішліть текст повідомлення одним повідомленням. Перед відправкою я покажу попередній перегляд.",
            reply_markup=admin_broadcast_keyboard()
        )
        return

    if data == "broadcast_cancel" and is_admin(user_id):
        broadcast_mode.pop(user_id, None)
        await query.edit_message_text("❌ Розсилку скасовано.", reply_markup=admin_broadcast_keyboard())
        return

    if data == "broadcast_confirm" and is_admin(user_id):
        ctx = broadcast_mode.pop(user_id, None)
        if not ctx or ctx.get("stage") != "confirm" or not ctx.get("text"):
            await query.edit_message_text("Чернетку розсилки не знайдено.", reply_markup=admin_broadcast_keyboard())
            return

        await query.edit_message_text("📣 Надсилаю розсилку...")
        if ctx.get("target") == "maintenance":
            starts_at = ctx["starts_at"]
            ends_at = ctx["ends_at"]
            await db.create_maintenance_window(
                starts_at.isoformat(sep=" ", timespec="seconds"),
                ends_at.isoformat(sep=" ", timespec="seconds"),
                ctx["text"],
            )
        result = await send_broadcast(ctx)
        maintenance_line = ""
        if ctx.get("target") == "maintenance":
            maintenance_line = f"Період техробіт: {format_maintenance_period(ctx['starts_at'], ctx['ends_at'])}\n"
        await query.edit_message_text(
            "📣 Розсилка завершена\n\n"
            f"Аудиторія: {_broadcast_target_label(ctx)}\n"
            f"{maintenance_line}"
            f"Успішно: {result['sent']}\n"
            f"Помилок: {result['failed']}\n"
            f"Всього отримувачів: {result['total']}",
            reply_markup=admin_broadcast_keyboard()
        )
        return

    if data == "owner_setup" and shop:
        reply_mode.pop(user_id, None)
        catalog_mode.pop(user_id, None)
        owner_test_mode.pop(user_id, None)
        setup_mode[user_id] = {
            "index": 0,
            "base_knowledge": shop.get("knowledge") or "",
            "answers": _parse_knowledge_answers(shop.get("knowledge") or ""),
        }
        state = setup_mode[user_id]
        await query.edit_message_text(
            _knowledge_question_text(state), reply_markup=_knowledge_question_keyboard(state)
        )
        return

    if data in {"setup_keep", "setup_skip", "setup_back"} and shop:
        state = setup_mode.get(user_id)
        if not state:
            await query.edit_message_text(
                "Сесію заповнення завершено. Відкрийте «📝 Дані бізнесу», щоб почати знову.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 Дані бізнесу", callback_data="owner_setup")],
                    [InlineKeyboardButton("⬅ Назад", callback_data="back_menu")],
                ]),
            )
            return
        if data == "setup_back":
            if int(state.get("index", 0)) == 0:
                setup_mode.pop(user_id, None)
                await query.edit_message_text(
                    await build_returning_text(shop),
                    parse_mode="HTML",
                    reply_markup=owner_menu_keyboard(has_bot=has_bot, is_admin_user=is_admin(user_id)),
                )
                return
            state["index"] = int(state.get("index", 0)) - 1
        else:
            state["index"] = int(state.get("index", 0)) + 1
            if state["index"] >= len(KNOWLEDGE_QUESTIONS):
                content = _compose_knowledge(state["base_knowledge"], state["answers"])
                if not content:
                    state["index"] = len(KNOWLEDGE_QUESTIONS) - 1
                    await query.edit_message_text(
                        "⚠️ Додайте хоча б одну відповідь, щоб створити базу знань.",
                        reply_markup=_knowledge_question_keyboard(state),
                    )
                    return
                await db.create_knowledge_draft(shop["shop_id"], content)
                preview = content[:3200] + ("\n…" if len(content) > 3200 else "")
                await query.edit_message_text(
                    "🔎 Перевірте дані бізнесу\n\n"
                    f"{preview}\n\n"
                    "Чинна база знань ще не змінена. Збережіть або поверніться до запитань.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Зберегти", callback_data="knowledge_save")],
                        [InlineKeyboardButton("⬅ До запитань", callback_data="setup_back")],
                        [InlineKeyboardButton("❌ Скасувати", callback_data="knowledge_cancel")],
                    ]),
                )
                return
        await query.edit_message_text(
            _knowledge_question_text(state), reply_markup=_knowledge_question_keyboard(state)
        )
        return

    if data == "owner_connect_bot":
        clear_owner_modes(user_id, keep="token")
        waiting_token.add(user_id)
        await query.edit_message_text(
            "🤖 Як підключити вашого Telegram-бота\n\n"
            "Що робити покроково:\n\n"
            "1. Відкрийте @BotFather у Telegram\n"
            "2. Надішліть команду /newbot\n"
            "3. Вкажіть ім'я бота\n"
            "   Наприклад: Назва бізнесу Customer Service\n"
            "4. Вкажіть username бота — саме за ним клієнти зможуть знайти вашого бота в Telegram\n"
            "   Username потрібно писати англійськими буквами, без пробілів, і він має закінчуватись на \"bot\"\n"
            "   Наприклад: nazvabiznesubot\n"
            "5. Після цього BotFather надішле вам токен\n"
            "6. Скопіюйте токен повністю\n"
            "7. Надішліть його сюди наступним повідомленням\n\n"
            "💡 Приклад токена:\n"
            "1234567890:AA… (довгий секретний рядок від BotFather)\n\n"
            "⚠️ Важливо:\n"
            "— токен — це пароль до вашого бота; не надсилайте його нікому, крім цього налаштування\n"
            "— надсилайте токен без зайвих пробілів\n"
            "— не змінюйте символи в токені\n"
            "— якщо токен неправильний, бот попросить надіслати його ще раз",
            reply_markup=back_btn
        )
        return

    if data == "owner_chats" or data.startswith("owner_chats_page_"):
        if not shop:
            return
        page = 0
        if data.startswith("owner_chats_page_"):
            try:
                page = max(0, int(data.rsplit("_", 1)[-1]))
            except ValueError:
                page = 0

        clients = await db.get_clients(shop["shop_id"])
        if not clients:
            await query.edit_message_text(
                "💬 Переписок поки немає.\n\nℹ️ Неактивні чати автоматично видаляються через 14 днів.",
                reply_markup=back_btn
            )
            return

        start = page * CHATS_PAGE_SIZE
        end = start + CHATS_PAGE_SIZE
        page_clients = clients[start:end]

        buttons = []
        for c in page_clients:
            if c.get("first_name"):
                label = normalize_client_label(c["first_name"])
            elif c.get("username"):
                label = normalize_client_label(f"@{c['username']}")
            else:
                label = str(c["user_id"])
            buttons.append([InlineKeyboardButton(f"👤 {label}", callback_data=f"chat_{c['user_id']}")])

        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"owner_chats_page_{page - 1}"))
        if end < len(clients):
            nav_row.append(InlineKeyboardButton("➡️", callback_data=f"owner_chats_page_{page + 1}"))
        if nav_row:
            buttons.append(nav_row)

        total_pages = (len(clients) + CHATS_PAGE_SIZE - 1) // CHATS_PAGE_SIZE
        buttons.append([InlineKeyboardButton("⬅ Назад", callback_data="back_menu")])
        await query.edit_message_text(
            f"💬 Переписки клієнтів\n\nℹ️ Неактивні чати автоматично видаляються через 14 днів.\nСторінка {page + 1}/{max(total_pages, 1)}\n\nОберіть клієнта:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("chat_"):
        client_id = int(data.split("_")[1])
        if not shop:
            return
        history = await db.get_conversation_full(shop["shop_id"], client_id)
        if not history:
            await query.edit_message_text(
                "Переписка порожня.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅ До списку", callback_data="owner_chats_page_0")],
                ]),
            )
            return
        lines = []
        for msg in history:
            role = "👤" if msg["role"] == "user" else "🤖"
            lines.append(f"{role} {msg['content']}")
        text = "\n\n".join(lines)
        if len(text) > 3800:
            text = "…(скорочено)\n\n" + text[-3800:]
        client = await db.get_client(shop["shop_id"], client_id)
        if client and client.get("first_name"):
            client_label = normalize_client_label(client["first_name"])
        elif client and client.get("username"):
            client_label = normalize_client_label(client["username"])
        else:
            client_label = str(client_id)

        contact_line = ""
        if client and client.get("username"):
            contact_line = f"\n\nКонтакт: @{client['username']}"

        paused = await db.is_human_paused(shop["shop_id"], client_id)
        pause_button = InlineKeyboardButton(
            "▶️ Увімкнути AI" if paused else "⏸ Поставити AI на паузу",
            callback_data=f"resume_ai_{client_id}" if paused else f"pause_ai_{client_id}",
        )
        await query.edit_message_text(
            f"💬 Переписка: {client_label}{contact_line}\n\n{text}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✍️ Написати самостійно", callback_data=f"reply_direct_{client_id}"),
                    InlineKeyboardButton("🤖 Через manager bot", callback_data=f"reply_manager_{client_id}"),
                ],
                [pause_button],
                [InlineKeyboardButton("⬅ До списку", callback_data="owner_chats_page_0")],
            ])
        )
        return

    if data.startswith("pause_ai_") or data.startswith("resume_ai_"):
        if not shop:
            return
        client_id = int(data.rsplit("_", 1)[-1])
        client = await db.get_client(shop["shop_id"], client_id)
        if not client:
            await query.edit_message_text("Клієнта не знайдено у вашому бізнесі.", reply_markup=back_btn)
            return
        paused = data.startswith("pause_ai_")
        await db.set_human_pause(shop["shop_id"], client_id, paused, "owner_action")
        await db.add_audit_event(
            shop["shop_id"], "owner", user_id,
            "ai_paused" if paused else "ai_resumed", "client", str(client_id),
        )
        await query.edit_message_text(
            "⏸ AI у цьому чаті поставлено на паузу."
            if paused else "▶️ AI у цьому чаті знову відповідає.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ До переписки", callback_data=f"chat_{client_id}")],
            ]),
        )
        return

    if data.startswith("reply_direct_"):
        client_id = int(data.split("_")[-1])
        client = await db.get_client(shop["shop_id"], client_id)
        if client and client.get("username"):
            username = client["username"]
            await query.edit_message_text(
                f"✍️ Написати клієнту самостійно\n\nВідкрийте: https://t.me/{username}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅ До переписки", callback_data=f"chat_{client_id}")],
                ])
            )
        else:
            await query.edit_message_text(
                "У клієнта немає username. Написати йому можна тільки через manager bot.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🤖 Через manager bot", callback_data=f"reply_manager_{client_id}")],
                    [InlineKeyboardButton("⬅ До переписки", callback_data=f"chat_{client_id}")],
                ])
            )
        return

    if data.startswith("reply_manager_"):
        client_id = int(data.split("_")[-1])
        reply_mode[user_id] = {
            "client_id": client_id,
            "shop_id": shop["shop_id"] if shop else "",
            "expires_at": datetime.utcnow().timestamp() + REPLY_MODE_TIMEOUT_SECONDS,
        }
        await query.edit_message_text(
            "🤖 Надішліть повідомлення сюди одним повідомленням — manager bot передасть його клієнту.\n\nПісля одного відправленого повідомлення цей режим вимкнеться автоматично.\n\nЯкщо захочете написати клієнту ще раз, натисніть \"🤖 Через manager bot\".",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ До переписки", callback_data=f"chat_{client_id}")],
            ])
        )
        return

    if data == "owner_orders":
        if not shop:
            return
        orders = await db.list_active_orders(shop["shop_id"])
        if not orders:
            await query.edit_message_text(
                "📦 Активних заявок поки немає.",
                reply_markup=back_btn
            )
            return

        buttons = []
        for order in orders[:20]:
            client = await db.get_client(shop["shop_id"], order["user_id"])
            if client and client.get("first_name"):
                label = normalize_client_label(client["first_name"])
            elif client and client.get("username"):
                label = normalize_client_label(f"@{client['username']}")
            else:
                label = str(order["user_id"])

            dt_label = normalize_client_label(order.get("datetime_text") or "без часу")
            buttons.append([
                InlineKeyboardButton(
                    f"👤 {label} — {dt_label}",
                    callback_data=f"order_{order['order_id']}"
                )
            ])

        buttons.append([InlineKeyboardButton("⬅ Назад", callback_data="back_menu")])
        await query.edit_message_text(
            "📦 Активні заявки\n\nОберіть заявку:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("order_complete_"):
        order_id = int(data.split("_")[-1])
        done = await db.complete_order(shop["shop_id"], order_id) if shop else False
        await query.edit_message_text(
            "✅ Заявку завершено." if done else "Заявку не вдалося завершити або вона вже не активна.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ До заявок", callback_data="owner_orders")],
            ])
        )
        return

    if data.startswith("order_cancel_"):
        order_id = int(data.split("_")[-1])
        order = await db.get_order(shop["shop_id"], order_id) if shop else None
        done = await db.cancel_order(shop["shop_id"], order_id) if shop else False
        if done and order:
            await db.enqueue_outbox(
                event_key=f"owner-cancel:{shop['shop_id']}:{order_id}",
                shop_id=shop["shop_id"],
                topic="client_message",
                payload={"user_id": int(order["user_id"]), "text": "❌ Вашу заявку скасовано."},
            )
            await db.add_audit_event(
                shop["shop_id"], "owner", user_id, "order_cancelled", "order", str(order_id)
            )

        if not shop:
            await query.edit_message_text(
                "❌ Заявку скасовано." if done else "Заявку не вдалося скасувати або вона вже не активна.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅ До заявок", callback_data="owner_orders")],
                ])
            )
            return

        await query.edit_message_text(
            "✅ Заявку скасовано." if done else "Заявку не вдалося скасувати або вона вже не активна.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ До заявок", callback_data="owner_orders")],
            ]),
        )
        return

    if data.startswith("order_"):
        order_id = int(data.split("_")[-1])
        order = await db.get_order(shop["shop_id"], order_id) if shop else None
        if not order:
            await query.edit_message_text(
                "Заявку не знайдено.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅ До заявок", callback_data="owner_orders")],
                ]),
            )
            return
        client = await db.get_client(shop["shop_id"], order["user_id"])
        if client and client.get("first_name"):
            client_label = normalize_client_label(client["first_name"])
        elif client and client.get("username"):
            client_label = normalize_client_label(f"@{client['username']}")
        else:
            client_label = str(order["user_id"])

        contact_line = f"tg://user?id={order['user_id']}"
        if client and client.get("username"):
            contact_line = f"@{client['username']}"

        buttons = []
        if order.get("status") == "active":
            buttons.append([
                InlineKeyboardButton("✅ Завершити", callback_data=f"order_complete_{order['order_id']}"),
                InlineKeyboardButton("❌ Скасувати", callback_data=f"order_cancel_{order['order_id']}"),
            ])
        buttons.append([InlineKeyboardButton("⬅ До заявок", callback_data="owner_orders")])

        await query.edit_message_text(
            f"📦 Заявка #{order['order_id']}\n\n"
            f"Клієнт: {client_label}\n"
            f"Контакт: {contact_line}\n"
            f"Ім'я: {order.get('customer_name') or '—'}\n"
            f"Товар/Послуга: {order.get('item') or '—'}\n"
            f"Дата/Час: {order.get('datetime_text') or '—'}\n"
            f"Телефон: {order.get('phone') or '—'}\n"
            f"Деталі: {order.get('details') or '—'}\n"
            f"Статус: {order.get('status') or '—'}",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data == "confirm_reset":
        if shop:
            counts = await db.clear_shop_conversations(shop["shop_id"])
            await db.add_audit_event(
                shop["shop_id"], "owner", user_id, "shop_conversations_cleared",
                metadata={"rows": sum(counts.values())},
            )
        await query.edit_message_text("🔄 Усі переписки цього бізнесу очищено.")
        return

    if data == "confirm_delete_account":
        if not shop:
            await query.edit_message_text("Бізнес уже видалено або не знайдено.")
            return
        shop_id = shop["shop_id"]
        bot_runner.stop_bot(shop_id)
        await db.delete_shop_data(shop_id)
        waiting_token.discard(user_id)
        setup_mode.pop(user_id, None)
        catalog_mode.pop(user_id, None)
        owner_test_mode.pop(user_id, None)
        reply_mode.pop(user_id, None)
        await query.edit_message_text(
            "✅ Бізнес, налаштування, переписки, заявки і локальні платіжні записи видалено."
        )
        return

    if data == "owner_readiness" and shop:
        owner_test_mode.pop(user_id, None)
        readiness = await db.get_onboarding_status(shop["shop_id"])
        actions = []
        if not readiness["bot_connected"]:
            actions.append([InlineKeyboardButton("🔌 Підключити бота", callback_data="owner_connect_bot")])
        else:
            actions.append([InlineKeyboardButton("🔄 Змінити бота", callback_data="owner_connect_bot")])
        actions.append([InlineKeyboardButton("📝 Заповнити дані бізнесу", callback_data="owner_setup")])
        if readiness["bot_connected"] and readiness["knowledge_ready"]:
            actions.append([InlineKeyboardButton("🧪 Перевірити AI", callback_data="owner_ai_test")])
        if readiness["ai_enabled"]:
            actions.append([InlineKeyboardButton("⏸ Призупинити AI", callback_data="ai_disable")])
        elif readiness["ready_to_enable"]:
            actions.append([InlineKeyboardButton("▶️ Увімкнути AI", callback_data="ai_enable")])
        actions.append([InlineKeyboardButton("⬅ Назад", callback_data="back_menu")])
        await query.edit_message_text(
            await build_readiness_text(shop),
            reply_markup=InlineKeyboardMarkup(actions),
        )
        return

    if data in {"ai_enable", "ai_disable"} and shop:
        try:
            await db.set_shop_ai_enabled(shop["shop_id"], data == "ai_enable")
        except ValueError:
            await query.edit_message_text(
                "⚠️ AI ще не можна ввімкнути. Підключіть бота, заповніть дані бізнесу та перевірте відповіді AI.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ До готовності", callback_data="owner_readiness")],
                    [InlineKeyboardButton("⬅ Назад", callback_data="back_menu")],
                ]),
            )
            return
        notice = "✅ AI увімкнено для клієнтів." if data == "ai_enable" else "✅ AI призупинено. Клієнтам він не відповідає."
        updated_shop = await db.get_shop_by_id(shop["shop_id"])
        readiness_text = await build_readiness_text(updated_shop)
        await query.edit_message_text(
            f"{notice}\n\n{readiness_text}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ До готовності", callback_data="owner_readiness")],
                [InlineKeyboardButton("⬅ Головне меню", callback_data="back_menu")],
            ]),
        )
        return

    if data == "owner_preferences" and shop:
        reply_mode.pop(user_id, None)
        setup_mode.pop(user_id, None)
        catalog_mode.pop(user_id, None)
        owner_test_mode.pop(user_id, None)
        await query.edit_message_text(
            await build_preferences_text(shop["shop_id"]), reply_markup=preferences_keyboard()
        )
        return

    if data.startswith("pref_tone_") and shop:
        tone = data.removeprefix("pref_tone_")
        try:
            current = await db.get_shop_settings(shop["shop_id"])
            if current["tone"] == tone:
                notice = f"ℹ️ Стиль «{TONE_LABELS[tone]}» уже вибрано."
            else:
                await db.update_shop_settings(shop["shop_id"], tone=tone)
                notice = f"✅ Стиль відповідей змінено на «{TONE_LABELS[tone]}»."
            await query.edit_message_text(
                await build_preferences_text(shop["shop_id"], notice), reply_markup=preferences_keyboard()
            )
        except (ValueError, KeyError):
            await query.edit_message_text(
                "⚠️ Не вдалося змінити стиль. Попереднє значення збережено — спробуйте ще раз.",
                reply_markup=preferences_keyboard(),
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        return

    if (data.startswith("pref_lang_") or data in {"pref_timezone", "pref_hours"}) and shop:
        await query.edit_message_text(
            await build_preferences_text(
                shop["shop_id"],
                "ℹ️ Цю настройку прибрано. AI автоматично відповідає мовою клієнта та працює цілодобово.",
            ),
            reply_markup=preferences_keyboard(),
        )
        return

    if data == "owner_ai_test" and shop:
        reply_mode.pop(user_id, None)
        setup_mode.pop(user_id, None)
        catalog_mode.pop(user_id, None)
        readiness = await db.get_onboarding_status(shop["shop_id"])
        if not readiness["bot_connected"]:
            await query.edit_message_text(
                "Спочатку підключіть клієнтського бота — після цього можна буде перевірити відповіді AI.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔌 Підключити бота", callback_data="owner_connect_bot")],
                    [InlineKeyboardButton("⬅ Назад", callback_data="back_menu")],
                ]),
            )
            return
        if not readiness["knowledge_ready"]:
            await query.edit_message_text(
                "Спочатку заповніть дані бізнесу, щоб AI мав на що спиратися у відповідях.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 Заповнити дані", callback_data="owner_setup")],
                    [InlineKeyboardButton("⬅ Назад", callback_data="back_menu")],
                ]),
            )
            return
        owner_test_mode[user_id] = {"history": [], "reply_count": 0}
        await query.edit_message_text(
            "🧪 Перевірка AI\n\n"
            "Пишіть так, ніби ви клієнт вашого бізнесу. AI відповідає з поточною базою знань і вибраним стилем.\n\n"
            "Тест не створює заявки, не повідомляє менеджера і не потрапляє в клієнтську аналітику.",
            reply_markup=owner_test_keyboard(),
        )
        return

    if data in {"test_clear", "test_settings", "test_finish"} and shop:
        state = owner_test_mode.get(user_id)
        if data == "test_clear":
            owner_test_mode[user_id] = {"history": [], "reply_count": 0}
            await query.edit_message_text(
                "🗑 Тестовий діалог очищено. Напишіть нове повідомлення як клієнт.",
                reply_markup=owner_test_keyboard(),
            )
            return
        if data == "test_settings":
            owner_test_mode.pop(user_id, None)
            await query.edit_message_text(
                await build_preferences_text(shop["shop_id"]), reply_markup=preferences_keyboard()
            )
            return
        if not state or int(state.get("reply_count", 0)) < 1:
            await query.edit_message_text(
                "Спочатку надішліть хоча б одне тестове повідомлення й перевірте відповідь AI.",
                reply_markup=owner_test_keyboard(),
            )
            return
        await db.mark_onboarding_test_passed(shop["shop_id"], True)
        owner_test_mode.pop(user_id, None)
        await query.edit_message_text(
            "✅ Відповіді AI перевірено. Тепер його можна увімкнути для клієнтів.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ До готовності", callback_data="owner_readiness")],
                [InlineKeyboardButton("⬅ Головне меню", callback_data="back_menu")],
            ]),
        )
        return

    if data == "knowledge_save" and shop:
        draft = await db.get_knowledge_draft(shop["shop_id"])
        try:
            await db.commit_knowledge_draft(shop["shop_id"])
        except ValueError:
            await query.edit_message_text(
                "⚠️ Попередній перегляд уже застарів. Дані не змінено — відкрийте «📝 Дані бізнесу» і повторіть збереження.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 Дані бізнесу", callback_data="owner_setup")],
                    [InlineKeyboardButton("⬅ Назад", callback_data="owner_info")],
                ]),
            )
            return
        name = _extract_shop_name((draft or {}).get("content", ""))
        if name:
            await db.update_shop_name(shop["shop_id"], name)
        setup_mode.pop(user_id, None)
        await query.edit_message_text(
            "✅ Дані бізнесу збережено. AI використовуватиме їх у наступних відповідях.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧪 Перевірити AI", callback_data="owner_ai_test")],
                [InlineKeyboardButton("📋 Переглянути базу знань", callback_data="owner_info")],
                [InlineKeyboardButton("⬅ Головне меню", callback_data="back_menu")],
            ]),
        )
        return

    if data == "knowledge_cancel" and shop:
        await db.delete_knowledge_draft(shop["shop_id"])
        setup_mode.pop(user_id, None)
        await query.edit_message_text(
            "Чернетку скасовано. Чинні дані бізнесу не змінилися.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Дані бізнесу", callback_data="owner_setup")],
                [InlineKeyboardButton("⬅ Головне меню", callback_data="back_menu")],
            ]),
        )
        return

    if data == "knowledge_history" and shop:
        await query.edit_message_text(
            "Історія змін зберігається автоматично й не потребує налаштування.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ До бази знань", callback_data="owner_info")],
            ]),
        )
        return

    if data.startswith("knowledge_rollback_") and shop:
        await query.edit_message_text(
            "Відновлення історії приховано з основного інтерфейсу. Чинні дані бізнесу не змінено.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ До бази знань", callback_data="owner_info")],
            ]),
        )
        return

    if data == "owner_catalog" and shop:
        reply_mode.pop(user_id, None)
        setup_mode.pop(user_id, None)
        catalog_mode.pop(user_id, None)
        owner_test_mode.pop(user_id, None)
        text, markup = await build_catalog_view(shop["shop_id"])
        await query.edit_message_text(text, reply_markup=markup)
        return

    if data == "catalog_add" and shop:
        setup_mode.pop(user_id, None)
        owner_test_mode.pop(user_id, None)
        catalog_mode[user_id] = {
            "stage": "name",
            "item_id": None,
            "values": {"name": "", "price": "", "availability": "unknown", "description": ""},
        }
        state = catalog_mode[user_id]
        await query.edit_message_text(
            _catalog_step_text(state), reply_markup=_catalog_step_keyboard(state)
        )
        return

    if data.startswith("catalog_edit_") and shop:
        item_id = int(data.rsplit("_", 1)[-1])
        item = next((row for row in await db.list_catalog_items(shop["shop_id"]) if int(row["item_id"]) == item_id), None)
        if not item:
            await query.edit_message_text(
                "Позицію не знайдено.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ До каталогу", callback_data="owner_catalog")]]),
            )
            return
        catalog_mode[user_id] = {
            "stage": "name",
            "item_id": item_id,
            "values": {
                "name": item.get("name") or "",
                "price": item.get("price") or "",
                "availability": item.get("availability") or "unknown",
                "description": item.get("description") or "",
            },
        }
        state = catalog_mode[user_id]
        await query.edit_message_text(
            _catalog_step_text(state), reply_markup=_catalog_step_keyboard(state)
        )
        return

    if data in {"catalog_keep", "catalog_skip", "catalog_back"} and shop:
        state = catalog_mode.get(user_id)
        if not state:
            await query.edit_message_text(
                "Сесію редагування завершено.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ До каталогу", callback_data="owner_catalog")]]),
            )
            return
        stages = ("name", "price", "availability", "description")
        index = stages.index(state.get("stage", "name"))
        if data == "catalog_back":
            if index == 0:
                catalog_mode.pop(user_id, None)
                text, markup = await build_catalog_view(shop["shop_id"])
                await query.edit_message_text(text, reply_markup=markup)
                return
            state["stage"] = stages[index - 1]
        else:
            if state["stage"] == "name" and not state["values"].get("name"):
                await query.edit_message_text(
                    "Назва обов'язкова. Напишіть назву товару або послуги.",
                    reply_markup=_catalog_step_keyboard(state),
                )
                return
            if index == len(stages) - 1:
                item_id, name = await _save_catalog_state(shop["shop_id"], state)
                catalog_mode.pop(user_id, None)
                await db.add_audit_event(
                    shop["shop_id"], "owner", user_id, "catalog_item_saved", "catalog_item", str(item_id)
                )
                await query.edit_message_text(
                    f"✅ Позицію «{name}» збережено. Ціни в каталозі вказуються у гривнях.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ До каталогу", callback_data="owner_catalog")]]),
                )
                return
            state["stage"] = stages[index + 1]
        await query.edit_message_text(
            _catalog_step_text(state), reply_markup=_catalog_step_keyboard(state)
        )
        return

    if data.startswith("catalog_avail_") and shop:
        state = catalog_mode.get(user_id)
        if not state or state.get("stage") != "availability":
            await query.edit_message_text(
                "Сесію редагування завершено.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ До каталогу", callback_data="owner_catalog")]]),
            )
            return
        availability = data.removeprefix("catalog_avail_")
        if availability not in AVAILABILITY_LABELS:
            return
        state["values"]["availability"] = availability
        state["stage"] = "description"
        await query.edit_message_text(
            _catalog_step_text(state), reply_markup=_catalog_step_keyboard(state)
        )
        return

    if data == "owner_analytics" and shop:
        summary = await db.get_analytics_summary(shop["shop_id"])
        usage = summary.pop("ai_usage")
        period_days = summary.pop("period_days")
        labels = {"inquiry": "Звернення", "handoff": "Передачі людині", "order": "Заявки", "ai_error": "AI-помилки"}
        lines = [f"{labels.get(key, key)}: {int(value['events'])}" for key, value in sorted(summary.items())]
        lines.extend([
            f"AI-діалоги клієнтів {usage['period']}: {usage['request_count']} / {usage['request_limit']}",
            f"Оцінка витрат: ${float(usage['estimated_cost_usd']):.4f} / ${float(usage['cost_limit_usd']):.2f}",
        ])
        await query.edit_message_text(
            f"📊 Аналітика за {period_days} днів\n\n" + "\n".join(lines), reply_markup=back_btn
        )
        return

    if data == "cancel_reset":
        await query.edit_message_text(
            await build_returning_text(shop),
            parse_mode="HTML",
            reply_markup=owner_menu_keyboard(has_bot=has_bot, is_admin_user=is_admin(user_id))
        )
        return

    if data == "owner_info":
        if shop and shop["knowledge"]:
            await query.edit_message_text(
                f"📋 База знань:\n\n{shop['knowledge'][:3200]}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 Змінити дані", callback_data="owner_setup")],
                    [InlineKeyboardButton("⬅ Назад", callback_data="back_menu")],
                ]),
            )
        else:
            await query.edit_message_text(
                "📋 База знань порожня. Заповніть короткі питання про ваш бізнес.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 Заповнити дані", callback_data="owner_setup")],
                    [InlineKeyboardButton("⬅ Назад", callback_data="back_menu")],
                ]),
            )

    elif data == "owner_link":
        if shop:
            client_username = shop.get("client_bot_username") or ""
            if not client_username:
                await query.edit_message_text(
                    "Спочатку підключіть окремого клієнтського бота. "
                    "Посилання з'явиться після успішної перевірки токена.",
                    reply_markup=back_btn,
                )
                return
            link = f"https://t.me/{client_username}"
            await query.edit_message_text(
                f"🔗 Посилання для ваших клієнтів\n\n"
                f"Ваші клієнти можуть знайти бота двома способами:\n\n"
                f"1. Username бота:\n@{client_username}\n\n"
                f"2. Пряме посилання:\n{link}\n\n"
                f"Різниці в роботі немає — можете використовувати будь-який варіант, як вам зручніше.\n\n"
                f"💡 Посилання одразу відкриває бота.\n"
                f"Якщо використовуєте username — клієнту потрібно буде самостійно ввести або вставити його в Telegram-пошук.\n\n"
                f"Ви можете:\n"
                f"— додати username або посилання в Instagram / TikTok / Telegram канал\n"
                f"— розмістити бота на сайті або в рекламі",
                reply_markup=back_btn
            )

    elif data == "owner_support":
        await query.edit_message_text(
            "🛠 Технічна підтримка\n\n"
            f"Якщо у вас виникли проблеми з ботом або є пропозиції — пишіть нам:\n\nInstagram: {SUPPORT_INSTAGRAM_HANDLE_LINK}",
            parse_mode="HTML",
            reply_markup=back_btn
        )

    elif data == "owner_privacy":
        await query.edit_message_text(
            "🔐 Дані та приватність\n\n"
            "Сервіс зберігає налаштування бізнесу, переписки, заявки та технічні стани, "
            "потрібні для роботи бота. Неактивні переписки й завершені заявки очищаються "
            "за 14 днів. Токен клієнтського бота зберігається у зашифрованому вигляді.\n\n"
            "Команди:\n/export — отримати копію даних свого бізнесу без секретного токена.\n"
            "/reset — видалити всі переписки цього бізнесу.\n"
            "/deleteaccount — видалити бізнес і пов'язані локальні дані.",
            reply_markup=back_btn,
        )

    elif data == "owner_subscription":
        if not shop:
            return
        is_active_sub = await payments.is_active(shop["shop_id"])
        days = await payments.days_left(shop["shop_id"])
        status_line = (
            f"✅ Підписка активна. Залишилось днів: {days}."
            if is_active_sub else
            "Підписка не активна."
        )
        await query.edit_message_text(
            f"💳 Підписка\n\n{status_line}\n\nВартість: ${payments.SUBSCRIPTION_PRICE} за 30 днів.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Створити рахунок", callback_data="pay_subscribe")],
                [InlineKeyboardButton("⬅ Назад", callback_data="back_menu")],
            ]),
        )

    elif data == "pay_subscribe":
        if not shop:
            return
        await query.edit_message_text("⏳ Створюю рахунок для оплати...")
        invoice = await payments.create_invoice(shop["shop_id"])
        if invoice:
            is_active_sub = await payments.is_active(shop["shop_id"])
            days = await payments.days_left(shop["shop_id"])
            status_line = f"✅ Підписка активна — залишилось днів: {days}\n\n" if is_active_sub else ""
            await query.edit_message_text(
                f"💳 Рахунок на оплату\n\n{status_line}Сума: ${payments.SUBSCRIPTION_PRICE} (USDT/TON/BTC/ETH)\n\nНатисніть кнопку нижче щоб оплатити:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Оплатити", url=invoice["pay_url"])],
                    [InlineKeyboardButton("✅ Перевірити оплату", callback_data=f"check_{invoice['invoice_id']}_{shop['shop_id']}_1")],
                    [InlineKeyboardButton("⬅ Назад", callback_data="owner_subscription")],
                ])
            )
        else:
            await query.edit_message_text(
                "❌ Помилка створення рахунку. Спробуйте пізніше.",
                reply_markup=owner_menu_keyboard(has_bot=bool(shop and shop.get("client_bot_token")), is_admin_user=is_admin(user_id))
            )

    elif data.startswith("check_"):
        parts = data.split("_")
        if len(parts) < 3 or not shop:
            await query.edit_message_text("❌ Некоректний запит перевірки оплати.")
            return
        invoice_id = parts[1]
        shop_id = parts[2]
        attempt = int(parts[3]) if len(parts) > 3 else 1
        if shop_id != shop["shop_id"]:
            logger.warning("Rejected cross-shop invoice callback | user_id=%s", user_id)
            await query.edit_message_text("❌ Цей рахунок не належить вашому магазину.")
            return

        local_invoice = await payments.get_local_invoice(invoice_id)
        if not local_invoice or local_invoice["shop_id"] != shop["shop_id"]:
            await query.edit_message_text("❌ Рахунок не знайдено для вашого магазину.")
            return

        provider_invoice = await payments.check_invoice_details(invoice_id)
        status = provider_invoice.get("status") if provider_invoice else "unknown"
        if status == "paid":
            result = await payments.process_paid_invoice(invoice_id, shop_id, provider_invoice)
            if result.get("processed"):
                if shop.get("client_bot_token"):
                    bot_runner.start_bot(shop_id, shop["client_bot_token"])
            elif result.get("reason") != "already_processed":
                logger.warning(
                    "Rejected paid invoice | invoice_id=%s reason=%s",
                    invoice_id, result.get("reason"),
                )
                await query.edit_message_text(
                    "❌ Дані платежу не пройшли перевірку. Зверніться у підтримку.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅ Назад", callback_data="owner_subscription")],
                    ]),
                )
                return
            days = await payments.days_left(shop_id)
            await query.edit_message_text(
                f"✅ Оплату підтверджено!\n\nПідписка активна, залишилось днів: {days}.",
                reply_markup=owner_menu_keyboard(has_bot=bool(shop and shop.get("client_bot_token")), is_admin_user=is_admin(user_id))
            )
        elif attempt == 1:
            await query.edit_message_text(
                "⏳ Оплату не знайдено\n\nМожливо, платіж ще обробляється. Спробуйте ще раз через 5 хвилин.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Перевірити ще раз", callback_data=f"check_{invoice_id}_{shop_id}_2")],
                    [InlineKeyboardButton("⬅ Назад", callback_data="owner_subscription")],
                ])
            )
        else:
            await query.edit_message_text(
                f"❌ Оплату не знайдено\n\nЯкщо ви впевнені що оплатили — напишіть нам:\n\nInstagram: {SUPPORT_INSTAGRAM_HANDLE_LINK}\n\nЗбережіть скріншот оплати — він прискорить вирішення питання.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅ Назад", callback_data="owner_subscription")],
                ])
            )

    elif data == "back_menu":
        waiting_token.discard(user_id)
        setup_mode.pop(user_id, None)
        catalog_mode.pop(user_id, None)
        owner_test_mode.pop(user_id, None)
        reply_mode.pop(user_id, None)
        await query.edit_message_text(
            await build_returning_text(shop),
            parse_mode="HTML",
            reply_markup=owner_menu_keyboard(has_bot=has_bot, is_admin_user=is_admin(user_id))
        )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    shop = await db.get_shop_by_admin(user_id)
    if not shop:
        await update.message.reply_text("У вас немає магазину. Напишіть /start щоб зареєструватись.")
        return
    clear_owner_modes(user_id, keep="setup")
    setup_mode[user_id] = {
        "index": 0,
        "base_knowledge": shop.get("knowledge") or "",
        "answers": _parse_knowledge_answers(shop.get("knowledge") or ""),
    }
    state = setup_mode[user_id]
    await update.message.reply_text(
        _knowledge_question_text(state), reply_markup=_knowledge_question_keyboard(state)
    )


async def knowledge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    shop = await db.get_shop_by_admin(user_id)
    if not shop:
        await update.message.reply_text("Ви не є власником бізнесу.")
        return
    knowledge = shop["knowledge"] or ""
    if knowledge:
        await update.message.reply_text(f"📋 База знань:\n\n{knowledge[:3500]}")
    else:
        await update.message.reply_text("База знань порожня. Напишіть боту про ваш бізнес!")


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    shop = await db.get_shop_by_admin(user_id)
    if not shop:
        await update.message.reply_text("Ви не є власником бізнесу.")
        return
    orders = await db.list_active_orders(shop["shop_id"])
    if not orders:
        await update.message.reply_text("📦 Активних заявок поки немає.")
        return
    lines = []
    for order in orders[:20]:
        client = await db.get_client(shop["shop_id"], order["user_id"])
        if client and client.get("first_name"):
            label = normalize_client_label(client["first_name"])
        elif client and client.get("username"):
            label = normalize_client_label(f"@{client['username']}")
        else:
            label = str(order["user_id"])
        item = order.get("item") or "Без назви"
        dt_label = order.get("datetime_text") or "без часу"
        lines.append(f"• #{order['order_id']} — {label} — {item} — {dt_label}")
    await update.message.reply_text("📦 Активні заявки\n\n" + "\n".join(lines[:20]))


async def chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    shop = await db.get_shop_by_admin(user_id)
    if not shop:
        await update.message.reply_text("Ви не є власником бізнесу.")
        return
    clients = await db.get_clients(shop["shop_id"])
    if not clients:
        await update.message.reply_text("💬 Переписок поки немає.\n\nℹ️ Неактивні чати автоматично видаляються через 14 днів.")
        return
    lines = []
    for c in clients[:20]:
        if c.get("first_name"):
            label = normalize_client_label(c["first_name"])
        elif c.get("username"):
            label = normalize_client_label(f"@{c['username']}")
        else:
            label = str(c["user_id"])
        lines.append(f"• {label}")
    await update.message.reply_text("💬 Переписки клієнтів\n\n" + "\n".join(lines[:20]))


async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    shop = await db.get_shop_by_admin(user_id)
    if not shop:
        await update.message.reply_text("Ви не є власником бізнесу.")
        return
    client_username = shop.get("client_bot_username") or ""
    if not client_username:
        await update.message.reply_text(
            "Спочатку підключіть окремого клієнтського бота. Після цього тут з'явиться правильне посилання."
        )
        return
    link = f"https://t.me/{client_username}"
    await update.message.reply_text(
        f"🔗 Посилання для ваших клієнтів\n\n"
        f"1. Username бота:\n@{client_username}\n\n"
        f"2. Пряме посилання:\n{link}\n\n"
        f"Різниці в роботі немає — можете використовувати будь-який варіант, як вам зручніше."
    )


async def subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    shop = await db.get_shop_by_admin(user_id)
    if not shop:
        await update.message.reply_text("Ви не є власником бізнесу.")
        return
    is_active_sub = await payments.is_active(shop["shop_id"])
    days = await payments.days_left(shop["shop_id"])
    if is_active_sub:
        await update.message.reply_text(f"💳 Підписка активна. Залишилось днів: {days}")
    else:
        await update.message.reply_text("💳 Підписка не активна. Відкрийте меню та натисніть кнопку підписки для оплати.")


async def send_broadcast(ctx: dict) -> dict:
    text = ctx["text"]
    target = ctx.get("target")
    recipients: list[int] = []
    bot_token = BOT_TOKEN

    if target in {"owners", "maintenance"}:
        recipients = await db.get_all_admin_ids()
    elif target == "clients":
        shop_id = ctx.get("shop_id", "")
        target_shop = await db.get_shop_by_id(shop_id)
        if not target_shop or not target_shop.get("client_bot_token"):
            logger.warning("Broadcast skipped: client bot missing | shop_id=%s", shop_id)
            return {"sent": 0, "failed": 0, "total": 0}
        bot_token = target_shop["client_bot_token"]
        recipients = await db.get_client_ids(shop_id)

    sent = 0
    failed = 0
    if not recipients or not bot_token:
        return {"sent": 0, "failed": 0, "total": 0}

    sender_bot = Bot(token=bot_token)
    for recipient_id in recipients:
        delivered = False
        for attempt in range(2):
            try:
                await sender_bot.send_message(chat_id=recipient_id, text=text)
                delivered = True
                break
            except RetryAfter as exc:
                if attempt == 0:
                    await asyncio.sleep(min(float(exc.retry_after), 30.0))
                    continue
                logger.warning("Broadcast rate-limited twice | target=%s", target)
            except Exception as exc:
                logger.warning(
                    "Broadcast delivery failed | target=%s | recipient_id=%s | error=%s",
                    target, recipient_id, type(exc).__name__,
                )
                break
        if delivered:
            sent += 1
        else:
            failed += 1

    logger.info(
        "Broadcast sent | target=%s | shop_id=%s | sent=%s | failed=%s | total=%s",
        target,
        ctx.get("shop_id", ""),
        sent,
        failed,
        len(recipients),
    )
    return {"sent": sent, "failed": failed, "total": len(recipients)}


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠 Технічна підтримка\n\n"
        f"Якщо у вас виникли проблеми з ботом або є пропозиції — пишіть нам:\n\nInstagram: {SUPPORT_INSTAGRAM_HANDLE_LINK}",
        parse_mode="HTML",
    )


async def privacy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    shop = await db.get_shop_by_admin(user_id)
    if not shop:
        await update.message.reply_text("Ви не є власником бізнесу.")
        return
    await update.message.reply_text(
        "🔐 Дані та приватність\n\n"
        "Неактивні переписки й завершені заявки очищаються за 14 днів. "
        "Токен клієнтського бота зберігається зашифровано.\n\n"
        "/export — експорт ваших даних без секретного токена.\n"
        "/reset — видалити переписки.\n"
        "/deleteaccount — видалити бізнес і пов'язані локальні дані."
    )


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    shop = await db.get_shop_by_admin(user_id)
    if not shop:
        await update.message.reply_text("Ви не є власником бізнесу.")
        return
    exported = await db.export_shop_data(shop["shop_id"])
    content = json.dumps(exported, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    await update.message.reply_document(
        document=InputFile(BytesIO(content), filename=f"customerserviceai-{shop['shop_id']}-export.json"),
        caption="Експорт даних вашого бізнесу. Секретний токен бота не включено.",
    )
    await db.add_audit_event(shop["shop_id"], "owner", user_id, "shop_data_exported")


async def delete_account_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    shop = await db.get_shop_by_admin(user_id)
    if not shop:
        await update.message.reply_text("Ви не є власником бізнесу.")
        return
    await update.message.reply_text(
        "⚠️ Видалити бізнес без можливості відновлення?\n\n"
        "Буде видалено налаштування, переписки, клієнтів, заявки, підписку та локальну історію платежів. "
        "Перед цим можна виконати /export.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Так, видалити", callback_data="confirm_delete_account"),
            InlineKeyboardButton("Скасувати", callback_data="back_menu"),
        ]]),
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚠️ Ви точно хочете очистити всі переписки?",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Так, очистити", callback_data="confirm_reset"),
                InlineKeyboardButton("❌ Скасувати", callback_data="cancel_reset"),
            ]
        ])
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    if not text:
        return

    shop = await db.get_shop_by_admin(user_id)
    if not shop:
        await update.message.reply_text("👋 Використайте /start щоб зареєструватись як власник бізнесу.")
        return
    if not await db.claim_update(shop["shop_id"], update.update_id, source="manager"):
        logger.info("Duplicate manager message ignored | user_id=%s update_id=%s", user_id, update.update_id)
        return

    if is_admin(user_id) and user_id in broadcast_mode:
        ctx = broadcast_mode[user_id]
        if ctx.get("stage") == "await_maintenance_period":
            period = parse_maintenance_period(text)
            if not period:
                await update.message.reply_text(
                    "⚠️ Не вдалося розпізнати період.\n\n"
                    "Приклади:\n"
                    "завтра з 10 до 22\n"
                    "25.07 10:00-18:00\n"
                    "сьогодні з 23 до 2",
                    reply_markup=admin_broadcast_keyboard()
                )
                return
            starts_at, ends_at = period
            ctx["stage"] = "await_text"
            ctx["starts_at"] = starts_at
            ctx["ends_at"] = ends_at
            broadcast_mode[user_id] = ctx
            await update.message.reply_text(
                "🛠 Період техробіт збережено:\n"
                f"{format_maintenance_period(starts_at, ends_at)}\n\n"
                "Тепер надішліть текст повідомлення для власників.\n"
                "Клієнтам у цей період бот автоматично відповідатиме, що проводяться технічні роботи.",
                reply_markup=admin_broadcast_keyboard()
            )
            return
        if ctx.get("stage") == "await_text":
            broadcast_text = text.strip()
            if len(broadcast_text) > MAX_BROADCAST_TEXT_LEN:
                await update.message.reply_text(
                    f"⚠️ Повідомлення занадто довге. Максимум {MAX_BROADCAST_TEXT_LEN} символів.",
                    reply_markup=admin_broadcast_keyboard()
                )
                return
            ctx["stage"] = "confirm"
            ctx["text"] = broadcast_text
            broadcast_mode[user_id] = ctx
            maintenance_line = ""
            if ctx.get("target") == "maintenance":
                maintenance_line = f"Період техробіт: {format_maintenance_period(ctx['starts_at'], ctx['ends_at'])}\n"
            await update.message.reply_text(
                "📣 Предпросмотр розсилки\n\n"
                f"Аудиторія: {_broadcast_target_label(ctx)}\n\n"
                f"{maintenance_line}"
                f"{broadcast_text}\n\n"
                "Надіслати?",
                reply_markup=broadcast_preview_keyboard()
            )
            return
        if ctx.get("stage") == "confirm":
            await update.message.reply_text(
                "📣 Чернетка розсилки вже готова. Натисніть «✅ Надіслати» або «❌ Скасувати».",
                reply_markup=broadcast_preview_keyboard()
            )
            return

    if user_id in waiting_token:
        token = text.strip()
        try:
            test_bot = Bot(token=token)
            bot_info = await test_bot.get_me()
            username = bot_info.username
        except Exception:
            await update.message.reply_text(
                "❌ Невірний токен. Перевірте і спробуйте ще раз.\n\nТокен копіюйте повністю, без зайвих пробілів."
            )
            return

        try:
            await db.update_client_bot(shop["shop_id"], token, username, bot_info.id)
        except ValueError as exc:
            if str(exc) == "telegram_bot_already_connected":
                await update.message.reply_text(
                    "❌ Цей Telegram-бот уже підключений до іншого бізнесу. "
                    "Створіть окремого бота в @BotFather або від'єднайте його від попереднього бізнесу."
                )
                return
            raise
        waiting_token.discard(user_id)
        bot_runner.restart_bot(shop["shop_id"], token)

        await update.message.reply_text(
            f"✅ Бота @{username} успішно підключено!\n\n"
            f"Тепер заповніть дані бізнесу й перевірте відповіді AI перед запуском для клієнтів.\n\n"
            f"Username або посилання на вашого бота ви завжди можете знайти у кнопці 🔗 Посилання.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Заповнити дані бізнесу", callback_data="owner_setup")],
                [InlineKeyboardButton("✅ До готовності", callback_data="owner_readiness")],
                [InlineKeyboardButton("⬅ Головне меню", callback_data="back_menu")],
            ]),
        )
        return

    if user_id in catalog_mode:
        state = catalog_mode[user_id]
        stage = state.get("stage", "name")
        if stage == "availability":
            await update.message.reply_text(
                "Оберіть статус кнопкою нижче.", reply_markup=_catalog_step_keyboard(state)
            )
            return
        value = text.strip()
        if stage == "name" and not value:
            await update.message.reply_text(
                "Назва обов'язкова. Напишіть назву товару або послуги.",
                reply_markup=_catalog_step_keyboard(state),
            )
            return
        state["values"][stage] = value
        stages = ("name", "price", "availability", "description")
        index = stages.index(stage)
        if index == len(stages) - 1:
            try:
                item_id, name = await _save_catalog_state(shop["shop_id"], state)
            except ValueError:
                await update.message.reply_text(
                    "⚠️ Не вдалося зберегти позицію. Перевірте введені дані та повторіть.",
                    reply_markup=_catalog_step_keyboard(state),
                )
                return
            catalog_mode.pop(user_id, None)
            await db.add_audit_event(
                shop["shop_id"], "owner", user_id, "catalog_item_saved", "catalog_item", str(item_id)
            )
            await update.message.reply_text(
                f"✅ Позицію «{name}» збережено. Ціна вказана у гривнях.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ До каталогу", callback_data="owner_catalog")]]),
            )
            return
        state["stage"] = stages[index + 1]
        await update.message.reply_text(
            _catalog_step_text(state), reply_markup=_catalog_step_keyboard(state)
        )
        return

    if user_id in owner_test_mode:
        state = owner_test_mode[user_id]
        if not await db.reserve_ai_request(shop["shop_id"]):
            await update.message.reply_text(
                "⚠️ Місячний ліміт AI вичерпано. Збільште ліміт перед наступною перевіркою.",
                reply_markup=preferences_keyboard(),
            )
            return
        settings = await db.get_shop_settings(shop["shop_id"])
        catalog = await db.list_catalog_items(shop["shop_id"])
        system = ai.system_prompt_customer(shop.get("shop_name") or "", shop.get("knowledge") or "", settings, catalog)
        result = await ai.customer_chat(system, state.get("history", []), text, history_limit=10)
        usage = result.get("usage") or {}
        await db.record_ai_usage(
            shop["shop_id"],
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            usage.get("estimated_cost_usd", 0.0),
            count_request=False,
        )
        if not result.get("ok") or not result.get("content"):
            await update.message.reply_text(
                "⚠️ AI тимчасово не відповів. Налаштування та тестовий діалог збережено — спробуйте ще раз.",
                reply_markup=owner_test_keyboard(),
            )
            return
        history = state.setdefault("history", [])
        history.extend([
            {"role": "user", "content": text},
            {"role": "assistant", "content": result["content"]},
        ])
        state["history"] = history[-20:]
        state["reply_count"] = int(state.get("reply_count", 0)) + 1
        await update.message.reply_text(
            f"🤖 {result['content']}",
            reply_markup=owner_test_keyboard(),
        )
        return

    if user_id in reply_mode:
        reply_ctx = reply_mode.pop(user_id)
        client_id = reply_ctx["client_id"]
        if reply_ctx.get("expires_at", 0) < datetime.utcnow().timestamp():
            await update.message.reply_text(
                "⌛ Режим відповіді через manager bot вимкнено, бо минуло 5 хвилин. Якщо хочете написати клієнту — натисніть цю кнопку знову у переписці.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅ До переписки", callback_data=f"chat_{client_id}")],
                ])
            )
            return
        if not shop.get("client_bot_token"):
            await update.message.reply_text("❌ Клієнтський бот не підключено.")
            return
        await db.enqueue_outbox(
            event_key=f"manager-reply:{shop['shop_id']}:{update.update_id}",
            shop_id=shop["shop_id"],
            topic="client_message",
            payload={"user_id": client_id, "text": text},
        )
        await db.add_message(shop["shop_id"], client_id, "assistant", text)
        await db.add_audit_event(
            shop["shop_id"], "owner", user_id, "manager_reply_queued", "client", str(client_id)
        )
        await update.message.reply_text(
            "✅ Повідомлення поставлено в надійну чергу відправки. AI у цьому чаті не вмикається автоматично.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ До переписки", callback_data=f"chat_{client_id}")],
            ])
        )
        return

    has_bot = bool(shop.get("client_bot_token"))

    if user_id in setup_mode:
        state = setup_mode[user_id]
        answer = text.strip()
        if len(answer) > 1200:
            await update.message.reply_text(
                "⚠️ Відповідь занадто довга. Скоротіть її до 1200 символів і надішліть ще раз.",
                reply_markup=_knowledge_question_keyboard(state),
            )
            return
        question = _knowledge_question_by_index(int(state.get("index", 0)))
        state["answers"][question["key"]] = answer
        state["index"] = int(state.get("index", 0)) + 1
        if state["index"] < len(KNOWLEDGE_QUESTIONS):
            await update.message.reply_text(
                _knowledge_question_text(state), reply_markup=_knowledge_question_keyboard(state)
            )
            return
        content = _compose_knowledge(state["base_knowledge"], state["answers"])
        if len(content) > MAX_KNOWLEDGE_LEN:
            state["index"] = len(KNOWLEDGE_QUESTIONS) - 1
            await update.message.reply_text(
                "⚠️ Даних забагато. Скоротіть останню відповідь і спробуйте ще раз.",
                reply_markup=_knowledge_question_keyboard(state),
            )
            return
        await db.create_knowledge_draft(shop["shop_id"], content)
        preview = content[:3200] + ("\n…" if len(content) > 3200 else "")
        await update.message.reply_text(
            "🔎 Перевірте дані бізнесу\n\n"
            f"{preview}\n\n"
            "Чинна база знань ще не змінена. Збережіть або поверніться до запитань.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Зберегти", callback_data="knowledge_save")],
                [InlineKeyboardButton("⬅ До запитань", callback_data="setup_back")],
                [InlineKeyboardButton("❌ Скасувати", callback_data="knowledge_cancel")],
            ]),
        )
    else:
        await update.message.reply_text(
            "⚠️ Щоб змінити інформацію про бізнес — відкрийте «📝 Дані бізнесу».",
            reply_markup=owner_menu_keyboard(has_bot=has_bot, is_admin_user=is_admin(user_id))
        )


async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот не приймає файли. Напишіть ваше питання текстом 🙏")


async def manager_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    incident_id = f"INC-{int(datetime.utcnow().timestamp())}-{id(context.error) % 10000:04d}"
    error = context.error
    error_info = (type(error), error, error.__traceback__) if error else None
    if type(error).__name__ == "Conflict":
        record_polling_conflict("manager")
    logger.error(
        "Unhandled manager bot error | incident_id=%s error_type=%s",
        incident_id,
        type(error).__name__ if error else "Unknown",
        exc_info=error_info,
    )
    message = getattr(update, "effective_message", None)
    if message:
        try:
            await message.reply_text(
                "⚠️ Не вдалося виконати дію. Попередні дані не змінено — спробуйте ще раз.\n"
                f"Якщо помилка повториться, повідомте підтримці код: {incident_id}"
            )
        except Exception:
            logger.warning("Could not deliver manager incident id | incident_id=%s", incident_id)


def main():
    if not BOT_TOKEN:
        print("ERROR: MANAGER_BOT_TOKEN not set in .env")
        return

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(5)
        .read_timeout(15)
        .write_timeout(15)
        .pool_timeout(5)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("knowledge", knowledge_command))
    app.add_handler(CommandHandler("orders", orders_command))
    app.add_handler(CommandHandler("chats", chats_command))
    app.add_handler(CommandHandler("link", link_command))
    app.add_handler(CommandHandler("subscription", subscription_command))
    app.add_handler(CommandHandler("support", support_command))
    app.add_handler(CommandHandler("privacy", privacy_command))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("deleteaccount", delete_account_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(
        ~filters.TEXT & ~filters.COMMAND,
        handle_unsupported
    ))
    app.add_error_handler(manager_error_handler)

    print("🤖 Manager bot starting...")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
