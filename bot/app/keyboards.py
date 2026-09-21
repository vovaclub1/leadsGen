from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.callbacks import AdmCb, LeadCb, MenuCb

ROLE_RU = {"owner": "Владелец", "senior": "Старший", "sdr": "SDR", "buyer": "Байер"}
ROLE_ICON = {"owner": "👑", "senior": "🎯", "sdr": "🚀", "buyer": "🛒"}

NOT_TARGET_REASONS = {
    "media": "Площадка / контентный канал",
    "agency": "Агентство / биржа рекламы",
    "scam": "Скам / серые схемы",
    "nobiz": "Не бизнес / нет бюджета",
    "niche": "Не наша ниша",
    "other": "Другое",
}
LOST_REASONS = {
    "price": "Дорого",
    "silent": "Перестал отвечать",
    "competitor": "Ушёл к конкуренту",
    "season": "Не сезон / отложил",
    "other": "Другое",
}
BUDGETS = {"lt10": "до 10k", "10-30": "10–30k", "30-100": "30–100k", "100+": "100k+", "na": "Не назвал"}
TIMINGS = {"now": "Запуск сейчас", "week": "В течение недели", "month": "В течение месяца", "na": "Не определился"}


def main_menu(role: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📋 Очередь", callback_data=MenuCb(a="queue"))
    b.button(text="🗂 Мои лиды", callback_data=MenuCb(a="my"))
    b.button(text="🏆 Рейтинг", callback_data=MenuCb(a="top"))
    b.button(text="➕ Добавить лид", callback_data=MenuCb(a="add"))
    if role in ("owner", "senior"):
        b.button(text="📨 Передачи", callback_data=MenuCb(a="handoffs"))
    if role == "owner":
        b.button(text="⚙️ Админ-панель", callback_data=AdmCb(s="root"))
    b.button(text="❓ Как работать", callback_data=MenuCb(a="help"))
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def group_card_kb(lead_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🙋 Беру", callback_data=LeadCb(a="claim", id=lead_id))
    b.button(text="❌ Нецелевой", callback_data=LeadCb(a="nt", id=lead_id))
    b.button(text="♻️ Дубликат", callback_data=LeadCb(a="dup", id=lead_id))
    b.adjust(1, 2)
    return b.as_markup()


def queue_item_kb(lead_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🙋 Беру", callback_data=LeadCb(a="claim", id=lead_id))
    b.button(text="👁 Карточка", callback_data=LeadCb(a="open", id=lead_id))
    return b.as_markup()


def private_card_kb(lead: dict, role: str) -> InlineKeyboardMarkup | None:
    status = lead["status"]
    lid = lead["id"]
    b = InlineKeyboardBuilder()
    if status == "NEW":
        b.button(text="🙋 Беру", callback_data=LeadCb(a="claim", id=lid))
        b.button(text="❌ Нецелевой", callback_data=LeadCb(a="nt", id=lid))
        b.adjust(1, 1)
        return b.as_markup()
    if status == "CLAIMED":
        b.button(text="✉️ Написал — подтвердить", callback_data=LeadCb(a="wrote", id=lid))
        b.button(text="📝 Проверить сообщение", callback_data=LeadCb(a="chk", id=lid))
        b.button(text="🚫 Просил не писать", callback_data=LeadCb(a="dnc", id=lid))
        b.button(text="❌ Нецелевой", callback_data=LeadCb(a="nt", id=lid))
        b.button(text="⬆️ Передать старшему", callback_data=LeadCb(a="hand", id=lid))
        b.button(text="🗒 Заметка", callback_data=LeadCb(a="note", id=lid))
        b.adjust(1, 1, 2, 2)
        return b.as_markup()
    if status == "CONTACTED":
        b.button(text="💬 Клиент ответил", callback_data=LeadCb(a="replied", id=lid))
        b.button(text="✅ Сделал касание", callback_data=LeadCb(a="touch", id=lid))
        b.button(text="⏸ Отложить", callback_data=LeadCb(a="post", id=lid))
        b.button(text="📝 Проверить сообщение", callback_data=LeadCb(a="chk", id=lid))
        b.button(text="🚫 Просил не писать", callback_data=LeadCb(a="dnc", id=lid))
        b.button(text="❌ Нецелевой", callback_data=LeadCb(a="nt", id=lid))
        b.button(text="⬆️ Передать старшему", callback_data=LeadCb(a="hand", id=lid))
        b.button(text="🗒 Заметка", callback_data=LeadCb(a="note", id=lid))
        b.adjust(1, 2, 1, 2, 2)
        return b.as_markup()
    if status == "REPLIED":
        b.button(text="⬆️ Передать старшему", callback_data=LeadCb(a="hand", id=lid))
        b.button(text="📝 Проверить сообщение", callback_data=LeadCb(a="chk", id=lid))
        b.button(text="⏸ Отложить", callback_data=LeadCb(a="post", id=lid))
        b.button(text="🚫 Просил не писать", callback_data=LeadCb(a="dnc", id=lid))
        b.button(text="❌ Нецелевой", callback_data=LeadCb(a="nt", id=lid))
        b.button(text="🗒 Заметка", callback_data=LeadCb(a="note", id=lid))
        b.adjust(1, 2, 2, 1)
        return b.as_markup()
    if status == "HANDOFF" and role in ("owner", "senior"):
        return handoff_kb(lid)
    if status == "ACCEPTED" and role in ("owner", "senior"):
        return accepted_kb(lid)
    if status in ("HANDOFF", "ACCEPTED"):
        b.button(text="🗒 Заметка", callback_data=LeadCb(a="note", id=lid))
        return b.as_markup()
    return None


def handoff_kb(lead_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Принять", callback_data=LeadCb(a="acc", id=lead_id))
    b.button(text="↩️ Вернуть SDR", callback_data=LeadCb(a="ret", id=lead_id))
    return b.as_markup()


def accepted_kb(lead_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🏆 WON — сделка", callback_data=LeadCb(a="won", id=lead_id))
    b.button(text="💤 LOST", callback_data=LeadCb(a="lost", id=lead_id))
    b.button(text="🗒 Заметка", callback_data=LeadCb(a="note", id=lead_id))
    b.adjust(2, 1)
    return b.as_markup()


def reasons_kb(lead_id: int, kind: str) -> InlineKeyboardMarkup:
    reasons = NOT_TARGET_REASONS if kind == "nt" else LOST_REASONS
    b = InlineKeyboardBuilder()
    for code, label in reasons.items():
        b.button(text=label, callback_data=LeadCb(a=f"{kind}r", id=lead_id, v=code))
    b.button(text="Отмена", callback_data=LeadCb(a="open", id=lead_id))
    b.adjust(1)
    return b.as_markup()


def confirm_dnc_kb(lead_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Да, просил не писать", callback_data=LeadCb(a="dncok", id=lead_id))
    b.button(text="Отмена", callback_data=LeadCb(a="open", id=lead_id))
    b.adjust(1)
    return b.as_markup()


def postpone_kb(lead_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for days in (1, 3, 7, 14):
        b.button(text=f"{days} дн.", callback_data=LeadCb(a="postd", id=lead_id, v=str(days)))
    b.button(text="Отмена", callback_data=LeadCb(a="open", id=lead_id))
    b.adjust(4, 1)
    return b.as_markup()


def budget_kb(lead_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for code, label in BUDGETS.items():
        b.button(text=label, callback_data=LeadCb(a="budget", id=lead_id, v=code))
    b.button(text="Отмена", callback_data=LeadCb(a="open", id=lead_id))
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


def timing_kb(lead_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for code, label in TIMINGS.items():
        b.button(text=label, callback_data=LeadCb(a="timing", id=lead_id, v=code))
    b.adjust(2, 2)
    return b.as_markup()


def touch_kb(lead_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Сделал касание", callback_data=LeadCb(a="touch", id=lead_id))
    b.button(text="💬 Ответил", callback_data=LeadCb(a="replied", id=lead_id))
    b.button(text="⏸ Отложить", callback_data=LeadCb(a="post", id=lead_id))
    b.button(text="❌ Нецелевой", callback_data=LeadCb(a="nt", id=lead_id))
    b.adjust(2, 2)
    return b.as_markup()


def open_kb(lead_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👁 Открыть карточку", callback_data=LeadCb(a="open", id=lead_id))
    return b.as_markup()


def supervise_kb(lead_id: int, sdr_id: int) -> InlineKeyboardMarkup:
    """Кнопки надзора за первым сообщением новичка (ТЗ 9.6)."""
    b = InlineKeyboardBuilder()
    b.button(text="👍 ок", callback_data=LeadCb(a="supok", id=lead_id, v=str(sdr_id)))
    b.button(text="✍️ Замечание", callback_data=LeadCb(a="supnote", id=lead_id, v=str(sdr_id)))
    b.adjust(2)
    return b.as_markup()


def skip_kb(action: str, lead_id: int = 0) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Пропустить", callback_data=LeadCb(a=action, id=lead_id, v="skip"))
    return b.as_markup()


# ---------- админ-панель ----------

def admin_root() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👥 Сотрудники", callback_data=AdmCb(s="emp"))
    b.button(text="🔑 API-ключи", callback_data=AdmCb(s="key"))
    b.button(text="🚫 Красный список", callback_data=AdmCb(s="dnc"))
    b.button(text="📡 Каналы-доноры", callback_data=AdmCb(s="don"))
    b.button(text="⛔ Чёрный список сущностей", callback_data=AdmCb(s="bl"))
    b.button(text="🤖 ИИ", callback_data=AdmCb(s="ai"))
    b.button(text="📺 Каналы MORIER", callback_data=AdmCb(s="mch"))
    b.button(text="⚙️ Настройки", callback_data=AdmCb(s="set"))
    b.button(text="📊 Дашборд", callback_data=AdmCb(s="dash"))
    b.button(text="🗓 Отчёт за неделю", callback_data=AdmCb(s="rep"))
    b.adjust(2, 2, 1, 1, 2, 1, 1)
    return b.as_markup()


def ai_kb(vision_on: bool, vision_possible: bool, thrifty_on: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔍 Проверить ИИ", callback_data=AdmCb(s="ai", a="test"))
    if vision_possible:
        b.button(text=("👁 Зрение: вкл" if vision_on else "👁 Зрение: выкл"), callback_data=AdmCb(s="ai", a="vision"))
    b.button(text=("💰 Экономный режим: вкл" if thrifty_on else "💰 Экономный режим: выкл"), callback_data=AdmCb(s="ai", a="thrifty"))
    b.button(text="💵 Лимит расхода", callback_data=AdmCb(s="ai", a="cap"))
    b.button(text="📈 Расход по задачам", callback_data=AdmCb(s="ai", a="usage"))
    b.button(text="⬅️ Назад", callback_data=AdmCb(s="root"))
    b.adjust(1)
    return b.as_markup()


def back_kb(section: str = "root") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=AdmCb(s=section))
    return b.as_markup()


def employees_kb(users: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for user in users:
        paused = "" if user["status"] == "active" else " ⏸"
        name = f"@{user['username']}" if user.get("username") else (user.get("full_name") or str(user["id"]))
        b.button(text=f"{ROLE_ICON[user['role']]} {name}{paused}", callback_data=AdmCb(s="emp", a="open", id=user["id"]))
    b.button(text="➕ Добавить сотрудника", callback_data=AdmCb(s="emp", a="add"))
    b.button(text="⬅️ Назад", callback_data=AdmCb(s="root"))
    b.adjust(1)
    return b.as_markup()


def role_kb(user_id: int, prefix_action: str = "role") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚀 SDR — младший менеджер", callback_data=AdmCb(s="emp", a=prefix_action, id=user_id, v="sdr"))
    b.button(text="🎯 Старший менеджер", callback_data=AdmCb(s="emp", a=prefix_action, id=user_id, v="senior"))
    b.button(text="🛒 Байер", callback_data=AdmCb(s="emp", a=prefix_action, id=user_id, v="buyer"))
    b.button(text="👑 Владелец", callback_data=AdmCb(s="emp", a=prefix_action, id=user_id, v="owner"))
    b.button(text="Отмена", callback_data=AdmCb(s="emp"))
    b.adjust(1)
    return b.as_markup()


def employee_kb(user: dict, is_self: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if not is_self:
        b.button(text="🔁 Сменить роль", callback_data=AdmCb(s="emp", a="chrole", id=user["id"]))
        if user["status"] == "active":
            b.button(text="⏸ Приостановить доступ", callback_data=AdmCb(s="emp", a="pause", id=user["id"]))
        else:
            b.button(text="▶️ Вернуть доступ", callback_data=AdmCb(s="emp", a="resume", id=user["id"]))
        b.button(text="🗑 Удалить из бота", callback_data=AdmCb(s="emp", a="del", id=user["id"]))
    b.button(text="⬅️ К списку", callback_data=AdmCb(s="emp"))
    b.adjust(1)
    return b.as_markup()


def confirm_kb(section: str, action: str, item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, подтверждаю", callback_data=AdmCb(s=section, a=action, id=item_id))
    b.button(text="Отмена", callback_data=AdmCb(s=section))
    b.adjust(1)
    return b.as_markup()


def keys_kb(keys: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key in keys:
        icon = {"active": "🟢", "exhausted": "🟡", "banned": "🔴", "disabled": "⚪"}.get(key["status"], "⚪")
        scopes = "+".join(s for s in ("stat", "search") if s in (key["scopes"] or ""))
        b.button(text=f"{icon} {key['label'] or 'ключ'} · …{key['key_tail']} · {scopes or '?'}", callback_data=AdmCb(s="key", a="open", id=key["id"]))
    b.button(text="➕ Добавить ключ", callback_data=AdmCb(s="key", a="add"))
    b.button(text="⬅️ Назад", callback_data=AdmCb(s="root"))
    b.adjust(1)
    return b.as_markup()


def key_kb(key: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if key["status"] == "disabled":
        b.button(text="▶️ Включить", callback_data=AdmCb(s="key", a="enable", id=key["id"]))
    else:
        b.button(text="⏸ Отключить", callback_data=AdmCb(s="key", a="disable", id=key["id"]))
    b.button(text="🔄 Проверить квоту", callback_data=AdmCb(s="key", a="probe", id=key["id"]))
    if "search" in (key["scopes"] or ""):
        b.button(text="🔎 Добавить ключевое слово", callback_data=AdmCb(s="key", a="kwadd", id=key["id"]))
    b.button(text="🗑 Удалить", callback_data=AdmCb(s="key", a="del", id=key["id"]))
    b.button(text="⬅️ К списку", callback_data=AdmCb(s="key"))
    b.adjust(2, 1, 1, 1)
    return b.as_markup()


def scope_kb(detected: str | None = None) -> InlineKeyboardMarkup:
    """Галочкой помечаем то, что Trustat реально разрешил ключу, — но выбор остаётся за владельцем."""
    found = {p for p in (detected or "").split(",") if p}
    mark = lambda value, text: f"✅ {text}" if found and set(value.split(",")) <= found else text  # noqa: E731
    b = InlineKeyboardBuilder()
    b.button(text=mark("stat", "📊 Stat"), callback_data=AdmCb(s="key", a="scope", v="stat"))
    b.button(text=mark("search", "🔎 Search"), callback_data=AdmCb(s="key", a="scope", v="search"))
    b.button(text=mark("stat,search", "Оба"), callback_data=AdmCb(s="key", a="scope", v="stat,search"))
    b.adjust(3)
    return b.as_markup()


def limits_kb(api_limit: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if api_limit:
        b.button(text=f"✅ Как в Trustat ({api_limit}/мес)", callback_data=AdmCb(s="key", a="lim", v=f"0,{api_limit}"))
    b.button(text="Бесплатный Stat (10/день, 50/мес)", callback_data=AdmCb(s="key", a="lim", v="10,50"))
    b.button(text="Бесплатный Search (100/мес)", callback_data=AdmCb(s="key", a="lim", v="0,100"))
    b.button(text="Без лимита (платный)", callback_data=AdmCb(s="key", a="lim", v="0,0"))
    b.button(text="Ввести вручную", callback_data=AdmCb(s="key", a="lim", v="custom"))
    b.adjust(1)
    return b.as_markup()


def kw_skip_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Без ключевых слов", callback_data=AdmCb(s="key", a="kwskip"))
    return b.as_markup()


def kw_item_kb(keyword_id: int, key_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Удалить слово", callback_data=AdmCb(s="key", a="kwdel", id=keyword_id))
    b.button(text="⬅️ К ключу", callback_data=AdmCb(s="key", a="open", id=key_id))
    return b.as_markup()


def dnc_kb(rows: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for row in rows:
        icon = {"red": "🚫", "orange": "🟠", "yellow": "🟡"}[row["level"]]
        name = f"@{row['username']}" if row.get("username") else f"id {row['user_id']}"
        b.button(text=f"{icon} {name}", callback_data=AdmCb(s="dnc", a="open", id=row["id"]))
    b.button(text="➕ Добавить контакт", callback_data=AdmCb(s="dnc", a="add"))
    b.button(text="⬅️ Назад", callback_data=AdmCb(s="root"))
    b.adjust(2)
    return b.as_markup()


def dnc_level_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚫 Красный — никогда", callback_data=AdmCb(s="dnc", a="level", v="red"))
    b.button(text="🟠 Оранжевый — только старший", callback_data=AdmCb(s="dnc", a="level", v="orange"))
    b.button(text="🟡 Жёлтый — временно", callback_data=AdmCb(s="dnc", a="level", v="yellow"))
    b.adjust(1)
    return b.as_markup()


def dnc_days_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for days in (14, 30, 60, 90, 180):
        b.button(text=f"{days} дн.", callback_data=AdmCb(s="dnc", a="days", v=str(days)))
    b.adjust(5)
    return b.as_markup()


def dnc_item_kb(dnc_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Снять флаг", callback_data=AdmCb(s="dnc", a="del", id=dnc_id))
    b.button(text="⬅️ К списку", callback_data=AdmCb(s="dnc"))
    return b.as_markup()


def donors_kb(rows: list[dict], hints: int = 0) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for row in rows:
        state = "📡" if row["joined"] else "⏳"
        b.button(text=f"{state} @{row['username']} · {row['ads_found']} рекл.", callback_data=AdmCb(s="don", a="open", id=row["id"]))
    b.button(text=f"💡 Кандидаты: {hints}" if hints else "💡 Найти похожие каналы", callback_data=AdmCb(s="don", a="hints"))
    b.button(text="➕ Добавить каналы", callback_data=AdmCb(s="don", a="add"))
    b.button(text="⬅️ Назад", callback_data=AdmCb(s="root"))
    b.adjust(1)
    return b.as_markup()


def donor_hints_kb(rows: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for row in rows:
        subs = f" · {row['subscribers'] // 1000}k" if (row.get("subscribers") or 0) >= 1000 else ""
        b.button(text=f"➕ @{row['username']}{subs}", callback_data=AdmCb(s="don", a="hadd", id=row["id"]))
        b.button(text="🚫", callback_data=AdmCb(s="don", a="hhide", id=row["id"]))
    b.button(text="🔄 Искать ещё", callback_data=AdmCb(s="don", a="scan"))
    b.button(text="⬅️ К донорам", callback_data=AdmCb(s="don"))
    b.adjust(*([2] * len(rows) + [1, 1]))
    return b.as_markup()


def donor_item_kb(donor_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Убрать из сканера", callback_data=AdmCb(s="don", a="del", id=donor_id))
    b.button(text="⬅️ К списку", callback_data=AdmCb(s="don"))
    return b.as_markup()


def blacklist_kb(rows: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for row in rows:
        b.button(text=f"⛔ {row['entity_key'].split(':', 1)[-1]}", callback_data=AdmCb(s="bl", a="del", id=row["id"]))
    b.button(text="➕ Добавить", callback_data=AdmCb(s="bl", a="add"))
    b.button(text="⬅️ Назад", callback_data=AdmCb(s="root"))
    b.adjust(1)
    return b.as_markup()


def settings_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Изменить параметр", callback_data=AdmCb(s="set", a="edit"))
    b.button(text="⬅️ Назад", callback_data=AdmCb(s="root"))
    b.adjust(1)
    return b.as_markup()


def channels_kb(rows: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить каналы", callback_data=AdmCb(s="mch", a="add"))
    b.button(text="⬅️ Назад", callback_data=AdmCb(s="root"))
    b.adjust(1)
    return b.as_markup()


def channel_item_kb(channel_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Удалить", callback_data=AdmCb(s="mch", a="del", id=channel_id))
    b.button(text="⬅️ К списку", callback_data=AdmCb(s="mch"))
    return b.as_markup()
