"""Обогащение лида: описание, подписчики, просмотры, контакт. Сначала MTProto (если сканер залогинен), иначе Bot API."""
import logging
import re

from aiogram.exceptions import TelegramAPIError

from app import runtime
from app.utils import trunc

log = logging.getLogger(__name__)

MENTION_RE = re.compile(r"(?:t\.me/|@)([A-Za-z][A-Za-z0-9_]{3,31})")
CONTACT_HINT_RE = re.compile(r"реклам|сотрудн|менеджер|manager|\bads?\b|\bpr\b|связ|contact|вопрос|предлож|админ|admin|owner|владел", re.I)
SKIP = {"joinchat", "addstickers", "share", "proxy", "socks", "iv", "s", "c"}


def extract_contact(about: str | None, own_username: str | None) -> str | None:
    if not about:
        return None
    candidates: list[tuple[int, str]] = []
    for line in about.splitlines():
        hinted = 2 if CONTACT_HINT_RE.search(line) else 1
        for match in MENTION_RE.finditer(line):
            username = match.group(1)
            if username.lower() in SKIP or (own_username and username.lower() == own_username.lower()):
                continue
            if username.lower().endswith("bot"):
                continue
            candidates.append((hinted, username))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


async def enrich(lead: dict) -> dict:
    username = lead.get("username")
    if not username or lead.get("kind") == "site":
        return {}
    data = await _via_telethon(username)
    if data is None:
        data = await _via_bot_api(username)
    if not data:
        return {}
    if data.get("kind") != "user":
        data["contact_username"] = extract_contact(data.get("about"), username)
    return data


async def _via_telethon(username: str) -> dict | None:
    from app.services.scanner import scanner  # локальный импорт: сканер сам импортирует leads

    client = scanner.client
    if client is None:
        return None
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.types import Channel, User

        entity = await client.get_entity(username)
        if isinstance(entity, Channel):
            full = await client(GetFullChannelRequest(entity))
            messages = await client.get_messages(entity, limit=15)
            views = [m.views for m in messages if getattr(m, "views", None)]
            texts = [trunc(m.text, 300) for m in messages if getattr(m, "text", None)][:4]
            return {
                "title": entity.title,
                "about": full.full_chat.about,
                "subscribers": full.full_chat.participants_count,
                "avg_views": int(sum(views) / len(views)) if views else None,
                "recent_posts": "\n---\n".join(texts) or None,
                "kind": "channel" if entity.broadcast else "chat",
            }
        if isinstance(entity, User):
            return {
                "title": " ".join(filter(None, [entity.first_name, entity.last_name])) or username,
                "kind": "user",
                "contact_user_id": entity.id,
                "contact_username": username,
            }
    except Exception as exc:  # noqa: BLE001 — любая ошибка MTProto не должна ломать создание лида
        log.info("MTProto обогащение @%s не удалось: %s", username, exc)
    return None


async def _via_bot_api(username: str) -> dict | None:
    bot = runtime.bot
    if bot is None:
        return None
    try:
        chat = await bot.get_chat(f"@{username}")
    except TelegramAPIError as exc:
        log.info("Bot API get_chat @%s: %s", username, exc)
        return None
    if chat.type == "channel":
        kind = "channel"
    elif chat.type in ("group", "supergroup"):
        kind = "chat"
    else:
        kind = "user"
    data: dict = {
        "title": chat.title or " ".join(filter(None, [getattr(chat, "first_name", None), getattr(chat, "last_name", None)])) or username,
        "about": getattr(chat, "description", None) or getattr(chat, "bio", None),
        "kind": kind,
    }
    if kind == "user":
        data["contact_username"] = username
        data["contact_user_id"] = chat.id
    else:
        try:
            data["subscribers"] = await bot.get_chat_member_count(chat.id)
        except TelegramAPIError:
            pass
    return data
