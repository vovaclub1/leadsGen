"""Клиент Trustat Public API (https://api-public.trustat.me, префикс /public/v1) по docs.trustat.me.

Ключ в заголовке X-API-Key. Ответ всегда в конверте {"status": "ok", "response": …}.
Коды: 426 — квота исчерпана, 429 — частота (в заголовке Retry-After), 503 — сервис недоступен,
401/403 — ключ недействителен или нет нужного scope, 422 — неверные параметры.

Тарификация идёт по трём осям: запросы, уникальные каналы и результаты списков. Поэтому
`posts/search` стоит 1 запрос без списания каналов, а любой запрос карточки канала — ещё и канал.
Фактический остаток приходит в заголовках X-Quota-*, им и доверяем больше, чем своим счётчикам.
"""
import asyncio
import logging

import httpx

from app.config import config
from app.services import keypool

log = logging.getLogger(__name__)

PREFIX = "/public/v1"


def _parse_quota(value) -> tuple[int, int] | None:
    """«120/500» → (120, 500). Безлимит в API приходит как «120/0» или «120/-».

    Тип не фиксируем: сегодня это строка, но встречались и голые числа — падать из-за формата нельзя.
    """
    if value is None:
        return None
    value = str(value)
    if "/" not in value:
        return None
    used, _, limit = value.partition("/")
    try:
        return int(used.strip()), int(limit.strip())
    except ValueError:
        try:
            return int(used.strip()), 0
        except ValueError:
            return None


async def _get(path: str, params: dict | None, raw_key: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30) as client:
        return await client.get(config.trustat_base + PREFIX + path, params=params, headers={"X-API-Key": raw_key})


async def request(scope: str, path: str, params: dict | None = None, priority: bool = False, key_id: int | None = None):
    """Возвращает (response_payload | None, error_code | None). Прозрачно переключает ключи при 426/401/403."""
    tried: set[int] = set()
    last_error = "no_key"
    for _ in range(6):
        key = await keypool.pick(scope, priority=priority, key_id=key_id, exclude=tried)
        if key is None:
            return None, last_error
        tried.add(key["id"])
        try:
            response = await _get(path, params, keypool.secret(key))
        except httpx.HTTPError as exc:
            last_error = f"network: {exc}"
            log.warning("Trustat сеть: %s", exc)
            continue

        status = response.status_code
        if status == 200:
            await keypool.mark_used(key["id"])
            # API знает точный остаток, наши счётчики — лишь приблизка между ответами.
            quota = _parse_quota(response.headers.get("X-Quota-Requests"))
            if quota:
                await keypool.sync_quota(key["id"], quota[0], quota[1])
            try:
                return response.json().get("response"), None
            except ValueError:
                return None, "bad_json"
        if status == 429:
            # Сервер прямо говорит, сколько ждать. Ключ не виноват, из ротации не выводим.
            tried.discard(key["id"])
            try:
                pause = min(float(response.headers.get("Retry-After") or 1), 10)
            except ValueError:
                pause = 1.0
            await asyncio.sleep(pause)
            last_error = "rate"
            continue
        if status == 503:
            tried.discard(key["id"])
            await asyncio.sleep(2)
            last_error = "unavailable"
            log.info("Trustat временно недоступен (503), пробую снова")
            continue
        if status == 426:
            await keypool.mark_exhausted(key["id"])
            last_error = "quota"
            log.info("Trustat ключ #%s исчерпан, переключаюсь", key["id"])
            if key_id is not None:
                return None, last_error
            continue
        if status in (401, 403):
            await keypool.mark_banned(key["id"], f"HTTP {status}: {response.text[:120]}")
            last_error = "banned"
            if key_id is not None:
                return None, last_error
            continue
        if status == 404:
            await keypool.mark_used(key["id"])
            return None, "not_found"
        last_error = f"http_{status}"
        log.warning("Trustat %s → %s %s", path, status, response.text[:200])
        return None, last_error
    return None, last_error


async def _resolve(ref: str, priority: bool) -> tuple[str | None, str | None]:
    """Приводит любой идентификатор к тому, что понимают одиночные ручки каналов.

    Ссылки со слэшами и приватные +hash в пути /channels/{channel} не принимаются — их сначала
    прогоняем через /lookup. Обычный username отдаём как есть, чтобы не платить за лишний запрос.
    """
    ref = ref.strip().lstrip("@")
    if "/" not in ref and not ref.startswith("+"):
        return ref, None
    payload, error = await lookup(ref, priority=priority)
    if not payload:
        return None, error
    results = payload.get("results") or []
    channel_id = results[0].get("channel_id") if results else None
    return (str(channel_id) if channel_id else None), None if channel_id else "not_found"


async def channel_info(ref: str, priority: bool = False):
    resolved, error = await _resolve(ref, priority)
    if not resolved:
        return None, error
    return await request("stat", f"/channels/{resolved}", priority=priority)


