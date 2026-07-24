"""
Telegram бот мониторинга билетов mostanet.ru — многопользовательский.

Каждый пользователь независимо настраивает маршруты и даты (per-route),
получает личные уведомления о появлении билетов.
"""

import asyncio
import calendar as cal_module
import logging

from datetime import datetime, timedelta, date as date_type
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

MONTHS_RU = ["Январь","Февраль","Март","Апрель","Май","Июнь",
             "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
DAYS_RU = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]


# ══════════════════════════════════════════════════════════════════════════════
# Календарь
# ══════════════════════════════════════════════════════════════════════════════

def build_calendar(
    year: int,
    month: int,
    added: set[str],
    selected: list[str],
    routes: list[st.Route],
    route_idx: int,
) -> InlineKeyboardMarkup:
    today = date_type.today()
    rows = []

    # ── Переключатель маршрутов ───────────────────────────────────────────────
    if routes:
        route_row = []
        for i, r in enumerate(routes):
            # Сокращаем название если длинное
            name = r.label()
            if len(name) > 20:
                name = f"{r.from_port[:8]}→{r.to_port[:8]}"
            label = f"► {name}" if i == route_idx else name
            route_row.append(InlineKeyboardButton(label, callback_data=f"cal_route|{i}"))
        # Разбиваем на строки по 2 если маршрутов больше 2
        for i in range(0, len(route_row), 2):
            rows.append(route_row[i:i+2])

    # ── Навигация по месяцу ───────────────────────────────────────────────────
    pm, py = (month - 1, year) if month > 1 else (12, year - 1)
    nm, ny = (month + 1, year) if month < 12 else (1, year + 1)
    rows.append([
        InlineKeyboardButton("◀", callback_data=f"cal_nav|{py}|{pm}"),
        InlineKeyboardButton(f"{MONTHS_RU[month-1]} {year}", callback_data="cal_noop"),
        InlineKeyboardButton("▶", callback_data=f"cal_nav|{ny}|{nm}"),
    ])

    # ── Заголовок дней ────────────────────────────────────────────────────────
    rows.append([InlineKeyboardButton(d, callback_data="cal_noop") for d in DAYS_RU])

    # ── Дни месяца ────────────────────────────────────────────────────────────
    for week in cal_module.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="cal_noop"))
                continue
            ds = f"{year:04d}-{month:02d}-{day:02d}"
            if date_type(year, month, day) < today:
                row.append(InlineKeyboardButton(f"·{day}", callback_data="cal_noop"))
            elif ds in added:
                row.append(InlineKeyboardButton(f"✅{day}", callback_data="cal_noop"))
            elif ds in selected:
                row.append(InlineKeyboardButton(f"📅{day}", callback_data=f"cal_deselect|{ds}"))
            else:
                row.append(InlineKeyboardButton(str(day), callback_data=f"cal_select|{ds}"))
        rows.append(row)

    # ── Кнопки действий ──────────────────────────────────────────────────────
    if selected:
        rows.append([
            InlineKeyboardButton(f"✅ Добавить ({len(selected)})", callback_data="cal_confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="cal_cancel"),
        ])
    else:
        rows.append([InlineKeyboardButton("❌ Закрыть", callback_data="cal_cancel")])

    return InlineKeyboardMarkup(rows)


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
    """Проверяет билеты для всех маршрутов пользователя (per-route dates)."""
    if not user.routes:
        return []

    all_tickets: list[Ticket] = []
    for route in user.routes:
        dates = user.get_dates(route.key())
        if not dates:
            continue
        monitor = TicketMonitor(routes=[{"from_port": route.from_port, "to_port": route.to_port}])
        try:
            tickets = await monitor.check_all(dates)
            all_tickets.extend(tickets)
            logger.info(f"[user {user.chat_id}] {route.label()}: {len(tickets)} рейсов")
        except Exception as e:
            logger.error(f"[user {user.chat_id}] {route.label()}: {e}")

    if notify and all_tickets:
        await send_notifications(user, all_tickets)

    return all_tickets


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
    users = st.all_users()
    active = [u for u in users if u.routes and u.all_dates()]
    if not active:
        return
    logger.info(f"Плановая проверка: {len(active)} пользователей")
    for user in active:
        await check_user(user, notify=True)


# ══════════════════════════════════════════════════════════════════════════════
# /start, /help, /status
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st.get_user(update.effective_chat.id)
    text = (
        "👋 *Бот мониторинга билетов mostanet.ru*\n\n"
        "Слежу за появлением билетов по твоим маршрутам и датам.\n"
        "Как только билеты появятся — сразу пришлю сообщение.\n\n"
        "📍 /addroute — добавить маршрут\n"
        "📅 /adddate — выбрать даты через календарь\n"
        "🔍 /check — проверить прямо сейчас\n"
        "📊 /status — мои настройки\n"
        "❓ /help — справка"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 *Справка*\n\n"
        "*Маршруты:*\n"
        "/addroute — добавить маршрут (пошаговый диалог)\n"
        "/routes — список маршрутов с кнопками удаления\n"
        "/clearroutes — удалить все маршруты\n\n"
        "*Даты (настраиваются отдельно для каждого маршрута):*\n"
        "/adddate — открыть календарь:\n"
        "   • переключай маршруты кнопками вверху\n"
        "   • нажимай дни для выбора (можно несколько)\n"
        "   • ✅ — уже добавлены, 📅 — выбраны, · — прошедшие\n"
        "/dates — даты по каждому маршруту (с удалением)\n"
        "/cleardates — удалить все даты\n\n"
        "*Мониторинг:*\n"
        "/check — немедленная проверка\n"
        "/clearnotified — сбросить кэш (уведомить заново)\n"
        "/status — маршруты, даты, статус планировщика"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = st.get_user(update.effective_chat.id)
    sch = "✅ работает" if scheduler and scheduler.running else "❌ остановлен"

    if not user.routes:
        routes_info = "  не добавлены"
    else:
        lines = []
        for r in user.routes:
            dates = user.get_dates(r.key())
            lines.append(f"  {r.label()} — {len(dates)} дат")
        routes_info = "\n".join(lines)

    text = (
        f"📊 *Твои настройки*\n\n"
        f"*Маршруты и даты:*\n{routes_info}\n\n"
        f"*Планировщик:* {sch}\n"
        f"*Интервал:* каждые {CHECK_INTERVAL // 60} мин.\n"
        f"*Кэш уведомлений:* {len(user.notified)} рейсов"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


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
        await update.message.reply_text("✏️ Напиши название порта отправления:", reply_markup=ReplyKeyboardRemove())
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
        await update.message.reply_text("✏️ Напиши название порта назначения:", reply_markup=ReplyKeyboardRemove())
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
            f"✅ Маршрут добавлен: *{from_port} → {to_port}*\n\n"
            f"Теперь добавь даты через /adddate",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.edit_message_text(f"ℹ️ Маршрут *{from_port} → {to_port}* уже есть.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


async def route_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# /routes
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
    lines = [f"{i+1}. {r.label()} ({len(user.get_dates(r.key()))} дат)" for i, r in enumerate(user.routes)]
    await update.message.reply_text(
        "📍 *Твои маршруты:*\n\n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def del_route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = st.get_user(query.message.chat_id)
    removed = user.remove_route(int(query.data.split("|")[1]))
    if removed:
        await query.edit_message_text(f"🗑 Маршрут удалён: *{removed.label()}*", parse_mode=ParseMode.MARKDOWN)
    else:
        await query.edit_message_text("⚠️ Маршрут не найден.")


# ══════════════════════════════════════════════════════════════════════════════
# /adddate — календарь с переключением маршрутов
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_adddate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = st.get_user(update.effective_chat.id)
    if not user.routes:
        await update.message.reply_text("⚠️ Сначала добавь маршрут через /addroute")
        return

    today = date_type.today()
    context.user_data["cal_selected"] = []
    context.user_data["cal_route_idx"] = 0

    route = user.routes[0]
    added = set(user.get_dates(route.key()))
    keyboard = build_calendar(today.year, today.month, added, [], user.routes, 0)
    await update.message.reply_text(
        "📅 *Выбери даты для мониторинга*\n\n"
        "Переключай маршруты кнопками вверху.\n"
        "Нажимай на дни — можно выбрать несколько.\n"
        "✅ — уже добавлены  |  📅 — выбраны  |  · — прошедшие",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def cal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user = st.get_user(query.message.chat_id)
    selected: list[str] = context.user_data.get("cal_selected", [])
    route_idx: int = context.user_data.get("cal_route_idx", 0)

    # Защита от невалидного индекса
    if route_idx >= len(user.routes):
        route_idx = 0
        context.user_data["cal_route_idx"] = 0

    def current_added() -> set[str]:
        if not user.routes:
            return set()
        return set(user.get_dates(user.routes[route_idx].key()))

    def rebuild(y: int, m: int) -> InlineKeyboardMarkup:
        return build_calendar(y, m, current_added(), selected, user.routes, route_idx)

    if data == "cal_noop":
        return

    elif data == "cal_cancel":
        context.user_data["cal_selected"] = []
        await query.edit_message_text("❌ Закрыто.")

    elif data.startswith("cal_route|"):
        new_idx = int(data.split("|")[1])
        if new_idx < len(user.routes):
            context.user_data["cal_route_idx"] = new_idx
            context.user_data["cal_selected"] = []  # сбрасываем выбор при смене маршрута
            today = date_type.today()
            kb = build_calendar(today.year, today.month,
                                set(user.get_dates(user.routes[new_idx].key())),
                                [], user.routes, new_idx)
            await query.edit_message_reply_markup(reply_markup=kb)

    elif data.startswith("cal_nav|"):
        _, y, m = data.split("|")
        await query.edit_message_reply_markup(reply_markup=rebuild(int(y), int(m)))

    elif data.startswith("cal_select|"):
        ds = data.split("|")[1]
        if ds not in selected:
            selected.append(ds)
        context.user_data["cal_selected"] = selected
        y, m, _ = ds.split("-")
        await query.edit_message_reply_markup(reply_markup=rebuild(int(y), int(m)))

    elif data.startswith("cal_deselect|"):
        ds = data.split("|")[1]
        if ds in selected:
            selected.remove(ds)
        context.user_data["cal_selected"] = selected
        y, m, _ = ds.split("-")
        await query.edit_message_reply_markup(reply_markup=rebuild(int(y), int(m)))

    elif data == "cal_confirm":
        if not user.routes:
            await query.edit_message_text("⚠️ Нет маршрутов.")
            return
        route = user.routes[route_idx]
        new_dates = [d for d in selected if user.add_date(d, route.key())]
        context.user_data["cal_selected"] = []
        await query.edit_message_text(
            f"✅ Добавлено {len(new_dates)} дат для *{route.label()}*. Запускаю проверку...",
            parse_mode=ParseMode.MARKDOWN,
        )
        await _instant_check(query.message, user, route, new_dates)


async def _instant_check(message, user: st.UserState, route: st.Route, dates: list[str]) -> None:
    monitor = TicketMonitor(routes=[{"from_port": route.from_port, "to_port": route.to_port}])
    try:
        tickets = await monitor.check_all(dates)
    except Exception as e:
        logger.error(f"Ошибка проверки: {e}")
        return

    if tickets:
        for t in tickets:
            user.mark_notified(t.trip_id)
        parts = [f"🎫 *Нашёл билеты!*\n"]
        for t in tickets:
            parts.append(format_ticket(t))
        await message.reply_text(
            "\n\n".join(parts),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎫 Купить", url="https://mostanet.ru")]]),
            disable_web_page_preview=True,
        )
    else:
        await message.reply_text(
            f"😔 На выбранные даты билетов пока нет.\n\n"
            f"👁 Слежу за: *{route.label()}*\n"
            f"⏱ Проверяю каждые {CHECK_INTERVAL // 60} мин. — пришлю как появятся.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ══════════════════════════════════════════════════════════════════════════════
# /dates
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = st.get_user(update.effective_chat.id)
    if not user.routes:
        await update.message.reply_text("Маршруты не добавлены.\n\nИспользуй /addroute")
        return

    has_dates = any(user.get_dates(r.key()) for r in user.routes)
    if not has_dates:
        await update.message.reply_text("Даты не добавлены.\n\nИспользуй /adddate")
        return

    buttons = []
    text_lines = ["📅 *Даты по маршрутам:*\n"]

    for r in user.routes:
        dates = user.get_dates(r.key())
        if not dates:
            text_lines.append(f"🚢 *{r.label()}* — нет дат")
            continue
        text_lines.append(f"🚢 *{r.label()}* ({len(dates)} дат):")
        for d in dates[:10]:
            text_lines.append(f"  {d}")
            buttons.append([InlineKeyboardButton(
                f"🗑 {r.from_port[:8]}→{r.to_port[:8]}: {d}",
                callback_data=f"del_date|{r.key()}|{d}"
            )])
        if len(dates) > 10:
            text_lines.append(f"  ...и ещё {len(dates)-10}")
        text_lines.append("")

    await update.message.reply_text(
        "\n".join(text_lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


async def del_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = st.get_user(query.message.chat_id)
    _, route_key, date = query.data.split("|", 2)
    if user.remove_date(date, route_key):
        route = next((r for r in user.routes if r.key() == route_key), None)
        label = route.label() if route else route_key
        await query.edit_message_text(f"🗑 Удалено: *{date}* ({label})", parse_mode=ParseMode.MARKDOWN)
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
    if not user.all_dates():
        await update.message.reply_text("⚠️ Даты не добавлены.\nИспользуй /adddate")
        return

    routes_with_dates = [r for r in user.routes if user.get_dates(r.key())]
    total_dates = sum(len(user.get_dates(r.key())) for r in routes_with_dates)
    msg = await update.message.reply_text(
        f"🔍 Проверяю {len(routes_with_dates)} маршрут(а), {total_dates} дат..."
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
    await msg.edit_text(
        "\n\n".join(parts),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎫 Купить билет", url="https://mostanet.ru")]]),
        disable_web_page_preview=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Очистка
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_clearnotified(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = st.get_user(update.effective_chat.id)
    count = len(user.notified)
    user.clear_notified()
    await update.message.reply_text(f"✅ Кэш сброшен ({count} записей). При следующей проверке уведомлю заново.")


async def cmd_cleardates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = st.get_user(update.effective_chat.id)
    count = user.clear_dates()
    await update.message.reply_text(f"✅ Удалено {count} дат по всем маршрутам.")


async def cmd_clearroutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = st.get_user(update.effective_chat.id)
    count = len(user.routes)
    if not count:
        await update.message.reply_text("Список маршрутов уже пуст.")
        return
    user.routes.clear()
    user.route_dates.clear()
    st.save()
    await update.message.reply_text(f"✅ Удалено {count} маршрутов.")


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.message.reply_text(f"💬 Chat ID: `{chat.id}`", parse_mode=ParseMode.MARKDOWN)


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
            ROUTE_FROM:    [MessageHandler(filters.TEXT & ~filters.COMMAND, route_got_from)],
            ROUTE_TO:      [MessageHandler(filters.TEXT & ~filters.COMMAND, route_got_to)],
            ROUTE_CONFIRM: [CallbackQueryHandler(route_confirm_callback, pattern=r"^route_")],
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
    app.add_handler(CommandHandler("cleardates",    cmd_cleardates))
    app.add_handler(CommandHandler("clearroutes",   cmd_clearroutes))
    app.add_handler(CommandHandler("myid",          cmd_myid))

    app.add_handler(CallbackQueryHandler(del_route_callback, pattern=r"^del_route\|"))
    app.add_handler(CallbackQueryHandler(del_date_callback,  pattern=r"^del_date\|"))
    app.add_handler(CallbackQueryHandler(cal_callback,       pattern=r"^cal_"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern=r"^noop$"))

    start_scheduler(app)
    logger.info("Бот запущен (многопользовательский режим).")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
