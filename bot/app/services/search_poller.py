"""Поиск спроса по ключевым словам через Trustat Search API: «ищу каналы для рекламы», «куплю рекламу» и т.п."""
import logging
import re
from datetime import timedelta

from app.db import db
from app.services import trustat
from app.utils import now_iso, now_utc, parse_iso

log = logging.getLogger(__name__)

LINK_RE = re.compile(r"t\.me/(?!c/|joinchat|\+)([A-Za-z][A-Za-z0-9_]{3,31})", re.I)
RUN_EVERY = timedelta(days=2)


async def run_due() -> int:
    rows = await db.fetchall(
        "SELECT k.*, a.status AS key_status FROM keywords k JOIN api_keys a ON a.id = k.api_key_id "
        "WHERE k.active = 1 AND a.status = 'active'"
    )
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
    payload, error = await trustat.posts_search(row["word"], int(since.timestamp()), key_id=row["api_key_id"])
    if payload is None:
        log.info("Search «%s»: %s", row["word"], error)
        if error in ("quota", "banned"):
            await db.execute("UPDATE keywords SET last_run = ? WHERE id = ?", (now_iso(), row["id"]))
        return 0

    posts = payload.get("posts") or []
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
    Если батч недоступен (нет ключа с пакетом Stat или кончилась квота), вытаскиваем ссылки из текста.
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
            for username in LINK_RE.findall(post.get("text") or ""):
                if username.lower() in seen:
                    continue
                seen.add(username.lower())
                out.append((username, post.get("text")))
    return out
