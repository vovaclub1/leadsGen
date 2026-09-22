"""Проверка гейтов входа: вертикаль, размер, состоявшийся проект, ER.

Запуск:  BOT_TOKEN=1:x OWNER_ID=1 DATA_DIR=/tmp/lh-gates python -m tests.test_gates
"""
import asyncio
import os

from app import settings_store as st
from app.db import db
from app.services import gates


async def seed_network():
    await db.execute(
        "INSERT INTO morier_channels (username, title, vertical, subscribers, reach_24h, flags, created_at) VALUES "
        "('bs_top', 'BS Top', 'brawl_stars', 50000, 8000, '', '2026-01-01T00:00:00'),"
        "('bs_risk', 'BS Risk', 'brawl_stars', 10000, 1500, 'casino', '2026-01-01T00:00:00'),"
        "('cs_top', 'CS Top', 'cs2', 30000, 5000, '', '2026-01-01T00:00:00')"
    )


def check(name: str, cond: bool):
    if not cond:
        raise AssertionError(f"FAIL: {name}")
    print(f"  ok: {name}")


async def main():
    await db.connect()
    await seed_network()

    # G1: вертикаль без нашего инвентаря — reject.
    cap, reasons = await gates.evaluate({"vertical": "dota2", "subscribers": 1000, "avg_views": 500})
    check("нет инвентаря -> cap 25", cap == gates.CAP_REJECT and reasons)

    # G1 off: strict_vertical выключен — вертикаль не режет.
    await st.set_value("gate_strict_vertical", "off")
    cap, reasons = await gates.evaluate({"vertical": "dota2", "subscribers": 1000, "avg_views": 500})
    check("strict off -> вертикаль не режет", cap == 100 and not reasons)
    await st.set_value("gate_strict_vertical", "on")

    # G2: канал крупнее нашего лидера x2 (bs_top 50k -> reject от 100k).
    cap, reasons = await gates.evaluate({"vertical": "brawl_stars", "subscribers": 120_000, "avg_views": 20_000})
    check("крупнее сети -> cap 25", cap == gates.CAP_REJECT and any("крупнее" in r for r in reasons))

    # G2: ровно на границе (100k = 50k x2) — проходит.
    cap, reasons = await gates.evaluate({"vertical": "brawl_stars", "subscribers": 100_000, "avg_views": 20_000})
    check("на границе x2 -> проходит", cap == 100 and not reasons)

    # G2: абсолютный потолок 500k.
    cap, reasons = await gates.evaluate({"vertical": "brawl_stars", "subscribers": 600_000, "avg_views": 90_000})
    check("абсолютный потолок -> cap 25", cap == gates.CAP_REJECT and any("потолка" in r for r in reasons))

    # G3: состоявшийся проект.
    cap, reasons = await gates.evaluate({
        "vertical": "brawl_stars", "subscribers": 10_000, "avg_views": 2_000,
        "about": "Официальный канал игры Brawl Legends",
    })
    check("официальный канал -> cap 20", cap == gates.CAP_ESTABLISHED and any("состоявшегося" in r for r in reasons))

    # G4: мёртвая аудитория (ER 2% < 8%).
    cap, reasons = await gates.evaluate({"vertical": "brawl_stars", "subscribers": 10_000, "avg_views": 200})
    check("ER 2% -> cap 15", cap == gates.CAP_DEAD and any("ER" in r for r in reasons))

    # G4: маленький канал (<1000) не проверяется по ER.
    cap, reasons = await gates.evaluate({"vertical": "brawl_stars", "subscribers": 500, "avg_views": 5})
    check("мелкий без ER-гейта", cap == 100 and not reasons)

    # Комбинация: несколько гейтов сразу — берётся самый строгий потолок.
    cap, reasons = await gates.evaluate({
        "vertical": "brawl_stars", "subscribers": 10_000, "avg_views": 200,
        "about": "официальный канал",
    })
    check("комбинация -> min cap 15", cap == gates.CAP_DEAD and len(reasons) == 2)

    print("test_gates: все проверки пройдены")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print("FAIL:", exc)
        os._exit(1)
    os._exit(0)  # aiosqlite держит поток, обычный выход из корутины подвис бы
