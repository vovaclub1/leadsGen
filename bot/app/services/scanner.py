"""Сканер каналов-доноров (MTProto через Telethon): ловит рекламные посты и превращает рекламодателей в лиды."""
import asyncio
import logging
import re

from app.config import config
from app.db import db
from app.services.ai import ai
from app.utils import in_minutes, now_iso

log = logging.getLogger(__name__)

PROMO_WORDS = (
    "реклам", "erid", "промокод", "скидк", "подпис", "переход", "розыгрыш", "бонус", "регистр",
    "заказ", "купить", "цена", "бесплатно", "ссылк", "акци", "партнер", "партнёр", "успей", "жми",
    "забирай", "по ссылке", "промо", "депозит", "пополн", "донат", "ключи", "гифт",
)
MENTION_RE = re.compile(r"(?:t\.me/|@)([A-Za-z][A-Za-z0-9_]{3,31})")
URL_RE = re.compile(r"https?://([^\s/]+)")
SKIP = {"joinchat", "addstickers", "share", "proxy", "socks", "iv", "s", "c", "boost"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _worth_looking(text: str) -> bool:
    """Стоит ли тратить запрос зрения: пост без подписи или с намёком на промо."""
    low = text.lower()
    return len(text) < 120 or any(word in low for word in PROMO_WORDS)


def detect_ad(text: str, urls: list[str], donor: str) -> dict | None:
    low = text.lower()
    mentions: list[str] = []
    for source in [text, *urls]:
        for match in MENTION_RE.finditer(source):
            username = match.group(1)
            if username.lower() in SKIP or username.lower() == donor.lower() or username.lower().endswith("bot"):
                continue
            if username not in mentions:
                mentions.append(username)
    sites = [m.group(1).lower().removeprefix("www.") for m in URL_RE.finditer(" ".join([text, *urls])) if "t.me" not in m.group(1) and "telegram.me" not in m.group(1)]
    signals = sum(1 for word in PROMO_WORDS if word in low)
    explicit = "erid" in low or "#реклама" in low or "#промо" in low or "на правах рекламы" in low
    if not mentions and not sites:
        return None
    # Упоминание чужого канала в донорском посте само по себе почти всегда реклама или взаимный пиар —
    # достаточно одного промо-слова. Для внешних сайтов порог выше: ссылки на источники в постах обычны.
    if explicit or signals >= 2 or (mentions and signals >= 1):
        return {"username": mentions[0] if mentions else None, "site": sites[0] if sites else None, "signals": signals, "explicit": explicit}
    return None


class Scanner:
    def __init__(self):
        self.client = None
        self.donors: set[str] = set()
        self._task: asyncio.Task | None = None

    @property
    def configured(self) -> bool:
        return bool(config.tg_api_id and config.tg_api_hash)

    async def start(self) -> None:
        if not self.configured:
            log.info("Сканер выключен: TG_API_ID/TG_API_HASH не заданы")
            return
        if not (config.scanner_session.with_suffix(".session")).exists():
            log.warning("Сканер: сессии нет. Запустите `python login_scanner.py` на сервере.")
            return
        try:
            from telethon import TelegramClient, events

            self.client = TelegramClient(str(config.scanner_session), config.tg_api_id, config.tg_api_hash)
            await self.client.connect()
            if not await self.client.is_user_authorized():
                log.warning("Сканер: аккаунт не авторизован. Запустите `python login_scanner.py`.")
                self.client = None
                return
            await self.reload_donors()
            self.client.add_event_handler(self._on_message, events.NewMessage())
            self._task = asyncio.create_task(self.client.run_until_disconnected())
            me = await self.client.get_me()
            log.info("Сканер запущен от аккаунта %s, доноров: %s", me.username or me.id, len(self.donors))
        except Exception as exc:  # noqa: BLE001
            log.error("Сканер не запустился: %s", exc)
            self.client = None

    async def reload_donors(self) -> None:
        rows = await db.fetchall("SELECT * FROM donors WHERE active = 1")
        self.donors = {row["username"].lower() for row in rows}
        if self.client:
            asyncio.create_task(self._join_pending([row for row in rows if not row["joined"]]))

    async def _join_pending(self, rows: list[dict]) -> None:
        from telethon.errors import FloodWaitError
        from telethon.tl.functions.channels import JoinChannelRequest

        for row in rows:
            try:
                entity = await self.client.get_entity(row["username"])
                await self.client(JoinChannelRequest(entity))
                await db.execute("UPDATE donors SET joined = 1, title = ? WHERE id = ?", (getattr(entity, "title", None), row["id"]))
                log.info("Сканер вступил в @%s", row["username"])
                await asyncio.sleep(8)
            except FloodWaitError as exc:
                log.warning("FloodWait %s сек при вступлении — остальные доноры позже", exc.seconds)
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось вступить в @%s: %s", row["username"], exc)

    async def discover(self, check_limit: int = 12) -> dict:
        """Спрашивает у Telegram, какие каналы похожи на наших доноров, и копит кандидатов.

        Сигнал честный: рекомендации строит сам Telegram по пересечению аудиторий. Канал, который
        советуют сразу несколько наших доноров, почти наверняка крутит ту же рекламу.
        """
        if not self.client:
            return {"ok": False, "error": "Сканер не запущен — кандидатов искать нечем."}
        from telethon.errors import FloodWaitError
        from telethon.tl.functions.channels import GetChannelRecommendationsRequest

        seeds = await db.fetchall("SELECT username FROM donors WHERE active = 1 ORDER BY ads_found DESC LIMIT ?", (check_limit,))
        if not seeds:
            return {"ok": False, "error": "Сначала добавьте хотя бы одного донора."}

        # Каналы, которые уже в работе или которые владелец однажды отклонил, второй раз не предлагаем.
        known = {row["username"].lower() for row in await db.fetchall("SELECT username FROM donors")}
        known |= {row["username"].lower() for row in await db.fetchall("SELECT username FROM donor_hints WHERE status <> 'new'")}

        found: dict[str, dict] = {}
        checked = 0
        for seed in seeds:
            try:
                entity = await self.client.get_entity(seed["username"])
                result = await self.client(GetChannelRecommendationsRequest(channel=entity))
            except FloodWaitError as exc:
                log.warning("FloodWait %s сек при поиске похожих каналов — остановился", exc.seconds)
                break
            except Exception as exc:  # noqa: BLE001
                log.info("Похожие каналы для @%s не получены: %s", seed["username"], exc)
                continue
            checked += 1
            for chat in getattr(result, "chats", []):
                username = getattr(chat, "username", None)
                if not username or username.lower() in known or getattr(chat, "megagroup", False):
                    continue
                item = found.setdefault(
                    username.lower(),
                    {"username": username, "title": getattr(chat, "title", None),
                     "subscribers": getattr(chat, "participants_count", None), "sources": []},
                )
                item["sources"].append(seed["username"])
            await asyncio.sleep(2)

        fresh = 0
        for item in found.values():
            row = await db.fetchone("SELECT id FROM donor_hints WHERE LOWER(username) = ?", (item["username"].lower(),))
            sources = ", ".join(dict.fromkeys(item["sources"]))[:300]
            if row:
                await db.execute(
                    "UPDATE donor_hints SET votes = ?, title = ?, subscribers = ?, sources = ? WHERE id = ?",
                    (len(item["sources"]), item["title"], item["subscribers"], sources, row["id"]),
                )
                continue
            await db.execute(
                "INSERT INTO donor_hints (username, title, subscribers, votes, sources, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'new', ?)",
                (item["username"], item["title"], item["subscribers"], len(item["sources"]), sources, now_iso()),
            )
            fresh += 1
        log.info("Автопоиск доноров: опрошено %s каналов, кандидатов %s, новых %s", checked, len(found), fresh)
        return {"ok": True, "checked": checked, "total": len(found), "fresh": fresh}

    async def search_channels(self, query: str, limit: int = 15) -> list[dict]:
        """ТЗ 4.2 п.1: провайдер №1 — бесплатный глобальный поиск Telegram по названию/описанию.

        В отличие от Trustat Search (провайдер №2, платный и лимитированный по словам/запросам),
        это встроенный поиск самого Telegram — квоты нет, только флуд-контроль аккаунта-сканера.
        """
        if not self.client:
            return []
        from telethon.errors import FloodWaitError
        from telethon.tl.functions.contacts import SearchRequest
        from telethon.tl.types import Channel

        try:
            result = await self.client(SearchRequest(q=query, limit=limit, broadcasts=True))
        except FloodWaitError as exc:
            log.warning("FloodWait %s сек при глобальном поиске «%s» — жду", exc.seconds, query)
            return []
        except Exception as exc:  # noqa: BLE001
            log.info("Глобальный поиск «%s» не удался: %s", query, exc)
            return []

        out: list[dict] = []
        for chat in result.chats:
            username = getattr(chat, "username", None)
            if not username or not isinstance(chat, Channel) or getattr(chat, "megagroup", False):
                continue
            out.append({"username": username, "title": getattr(chat, "title", None)})
        return out

    async def _download_image(self, message) -> bytes | None:
        """Скачивает картинку поста в память. Видео и документы пропускаем — читаем только изображения."""
        media = getattr(message, "photo", None)
        if media is None:
            document = getattr(message, "document", None)
            mime = getattr(document, "mime_type", "") if document else ""
            if not mime.startswith("image/"):
                return None
        try:
            data = await asyncio.wait_for(self.client.download_media(message, file=bytes), timeout=45)
        except Exception as exc:  # noqa: BLE001
            log.info("Картинку поста скачать не удалось: %s", exc)
            return None
        return data if isinstance(data, (bytes, bytearray)) and len(data) <= MAX_IMAGE_BYTES else None

    async def _on_message(self, event) -> None:
        try:
            chat = await event.get_chat()
        except Exception:  # noqa: BLE001
            return
        username = getattr(chat, "username", None)
        if not username or username.lower() not in self.donors:
            return
        message = event.message
        text = message.raw_text or ""
        has_image = bool(getattr(message, "photo", None)) or (
            getattr(getattr(message, "document", None), "mime_type", "") or ""
        ).startswith("image/")
        # Картиночная реклама часто идёт вообще без подписи — такие посты раньше отсеивались по длине.
        if len(text) < 40 and not has_image:
            return
        urls: list[str] = []
        for entity in message.entities or []:
            url = getattr(entity, "url", None)
            if url:
                urls.append(url)
        if message.reply_markup:
            for row in getattr(message.reply_markup, "rows", []):
                for button in row.buttons:
                    url = getattr(button, "url", None)
                    if url:
                        urls.append(url)

        hit = detect_ad(text, urls, username)
        image_note = None
        if has_image and (hit or _worth_looking(text)) and await ai.vision_enabled():
            image = await self._download_image(message)
            if image:
                image_note = await ai.read_image(image, caption=text)
            if image_note:
                log.info("Сканер прочитал креатив @%s: %s", username, image_note[:90])
                # Текст с картинки участвует и в поиске рекламодателя, и во всём дальнейшем анализе.
                text = f"{text}\n[На картинке: {image_note}]".strip()
                if not hit:
                    hit = detect_ad(text, urls, username)
        if not hit:
            return

        confidence = 0.6 if hit["explicit"] else 0.45
        advertiser = hit["username"]
        verdict = None
        if await ai.should_classify_ad(hit["explicit"], hit["signals"]):
            try:
                verdict = await ai.classify_ad(text, username)
            except Exception as exc:  # noqa: BLE001
                log.info("ИИ-классификация рекламы: %s", exc)
        if verdict:
            if verdict.get("is_ad") is False and float(verdict.get("confidence") or 0) >= 0.6:
                return
            confidence = float(verdict.get("confidence") or confidence)
            hinted = verdict.get("advertiser")
            if isinstance(hinted, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", hinted.lstrip("@")):
                advertiser = hinted.lstrip("@")

        from app.services import leads

        ref = None
        if advertiser:
            ref = leads.parse_ref("@" + advertiser)
        if ref is None and hit["site"]:
            ref = leads.parse_ref("https://" + hit["site"])
        if ref is None:
            # Контакт в посте не найден — лид всё равно создаём: карточка с пометкой «нет
            # контакта» уедет в группу, SDR откроет пост по ссылке и найдёт связь вручную.
            # Ключ по посту, а не по донору: каждый такой пост — отдельный неизвестный рекламодатель.
            ref = {
                "kind": "unknown", "username": None,
                "url": f"https://t.me/{username}/{message.id}",
                "entity_key": f"post:{username.lower()}:{message.id}",
            }
        status, lead = await leads.create_lead(
            ref, source="scanner", donor=username, ad_text=text, ad_msg_id=message.id,
            ad_views=getattr(message, "views", None), confidence=confidence, image_note=image_note,
        )
        log.info("Сканер @%s → %s (%s) [%s]", username, ref["entity_key"], status, now_iso())

    async def recheck_views(self) -> None:
        """ТЗ 4.1: реальный охват рекламного поста — просмотры перечитываются через 24 и 48 ч после публикации."""
        if not self.client:
            return
        await self._recheck_window("views_24h", hours=24)
        await self._recheck_window("views_48h", hours=48)

    async def _recheck_window(self, column: str, hours: int) -> None:
        # Джоб идёт раз в ~30 минут — окно в 90 минут гарантирует, что ни один пост не пропустят,
        # даже если предыдущий запуск задержался.
        due = await db.fetchall(
            f"SELECT * FROM ad_posts WHERE {column} IS NULL AND msg_id IS NOT NULL AND donor IS NOT NULL "
            "AND created_at <= ? AND created_at > ?",
            (in_minutes(-hours * 60), in_minutes(-hours * 60 - 90)),
        )
        for post in due:
            try:
                message = await self.client.get_messages(post["donor"], ids=post["msg_id"])
            except Exception as exc:  # noqa: BLE001
                log.info("Перечит охвата @%s/%s: %s", post["donor"], post["msg_id"], exc)
                continue
            views = getattr(message, "views", None) if message else None
            await db.execute(f"UPDATE ad_posts SET {column} = ? WHERE id = ?", (views, post["id"]))


scanner = Scanner()
