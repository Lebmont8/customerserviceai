"""
CustomerServiceAI — мастер-бот для AI-помічників продажів.
Один бот обслуговує багато магазинів через deep links.
"""
import os
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from dotenv import load_dotenv
import database as db
import ai
import payments

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
BOT_TOKEN = os.getenv("SHOPBOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "ownercustomerservicebot")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# In-memory: user_id → shop_id (для клієнтів які вже обрали магазин)
user_shop_map: dict[int, str] = {}


async def auto_check_payments(context):
    """Every 60s: check pending invoices, activate if paid."""
    pending = await payments.get_pending_invoices()
    for inv in pending:
        status = await payments.check_invoice(inv["invoice_id"])
        if status == "paid":
            await payments.process_paid_invoice(inv["invoice_id"], inv["shop_id"])
            try:
                await context.bot.send_message(
                    chat_id=inv["admin_id"],
                    text="✅ *Оплату підтверджено!*\n\n"
                         "Підписку продовжено на 1 місяць. Дякуємо! 🎉",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to notify payment: {e}")


async def check_and_notify(context):
    """Job: runs 2 min before trial ends. Checks payment, notifies owner."""
    shop_id = context.job.data["shop_id"]
    admin_id = context.job.data["admin_id"]

    sub_active = await payments.is_active(shop_id)

    if sub_active:
        # Вже оплачено — повідомляємо
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text="✅ *Оплату підтверджено!*\n\n"
                     "Підписку продовжено на 1 місяць. Дякуємо! 🎉",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to notify paid: {e}")
    else:
        # Не оплачено — нагадування
        is_last = context.job.data.get("last", False)
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
    await payments.mark_reminder_sent(shop_id)


async def post_init(app: Application):
    await db.init_db()
    await payments.init_payments_db()
    await app.bot.set_my_commands([
        BotCommand("start", "Почати / головне меню"),
        BotCommand("myshop", "Керування вашим магазином"),
        BotCommand("info", "База знань магазину"),
        BotCommand("link", "Посилання для клієнтів"),
        BotCommand("reset", "Очистити історію розмови"),
    ])
    logger.info("Bot started")


# ─── HELPERS ────────────────────────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🏪 Я власник бізнесу", callback_data="role_owner"),
        InlineKeyboardButton("💬 Я клієнт", callback_data="role_customer"),
    ]])


def owner_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Налаштування", callback_data="owner_setup"),
         InlineKeyboardButton("📋 База знань", callback_data="owner_info")],
        [InlineKeyboardButton("🔗 Посилання", callback_data="owner_link"),
         InlineKeyboardButton("💳 Підписка", callback_data="owner_subscription")],
        [InlineKeyboardButton("🛠 Тех. підтримка", callback_data="owner_support")],
    ])


# ─── /start ─────────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    # Клієнт прийшов за посиланням магазину: /start SHOP_ID
    if args:
        shop_id = args[0]
        shop = await db.get_shop_by_id(shop_id)
        if shop:
            user_shop_map[user_id] = shop_id
            shop_name = shop["shop_name"] or "бізнес"
            await update.message.reply_text(
                f"👋 Вітаю! Я — AI-консультант {shop_name}.\n\n"
                f"Запитайте мене про:\n"
                f"• Послуги та ціни\n"
                f"• Графік роботи\n"
                f"• Адресу та доставку\n"
                f"• Як зробити замовлення або записатися\n"
                f"• Або будь-яке інше питання про послуги\n\n"
                f"Якщо у вас виникли проблеми з ботом або є пропозиції — пишіть нам:\n"
                f'<a href="https://example.invalid/support">Instagram</a>',
                parse_mode="HTML",
            )
            return
        else:
            await update.message.reply_text("❌ Не знайдено. Спробуйте інше посилання.")
            return

    # Власник вже зареєстрований
    shop = await db.get_shop_by_admin(user_id)
    if shop:
        await update.message.reply_text(
            f"👋 З поверненням! Ваш магазин: *{shop['shop_name'] or 'без назви'}*",
            parse_mode="Markdown",
            reply_markup=owner_menu_keyboard()
        )
        return

    # Новий користувач — вибір ролі
    await update.message.reply_text(
        "👋 Вітаю! Я — AI-помічник для бізнесу.\n\nХто ви?",
        reply_markup=main_menu_keyboard()
    )


