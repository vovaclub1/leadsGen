import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import config
from app.db import db
from app.utils import now_iso

log = logging.getLogger(__name__)

ROLE_RU = {"owner": "Владелец", "senior": "Старший менеджер", "sdr": "Младший менеджер (SDR)", "buyer": "Байер"}


class AccessMiddleware(BaseMiddleware):
    """Белый список: в бот попадают только сотрудники из таблицы users. Первый вход OWNER_ID создаёт владельца."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return None

        me = await db.fetchone("SELECT * FROM users WHERE id = ?", (tg_user.id,))
        if me is None and tg_user.id == config.owner_id:
            await db.execute(
                "INSERT INTO users (id, username, full_name, role, status, added_by, created_at, last_seen) "
                "VALUES (?, ?, ?, 'owner', 'active', ?, ?, ?)",
                (tg_user.id, tg_user.username, tg_user.full_name, tg_user.id, now_iso(), now_iso()),
            )
            me = await db.fetchone("SELECT * FROM users WHERE id = ?", (tg_user.id,))
            log.info("Создан владелец %s", tg_user.id)

        if me is None:
            await self._deny(event, tg_user.id, "Нет доступа.")
            return None
        if me["status"] != "active":
            await self._deny(event, tg_user.id, "Доступ приостановлен. Обратитесь к владельцу.")
            return None

        if me["username"] != tg_user.username or me["full_name"] != tg_user.full_name:
            await db.execute(
                "UPDATE users SET username = ?, full_name = ?, last_seen = ? WHERE id = ?",
                (tg_user.username, tg_user.full_name, now_iso(), tg_user.id),
            )
            me["username"], me["full_name"] = tg_user.username, tg_user.full_name
        else:
            await db.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now_iso(), tg_user.id))

        data["me"] = me
        return await handler(event, data)

    @staticmethod
    async def _deny(event: TelegramObject, user_id: int, reason: str) -> None:
        # В группах чужим не отвечаем вовсе — иначе бот превратится в спамера.
        if isinstance(event, Message) and event.chat.type == "private":
            await event.answer(
                f"{reason}\n\nЭто внутренний бот агентства MORIER.\n"
                f"Ваш Telegram ID: <code>{user_id}</code> — передайте его владельцу, чтобы вас добавили."
            )
        elif isinstance(event, CallbackQuery):
            await event.answer(reason, show_alert=True)
