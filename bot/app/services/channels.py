"""Каналы MORIER (ТЗ 8.1): каталог, разрешённая вилка цен, матрица размещаемости.

buy_price и категория C — только для владельца; SDR видит вилку, а не точную цену.
"""
from app.db import db
from app.utils import now_iso

FLAGS_RU = {"betting": "беттинг", "casino": "казино", "crypto": "крипта", "adult": "18+", "vpn": "VPN"}
CATEGORIES = ("A", "B", "C")


async def get_by_username(username: str) -> dict | None:
    return await db.fetchone("SELECT * FROM morier_channels WHERE LOWER(username) = ?", (username.lower().lstrip("@"),))


async def list_vertical(vertical: str, include_c: bool = True) -> list[dict]:
    sql = "SELECT * FROM morier_channels WHERE vertical = ?"
    if not include_c:
        sql += " AND category != 'C'"
    return await db.fetchall(sql + " ORDER BY category, subscribers DESC", (vertical,))


async def verticals() -> list[str]:
    rows = await db.fetchall("SELECT DISTINCT vertical FROM morier_channels ORDER BY vertical")
    return [row["vertical"] for row in rows]


async def upsert(fields: dict) -> None:
    await db.execute(
        "INSERT INTO morier_channels (username, title, vertical, subscribers, reach_24h, price_from, price_to, buy_price, category, flags, admin_contact, stat_updated_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(username) DO UPDATE SET title = excluded.title, vertical = excluded.vertical, subscribers = excluded.subscribers, "
        "reach_24h = excluded.reach_24h, price_from = excluded.price_from, price_to = excluded.price_to, buy_price = excluded.buy_price, "
        "category = excluded.category, flags = excluded.flags, admin_contact = excluded.admin_contact, stat_updated_at = excluded.stat_updated_at",
        (
            fields["username"], fields.get("title"), fields["vertical"], fields.get("subscribers"), fields.get("reach_24h"),
            fields.get("price_from"), fields.get("price_to"), fields.get("buy_price"), fields.get("category", "B"),
            fields.get("flags", ""), fields.get("admin_contact"), fields.get("stat_updated_at") or now_iso(), now_iso(),
        ),
    )


async def delete(channel_id: int) -> None:
    await db.execute("DELETE FROM morier_channels WHERE id = ?", (channel_id,))


async def price_range(vertical: str) -> tuple[int, int] | None:
    """Разрешённая вилка по вертикали: от минимальной нижней до максимальной верхней границы."""
    row = await db.fetchone(
        "SELECT MIN(price_from) AS lo, MAX(price_to) AS hi FROM morier_channels "
        "WHERE vertical = ? AND category != 'C' AND price_from IS NOT NULL AND price_from > 0",
        (vertical,),
    )
    if row and row["lo"] and row["hi"] and row["hi"] >= row["lo"]:
        return int(row["lo"]), int(row["hi"])
    return None


async def placement_matrix(vertical: str | None, risk_topic: str) -> str:
    """Строка для карточки передачи (ТЗ 2.4): куда ставить, кто не принимает тематику."""
    if not vertical:
        return ""
    rows = await list_vertical(vertical, include_c=False)
    if not rows:
        return f"Каналов MORIER в вертикали «{vertical}» пока нет — куда ставить, согласуйте со старшим."
    ok, rejected = [], []
    for row in rows:
        accepts = {flag.strip() for flag in (row["flags"] or "").split(",") if flag.strip()}
        name = f"@{row['username']} ({row['category']})"
        if risk_topic != "none" and risk_topic not in accepts:
            rejected.append(name)
        else:
            ok.append(name)
    lines = ["Куда ставить: " + (", ".join(ok) if ok else "подходящих каналов нет — согласуйте с владельцем")]
    if rejected:
        lines.append("Не принимают тематику: " + ", ".join(rejected))
    return "\n".join(lines)


def format_channel(row: dict, is_owner: bool) -> str:
    subs = f"{row['subscribers'] // 1000}k подп." if row.get("subscribers") else "—"
    reach = f"{row['reach_24h'] // 1000}k/24ч" if row.get("reach_24h") else "—"
    if row.get("price_from") and row.get("price_to"):
        price = f"{row['price_from']:,}–{row['price_to']:,} ₽".replace(",", " ")
    elif row.get("price_from"):
        price = f"от {row['price_from']:,} ₽".replace(",", " ")
    else:
        price = "цена не задана"
    flags = ", ".join(FLAGS_RU[f] for f in (row["flags"] or "").split(",") if f.strip() in FLAGS_RU) or "только чистые тематики"
    line = f"@{row['username']} — {row['category']} — {subs} · {reach} — {price} — принимает: {flags}"
    if is_owner:
        if row.get("buy_price"):
            line += f" · закуп {row['buy_price']:,} ₽".replace(",", " ")
        if row.get("admin_contact"):
            line += f" · админ {row['admin_contact']}"
    return line
