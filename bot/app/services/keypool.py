"""Пул API-ключей: несколько бесплатных/платных ключей Trustat, маршрутизация по остатку квоты, резерв под приоритетные вызовы."""
import logging

from app import security
from app.db import db
from app.utils import day_key, now_iso, season

log = logging.getLogger(__name__)

RESERVE_SHARE = 0.20
FREE_LIMITS = {"stat": (10, 50), "search": (0, 100)}


def scope_of(key: dict, scope: str) -> bool:
    scopes = (key.get("scopes") or "").lower()
    return scope in scopes


def _remaining(key: dict) -> tuple[int, int]:
    day_left = key["limit_day"] - key["used_day"] if key["limit_day"] > 0 else 10**9
    month_left = key["limit_month"] - key["used_month"] if key["limit_month"] > 0 else 10**9
    return day_left, month_left


async def roll_periods() -> None:
    today, month = day_key(), season()
    await db.execute("UPDATE api_keys SET used_day = 0, day_key = ? WHERE day_key IS NULL OR day_key != ?", (today, today))
    await db.execute(
        "UPDATE api_keys SET used_month = 0, month_key = ?, status = CASE WHEN status = 'exhausted' THEN 'active' ELSE status END "
        "WHERE month_key IS NULL OR month_key != ?",
        (month, month),
    )
    await db.execute(
        "UPDATE api_keys SET status = 'active' WHERE status = 'exhausted' AND limit_day > 0 AND used_day < limit_day "
        "AND (limit_month = 0 OR used_month < limit_month)"
    )


async def add_key(raw_key: str, label: str, scopes: str, limit_day: int, limit_month: int, plan: str | None) -> int:
    return await db.insert(
        "INSERT INTO api_keys (provider, key_enc, key_tail, label, scopes, limit_day, limit_month, day_key, month_key, status, plan, created_at) "
        "VALUES ('trustat', ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (security.encrypt(raw_key), raw_key[-4:], label, scopes, limit_day, limit_month, day_key(), season(), plan, now_iso()),
    )


async def list_keys() -> list[dict]:
    await roll_periods()
    return await db.fetchall("SELECT * FROM api_keys ORDER BY status = 'active' DESC, id")


async def get_key(key_id: int) -> dict | None:
    return await db.fetchone("SELECT * FROM api_keys WHERE id = ?", (key_id,))


def secret(key: dict) -> str:
    return security.decrypt(key["key_enc"])


async def pick(scope: str, priority: bool = False, key_id: int | None = None, exclude: set[int] | None = None) -> dict | None:
    await roll_periods()
    exclude = exclude or set()
    keys = [k for k in await db.fetchall("SELECT * FROM api_keys WHERE status = 'active'") if scope_of(k, scope) and k["id"] not in exclude]
    if key_id is not None:
        keys = [k for k in keys if k["id"] == key_id]

    usable = []
    for key in keys:
        day_left, month_left = _remaining(key)
        if day_left <= 0 or month_left <= 0:
            await db.execute("UPDATE api_keys SET status = 'exhausted' WHERE id = ?", (key["id"],))
            continue
        usable.append((month_left, day_left, key))
    if not usable:
        return None

    if not priority and key_id is None:
        total_limit = sum(k["limit_month"] for _, _, k in usable if k["limit_month"] > 0)
        total_left = sum(m for m, _, k in usable if k["limit_month"] > 0)
        if total_limit > 0 and total_left <= total_limit * RESERVE_SHARE:
            log.info("Пул ключей (%s) в резерве: осталось %s из %s", scope, total_left, total_limit)
            return None

    usable.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return usable[0][2]


async def mark_used(key_id: int) -> None:
    await db.execute("UPDATE api_keys SET used_day = used_day + 1, used_month = used_month + 1, last_error = NULL WHERE id = ?", (key_id,))


async def sync_quota(key_id: int, used_month: int, limit_month: int) -> None:
    """Подтягивает реальный расход из заголовков ответа: API считает точнее наших локальных счётчиков."""
    if limit_month > 0:
        await db.execute(
            "UPDATE api_keys SET used_month = ?, limit_month = ? WHERE id = ?", (used_month, limit_month, key_id)
        )
        if used_month >= limit_month:
            await db.execute("UPDATE api_keys SET status = 'exhausted', last_error = 'месячная квота исчерпана' WHERE id = ?", (key_id,))
    else:
        await db.execute("UPDATE api_keys SET used_month = ? WHERE id = ?", (used_month, key_id))


async def mark_exhausted(key_id: int) -> None:
    await db.execute(
        "UPDATE api_keys SET status = 'exhausted', last_error = 'квота исчерпана (426)', "
        "used_day = CASE WHEN limit_day > 0 THEN limit_day ELSE used_day END WHERE id = ?",
        (key_id,),
    )


async def mark_banned(key_id: int, error: str) -> None:
    await db.execute("UPDATE api_keys SET status = 'banned', last_error = ? WHERE id = ?", (error[:200], key_id))


async def set_status(key_id: int, status: str) -> None:
    await db.execute("UPDATE api_keys SET status = ? WHERE id = ?", (status, key_id))


async def delete_key(key_id: int) -> None:
    await db.execute("UPDATE keywords SET active = 0 WHERE api_key_id = ?", (key_id,))
    await db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))


async def pool_summary() -> dict:
    keys = await list_keys()
    summary = {"stat": [0, 0, 0], "search": [0, 0, 0]}  # left, limit, keys
    for key in keys:
        if key["status"] != "active":
            continue
        _, month_left = _remaining(key)
        for scope in ("stat", "search"):
            if scope_of(key, scope):
                summary[scope][2] += 1
                if key["limit_month"] > 0:
                    summary[scope][0] += month_left
                    summary[scope][1] += key["limit_month"]
    return summary
