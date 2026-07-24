"""
Telegram бот мониторинга билетов mostanet.ru — многопользовательский.

Каждый пользователь независимо настраивает маршруты и даты,
получает личные уведомления о появлении билетов.

Команды:
  /start          — приветствие
  /addroute       — добавить маршрут (пошаговый диалог)
  /routes         — список маршрутов с кнопками удаления
  /adddate        — добавить дату(ы)
  /dates          — список дат с кнопками удаления
  /check          — проверить прямо сейчас
  /clearnotified  — сбросить кэш уведомлений
  /status         — текущие настройки
  /help           — справка
"""

import asyncio
import logging

from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, ContextTypes, filters,
)

import state as st
from config import TELEGRAM_BOT_TOKEN, CHECK_INTERVAL, KNOWN_PORTS
from monitor import TicketMonitor, Ticket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

scheduler: AsyncIOScheduler | None = None
app_ref: Application | None = None

ROUTE_FROM, ROUTE_TO, ROUTE_CONFIRM = range(3)


# ══════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ══════════════════════════════════════════════════════════════════════════════

def port_keyboard() -> ReplyKeyboardMarkup:
    rows = [[p] for p in KNOWN_PORTS] + [["✏️ Ввести вручную"]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def format_ticket(t: Ticket) -> str:
    lines = [
        f"🚢 *{t.route}*",
        f"📅 {t.date}",
        f"🕐 {t.departure_time} → {t.arrival_time}",
        f"💺 Всего мест: *{t.seats_available}*",
    ]
    if t.comfort_info:
        lines.append("")
        for c in t.comfort_info:
            lines.append(f"  • {c}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Проверка билетов
# ══════════════════════════════════════════════════════════════════════════════

async def check_user(user: st.UserState, notify: bool = True) -> list[Ticket]:
    """Проверяет билеты для одного пользователя."""
    if not user.routes or not user.dates:
        return []

    monitor = TicketMonitor(
        routes=[{"from_port": r.from_port, "to_port": r.to_port} for r in user.routes]
    )
    try:
        tickets = await monitor.check_all(user.dates)
    except Exception as e:
        logger.error(f"[user {user.chat_id}] Ошибка проверки: {e}")
        return []

    if notify and tickets:
        await send_notifications(user, tickets)

    return tickets


async def send_notifications(user: st.UserState, tickets: list[Ticket]) -> None:
    if not app_ref:
        return

    new_tickets = [t for t in tickets if not user.is_notified(t.trip_id)]
    if not new_tickets:
        return

    for t in new_tickets:
        user.mark_notified(t.trip_id)

    keyboard = [[InlineKeyboardButton("🎫 Купить билет", url="https://mostanet.ru")]]
    markup = InlineKeyboardMarkup(keyboard)

    if len(new_tickets) == 1:
        text = "🔔 *Появились билеты!*\n\n" + format_ticket(new_tickets[0])
    else:
        parts = [f"🔔 *Появились билеты! ({len(new_tickets)} рейсов)*\n"]
        for t in new_tickets:
            parts.append(format_ticket(t))
        text = "\n\n".join(parts)

    try:
        await app_ref.bot.send_message(
            chat_id=user.chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        logger.info(f"[user {user.chat_id}] Отправлено {len(new_tickets)} уведомлений")
    except Exception as e:
        logger.error(f"[user {user.chat_id}] Ошибка отправки: {e}")


async def scheduled_check() -> None:
    """Плановая проверка для всех пользователей."""
    users = st.all_users()
    active = [u for u in users if u.routes and u.dates]
    if not active:
        return
    logger.info(f"Плановая проверка: {len(active)} пользователей")
    for user in active:
        await check_user(user, notify=True)


# ══════════════════════════════════════════════════════════════════════════════
# /start, /help, /status
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = st.get_user(update.effective_chat.id)
    text = (
        "👋 *Бот мониторинга билетов mostanet\\.ru*\n\n"
        "Слежу за появлением билетов по твоим маршрутам и датам\\.\n"
        "Как только билеты появятся — сразу пришлю сообщение\\.\n\n"
        "📍 /addroute — добавить маршрут\n"
        "📅 /adddate — добавить дату\n"
        "🔍 /check — проверить прямо сейчас\n"
        "📊 /status — мои настройки\n"
        "❓ /help — справка"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 *Справка*\n\n"
        "*Маршруты:*\n"
        "/addroute — добавить маршрут\n"
        "/routes — список маршрутов \\(с удалением\\)\n\n"
        "*Даты:*\n"
        "/adddate 2024\\-07\\-20 — добавить дату\n"
        "/adddate \\+30 — ближайшие 30 дней\n"
        "/dates — список дат \\(с удалением\\)\n\n"
        "*Прочее:*\n"
        "/check — проверить прямо сейчас\n"
        "/clearnotified — уведомить заново обо всех найденных\n"
        "/status — мои настройки и статус мониторинга"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = st.get_user(update.effective_chat.id)

    routes_str = (
        "\n".join(f"  {i+1}. {r.label()}" for i, r in enumerate(user.routes))
        or "  не добавлены"
    )
    dates_str = (
        "\n".join(f"  {d}" for d in user.dates[:10])
        + (f"\n  ...и ещё {len(user.dates)-10}" if len(user.dates) > 10 else "")
        or "  не добавлены"
    )
    sch = "✅ работает" if scheduler and scheduler.running else "❌ остановлен"

    text = (
        f"📊 *Твои настройки*\n\n"
        f"*Маршруты:*\n{routes_str}\n\n"
        f"*Дат в мониторинге:* {len(user.dates)}\n"
        f"{dates_str}\n\n"
        f"*Планировщик:* {sch}\n"
        f"*Интервал:* каждые {CHECK_INTERVAL // 60} мин\\.\n"
        f"*Кэш уведомлений:* {len(user.notified)} рейсов"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


# ══════════════════════════════════════════════════════════════════════════════
# /addroute — пошаговый диалог
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_addroute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "📍 *Шаг 1/2 — Откуда?*\n\nВыбери из списка или напиши название порта вручную.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=port_keyboard(),
    )
    return ROUTE_FROM


async def route_got_from(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == "✏️ Ввести вручную":
        await update.message.reply_text(
            "✏️ Напиши название порта отправления:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ROUTE_FROM

    context.user_data["from_port"] = text
    await update.message.reply_text(
        f"✅ Откуда: *{text}*\n\n📍 *Шаг 2/2 — Куда?*\n\nВыбери из списка или напиши вручную.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=port_keyboard(),
    )
    return ROUTE_TO


async def route_got_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == "✏️ Ввести вручную":
        await update.message.reply_text(
            "✏️ Напиши название порта назначения:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ROUTE_TO

    from_port = context.user_data.get("from_port", "?")
    to_port = text

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Добавить", callback_data=f"route_confirm|{from_port}|{to_port}"),
        InlineKeyboardButton("❌ Отмена", callback_data="route_cancel"),
    ]])
    await update.message.reply_text(
        f"Добавить маршрут?\n\n🚢 *{from_port} → {to_port}*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )
    await update.message.reply_text("👆 Подтверди выше", reply_markup=ReplyKeyboardRemove())
    return ROUTE_CONFIRM


async def route_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "route_cancel":
        await query.edit_message_text("❌ Добавление отменено.")
        return ConversationHandler.END

    _, from_port, to_port = query.data.split("|", 2)
    user = st.get_user(query.message.chat_id)
    added = user.add_route(from_port, to_port)

    if added:
        await query.edit_message_text(
            f"✅ Маршрут добавлен: *{from_port} → {to_port}*\n\nВсего маршрутов: {len(user.routes)}",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.edit_message_text(
            f"ℹ️ Маршрут *{from_port} → {to_port}* уже есть.",
            parse_mode=ParseMode.MARKDOWN,
        )
    return ConversationHandler.END


async def route_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# /routes — список с кнопками удаления
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_routes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = st.get_user(update.effective_chat.id)
    if not user.routes:
        await update.message.reply_text("Маршруты не добавлены.\n\nИспользуй /addroute")
        return

    buttons = [
        [InlineKeyboardButton(f"🗑 {r.label()}", callback_data=f"del_route|{i}")]
        for i, r in enumerate(user.routes)
    ]
    text = "📍 *Твои маршруты:*\n\n" + "\n".join(f"{i+1}. {r.label()}" for i, r in enumerate(user.routes))
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


async def del_route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = st.get_user(query.message.chat_id)
    index = int(query.data.split("|")[1])
    removed = user.remove_route(index)
    if removed:
        await query.edit_message_text(f"🗑 Маршрут удалён: *{removed.label()}*", parse_mode=ParseMode.MARKDOWN)
    else:
        await query.edit_message_text("⚠️ Маршрут не найден.")


# ══════════════════════════════════════════════════════════════════════════════
# /adddate
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_adddate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if args:
        await _process_date_arg(update, args[0], st.get_user(update.effective_chat.id))
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("+7 дней", callback_data="adddate|+7"),
         InlineKeyboardButton("+14 дней", callback_data="adddate|+14")],
        [InlineKeyboardButton("+30 дней", callback_data="adddate|+30"),
         InlineKeyboardButton("+60 дней", callback_data="adddate|+60")],
        [InlineKeyboardButton("✏️ Указать дату вручную", callback_data="adddate|manual")],
    ])
    await update.message.reply_text(
        "📅 *Добавить даты мониторинга*\n\nВыбери вариант или укажи конкретную дату:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def adddate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    arg = query.data.split("|")[1]
    user = st.get_user(query.message.chat_id)

    if arg == "manual":
        await query.edit_message_text(
            "✏️ Отправь дату в формате *ГГГГ-ММ-ДД*\n\nПример: `2024-07-20`",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data["awaiting_date"] = True
        return

    n = int(arg[1:])
    today = datetime.now().date()
    added = sum(1 for i in range(n) if user.add_date((today + timedelta(days=i)).strftime("%Y-%m-%d")))
    await query.edit_message_text(f"✅ Добавлено {added} дат.\nВсего дат: {len(user.dates)}")


async def handle_date_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_date"):
        return
    context.user_data.pop("awaiting_date")
    user = st.get_user(update.effective_chat.id)
    await _process_date_arg(update, update.message.text.strip(), user)


async def _process_date_arg(update: Update, arg: str, user: st.UserState) -> None:
    if arg.startswith("+"):
        try:
            n = int(arg[1:])
            today = datetime.now().date()
            added = sum(1 for i in range(n) if user.add_date((today + timedelta(days=i)).strftime("%Y-%m-%d")))
            await update.message.reply_text(f"✅ Добавлено {added} дат.\nВсего дат: {len(user.dates)}")
        except ValueError:
            await update.message.reply_text("❌ Неверный формат. Пример: `/adddate +7`", parse_mode=ParseMode.MARKDOWN)
    else:
        try:
            datetime.strptime(arg, "%Y-%m-%d")
            if user.add_date(arg):
                await update.message.reply_text(f"✅ Дата добавлена: *{arg}*\nВсего дат: {len(user.dates)}", parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(f"ℹ️ Дата {arg} уже есть.")
        except ValueError:
            await update.message.reply_text("❌ Формат: `ГГГГ-ММ-ДД`\nПример: `/adddate 2024-07-20`", parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════════
# /dates
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = st.get_user(update.effective_chat.id)
    if not user.dates:
        await update.message.reply_text("Даты не добавлены.\n\nИспользуй /adddate")
        return

    dates_to_show = user.dates[:30]
    buttons = [[InlineKeyboardButton(f"🗑 {d}", callback_data=f"del_date|{d}")] for d in dates_to_show]
    if len(user.dates) > 30:
        buttons.append([InlineKeyboardButton(f"...и ещё {len(user.dates)-30}", callback_data="noop")])

    text = f"📅 *Даты мониторинга* ({len(user.dates)} шт.):\n\n" + "\n".join(dates_to_show)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))


async def del_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = st.get_user(query.message.chat_id)
    date = query.data.split("|")[1]
    if user.remove_date(date):
        await query.edit_message_text(f"🗑 Дата удалена: *{date}*", parse_mode=ParseMode.MARKDOWN)
    else:
        await query.edit_message_text("⚠️ Дата не найдена.")


# ══════════════════════════════════════════════════════════════════════════════
# /check
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = st.get_user(update.effective_chat.id)

    if not user.routes:
        await update.message.reply_text("⚠️ Маршруты не добавлены.\nИспользуй /addroute")
        return
    if not user.dates:
        await update.message.reply_text("⚠️ Даты не добавлены.\nИспользуй /adddate")
        return

    msg = await update.message.reply_text(
        f"🔍 Проверяю {len(user.routes)} маршрут(а) × {len(user.dates)} дат..."
    )

    tickets = await check_user(user, notify=False)

    if not tickets:
        await msg.edit_text("😔 Билетов пока нет. Продолжаю следить.")
        return

    new_tickets = [t for t in tickets if not user.is_notified(t.trip_id)]
    for t in tickets:
        user.mark_notified(t.trip_id)

    if not new_tickets:
        await msg.edit_text(
            f"ℹ️ Найдено {len(tickets)} рейсов, но все уже были показаны.\n"
            "Используй /clearnotified чтобы сбросить кэш."
        )
        return

    parts = [f"✅ *Найдено {len(new_tickets)} рейсов:*\n"]
    for t in new_tickets:
        parts.append(format_ticket(t))

    keyboard = [[InlineKeyboardButton("🎫 Купить билет", url="https://mostanet.ru")]]
    await msg.edit_text(
        "\n\n".join(parts),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# /clearnotified
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_clearnotified(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = st.get_user(update.effective_chat.id)
    count = len(user.notified)
    user.clear_notified()
    await update.message.reply_text(
        f"✅ Кэш сброшен ({count} записей).\nПри следующей проверке уведомлю заново."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Планировщик
# ══════════════════════════════════════════════════════════════════════════════

def start_scheduler(app: Application) -> None:
    global scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_check,
        trigger="interval",
        seconds=CHECK_INTERVAL,
        id="ticket_check",
        replace_existing=True,
        misfire_grace_time=60,
    )
    scheduler.start()
    logger.info(f"Планировщик запущен. Интервал: {CHECK_INTERVAL} сек.")


# ══════════════════════════════════════════════════════════════════════════════
# Запуск
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    global app_ref

    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("❌ TELEGRAM_BOT_TOKEN не задан в .env")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app_ref = app

    addroute_conv = ConversationHandler(
        entry_points=[CommandHandler("addroute", cmd_addroute)],
        states={
            ROUTE_FROM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, route_got_from)],
            ROUTE_TO:     [MessageHandler(filters.TEXT & ~filters.COMMAND, route_got_to)],
            ROUTE_CONFIRM:[CallbackQueryHandler(route_confirm_callback, pattern=r"^route_")],
        },
        fallbacks=[CommandHandler("cancel", route_cancel)],
        allow_reentry=True,
    )

    app.add_handler(addroute_conv)
    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("status",        cmd_status))
    app.add_handler(CommandHandler("routes",        cmd_routes))
    app.add_handler(CommandHandler("adddate",       cmd_adddate))
    app.add_handler(CommandHandler("dates",         cmd_dates))
    app.add_handler(CommandHandler("check",         cmd_check))
    app.add_handler(CommandHandler("clearnotified", cmd_clearnotified))

    app.add_handler(CallbackQueryHandler(del_route_callback, pattern=r"^del_route\|"))
    app.add_handler(CallbackQueryHandler(del_date_callback,  pattern=r"^del_date\|"))
    app.add_handler(CallbackQueryHandler(adddate_callback,   pattern=r"^adddate\|"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern=r"^noop$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date_text))

    start_scheduler(app)
    logger.info("Бот запущен (многопользовательский режим).")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
