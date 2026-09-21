"""Недельный отчёт владельцу: воронка, люди, доноры, расход ИИ и вывод, что с этим делать."""
from datetime import timedelta

from app.db import db
from app.services.ai import ai
from app.utils import h, in_days, local_now, mention

SOURCE_RU = {"scanner": "сканер", "search": "поиск", "manual": "вручную"}


def _pct(part: int, whole: int) -> str:
    return f"{round(part / whole * 100)}%" if whole else "—"


# Белый список фильтров: _count склеивает SQL только с этими константами, пользовательский
# ввод сюда не попадает никогда — параметр всегда один и тот же `since`.
COUNT_FILTERS = {
    "new": "created_at >= ?",
    "claimed": "claimed_at >= ?",
    "contacted": "contacted_at >= ?",
    "replied": "replied_at >= ?",
    "handed": "handoff_at >= ?",
    "won": "status = 'WON' AND closed_at >= ?",
    "lost": "status = 'LOST' AND closed_at >= ?",
    "not_target": "status = 'NOT_TARGET' AND closed_at >= ?",
}


async def _count(name: str, params: tuple) -> int:
    return int(await db.scalar(f"SELECT COUNT(*) FROM leads WHERE {COUNT_FILTERS[name]}", params) or 0)


async def weekly(days: int = 7) -> str:
    since = in_days(-days)
    ended = local_now().strftime("%d.%m")
    started = (local_now() - timedelta(days=days)).strftime("%d.%m")

    new = await _count("new", (since,))
    claimed = await _count("claimed", (since,))
    contacted = await _count("contacted", (since,))
    replied = await _count("replied", (since,))
    handed = await _count("handed", (since,))
    won = await _count("won", (since,))
    lost = await _count("lost", (since,))
    not_target = await _count("not_target", (since,))
    revenue = int(await db.scalar("SELECT COALESCE(SUM(won_amount), 0) FROM leads WHERE status = 'WON' AND closed_at >= ?", (since,)) or 0)

    lines = [f"<b>📊 Отчёт за неделю {started} — {ended}</b>", ""]

    by_source = await db.fetchall(
        "SELECT source, COUNT(*) AS c FROM leads WHERE created_at >= ? GROUP BY source ORDER BY c DESC", (since,)
    )
    source_text = ", ".join(f"{SOURCE_RU.get(r['source'], r['source'])} {r['c']}" for r in by_source) or "нет"
    lines.append(f"<b>Пришло лидов:</b> {new} ({source_text})")

    lines.append(
        f"<b>Воронка:</b> взято {claimed} ({_pct(claimed, new)} от новых) → контакт {contacted} → "
        f"ответ {replied} ({_pct(replied, contacted)} от контактов) → передано {handed} → сделок {won}"
    )
    if won:
        lines.append(f"<b>Выручка:</b> {revenue:,}".replace(",", " ") + f" ₽ · средний чек {revenue // won:,}".replace(",", " ") + " ₽")
    if not_target or lost:
        lines.append(f"<b>Закрыто впустую:</b> нецелевых {not_target}, проигранных {lost}")

    people = await db.fetchall(
        "SELECT u.id, u.username, u.full_name, u.role, "
        "  (SELECT COUNT(*) FROM leads l WHERE l.assigned_to = u.id AND l.claimed_at >= ?) AS took, "
        "  (SELECT COUNT(*) FROM leads l WHERE l.assigned_to = u.id AND l.contacted_at >= ?) AS wrote, "
        "  (SELECT COUNT(*) FROM leads l WHERE l.assigned_to = u.id AND l.replied_at >= ?) AS answers, "
        "  (SELECT COALESCE(SUM(delta), 0) FROM points p WHERE p.user_id = u.id AND p.created_at >= ?) AS pts "
        "FROM users u WHERE u.status = 'active' AND u.role IN ('sdr', 'senior') ORDER BY pts DESC",
        (since, since, since, since),
    )
    if people:
        lines.append("")
        lines.append("<b>Люди:</b>")
        for person in people:
            tail = f"взято {person['took']} · написал {person['wrote']} · ответов {person['answers']} · {person['pts']:+d} б."
            if person["wrote"]:
                tail += f" · конверсия в ответ {_pct(person['answers'], person['wrote'])}"
            elif person["took"]:
                tail += " · ни одного первого сообщения"
            lines.append(f"• {mention(person)}: {tail}")

    donors_top = await db.fetchall(
        "SELECT donor, COUNT(*) AS c, SUM(status = 'WON') AS wins FROM leads "
        "WHERE source = 'scanner' AND donor IS NOT NULL AND created_at >= ? GROUP BY donor ORDER BY c DESC LIMIT 5",
        (since,),
    )
    idle = await db.fetchall(
        "SELECT d.username FROM donors d WHERE d.active = 1 AND d.created_at < ? AND NOT EXISTS ("
        "  SELECT 1 FROM leads l WHERE l.donor = LOWER(d.username) AND l.created_at >= ?) LIMIT 8",
        (in_days(-30), in_days(-30)),
    )
    if donors_top or idle:
        lines.append("")
        lines.append("<b>Доноры:</b>")
    if donors_top:
        lines.append("Дали лидов: " + ", ".join(f"@{h(r['donor'])} — {r['c']}" + (f" (сделок {r['wins']})" if r["wins"] else "") for r in donors_top))
    if idle:
        lines.append("Молчат месяц: " + ", ".join(f"@{h(r['username'])}" for r in idle) + " — стоит заменить")

    week_cost = float(await db.scalar("SELECT COALESCE(SUM(cost_usd), 0) FROM ai_usage WHERE created_at >= ?", (since,)) or 0)
    saved = sum((await ai.skipped_month()).values())
    ai_line = f"<b>ИИ:</b> ${week_cost:.2f} за неделю, ${await ai.month_spent():.2f} за месяц"
    if saved:
        ai_line += f" · экономный режим срезал лишних запросов: {saved}"
    lines.append("")
    lines.append(ai_line)

    advice = _advice(new=new, claimed=claimed, contacted=contacted, replied=replied, handed=handed, won=won, idle=len(idle))
    if advice:
        lines.append("")
        lines.append("<b>На что смотреть:</b>")
        lines.extend(f"• {item}" for item in advice)
    return "\n".join(lines)


def _advice(*, new: int, claimed: int, contacted: int, replied: int, handed: int, won: int, idle: int) -> list[str]:
    """Только выводы, под которые есть цифры: советы «вообще» владелец читать не станет."""
    out: list[str] = []
    if new == 0:
        out.append("За неделю не пришло ни одного лида. Проверьте сканер и ключевые слова поиска.")
    elif claimed < new / 2:
        out.append(f"Разобрали меньше половины очереди ({claimed} из {new}). Лиды стареют быстрее, чем их берут.")
    if contacted and replied == 0:
        out.append(f"{contacted} первых сообщений и ноль ответов. Дело в тексте захода, а не в лидах.")
    elif contacted >= 10 and replied / contacted < 0.15:
        out.append(f"Отвечают только {round(replied / contacted * 100)}% — норма для холодных 20–30%. Разберите заходы на планёрке.")
    if claimed and not contacted:
        out.append("Лиды разобраны, но ни одного первого сообщения — работа встала сразу после взятия.")
    if handed and not won:
        out.append(f"{handed} передач старшим и ни одной сделки. Затык не в SDR, а на этапе переговоров.")
    if idle:
        out.append(f"Доноров без единого лида за месяц: {idle}. Освободите место под новые каналы.")
    return out
