"""
Хранилище состояния — каждый пользователь имеет свои маршруты, даты и кэш уведомлений.
Сохраняется в state.json.
"""

import json
import os
from dataclasses import dataclass, field

_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
STATE_FILE = os.path.join(_DATA_DIR, "state.json")


@dataclass
class Route:
    from_port: str
    to_port: str

    def label(self) -> str:
        return f"{self.from_port} → {self.to_port}"

    def key(self) -> str:
        return f"{self.from_port}|{self.to_port}"


@dataclass
class UserState:
    chat_id: int
    routes: list[Route] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
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
            save()
            return r
        return None

    # ── Даты ──────────────────────────────────────────────────────────────────

    def add_date(self, date: str) -> bool:
        if date in self.dates:
            return False
        self.dates.append(date)
        self.dates.sort()
        save()
        return True

    def remove_date(self, date: str) -> bool:
        if date in self.dates:
            self.dates.remove(date)
            save()
            return True
        return False

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
            "dates": self.dates,
            "notified": self.notified,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserState":
        return cls(
            chat_id=data["chat_id"],
            routes=[Route(**r) for r in data.get("routes", [])],
            dates=data.get("dates", []),
            notified=data.get("notified", []),
        )


# ── Глобальное хранилище всех пользователей ───────────────────────────────────

_users: dict[int, UserState] = {}


def get_user(chat_id: int) -> UserState:
    """Возвращает состояние пользователя, создаёт если нет."""
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
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"users": {str(cid): u.to_dict() for cid, u in _users.items()}},
            f, ensure_ascii=False, indent=2,
        )


# Загружаем при импорте
load()
