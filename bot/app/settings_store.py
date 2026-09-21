from app.db import db

DEFAULTS: dict[str, str] = {
    "sla_minutes": "30",
    "sla_warn_minutes": "10",
    "max_active": "5",
    "work_start": "10",
    "work_end": "20",
    "quiet_start": "22",
    "quiet_end": "9",
    "cooldown_days": "60",
    "dnc_cooldown_days": "180",
    "hot_threshold": "75",
    "warm_threshold": "50",
    "cadence_days": "3,7,7",
    "ai_monthly_cap_usd": "5",
    "ai_vision": "1",
    "ai_vision_daily_cap": "300",
    "ai_thrifty": "1",
    "ai_min_score": "50",
    "leads_group_id": "",
    "backup_chat_id": "",
    "plan_time": "10:00",
    "digest_time": "20:30",
    "aging_hot_minutes": "15",
    "aging_senior_hours": "24",
    "aging_archive_days": "5",
    "senior_sla_hours": "2",
    "supervised_leads": "5",
    "quiz_questions": "",
}

LABELS: dict[str, str] = {
    "sla_minutes": "SLA первого контакта, минут",
    "sla_warn_minutes": "Предупреждение до дедлайна, минут",
    "max_active": "Лимит активных лидов на человека",
    "work_start": "Начало рабочего дня (час)",
    "work_end": "Конец рабочего дня (час)",
    "quiet_start": "Тихие часы с (час)",
    "quiet_end": "Тихие часы до (час)",
    "cooldown_days": "Cooldown сущности после закрытия, дней",
    "dnc_cooldown_days": "Cooldown после «просил не писать», дней",
    "hot_threshold": "Порог «Горячий» (score)",
    "warm_threshold": "Порог «Тёплый» (score)",
    "cadence_days": "Каденция касаний, дни через запятую",
    "ai_monthly_cap_usd": "Месячный лимит расхода ИИ, $",
    "ai_vision": "Читать картинки рекламных постов (1 — да, 0 — нет)",
    "ai_vision_daily_cap": "Лимит разборов картинок в сутки",
    "ai_thrifty": "Экономный режим ИИ (1 — да, 0 — нет)",
    "ai_min_score": "Экономный режим: ниже этой оценки лид считаем формулами",
    "leads_group_id": "ID группы лидов (задаётся /bind в группе)",
    "backup_chat_id": "ID чата для бэкапов (задаётся /bind_backup)",
    "plan_time": "Время утреннего плана (ЧЧ:ММ)",
    "digest_time": "Время вечернего разбора (ЧЧ:ММ)",
    "aging_hot_minutes": "Горячий не взят → пинг всем, минут",
    "aging_senior_hours": "Никто не взял → старшему, часов",
    "aging_archive_days": "Никто не взял → архив, дней",
    "senior_sla_hours": "SLA принятия передачи старшим, часов",
    "supervised_leads": "Первые N лидов новичка под надзором старшего",
    "quiz_questions": "Вопросы квиза новичка (JSON; пусто — встроенные)",
}

_cache: dict[str, str] = {}


async def get(key: str) -> str:
    if key in _cache:
        return _cache[key]
    row = await db.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    value = row["value"] if row and row["value"] is not None else DEFAULTS.get(key, "")
    _cache[key] = value
    return value


async def get_int(key: str) -> int:
    try:
        return int(float(await get(key)))
    except (TypeError, ValueError):
        return int(DEFAULTS.get(key, "0") or 0)


async def get_float(key: str) -> float:
    try:
        return float(await get(key))
    except (TypeError, ValueError):
        return float(DEFAULTS.get(key, "0") or 0)


async def set_value(key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    _cache[key] = value


async def all_settings() -> dict[str, str]:
    result = dict(DEFAULTS)
    for row in await db.fetchall("SELECT key, value FROM settings"):
        result[row["key"]] = row["value"] or ""
    return result


async def cadence() -> list[int]:
    raw = await get("cadence_days")
    days = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            days.append(int(part))
    return days or [3, 7, 7]
