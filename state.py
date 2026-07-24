"""
Хранилище состояния — каждый пользователь имеет свои маршруты.
Даты хранятся отдельно для каждого маршрута.
Сохраняется в state.json.
"""

import json
import os
from dataclasses import dataclass, field

_DATA_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(__file__)
STATE_FILE = os.path.join(_DATA_DIR, "state.json")


@dataclass
class Route:
    from_port: str
    to_port: str

    def label(self) -> str:
        return f"{self.from_port} → {self.to_port}"

    def key(self) -> str:
        return f"{self.from_port}||{self.to_port}"


@dataclass
class UserState:
    chat_id: int
    routes: list[Route] = field(default_factory=list)
    route_dates: dict[str, list[str]] = field(default_factory=dict)  # route.key() → [dates]
    notified: list[str] = field(default_factory=list)

    # ── Маршруты ──────────────────────────────────────────────────────────────

    def add_route(self, from_port: str, to_port: str) -> bool:
        r = Route(from_port.strip(), to_port.strip())
        if any(x.key() == r.key() for x in self.routes):
            return False
        self.routes.append(r)
        save()
        return True

    def remove_route(self, index: int) -> Route | None:
        if 0 <= index < len(self.routes):
            r = self.routes.pop(index)
            self.route_dates.pop(r.key(), None)
            save()
            return r
        return None

    # ── Даты (per-route) ──────────────────────────────────────────────────────

    def get_dates(self, route_key: str) -> list[str]:
        return list(self.route_dates.get(route_key, []))

    def all_dates(self) -> list[str]:
        """Все уникальные даты по всем маршрутам."""
        return sorted(set(d for dl in self.route_dates.values() for d in dl))

    def add_date(self, date: str, route_key: str) -> bool:
        dates = self.route_dates.setdefault(route_key, [])
        if date in dates:
            return False
        dates.append(date)
        dates.sort()
        save()
        return True

    def remove_date(self, date: str, route_key: str) -> bool:
        dates = self.route_dates.get(route_key, [])
        if date in dates:
            dates.remove(date)
            save()
            return True
        return False

    def clear_dates(self, route_key: str | None = None) -> int:
        """Очищает даты — для конкретного маршрута или всех."""
        if route_key:
            count = len(self.route_dates.get(route_key, []))
            self.route_dates.pop(route_key, None)
        else:
            count = sum(len(v) for v in self.route_dates.values())
            self.route_dates.clear()
        save()
        return count

    # ── Кэш уведомлений ───────────────────────────────────────────────────────

    def is_notified(self, trip_id: str) -> bool:
        return trip_id in self.notified

    def mark_notified(self, trip_id: str) -> None:
        if trip_id not in self.notified:
            self.notified.append(trip_id)
            save()

    def clear_notified(self) -> None:
        self.notified.clear()
        save()

    # ── Сериализация ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "routes": [{"from_port": r.from_port, "to_port": r.to_port} for r in self.routes],
            "route_dates": self.route_dates,
            "notified": self.notified,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserState":
        state = cls(
            chat_id=data["chat_id"],
            routes=[Route(**r) for r in data.get("routes", [])],
            route_dates=data.get("route_dates", {}),
            notified=data.get("notified", []),
        )
        # Миграция старого формата (глобальные dates → per-route)
        old_dates = data.get("dates", [])
        if old_dates and not state.route_dates and state.routes:
            for r in state.routes:
                state.route_dates[r.key()] = list(old_dates)
        return state


# ── Глобальное хранилище ──────────────────────────────────────────────────────

_users: dict[int, UserState] = {}


def get_user(chat_id: int) -> UserState:
    if chat_id not in _users:
        _users[chat_id] = UserState(chat_id=chat_id)
        save()
    return _users[chat_id]


def all_users() -> list[UserState]:
    return list(_users.values())


def load() -> None:
    global _users
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _users = {
            int(cid): UserState.from_dict(u)
            for cid, u in data.get("users", {}).items()
        }
    except Exception:
        pass


def save() -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"users": {str(cid): u.to_dict() for cid, u in _users.items()}},
            f, ensure_ascii=False, indent=2,
        )


load()