# ─── CALLBACKS ───────────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "role_owner":
        shop = await db.get_shop_by_admin(user_id)
        is_new = not shop
        if not shop:
            shop_id = await db.create_shop(user_id)
            shop = await db.get_shop_by_id(shop_id)
            await payments.create_subscription(shop_id)

            # Планируем два напоминания: за 10 минут и за 1 минуту до конца
            from datetime import timezone
            sub = await payments.get_subscription(shop_id)
            trial_end = datetime.fromisoformat(sub["trial_end"]).replace(tzinfo=timezone.utc)

            context.application.job_queue.run_once(
                check_and_notify,
                when=trial_end - timedelta(minutes=10),
                data={"shop_id": shop_id, "admin_id": user_id, "last": False},
                name=f"remind_10_{shop_id}"
            )
            context.application.job_queue.run_once(
                check_and_notify,
                when=trial_end - timedelta(minutes=1),
                data={"shop_id": shop_id, "admin_id": user_id, "last": True},
                name=f"remind_1_{shop_id}"
            )

        if is_new:
            await query.edit_message_text(
                "🏪 *Ласкаво просимо!*\n\n"
                "🎁 У вас є *7 днів безкоштовного користування* ботом.\n"
                "Після закінчення пробного періоду підписка коштує $20/місяць.\n\n"
                "Розкажіть боту все про ваш бізнес — він запам'ятає і буде відповідати клієнтам.",
                parse_mode="Markdown",
                reply_markup=owner_menu_keyboard()
            )
        else:
            await query.edit_message_text(
                "🏪 *Панель власника*\n\n"
                "Розкажіть боту все про ваш бізнес — він запам'ятає і буде відповідати клієнтам.\n\n"
                "Просто напишіть текстом: назву, товари, ціни, доставку, оплату.",
                parse_mode="Markdown",
                reply_markup=owner_menu_keyboard()
            )

    elif data == "role_customer":
        await query.edit_message_text(
            "🛍 Ви — покупець.\n\n"
            "Щоб спілкуватися з ботом конкретного магазину, перейдіть за посиланням яке дав вам продавець.\n\n"
            "Якщо у вас є таке посилання — перейдіть за ним!"
        )

    elif data == "owner_setup":
        await query.edit_message_text(
            "📝 *Режим налаштування*\n\n"
            "Розкажіть все про ваш бізнес:\n"
            "• Назва бізнесу\n"
            "• Чим займаєтесь / що пропонуєте\n"
            "• Ціни на послуги або товари\n"
            "• Графік роботи\n"
            "• Адреса / як вас знайти\n"
            "• Як зв'язатися з менеджером (якщо потрібно)\n"
            "• Доставка (якщо є)\n"
            "• Акції або спеціальні пропозиції\n"
            "• Будь-яка інша інформація яку повинен знати ваш клієнт\n\n"
            "Можна писати у вільній формі — AI сам структурує інформацію.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Назад", callback_data="back_menu")],
            ])
        )

    elif data == "owner_info":
        shop = await db.get_shop_by_admin(user_id)
        back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Назад", callback_data="back_menu")]])
        if shop and shop["knowledge"]:
            text = shop["knowledge"][:3500]
            await query.edit_message_text(
                f"📋 *База знань:*\n\n{text}",
                parse_mode="Markdown",
                reply_markup=back_btn
            )
        else:
            await query.edit_message_text(
                "📋 База знань порожня. Напишіть боту про ваш бізнес!",
                reply_markup=back_btn
            )

    elif data == "owner_link":
        shop = await db.get_shop_by_admin(user_id)
        if shop:
            link = f"https://t.me/{BOT_USERNAME}?start={shop['shop_id']}"
            await query.edit_message_text(
                f"🔗 *Посилання для ваших клієнтів:*\n\n`{link}`\n\n"
                f"Надішліть його клієнтам або вставте у профіль вашого бізнесу — вони одразу потраплять до вашого AI-консультанта.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅ Назад", callback_data="back_menu")],
                ])
            )

    elif data == "owner_support":
        await query.edit_message_text(
            "🛠 Технічна підтримка\n\n"
            "Якщо у вас виникли проблеми з ботом або є пропозиції — пишіть нам:\n\n"
            '<a href="https://example.invalid/support">Instagram</a>',
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅ Назад", callback_data="back_menu")],
            ])
        )

    elif data in ("owner_subscription", "pay_subscribe"):
        shop = await db.get_shop_by_admin(user_id)
        if not shop:
            return
        await query.edit_message_text("⏳ Створюю рахунок для оплати...")
        invoice = await payments.create_invoice(shop["shop_id"])
        if invoice:
            days = await payments.days_left(shop["shop_id"])
            status_line = f"✅ Підписка активна — залишилось днів: {days}\n\n" if await payments.is_active(shop["shop_id"]) else ""
            await query.edit_message_text(
                f"💳 *Рахунок на оплату*\n\n"
                f"{status_line}"
                f"Сума: ${payments.SUBSCRIPTION_PRICE}\n\n"
                f"Натисніть кнопку нижче щоб оплатити:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Оплатити", url=invoice["pay_url"])],
                    [InlineKeyboardButton("✅ Перевірити оплату", callback_data=f"check_{invoice['invoice_id']}_{shop['shop_id']}_1")],
                    [InlineKeyboardButton("⬅ Назад", callback_data="back_menu")],
                ])
            )
        else:
            await query.edit_message_text(
                "❌ Помилка створення рахунку. Спробуйте пізніше.",
                reply_markup=owner_menu_keyboard()
            )

    elif data.startswith("check_"):
        parts = data.split("_")
        invoice_id = parts[1]
        shop_id = parts[2]
        attempt = int(parts[3]) if len(parts) > 3 else 1

        status = await payments.check_invoice(invoice_id)
        if status == "paid":
            await payments.process_paid_invoice(invoice_id, shop_id)
            days = await payments.days_left(shop_id)
            await query.edit_message_text(
                f"✅ *Оплату підтверджено!*\n\n"
                f"Підписку продовжено на {days} днів. Дякуємо! 🎉",
                parse_mode="Markdown",
                reply_markup=owner_menu_keyboard()
            )
        elif attempt == 1:
            await query.edit_message_text(
                "⏳ *Оплату не знайдено*\n\n"
                "Можливо, платіж ще обробляється. Спробуйте ще раз через 5 хвилин.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Перевірити ще раз", callback_data=f"check_{invoice_id}_{shop_id}_2")],
                ])
            )
        else:
            await query.edit_message_text(
                "❌ *Оплату не знайдено*\n\n"
                "Якщо ви впевнені що оплатили — напишіть нам, ми розберемось.\n\n"
                "📩 [Instagram](https://example.invalid/support)\n\n"
                "_Збережіть скріншот оплати — він прискорить вирішення питання._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Спробувати оплатити знову", callback_data="pay_subscribe")],
                ])
            )

    elif data == "back_menu":
        shop = await db.get_shop_by_admin(user_id)
        name = shop["shop_name"] if shop and shop["shop_name"] else "Ваш магазин"
        await query.edit_message_text(
            f"🏪 *{name}*\n\nОберіть дію:",
            parse_mode="Markdown",
            reply_markup=owner_menu_keyboard()
        )


