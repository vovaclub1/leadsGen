"""Поиск спроса по ключевым словам через Trustat Search API: «ищу каналы для рекламы», «куплю рекламу» и т.п."""
import logging
import re
from datetime import timedelta

from app.db import db
from app.services import trustat
from app.utils import now_iso, now_utc, parse_iso

log = logging.getLogger(__name__)

LINK_RE = re.compile(r"t\.me/(?!c/|joinchat|\+)([A-Za-z][A-Za-z0-9_]{3,31})", re.I)
# Контакты в постах спроса чаще пишут голым @username, а не ссылкой t.me.
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{4,31})")
# ТЗ 4.2: «Глубина 3 дня → опрос каждого слова раз в 2 дня, ничего не теряется» — тем же
# 100 запросов/мес на бесплатном ключе хватает без риска сжечь квоту раньше конца месяца.
# Плюс страницы выдачи — без них популярное слово теряло всё, что не влезло в первые 50 постов.
RUN_EVERY = timedelta(days=2)
PAGE_LIMIT = 3


async def run_due() -> int:
    rows = await db.fetchall("SELECT * FROM keywords WHERE active = 1")
    created = 0
    for row in rows:
        last_run = parse_iso(row["last_run"])
        if last_run and now_utc() - last_run < RUN_EVERY:
            continue
        created += await run_keyword(row)
    return created


async def run_keyword(row: dict) -> int:
    from app.services import leads

    since = parse_iso(row["last_run"]) or (now_utc() - timedelta(days=3))
    posts: list[dict] = []
    cursor: str | None = None
    for _ in range(PAGE_LIMIT):
        payload, error = await trustat.posts_search(row["word"], int(since.timestamp()), cursor=cursor)
        if payload is None:
            log.info("Search «%s»: %s", row["word"], error)
            # last_run не двигаем: через полчаса попробуем снова — уже другим ключом из пула.
            return 0
        posts.extend(payload.get("posts") or [])
        cursor = payload.get("next_cursor")
        if not cursor:
            break

    created = 0
    for username, text in await _authors(posts):
        ref = leads.parse_ref("@" + username)
        if not ref:
            continue
        status, _ = await leads.create_lead(ref, source="search", keyword=row["word"], ad_text=text)
        if status == "created":
            created += 1
    await db.execute("UPDATE keywords SET last_run = ?, found = found + ? WHERE id = ?", (now_iso(), created, row["id"]))
    log.info("Search «%s»: постов %s, новых лидов %s", row["word"], len(posts), created)
    return created


async def _authors(posts: list[dict]) -> list[tuple[str, str | None]]:
    """Превращает найденные посты в пары (username автора, текст поста).

    Тот, кто написал «ищу каналы для рекламы», и есть наш лид. В выдаче поиска приходит только
    числовой channel_id, поэтому имена берём одним батч-запросом на все каналы сразу, а не поштучно.
    Если батч недоступен (нет ключа с пакетом Stat или кончилась квота), вытаскиваем контакт из
    текста поста: сначала ссылки t.me, затем голые @упоминания.
    """
    texts: dict[int, str | None] = {}
    for post in posts:
        channel_id = post.get("channel_id")
        if channel_id is not None:
            texts.setdefault(int(channel_id), post.get("text"))

    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    if texts:
        batch, error = await trustat.channels_batch(list(texts))
        if batch:
            for channel in batch.get("channels") or []:
                username = channel.get("username")
                if not username or username.lower() in seen:
                    continue
                seen.add(username.lower())
                out.append((username, texts.get(channel.get("channel_id"))))
        else:
            log.info("Батч каналов недоступен (%s), беру ссылки из текста постов", error)

    if not out:
        for post in posts:
            text = post.get("text") or ""
            # Сначала явные ссылки t.me, затем голые @упоминания — контакт рекламодателя.
            for username in LINK_RE.findall(text) + MENTION_RE.findall(text):
                if username.lower() in seen:
                    continue
                seen.add(username.lower())
                out.append((username, text))
    return out
