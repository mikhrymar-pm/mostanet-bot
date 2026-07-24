"""
Мониторинг билетов через API mostanet.ru

Эндпоинты:
  GET /customer/busstops?name=...&routeTypeProcessingIds=2   — поиск порта по имени
  GET /customer/routesavailable?...                          — доступные рейсы
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://seat-customer-api-prod.mostanet.ru"
FERRY_TYPE = 2       # routeTypeProcessingIds=2 — теплоход

# Постоянный анонимный ID сессии (произвольный UUID)
REQUESTER_ID = "1a42328d-cf0b-4ea8-be1c-4f6a673958dd"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://mostanet.ru/",
    "Origin": "https://mostanet.ru",
}


@dataclass
class Ticket:
    route: str                      # "Курильск порт → Южно-Курильск порт"
    date: str                       # "2026-07-25"
    departure_time: str             # "25.07 14:00"
    arrival_time: str               # "26.07 09:00"
    seats_available: int            # реальный остаток мест
    price: Optional[float]          # минимальная цена
    trip_id: str                    # уникальный ID рейса
    comfort_info: list[str]         # ["Люкс: 3 мест — 9840 руб.", ...]


# ── Кэш ID портов (чтобы не дёргать API при каждой проверке) ─────────────────
_stop_cache: dict[str, str] = {}


async def resolve_stop_id(name: str) -> Optional[str]:
    """
    Ищет порт по имени через /customer/busstops.
    Возвращает ID первого совпадения или None.
    """
    key = name.lower()
    if key in _stop_cache:
        return _stop_cache[key]

    url = f"{API_BASE}/customer/busstops"
    params = {"name": name, "routeTypeProcessingIds": FERRY_TYPE}

    async with httpx.AsyncClient(headers=HEADERS, timeout=10) as client:
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            stops = resp.json()
        except Exception as e:
            logger.error(f"Ошибка поиска порта '{name}': {e}")
            return None

    if not stops:
        logger.warning(f"Порт не найден: '{name}'")
        return None

    stop_id = stops[0]["id"]
    stop_name = stops[0]["name"]
    _stop_cache[key] = stop_id
    logger.info(f"Порт '{name}' → '{stop_name}' ({stop_id})")
    return stop_id


def _fmt_time(iso: Optional[str]) -> str:
    """ISO → 'ДД.ММ ЧЧ:ММ'"""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return iso[:16]


# ── Основной класс ─────────────────────────────────────────────────────────────

class TicketMonitor:

    def __init__(self, routes: list[dict]):
        # routes = [{"from_port": "Курильск", "to_port": "Южно-Курильск"}, ...]
        self.routes = routes

    async def check_route(self, route: dict, date: str) -> list[Ticket]:
        from_port = route["from_port"]
        to_port = route["to_port"]

        from_id = await resolve_stop_id(from_port)
        to_id = await resolve_stop_id(to_port)

        if not from_id:
            logger.error(f"Не найден порт отправления: '{from_port}'")
            return []
        if not to_id:
            logger.error(f"Не найден порт назначения: '{to_port}'")
            return []

        url = f"{API_BASE}/customer/routesavailable"
        params = {
            "requesterId": REQUESTER_ID,
            "busStopDepartureId": from_id,
            "busStopArrivalId": to_id,
            "dateRide": date,
        }

        async with httpx.AsyncClient(headers=HEADERS, timeout=20) as client:
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                items = resp.json()
            except Exception as e:
                logger.error(f"Ошибка запроса рейсов {from_port}→{to_port} [{date}]: {e}")
                return []

        tickets = []
        for item in items:
            # Реальный остаток мест для онлайн-покупки
            seats = item.get("availableSeatAmount", 0) or 0

            # Продажа через сайт остановлена (закрыта за N минут до отправления)
            sale_stopped = item.get("isStopSaleRouteDepartureTimeWeb", False)

            if seats <= 0 or sale_stopped:
                continue

            trip_id = item.get("routeDepartureCalendarDateId", "")
            if not trip_id:
                trip_id = f"{item.get('routeDepartureId', '')}_{date}"

            # Цена и места по категориям из routeComfortTariffs
            comfort_lines = []
            min_price = None
            for cat in item.get("routeComfortTariffs", []):
                cat_seats = cat.get("comfortAvailableSeatAmount", 0) or 0
                if cat_seats <= 0:
                    continue
                price = cat.get("tariffValue")
                name = cat.get("comfortCategoryName", "")
                comfort_lines.append(f"{name}: {cat_seats} мест — {price} руб.")
                if price and (min_price is None or price < min_price):
                    min_price = price

            tickets.append(Ticket(
                route=f"{item.get('stopNameFrom', from_port)} → {item.get('stopNameTo', to_port)}",
                date=date,
                departure_time=_fmt_time(item.get("timeDeparture")),
                arrival_time=_fmt_time(item.get("timeArrival")),
                seats_available=seats,
                price=min_price,
                trip_id=trip_id,
                comfort_info=comfort_lines,
            ))

        return tickets

    async def check_all(self, dates: list[str]) -> list[Ticket]:
        all_tickets: list[Ticket] = []
        for route in self.routes:
            for date in dates:
                try:
                    tickets = await self.check_route(route, date)
                    all_tickets.extend(tickets)
                    logger.info(
                        f"{route['from_port']} → {route['to_port']} "
                        f"[{date}]: найдено {len(tickets)} рейсов"
                    )
                except Exception as e:
                    logger.error(
                        f"Ошибка: {route['from_port']} → {route['to_port']} [{date}]: {e}"
                    )
        return all_tickets