async def channel_stat(ref: str, priority: bool = False):
    resolved, error = await _resolve(ref, priority)
    if not resolved:
        return None, error
    return await request("stat", f"/channels/{resolved}/stat", priority=priority)


async def lookup(ref: str, priority: bool = False):
    """Ссылки со слэшами (t.me/name/123, приватные +hash) одиночные ручки не принимают — только /lookup."""
    return await request("stat", "/lookup", params={"ref": ref}, priority=priority)


async def channels_batch(channel_ids: list[int], priority: bool = False):
    """До 100 каналов за один запрос. Стоит 1 запрос + N уникальных каналов — сильно дешевле поштучных карточек."""
    if not channel_ids:
        return {"channels": []}, None
    ids = ",".join(str(cid) for cid in list(dict.fromkeys(channel_ids))[:100])
    return await request("stat", "/channels/batch", params={"ids": ids}, priority=priority)


async def posts_search(query: str, since_ts: int, key_id: int | None = None, limit: int = 50, cursor: str | None = None):
    """Поиск публикаций. Тарифицируется как 1 запрос, уникальные каналы не списываются."""
    params = {
        "q": query,
        "since_ts": since_ts,
        "limit": min(limit, 100),  # потолок страницы в API — 100
        "hide_forwards": "true",
        "peer_type": "all",  # спрос на рекламу чаще пишут в чатах-бар��холках, а не в каналах
        "sort": "date",
        "order": "desc",
    }
    if cursor:
        params["cursor"] = cursor
    return await request("search", "/posts/search", params=params, priority=True, key_id=key_id)


async def probe_key(raw_key: str) -> tuple[dict | None, str | None]:
    """Проверка ключа при добавлении. /usage/info не тарифицируется, дёргать можно свободно.

    Отдаёт поля API как есть плюс разобранные: месячный лимит запросов и наши пакеты под scopes ключа.
    """
    try:
        response = await _get("/usage/info", None, raw_key)
    except httpx.HTTPError as exc:
        return None, f"сеть: {exc}"
    if response.status_code == 200:
        try:
            info = response.json().get("response") or {}
        except ValueError:
            return None, "некорректный JSON"
        # spent_requests приходит строкой «использовано/лимит», а не числом.
        quota = _parse_quota(info.get("spent_requests"))
        if quota:
            info["used_month"], info["limit_month"] = quota
        scopes = {str(s).lower() for s in (info.get("scopes") or [])}
        packages = []
        if scopes & {"channels", "stats"}:
            packages.append("stat")
        if "posts" in scopes:
            packages.append("search")
        info["packages"] = ",".join(packages)
        return info, None
    if response.status_code in (401, 403):
        return None, "ключ не принят (401/403)"
    if response.status_code == 429:
        return None, "слишком часто (429), повторите через минуту"
    return None, f"HTTP {response.status_code}"


def format_usage(info: dict) -> str:
    """Человеческий вид /usage/info: три оси тарификации, частота и что ключу разрешено."""
    names = {
        "spent_requests": "запросы",
        "spent_unique_channels": "уникальные каналы",
        "spent_listing": "результаты списков",
    }
    lines = [f"Тариф: {info.get('plan') or '—'} · период {info.get('period') or '—'}"]
    for field, title in names.items():
        value = info.get(field)
        if not value:
            continue
        quota = _parse_quota(value)
        if quota and quota[1] > 0:
            left = max(quota[1] - quota[0], 0)
            lines.append(f"{title}: {quota[0]} из {quota[1]} (осталось {left})")
        else:
            lines.append(f"{title}: {value}")
    if info.get("rps"):
        lines.append(f"Частота: до {info['rps']} запросов в секунду")
    scopes = ", ".join(str(s) for s in (info.get("scopes") or [])) or "—"
    packages = {"stat": "Stat", "search": "Search"}
    detected = ", ".join(packages[p] for p in info.get("packages", "").split(",") if p in packages)
    lines.append(f"Разрешено: {scopes}" + (f" → пакеты {detected}" if detected else ""))
    return "\n".join(lines)


def format_stat(stat: dict | None) -> str:
    if not stat:
        return "Trustat: нет данных"
    parts = []
    if stat.get("participants") is not None:
        parts.append(f"подписчики {stat['participants']:,}".replace(",", " "))
    if stat.get("views24") is not None:
        parts.append(f"просм/24ч {stat['views24']:,}".replace(",", " "))
    if stat.get("er24") is not None:
        parts.append(f"ER24 {stat['er24']}%")
    if stat.get("month_growth") is not None:
        sign = "+" if stat["month_growth"] >= 0 else ""
        parts.append(f"рост за месяц {sign}{stat['month_growth']:,}".replace(",", " "))
    if stat.get("quality_score") is not None:
        parts.append(f"качество {stat['quality_score']}")
    return "Trustat: " + " · ".join(parts) if parts else "Trustat: пусто"
