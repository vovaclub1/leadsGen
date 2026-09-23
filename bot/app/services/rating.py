from app.db import db
from app.utils import now_iso, parse_iso, season

POINTS = {
    "contact_30": 10,
    "contact_2h": 5,
    "personal_fact": 3,
    "touch_on_time": 2,
    "replied": 15,
    "accepted": 25,
    "won": 50,
    "won_repeat": 30,
    "not_target": 1,
    "duplicate": 1,
    "dnc_honest": 2,
    "timer": -5,
    "wrote_red": -15,
    "rework": -3,
}

REASON_RU = {
    "contact_30": "первый контакт подтверждён ≤ 30 мин",
    "contact_2h": "первый контакт подтверждён ≤ 2 ч",
    "personal_fact": "персональный факт в первом сообщении",
    "touch_on_time": "касание сделано в срок",
    "replied": "клиент ответил",
    "accepted": "лид принят старшим",
    "won": "сделка закрыта",
    "won_repeat": "повторная сделка с тем же клиентом",
    "not_target": "верно отмечен нецелевой",
    "duplicate": "верно отмечен дубликат",
    "dnc_honest": "честно отметил «просил не писать»",
    "timer": "лид освобождён по таймеру",
    "wrote_red": "написал контакту из красного списка",
    "rework": "карточка возвращена на доработку",
    "adjust": "корректировка владельца",
}


async def add(user_id: int, reason: str, lead_id: int | None = None, created_by: int | None = None, delta: int | None = None, note: str | None = None) -> int:
    value = POINTS.get(reason, 0) if delta is None else delta
    text = REASON_RU.get(reason, reason)
    if note:
        text = f"{text}: {note}"
    await db.execute(
        "INSERT INTO points (user_id, delta, reason, lead_id, created_by, season, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, value, text, lead_id, created_by, season(), now_iso()),
    )
    return value


async def total(user_id: int, current_season: str | None = None) -> int:
    value = await db.scalar(
        "SELECT COALESCE(SUM(delta), 0) FROM points WHERE user_id = ? AND season = ?",
        (user_id, current_season or season()),
    )
    return int(value or 0)


async def leaderboard(current_season: str | None = None, limit: int = 15) -> list[dict]:
    return await db.fetchall(
        "SELECT u.id, u.username, u.full_name, u.role, COALESCE(SUM(p.delta), 0) AS pts, "
        "SUM(CASE WHEN p.reason LIKE 'сделка%' THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN p.reason LIKE 'клиент ответил%' THEN 1 ELSE 0 END) AS replies "
        "FROM users u LEFT JOIN points p ON p.user_id = u.id AND p.season = ? "
        "WHERE u.status = 'active' AND u.role IN ('sdr', 'senior') "
        "GROUP BY u.id ORDER BY pts DESC, replies DESC LIMIT ?",
        (current_season or season(), limit),
    )


async def rank(user_id: int) -> int | None:
    board = await leaderboard(limit=100)
    for index, row in enumerate(board, start=1):
        if row["id"] == user_id:
            return index
    return None


async def history(user_id: int, limit: int = 10) -> list[dict]:
    return await db.fetchall(
        "SELECT delta, reason, lead_id, created_at FROM points WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    return ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) // 2


async def median_first_contact(user_id: int | None = None) -> int | None:
    """Медиана минут между взятием лида и первым контактом (ТЗ 9.3). None — данных нет."""
    sql = "SELECT assigned_to, claimed_at, contacted_at FROM leads WHERE claimed_at IS NOT NULL AND contacted_at IS NOT NULL"
    params: tuple = ()
    if user_id:
        sql += " AND assigned_to = ?"
        params = (user_id,)
    current = season()
    minutes = []
    for row in await db.fetchall(sql, params):
        if row["claimed_at"][:7] != current:
            continue
        delta = (parse_iso(row["contacted_at"]) - parse_iso(row["claimed_at"])).total_seconds() / 60
        if delta >= 0:
            minutes.append(int(delta))
    return _median(minutes)


def fmt_minutes(value: int | None) -> str:
    if value is None:
        return "нет данных"
    if value < 60:
        return f"{value} мин"
    return f"{value // 60} ч {value % 60:02d} мин"
