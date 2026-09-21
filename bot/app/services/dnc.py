"""Красный список контактов: кому нельзя писать про рекламу. Уровни red / orange / yellow, сопоставление по user_id и username."""
import logging

from app import runtime
from app.db import db
from app.utils import fmt_date, h, in_days, now_iso

log = logging.getLogger(__name__)

LEVEL_RU = {"red": "🚫 Красный — никогда", "orange": "🟠 Оранжевый — только старший", "yellow": "🟡 Жёлтый — временно"}
LEVEL_SHORT = {"red": "красный", "orange": "оранжевый", "yellow": "жёлтый"}


def _norm(username: str | None) -> str | None:
    if not username:
        return None
    return username.strip().lstrip("@").lower() or None


async def expire_yellow() -> list[dict]:
    expired = await db.fetchall(
        "SELECT * FROM dnc_contacts WHERE active = 1 AND level = 'yellow' AND until IS NOT NULL AND until < ?",
        (now_iso(),),
    )
    for row in expired:
        await db.execute("UPDATE dnc_contacts SET active = 0 WHERE id = ?", (row["id"],))
    return expired


async def check(username: str | None = None, user_id: int | None = None) -> dict | None:
    username = _norm(username)
    if not username and not user_id:
        return None
    clauses, params = [], []
    if user_id:
        clauses.append("user_id = ?")
        params.append(user_id)
    if username:
        clauses.append("LOWER(username) = ?")
        params.append(username)
    row = await db.fetchone(
        f"SELECT * FROM dnc_contacts WHERE active = 1 AND ({' OR '.join(clauses)}) "
        "ORDER BY CASE level WHEN 'red' THEN 0 WHEN 'orange' THEN 1 ELSE 2 END LIMIT 1",
        tuple(params),
    )
    if row and row["until"] and row["until"] < now_iso():
        await db.execute("UPDATE dnc_contacts SET active = 0 WHERE id = ?", (row["id"],))
        return None
    return row


async def add(level: str, reason: str, added_by: int, username: str | None = None, user_id: int | None = None, days: int | None = None) -> int:
    username = _norm(username)
    until = in_days(days) if (level == "yellow" and days) else None
    existing = await check(username=username, user_id=user_id)
    if existing:
        await db.execute("UPDATE dnc_contacts SET active = 0 WHERE id = ?", (existing["id"],))
    dnc_id = await db.insert(
        "INSERT INTO dnc_contacts (user_id, username, level, reason, added_by, until, active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        (user_id, username, level, reason, added_by, until, now_iso()),
    )
    await propagate(level, reason, username=username, user_id=user_id)
    return dnc_id


async def remove(dnc_id: int) -> None:
    row = await db.fetchone("SELECT * FROM dnc_contacts WHERE id = ?", (dnc_id,))
    if not row:
        return
    await db.execute("UPDATE dnc_contacts SET active = 0 WHERE id = ?", (dnc_id,))
    clauses, params = [], []
    if row["user_id"]:
        clauses.append("contact_user_id = ?")
        params.append(row["user_id"])
    if row["username"]:
        clauses.append("LOWER(contact_username) = ?")
        params.append(row["username"])
    if clauses:
        await db.execute(f"UPDATE leads SET contact_state = 'ok' WHERE {' OR '.join(clauses)}", tuple(params))


async def propagate(level: str, reason: str, username: str | None, user_id: int | None) -> None:
    """Флаг ставится на человека и сразу закрывает контакт во всех лидах, включая активные."""
    clauses, params = [], []
    if user_id:
        clauses.append("contact_user_id = ?")
        params.append(user_id)
    if username:
        clauses.append("LOWER(contact_username) = ?")
        params.append(username)
    if not clauses:
        return
    leads = await db.fetchall(f"SELECT id, assigned_to, title, status FROM leads WHERE {' OR '.join(clauses)}", tuple(params))
    for lead in leads:
        await db.execute("UPDATE leads SET contact_state = ?, updated_at = ? WHERE id = ?", (level, now_iso(), lead["id"]))
        if lead["assigned_to"] and lead["status"] in ("CLAIMED", "CONTACTED", "REPLIED") and runtime.bot:
            try:
                await runtime.bot.send_message(
                    lead["assigned_to"],
                    f"⚠️ Контакт по лиду #{lead['id']} «{h(lead['title'])}» закрыт: {LEVEL_SHORT[level]} список — {h(reason)}.\n"
                    f"Дальше не пишите. {'Передайте старшему кнопкой на карточке.' if level == 'orange' else 'Отметьте лид как нецелевой или передайте старшему.'}",
                )
            except Exception as exc:  # noqa: BLE001 — сотрудник мог заблокировать бота
                log.warning("Не удалось уведомить %s: %s", lead["assigned_to"], exc)


async def list_active(limit: int = 30) -> list[dict]:
    return await db.fetchall(
        "SELECT d.*, u.username AS by_username FROM dnc_contacts d LEFT JOIN users u ON u.id = d.added_by "
        "WHERE d.active = 1 ORDER BY d.id DESC LIMIT ?",
        (limit,),
    )


def badge(row: dict | None) -> str:
    if not row:
        return ""
    text = f"{LEVEL_SHORT[row['level']]} список · «{h(row.get('reason') or 'без причины')}»"
    if row.get("until"):
        text += f" · до {fmt_date(row['until'])}"
    return text
