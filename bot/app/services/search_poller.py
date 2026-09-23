"""Поиск спроса по ключевым словам через Trustat Search API: «ищу каналы для рекламы», «куплю рекламу» и т.п."""
import asyncio
import logging
import re
from datetime import timedelta

from app import statuses
from app.db import db
from app.services import trustat
from app.utils import now_iso, now_utc, parse_iso

log = logging.getLogger(__name__)

LINK_RE = re.compile(r"t\.me/(?!c/|joinchat|\+)([A-Za-z][A-Za-z0-9_]{3,31})", re.I)
# Контакты в постах спроса чаще пишут голым @username, а не ссылкой t.me.
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{4,31})")
# ТЗ 4.2: «Глубина 3 дня → опрос каждого слова раз в 2 дня, ничего не теряется» — база для активных слов,
# тем же 100 запросов/мес на бесплатном ключе хватает без риска сжечь квоту раньше конца месяца.
# Плюс страницы выдачи — без них популярное слово теряло всё, что не влезло в первые 50 постов.
#
# Адаптивный шаг: слово, у которого подряд несколько заходов вообще не нашли ни одного поста
# (спрос по нему сейчас не пишут), опрашивается всё реже — освобождает квоту для слов, которые
# реально приносят лиды. Как только по слову снова хоть что-то нашлось — счётчик сбрасывается
# и слово возвращается к базовому шагу в 2 дня.
#
# Спрос-триггер: график выше не главный. Если в очереди не осталось ни одного открытого лида
# именно от поиска (только от донор-сканера), это сигнал «поиск исчерпался как источник» — все
# активные слова опрашиваются немедленно, вне графика, один раз, а дальше снова по расписанию,
# пока очередь не опустеет заново (см. search_stack_empty() и run_due()).
BACKOFF_STEPS = (timedelta(days=2), timedelta(days=4), timedelta(days=7), timedelta(days=14))
PAGE_LIMIT = 3


def interval_for(empty_streak: int) -> timedelta:
    """Публичная — используется и в run_due(), и в админке для показа текущего шага опроса."""
    return BACKOFF_STEPS[min(empty_streak, len(BACKOFF_STEPS) - 1)]


async def search_stack_empty() -> bool:
    """Есть ли сейчас хоть один открытый лид именно от поиска (не от донор-сканера)?

    Публичная — используется run_due() для решения «опросить прямо сейчас» и админкой
    для показа причины внеочередного опроса.
    """
    placeholders = ",".join("?" for _ in statuses.CLOSED)
    count = await db.scalar(
        f"SELECT COUNT(*) FROM leads WHERE source = 'search' AND status NOT IN ({placeholders})",
        statuses.CLOSED,
    )
    return not count


async def run_due() -> int:
    rows = await db.fetchall("SELECT * FROM keywords WHERE active = 1")
    stack_empty = await search_stack_empty()
    if stack_empty:
        log.info("Очередь лидов от поиска пуста (остались только доноры) — опрашиваю все слова вне графика")
    else:
        # Очередь снова живая — сбрасываем флаг «уже опрошено внеочередно», чтобы следующее
        # опустошение очереди снова вызвало немедленный опрос, а не ждало обычный шаг.
        await db.execute("UPDATE keywords SET force_poll_used = 0 WHERE force_poll_used = 1")

    created = 0
    for row in rows:
        last_run = parse_iso(row["last_run"])
        on_schedule = last_run and now_utc() - last_run < interval_for(row["empty_streak"] or 0)
        forced = stack_empty and not row["force_poll_used"]
        if on_schedule and not forced:
            continue
        created += await run_keyword(row)
        if forced:
            await db.execute("UPDATE keywords SET force_poll_used = 1 WHERE id = ?", (row["id"],))
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

    # Ноль постов за всё окно — слово сейчас «молчит», не только эта конкретная волна дубликатов.
    empty_streak = 0 if posts else (row["empty_streak"] or 0) + 1
    await db.execute(
        "UPDATE keywords SET last_run = ?, found = found + ?, empty_streak = ? WHERE id = ?",
        (now_iso(), created, empty_streak, row["id"]),
    )
    next_in = interval_for(empty_streak).days
    log.info(
        "Search «%s»: постов %s, новых лидов %s, тишина подряд %s заходов, следующий через %s дн.",
        row["word"], len(posts), created, empty_streak, next_in,
    )
    return created


# ТЗ 4.2 п.1: провайдер №1 — бесплатный MTProto, лимитирован только флудом аккаунта, не квотой ключа.
# Гоняем его чаще, чем платный Trustat, но не на каждый тик джоба — бережём сканер.
MTPROTO_RUN_EVERY = timedelta(hours=6)


async def run_due_mtproto() -> int:
    from app.services.scanner import scanner

    if not scanner.client:
        return 0
    rows = await db.fetchall("SELECT * FROM keywords WHERE active = 1")
    created = 0
    for row in rows:
        last_run = parse_iso(row.get("mtproto_last_run"))
        if last_run and now_utc() - last_run < MTPROTO_RUN_EVERY:
            continue
        created += await _run_keyword_mtproto(row)
        await asyncio.sleep(2)  # пауза между словами — та же дисциплина лимитов, что и у сканера
    return created


async def _run_keyword_mtproto(row: dict) -> int:
    from app.services import leads
    from app.services.scanner import scanner

    channels = await scanner.search_channels(row["word"])
    created = 0
    for item in channels:
        ref = leads.parse_ref("@" + item["username"])
        if not ref:
            continue
        status, _ = await leads.create_lead(ref, source="search", keyword=row["word"])
        if status == "created":
            created += 1
    await db.execute(
        "UPDATE keywords SET mtproto_last_run = ?, mtproto_found = mtproto_found + ? WHERE id = ?",
        (now_iso(), created, row["id"]),
    )
    log.info("MTProto-поиск «%s»: каналов %s, новых лидов %s", row["word"], len(channels), created)
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
            # Сначала явные ссылки t.me, затем голые @упоминания — контакт рекл��модателя.
            for username in LINK_RE.findall(text) + MENTION_RE.findall(text):
                if username.lower() in seen:
                    continue
                seen.add(username.lower())
                out.append((username, text))
    return out