# ─── COMMANDS ────────────────────────────────────────────────────────────────

async def myshop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    shop = await db.get_shop_by_admin(user_id)
    if not shop:
        await update.message.reply_text(
            "У вас немає магазину. Напишіть /start і оберіть 'Я власник бізнесу'."
        )
        return
    await update.message.reply_text(
        f"🏪 *Ваш магазин:* {shop['shop_name'] or 'без назви'}\n"
        f"🆔 ID: `{shop['shop_id']}`",
        parse_mode="Markdown",
        reply_markup=owner_menu_keyboard()
    )


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    shop = await db.get_shop_by_admin(user_id)
    if not shop:
        await update.message.reply_text("Ви не є власником магазину.")
        return
    knowledge = shop["knowledge"] or ""
    if knowledge:
        await update.message.reply_text(
            f"📋 *База знань:*\n\n{knowledge[:3500]}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("База знань порожня. Напишіть боту про ваш бізнес!")


async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    shop = await db.get_shop_by_admin(user_id)
    if not shop:
        await update.message.reply_text("Ви не є власником магазину.")
        return
    link = f"https://t.me/{BOT_USERNAME}?start={shop['shop_id']}"
    await update.message.reply_text(
        f"🔗 *Посилання для клієнтів:*\n\n`{link}`",
        parse_mode="Markdown"
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    shop_id = user_shop_map.get(user_id)
    if not shop_id:
        shop = await db.get_shop_by_admin(user_id)
        shop_id = shop["shop_id"] if shop else None
    if shop_id:
        await db.clear_conversation(shop_id, user_id)
    await update.message.reply_text("🔄 Історію розмови очищено.")


# ─── MESSAGES ────────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    if not text:
        return

    await update.message.chat.send_action("typing")

    # Перевіряємо: власник?
    shop = await db.get_shop_by_admin(user_id)
    if shop:
        old_knowledge = shop["knowledge"] or ""
        new_knowledge = await ai.extract_and_merge_knowledge(text, old_knowledge)
        knowledge_updated = new_knowledge.strip() != old_knowledge.strip()
        await db.update_knowledge(shop["shop_id"], new_knowledge)

        if not shop["shop_name"] and new_knowledge:
            for line in new_knowledge.split('\n'):
                line_clean = line.replace('**', '').replace('#', '').strip()
                if line_clean.lower().startswith('назва:') or line_clean.lower().startswith('назва -'):
                    name = line_clean.split(':', 1)[-1].split('-', 1)[-1].strip()
                    if name and len(name) < 60 and 'база знань' not in name.lower() and 'оновлен' not in name.lower():
                        await db.update_shop_name(shop["shop_id"], name)
                    break

        history = await db.get_conversation(shop["shop_id"], user_id)
        system = ai.system_prompt_admin(shop["shop_name"], new_knowledge)
        response = await ai.chat(system, history, text)

        await db.add_message(shop["shop_id"], user_id, "user", text)
        await db.add_message(shop["shop_id"], user_id, "assistant", response)

        # Ссылка только при первом сохранении базы знаний
        if not old_knowledge.strip() and new_knowledge.strip():
            link = f"https://t.me/{BOT_USERNAME}?start={shop['shop_id']}"
            full_response = (
                f"{response}\n\n"
                f"─────────────────\n"
                f"🔗 *Посилання для ваших клієнтів:*\n"
                f"`{link}`\n\n"
                f"Надішліть його клієнтам або вставте у профіль вашого бізнесу — вони одразу потраплять до вашого AI-консультанта."
            )
        else:
            full_response = response
        await update.message.reply_text(
            full_response,
            parse_mode="Markdown",
            reply_markup=owner_menu_keyboard()
        )
        return

    # Клієнт з прив'язаним магазином?
    shop_id = user_shop_map.get(user_id)
    if shop_id:
        shop = await db.get_shop_by_id(shop_id)
        if shop:
            # Перевіряємо підписку
            if not await payments.is_active(shop_id):
                await update.message.reply_text(
                    "⏸ Бот тимчасово недоступний. Спробуйте пізніше."
                )
                return
            history = await db.get_conversation(shop_id, user_id)
            system = ai.system_prompt_customer(shop["shop_name"], shop["knowledge"] or "")
            response = await ai.chat(system, history, text)
            await db.add_message(shop_id, user_id, "user", text)
            await db.add_message(shop_id, user_id, "assistant", response)

            # Перевіряємо чи є заявка
            if "[ЗАЯВКА]" in response:
                # Відправляємо власнику
                lines = response.split("\n")
                order_lines = [l for l in lines if "[ЗАЯВКА]" in l]
                clean_response = "\n".join(l for l in lines if "[ЗАЯВКА]" not in l).strip()

                for order_line in order_lines:
                    user_info = update.effective_user
                    username = f"@{user_info.username}" if user_info.username else user_info.full_name
                    try:
                        await context.bot.send_message(
                            chat_id=shop["admin_id"],
                            text=f"📬 *Нова заявка!*\n\n"
                                 f"👤 Клієнт: {username}\n"
                                 f"{order_line.replace('[ЗАЯВКА]', '').strip()}\n\n"
                                 f"💬 Написати клієнту: tg://user?id={user_id}",
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.error(f"Failed to notify admin: {e}")

                # Клієнту відправляємо без тега [ЗАЯВКА]
                if clean_response:
                    await update.message.reply_text(clean_response)
                else:
                    await update.message.reply_text("✅ Вашу заявку прийнято! Менеджер зв'яжеться з вами.")
            else:
                await update.message.reply_text(response)
            return

    # Невідомий користувач
    await update.message.reply_text(
        "👋 Вітаю! Використайте /start щоб почати.\n\n"
        "Якщо ви покупець — перейдіть за посиланням від вашого продавця."
    )


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print("ERROR: SHOPBOT_TOKEN not set in .env")
        return

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("myshop", myshop_command))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("link", link_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Бот не приймає файли. Напишіть ваше питання текстом 🙏")

    app.add_handler(MessageHandler(
        (filters.Document.ALL | filters.AUDIO | filters.VIDEO | filters.Sticker.ALL | filters.VOICE | filters.VIDEO_NOTE) & ~filters.COMMAND,
        handle_unsupported
    ))

    print("🤖 CustomerServiceAI (master) starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    raise SystemExit(
        "Legacy bot.py is disabled. Use main.py (manager_bot.py + client_bot.py)."
    )
