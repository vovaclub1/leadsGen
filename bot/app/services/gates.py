"""Гейты входа — фильтр «зачем нам этот рекламодатель» до скоринга.

Гейт не удаляет лид, а ставит потолок скоринга ниже «тёплого» порога: лид остаётся
в истории и аналитике (данные для скоринга v2), но не попадает в очередь группы.
Причины пишутся в leads.gate_note и событие lead_events.
"""
import re

from app import settings_store as st
from app.db import db

# Потолки скоринга: всё ниже warm_threshold → лид не попадает в очередь.
CAP_REJECT = 25
CAP_ESTABLISHED = 20
CAP_DEAD = 15

# G3: признаки состоявшегося проекта/студии — нам такие клиенты не нужны.
_ESTABLISHED_RE = re.compile(
    r"официальн|official|студия|studio|релиз|released|издател|publisher|сиквел|франшиз", re.I
)


async def our_network() -> dict[str, dict]:
    """Сила нашей сети по вертикалям из morier_channels: max подписчиков, суммарный охват, флаги."""
    rows = await db.fetchall("SELECT vertical, subscribers, reach_24h, flags FROM morier_channels")
    net: dict[str, dict] = {}
    for row in rows:
        slot = net.setdefault(row["vertical"], {"max_subs": 0, "total_reach": 0, "flags": set()})
        slot["max_subs"] = max(slot["max_subs"], row["subscribers"] or 0)
        slot["total_reach"] += row["reach_24h"] or 0
        slot["flags"].update(f for f in (row["flags"] or "").split(",") if f)
    return net


async def evaluate(lead: dict) -> tuple[int, list[str]]:
    """Возвращает (потолок_скоринга, причины). Пустой список — гейты пройдены."""
    cap = 100
    reasons: list[str] = []

    subs = lead.get("subscribers") or 0
    views = lead.get("avg_views") or 0
    # Вертикаль неизвестная (None) — это не «неподходящая», а «неопределённая»: в экономном
    # режиме без ИИ она не заполняется вовсе, и резать таких лидов нельзя. Резим только
    # «other» — когда ИИ явно не смог отнести лид ни к одной нашей вертикали.
    vertical = lead.get("vertical")
    about = f"{lead.get('about') or ''} {lead.get('title') or ''} {lead.get('ad_text') or ''}"

    net = await our_network()
    slot = net.get(vertical) if vertical else None

    # G1: нет нашего инвентаря в вертикали — продавать нечего.
    if await st.get("gate_strict_vertical") == "on" and vertical and slot is None:
        cap = min(cap, CAP_REJECT)
        reasons.append(f"нет наших каналов в вертикали «{vertical}»")

    # G2: канал крупнее нашей сети — ему мы не нужны.
    max_subs = await st.get_int("gate_max_subs")
    if subs > max_subs:
        cap = min(cap, CAP_REJECT)
        reasons.append(f"{subs:,} подписчиков — больше потолка {max_subs:,}".replace(",", " "))
    elif slot and subs and subs > slot["max_subs"] * await st.get_float("gate_size_ratio"):
        cap = min(cap, CAP_REJECT)
        our_max = slot["max_subs"]
        reasons.append(
            f"{subs:,} подписчиков — крупнее нашего лидера в «{vertical}» ({our_max:,})".replace(",", " ")
        )

    # G3: состоявшийся проект.
    if _ESTABLISHED_RE.search(about):
        cap = min(cap, CAP_ESTABLISHED)
        reasons.append("признаки состоявшегося проекта/студии")

    # G4: мёртвая аудитория (накрутка или забитый канал).
    min_er = await st.get_float("gate_min_er")
    if subs >= 1000 and views and views / subs * 100 < min_er:
        cap = min(cap, CAP_DEAD)
        reasons.append(f"ER {views / subs * 100:.1f}% ниже порога {min_er:g}%")

    return cap, reasons
