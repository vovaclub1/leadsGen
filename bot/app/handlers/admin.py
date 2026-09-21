"""Админ-панель владельца: сотрудники, API-ключи, красный список, доноры, чёрный список, настройки, дашборд."""
import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, MessageOriginUser

from app import runtime
from app import settings_store as st
from app.callbacks import AdmCb
from app.db import db
from app.filters import OWNER, SENIOR_UP
from app.config import config
from app.keyboards import (
    ROLE_ICON, ROLE_RU, admin_root, ai_kb, back_kb, blacklist_kb, confirm_kb, dnc_days_kb, dnc_item_kb, dnc_kb,
    dnc_level_kb, donor_hints_kb, donor_item_kb, donors_kb, employee_kb, employees_kb, key_kb, keys_kb, kw_item_kb, kw_skip_kb,
    limits_kb, role_kb, scope_kb, settings_kb,
)
from app.services import dnc, keypool, leads, rating, report, trustat
from app.services.ai import ai
from app.services.scanner import scanner
from app.utils import fmt_date, h, in_days, mention, now_iso, season

log = logging.getLogger(__name__)
router = Router(name="admin")


class AddEmployee(StatesGroup):
    ident = State()


class AddKey(StatesGroup):
    key = State()
    limits = State()
    label = State()
    keywords = State()


class AddKeyword(StatesGroup):
    word = State()


class AddDnc(StatesGroup):
    ident = State()
    reason = State()


class AddDonor(StatesGroup):
    text = State()


class AddBlacklist(StatesGroup):
    text = State()


class EditSetting(StatesGroup):
    kv = State()


class EditAiCap(StatesGroup):
    value = State()


async def _show(target, text: str, kb) -> None:
    """Редактирует сообщение с кнопками, если это callback, иначе отправляет новое."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except TelegramAPIError:
            await target.message.answer(text, reply_markup=kb)
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb)


# ---------- корень ----------

async def root_text() -> str:
    users = await db.scalar("SELECT COUNT(*) FROM users WHERE status = 'active'") or 0
    pool = await keypool.pool_summary()
    queue_size = await db.scalar("SELECT COUNT(*) FROM leads WHERE status = 'NEW'") or 0
    active = await db.scalar("SELECT COUNT(*) FROM leads WHERE status IN ('CLAIMED','CONTACTED','REPLIED','HANDOFF','ACCEPTED')") or 0
    donors = await db.scalar("SELECT COUNT(*) FROM donors WHERE active = 1") or 0
    scanner_state = "работает" if scanner.client else ("нет сессии — python login_scanner.py" if scanner.configured else "выключен")
    return (
        "<b>⚙️ Админ-панель</b>\n"
        f"Сотрудников: {users} · В очереди: {queue_size} · В работе: {active}\n"
        f"Stat-ключей: {pool['stat'][2]} (осталось {pool['stat'][0]}/{pool['stat'][1]} в мес.) · Search-ключей: {pool['search'][2]} ({pool['search'][0]}/{pool['search'][1]})\n"
        f"Доноров: {donors} · Сканер: {scanner_state}\n"
        f"ИИ за месяц: ${await ai.month_spent():.2f} из ${await st.get_float('ai_monthly_cap_usd'):.0f}"
    )


@router.message(Command("admin"), OWNER)
async def admin_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(await root_text(), reply_markup=admin_root())


@router.callback_query(AdmCb.filter(F.s == "root"), OWNER)
async def admin_root_cb(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show(query, await root_text(), admin_root())


# ---------- сотрудники ----------

async def employees_view(target) -> None:
    users = await db.fetchall("SELECT * FROM users ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'senior' THEN 1 WHEN 'sdr' THEN 2 ELSE 3 END, created_at")
    await _show(target, "<b>👥 Сотрудники</b>\nТолько люди из этого списка могут пользоваться ботом.", employees_kb(users))


@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a == "list")), OWNER)
async def employees_list(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await employees_view(query)


@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a == "open")), OWNER)
async def employee_open(query: CallbackQuery, callback_data: AdmCb, me: dict) -> None:
    user = await db.fetchone("SELECT * FROM users WHERE id = ?", (callback_data.id,))
    if not user:
        await query.answer("Уже удалён.", show_alert=True)
        return
    active = len(await leads.my_leads(user["id"]))
    text = (
        f"{ROLE_ICON[user['role']]} <b>{mention(user)}</b>\nID: <code>{user['id']}</code>\nРоль: {ROLE_RU[user['role']]} · "
        f"Статус: {'активен' if user['status'] == 'active' else 'приостановлен'}\n"
        f"Баллы за сезон: {await rating.total(user['id'])} · Активных лидов: {active}\nДобавлен: {fmt_date(user['created_at'])}"
    )
    await _show(query, text, employee_kb(user, is_self=user["id"] == me["id"]))


@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a == "add")), OWNER)
async def employee_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddEmployee.ident)
    await query.message.answer(
        "Пришлите <b>Telegram ID</b> сотрудника (число) или перешлите любое его сообщение.\n"
        "ID сотрудник узнает, написав этому боту /start — бот покажет ID в ответе об отказе.\n/cancel — отмена."
    )
    await query.answer()


@router.message(AddEmployee.ident, OWNER)
async def employee_ident(message: Message, state: FSMContext) -> None:
    user_id = None
    name = None
    if isinstance(message.forward_origin, MessageOriginUser):
        user_id = message.forward_origin.sender_user.id
        name = message.forward_origin.sender_user.full_name
    elif message.text and message.text.strip().isdigit():
        user_id = int(message.text.strip())
    if not user_id:
        await message.answer("Нужен числовой ID или пересланное сообщение сотрудника (если у него скрыта пересылка — только ID).")
        return
    await state.clear()
    existing = await db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
    if existing:
        await message.answer(f"{mention(existing)} уже добавлен как {ROLE_RU[existing['role']]}.")
        return
    if not name and runtime.bot:
        try:
            chat = await runtime.bot.get_chat(user_id)
            name = chat.full_name
        except TelegramAPIError:
            name = None
    await message.answer(f"ID <code>{user_id}</code>{' · ' + h(name) if name else ''}. Выберите роль:", reply_markup=role_kb(user_id, "role"))


@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a == "role")), OWNER)
async def employee_role(query: CallbackQuery, callback_data: AdmCb, me: dict) -> None:
    if callback_data.v not in ROLE_RU:
        await query.answer("Неизвестная роль.", show_alert=True)
        return
    if await db.fetchone("SELECT 1 FROM users WHERE id = ?", (callback_data.id,)):
        await query.answer("Уже добавлен.", show_alert=True)
        return
    await db.execute(
        "INSERT INTO users (id, username, full_name, role, status, added_by, created_at, last_seen) VALUES (?, NULL, NULL, ?, 'active', ?, ?, ?)",
        (callback_data.id, callback_data.v, me["id"], now_iso(), now_iso()),
    )
    await leads.dm(callback_data.id, f"Вам открыт доступ к LeadHunter (роль: {ROLE_RU[callback_data.v]}). Нажмите /start.")
    await query.message.edit_text(f"Добавлен: <code>{callback_data.id}</code> — {ROLE_RU[callback_data.v]}. Пусть напишет боту /start.")
    await query.answer()
    await employees_view(query.message)


@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a == "chrole")), OWNER)
async def employee_change_role(query: CallbackQuery, callback_data: AdmCb) -> None:
    await _show(query, "Новая роль:", role_kb(callback_data.id, "setrole"))


@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a == "setrole")), OWNER)
async def employee_set_role(query: CallbackQuery, callback_data: AdmCb, me: dict) -> None:
    if callback_data.id == me["id"]:
        await query.answer("Свою роль менять нельзя.", show_alert=True)
        return
    await db.execute("UPDATE users SET role = ? WHERE id = ?", (callback_data.v, callback_data.id))
    await leads.dm(callback_data.id, f"Ваша роль в LeadHunter изменена: {ROLE_RU[callback_data.v]}. Нажмите /start, чтобы обновить меню.")
    await query.answer("Роль изменена.")
    await employees_view(query)


@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a.in_({"pause", "resume"}))), OWNER)
async def employee_toggle(query: CallbackQuery, callback_data: AdmCb, me: dict) -> None:
    if callback_data.id == me["id"]:
        await query.answer("Себя нельзя.", show_alert=True)
        return
    status = "paused" if callback_data.a == "pause" else "active"
    await db.execute("UPDATE users SET status = ? WHERE id = ?", (status, callback_data.id))
    await query.answer("Доступ приостановлен." if status == "paused" else "Доступ возвращён.")
    await employees_view(query)


@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a == "del")), OWNER)
async def employee_delete_confirm(query: CallbackQuery, callback_data: AdmCb, me: dict) -> None:
    if callback_data.id == me["id"]:
        await query.answer("Себя нельзя.", show_alert=True)
        return
    await _show(query, "Удалить сотрудника? Его лиды в ожидании контакта вернутся в очередь, остальные активные перейдут к вам.", confirm_kb("emp", "deld", callback_data.id))


@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a == "deld")), OWNER)
async def employee_delete(query: CallbackQuery, callback_data: AdmCb, me: dict) -> None:
    if callback_data.id == me["id"]:
        await query.answer("Себя нельзя.", show_alert=True)
        return
    uid = callback_data.id
    for lead in await db.fetchall("SELECT * FROM leads WHERE assigned_to = ? AND status = 'CLAIMED'", (uid,)):
        await leads.update(lead["id"], {"status": "NEW", "assigned_to": None, "claimed_at": None, "contact_deadline": None})
        await leads.post_to_group(await leads.get(lead["id"]))
    await db.execute("UPDATE leads SET assigned_to = ? WHERE assigned_to = ? AND status IN ('CONTACTED','REPLIED','HANDOFF','ACCEPTED')", (me["id"], uid))
    await db.execute("DELETE FROM users WHERE id = ?", (uid,))
    await query.answer("Удалён.")
    await employees_view(query)


# ---------- API-ключи ----------

async def keys_view(target) -> None:
    keys = await keypool.list_keys()
    pool = await keypool.pool_summary()
    text = (
        "<b>🔑 API-ключи Trustat</b>\n"
        f"Stat: {pool['stat'][2]} ключей, осталось {pool['stat'][0]} из {pool['stat'][1]} в месяц\n"
        f"Search: {pool['search'][2]} ключей, осталось {pool['search'][0]} из {pool['search'][1]}\n\n"
        "Бот держит 20% месячной квоты в резерве под передачи старшему. Ключи с ролью Search работают по ключевым словам раз в 2 дня."
    )
    await _show(target, text, keys_kb(keys))


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "list")), OWNER)
async def keys_list(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await keys_view(query)


async def key_detail(target, key_id: int) -> None:
    key = await keypool.get_key(key_id)
    if not key:
        await _show(target, "Ключ удалён.", back_kb("key"))
        return
    words = await db.fetchall("SELECT * FROM keywords WHERE api_key_id = ? AND active = 1", (key_id,))
    text = (
        f"<b>{h(key['label'] or 'ключ')}</b> · …{key['key_tail']}\n"
        f"Статус: {key['status']} · Роли: {key['scopes']} · План: {h(key['plan'] or '—')}\n"
        f"Сегодня: {key['used_day']}/{key['limit_day'] or '∞'} · Месяц: {key['used_month']}/{key['limit_month'] or '∞'}\n"
    )
    if key.get("last_error"):
        text += f"Последняя ошибка: {h(key['last_error'])}\n"
    if words:
        text += "\nКлючевые слова:\n" + "\n".join(f"• {h(w['word'])} — найдено {w['found']} · /kw_{w['id']}" for w in words)
    await _show(target, text, key_kb(key))


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "open")), OWNER)
async def key_open(query: CallbackQuery, callback_data: AdmCb) -> None:
    await key_detail(query, callback_data.id)


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a.in_({"enable", "disable"}))), OWNER)
async def key_toggle(query: CallbackQuery, callback_data: AdmCb) -> None:
    await keypool.set_status(callback_data.id, "active" if callback_data.a == "enable" else "disabled")
    await key_detail(query, callback_data.id)


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "probe")), OWNER)
async def key_probe(query: CallbackQuery, callback_data: AdmCb) -> None:
    key = await keypool.get_key(callback_data.id)
    if not key:
        await query.answer("Ключ удалён.", show_alert=True)
        return
    info, error = await trustat.probe_key(keypool.secret(key))
    if info is None:
        await query.answer(f"Ошибка: {error}", show_alert=True)
        return
    await query.answer("Ключ отвечает. Данные ниже.")
    # Раз уж сходили за квотой — приводим наши счётчики к тому, что говорит API.
    if info.get("limit_month") is not None:
        await keypool.sync_quota(key["id"], info["used_month"], info["limit_month"])
    await query.message.answer(f"<b>Квота ключа</b>\n{h(trustat.format_usage(info))}")


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "del")), OWNER)
async def key_delete_confirm(query: CallbackQuery, callback_data: AdmCb) -> None:
    await _show(query, "Удалить ключ? Его ключевые слова тоже отключатся.", confirm_kb("key", "deld", callback_data.id))


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "deld")), OWNER)
async def key_delete(query: CallbackQuery, callback_data: AdmCb) -> None:
    await keypool.delete_key(callback_data.id)
    await query.answer("Удалён.")
    await keys_view(query)


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "add")), OWNER)
async def key_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddKey.key)
    await query.message.answer("Пришлите API-ключ Trustat одним сообщением. Сообщение с ключом бот сразу удалит. /cancel — отмена.")
    await query.answer()


@router.message(AddKey.key, OWNER, F.text)
async def key_receive(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    if len(raw) < 16 or " " in raw:
        await message.answer("Не похоже на ключ. Пришлите ещё раз или /cancel.")
        return
    if await db.fetchone("SELECT 1 FROM api_keys WHERE key_tail = ?", (raw[-4:],)):
        await message.answer("Ключ с таким окончанием уже есть. Если это другой ключ — продолжайте, иначе /cancel.")
    wait = await message.answer("Проверяю ключ…")
    info, error = await trustat.probe_key(raw)
    plan = None
    if info is None:
        await wait.edit_text(f"Trustat не принял ключ: {error}. Можно всё равно сохранить (если API временно недоступно) или /cancel.")
    else:
        plan = str(info.get("plan") or "") or None
        # API сам знает лимит и разрешённые разделы — подставляем их, чтобы владелец не гадал.
        await state.update_data(api_limit_month=info.get("limit_month"), api_packages=info.get("packages") or "")
        await wait.edit_text(f"<b>Ключ принят.</b>\n{h(trustat.format_usage(info))}")
    await state.update_data(raw_key=raw, plan=plan)
    data = await state.get_data()
    hint = data.get("api_packages")
    await message.answer(
        "Какие роли у ключа?" + (f"\nПо данным Trustat ключу доступно: <b>{h(hint)}</b>." if hint else ""),
        reply_markup=scope_kb(hint),
    )


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "scope")), OWNER)
async def key_scope(query: CallbackQuery, callback_data: AdmCb, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("raw_key"):
        await query.answer("Начните добавление заново.", show_alert=True)
        return
    await state.update_data(scopes=callback_data.v)
    await query.message.edit_text(
        f"Роли: {callback_data.v}. Лимиты запросов?", reply_markup=limits_kb(data.get("api_limit_month"))
    )
    await query.answer()


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "lim")), OWNER)
async def key_limits(query: CallbackQuery, callback_data: AdmCb, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("raw_key"):
        await query.answer("Начните добавление заново.", show_alert=True)
        return
    if callback_data.v == "custom":
        await state.set_state(AddKey.limits)
        await query.message.edit_text("Введите лимиты через запятую: <code>день,месяц</code> (0 — без лимита). Например <code>10,50</code>.")
        await query.answer()
        return
    day, month = (int(part) for part in callback_data.v.split(","))
    await state.update_data(limit_day=day, limit_month=month)
    await state.set_state(AddKey.label)
    await query.message.edit_text("Название ключа (например «аккаунт Макс» или «платный №1»):")
    await query.answer()


@router.message(AddKey.limits, OWNER, F.text)
async def key_limits_custom(message: Message, state: FSMContext) -> None:
    match = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", message.text or "")
    if not match:
        await message.answer("Формат: день,месяц — например 10,50")
        return
    await state.update_data(limit_day=int(match.group(1)), limit_month=int(match.group(2)))
    await state.set_state(AddKey.label)
    await message.answer("Название ключа:")


@router.message(AddKey.label, OWNER, F.text)
async def key_label(message: Message, state: FSMContext) -> None:
    await state.update_data(label=message.text.strip()[:60])
    data = await state.get_data()
    if "search" in data.get("scopes", ""):
        await state.set_state(AddKey.keywords)
        await message.answer(
            "Ключевые слова для поиска спроса, через запятую. Например:\n<code>ищу каналы для рекламы, куплю рекламу в телеграм, нужна реклама канала</code>",
            reply_markup=kw_skip_kb(),
        )
        return
    await _save_key(message, state, [])


async def _save_key(message: Message, state: FSMContext, words: list[str]) -> None:
    data = await state.get_data()
    await state.clear()
    key_id = await keypool.add_key(data["raw_key"], data.get("label") or "ключ", data.get("scopes", "stat"), int(data.get("limit_day", 0)), int(data.get("limit_month", 0)), data.get("plan"))
    for word in words:
        await db.execute("INSERT INTO keywords (word, api_key_id, active, created_at) VALUES (?, ?, 1, ?)", (word, key_id, now_iso()))
    await message.answer(f"Ключ «{h(data.get('label') or 'ключ')}» сохранён (#{key_id})." + (f" Ключевых слов: {len(words)}." if words else ""))
    await key_detail(message, key_id)


@router.message(AddKey.keywords, OWNER, F.text)
async def key_keywords(message: Message, state: FSMContext) -> None:
    words = [w.strip() for w in message.text.split(",") if len(w.strip()) >= 4][:10]
    await _save_key(message, state, words)


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "kwskip")), OWNER, AddKey.keywords)
async def key_keywords_skip(query: CallbackQuery, state: FSMContext) -> None:
    await _save_key(query.message, state, [])
    await query.answer()


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "kwadd")), OWNER)
async def keyword_add(query: CallbackQuery, callback_data: AdmCb, state: FSMContext) -> None:
    await state.set_state(AddKeyword.word)
    await state.update_data(key_id=callback_data.id)
    await query.message.answer("Ключевое слово или фраза (можно несколько через запятую):")
    await query.answer()


@router.message(AddKeyword.word, OWNER, F.text)
async def keyword_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    words = [w.strip() for w in message.text.split(",") if len(w.strip()) >= 4][:10]
    for word in words:
        await db.execute("INSERT INTO keywords (word, api_key_id, active, created_at) VALUES (?, ?, 1, ?)", (word, data["key_id"], now_iso()))
    await message.answer(f"Добавлено слов: {len(words)}.")
    await key_detail(message, data["key_id"])


@router.message(F.text.regexp(r"^/kw_(\d+)$").as_("match"), OWNER)
async def keyword_open(message: Message, match) -> None:
    word = await db.fetchone("SELECT * FROM keywords WHERE id = ?", (int(match.group(1)),))
    if not word:
        await message.answer("Слово не найдено.")
        return
    await message.answer(
        f"«{h(word['word'])}» · найдено лидов: {word['found']} · последний запуск: {fmt_date(word['last_run']) if word['last_run'] else 'ещё не было'}",
        reply_markup=kw_item_kb(word["id"], word["api_key_id"]),
    )


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "kwdel")), OWNER)
async def keyword_delete(query: CallbackQuery, callback_data: AdmCb) -> None:
    word = await db.fetchone("SELECT * FROM keywords WHERE id = ?", (callback_data.id,))
    if word:
        await db.execute("UPDATE keywords SET active = 0 WHERE id = ?", (callback_data.id,))
        await query.answer("Удалено.")
        await key_detail(query, word["api_key_id"])
    else:
        await query.answer("Уже удалено.", show_alert=True)


# ---------- красный список (владелец + старший) ----------

async def dnc_view(target) -> None:
    rows = await dnc.list_active(30)
    text = (
        "<b>🚫 Красный список контактов</b>\n"
        "🚫 красный — никогда не писать · 🟠 оранжевый — только старший · 🟡 жёлтый — временно.\n"
        "Флаг ставится на человека и сразу закрывает контакт во всех его лидах."
    )
    if not rows:
        text += "\n\nСписок пуст."
    await _show(target, text, dnc_kb(rows))


@router.callback_query(AdmCb.filter((F.s == "dnc") & (F.a == "list")), SENIOR_UP)
async def dnc_list(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await dnc_view(query)


@router.message(Command("dnc"), SENIOR_UP)
async def dnc_cmd(message: Message) -> None:
    await dnc_view(message)


@router.callback_query(AdmCb.filter((F.s == "dnc") & (F.a == "open")), SENIOR_UP)
async def dnc_open(query: CallbackQuery, callback_data: AdmCb) -> None:
    row = await db.fetchone("SELECT d.*, u.username AS by_username, u.full_name AS by_name FROM dnc_contacts d LEFT JOIN users u ON u.id = d.added_by WHERE d.id = ?", (callback_data.id,))
    if not row or not row["active"]:
        await query.answer("Флаг уже снят.", show_alert=True)
        return
    who = f"@{row['username']}" if row.get("username") else f"id {row['user_id']}"
    text = f"<b>{h(who)}</b>\n{dnc.LEVEL_RU[row['level']]}\nПричина: {h(row['reason'])}\nДобавил: {h(row.get('by_username') or row.get('by_name') or '—')} · {fmt_date(row['created_at'])}"
    if row.get("until"):
        text += f"\nДо: {fmt_date(row['until'])}"
    await _show(query, text, dnc_item_kb(row["id"]))


@router.callback_query(AdmCb.filter((F.s == "dnc") & (F.a == "del")), SENIOR_UP)
async def dnc_delete(query: CallbackQuery, callback_data: AdmCb) -> None:
    await dnc.remove(callback_data.id)
    await query.answer("Флаг сня��.")
    await dnc_view(query)


@router.callback_query(AdmCb.filter((F.s == "dnc") & (F.a == "add")), SENIOR_UP)
async def dnc_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddDnc.ident)
    await query.message.answer("Кого закрыть? Пришлите @username, числовой ID или перешлите сообщение человека. /cancel — отмена.")
    await query.answer()


@router.message(AddDnc.ident, SENIOR_UP)
async def dnc_ident(message: Message, state: FSMContext) -> None:
    username, user_id = None, None
    if isinstance(message.forward_origin, MessageOriginUser):
        user_id = message.forward_origin.sender_user.id
        username = message.forward_origin.sender_user.username
    else:
        text = (message.text or "").strip()
        if text.isdigit():
            user_id = int(text)
        else:
            ref = leads.parse_ref(text)
            username = ref["username"] if ref else None
    if not username and not user_id:
        await message.answer("Не распознал. Нужен @username, ID или пересылка.")
        return
    await state.update_data(username=username, user_id=user_id, days=None)
    await message.answer(f"Контакт: {h('@' + username if username else str(user_id))}. Уровень?", reply_markup=dnc_level_kb())


@router.callback_query(AdmCb.filter((F.s == "dnc") & (F.a == "level")), SENIOR_UP)
async def dnc_level(query: CallbackQuery, callback_data: AdmCb, state: FSMContext) -> None:
    data = await state.get_data()
    if not (data.get("username") or data.get("user_id")):
        await query.answer("Начните заново.", show_alert=True)
        return
    await state.update_data(level=callback_data.v)
    if callback_data.v == "yellow":
        await query.message.edit_text("На сколько дней?", reply_markup=dnc_days_kb())
    else:
        await state.set_state(AddDnc.reason)
        await query.message.edit_text("Причина одной строкой (её увидят менеджеры):")
    await query.answer()


@router.callback_query(AdmCb.filter((F.s == "dnc") & (F.a == "days")), SENIOR_UP)
async def dnc_days(query: CallbackQuery, callback_data: AdmCb, state: FSMContext) -> None:
    await state.update_data(days=int(callback_data.v))
    await state.set_state(AddDnc.reason)
    await query.message.edit_text("Причина одной строкой:")
    await query.answer()


@router.message(AddDnc.reason, SENIOR_UP, F.text)
async def dnc_reason(message: Message, me: dict, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await dnc.add(data["level"], message.text.strip()[:200], me["id"], username=data.get("username"), user_id=data.get("user_id"), days=data.get("days"))
    who = "@" + data["username"] if data.get("username") else str(data.get("user_id"))
    await message.answer(f"{dnc.LEVEL_RU[data['level']]}: {h(who)}. Все открытые лиды с этим контактом закрыты для переписки.")
    await dnc_view(message)


# ---------- доноры ----------

async def donors_view(target) -> None:
    rows = await db.fetchall("SELECT * FROM donors WHERE active = 1 ORDER BY ads_found DESC, id")
    state_line = "Сканер работает." if scanner.client else ("Сканер не авторизован — выполните на сервере <code>python login_scanner.py</code>." if scanner.configured else "Сканер выключен: задайте TG_API_ID и TG_API_HASH в .env.")
    hints = await db.scalar("SELECT COUNT(*) FROM donor_hints WHERE status = 'new'") or 0
    text = f"<b>📡 Каналы-доноры</b>\nБот читает их посты и вылавливает рекламодателей. 📡 — вступил, ⏳ — ждёт вступления.\n{state_line}"
    await _show(target, text, donors_kb(rows, hints))


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "list")), OWNER)
async def donors_list(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await donors_view(query)


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "open")), OWNER)
async def donor_open(query: CallbackQuery, callback_data: AdmCb) -> None:
    row = await db.fetchone("SELECT * FROM donors WHERE id = ?", (callback_data.id,))
    if not row:
        await query.answer("Удалён.", show_alert=True)
        return
    recent = await db.fetchall("SELECT advertiser_key, created_at FROM ad_posts WHERE donor = ? ORDER BY id DESC LIMIT 5", (row["username"].lower(),))
    text = f"<b>@{h(row['username'])}</b> {h(row.get('title') or '')}\nНайдено реклам: {row['ads_found']} · {'в канале' if row['joined'] else 'ещё не вступил'}"
    if recent:
        text += "\n\nПоследние рекламодатели:\n" + "\n".join(f"• {h(r['advertiser_key'].split(':', 1)[-1])} — {fmt_date(r['created_at'])}" for r in recent)
    await _show(query, text, donor_item_kb(row["id"]))


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "del")), OWNER)
async def donor_delete(query: CallbackQuery, callback_data: AdmCb) -> None:
    await db.execute("UPDATE donors SET active = 0 WHERE id = ?", (callback_data.id,))
    await scanner.reload_donors()
    await query.answer("Убран из сканера.")
    await donors_view(query)


async def hints_view(target, note: str = "") -> None:
    rows = await db.fetchall("SELECT * FROM donor_hints WHERE status = 'new' ORDER BY votes DESC, subscribers DESC LIMIT 15")
    lines = ["<b>💡 Кандидаты в доноры</b>"]
    if note:
        lines.append(note)
    if rows:
        lines.append("Каналы, которые Telegram считает похожими на ваших доноров. Чем больше «похож на», тем ближе аудитория.")
        for row in rows:
            subs = f"{row['subscribers']:,}".replace(",", " ") + " подписчиков" if row.get("subscribers") else "подписчики неизвестны"
            lines.append(f"• <b>@{h(row['username'])}</b> — {h(row.get('title') or '')}\n  {subs} · похож на {h(row.get('sources') or '—')}")
    else:
        lines.append("Пока пусто. Нажмите «Искать ещё» — бот опросит Telegram по вашим действующим донорам.")
    await _show(target, "\n".join(lines), donor_hints_kb(rows))


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "hints")), OWNER)
async def donor_hints(query: CallbackQuery) -> None:
    await hints_view(query)


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "scan")), OWNER)
async def donor_hints_scan(query: CallbackQuery) -> None:
    await query.answer("Опрашиваю Telegram, это займёт полминуты…")
    result = await scanner.discover()
    if not result["ok"]:
        await hints_view(query, f"⚠️ {h(result['error'])}")
        return
    await hints_view(query, f"Опрошено доноров: {result['checked']}, новых кандидатов: {result['fresh']}.")


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "hadd")), OWNER)
async def donor_hint_add(query: CallbackQuery, callback_data: AdmCb, me: dict) -> None:
    row = await db.fetchone("SELECT * FROM donor_hints WHERE id = ?", (callback_data.id,))
    if not row:
        await query.answer("Кандидат уже обработан.", show_alert=True)
        return
    existing = await db.fetchone("SELECT id FROM donors WHERE LOWER(username) = ?", (row["username"].lower(),))
    if existing:
        await db.execute("UPDATE donors SET active = 1, joined = 0 WHERE id = ?", (existing["id"],))
    else:
        await db.execute(
            "INSERT INTO donors (username, title, joined, added_by, created_at) VALUES (?, ?, 0, ?, ?)",
            (row["username"], row.get("title"), me["id"], now_iso()),
        )
    await db.execute("UPDATE donor_hints SET status = 'added' WHERE id = ?", (row["id"],))
    await scanner.reload_donors()
    await query.answer(f"@{row['username']} добавлен — сканер вступит в канал.")
    await hints_view(query)


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "hhide")), OWNER)
async def donor_hint_hide(query: CallbackQuery, callback_data: AdmCb) -> None:
    await db.execute("UPDATE donor_hints SET status = 'hidden' WHERE id = ?", (callback_data.id,))
    await query.answer("Больше не предложу.")
    await hints_view(query)


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "add")), OWNER)
async def donor_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddDonor.text)
    await query.message.answer("Пришлите каналы списком — @username или ссылки, по одному в строке или через запятую. /cancel — отмена.")
    await query.answer()


@router.message(AddDonor.text, OWNER, F.text)
async def donor_save(message: Message, me: dict, state: FSMContext) -> None:
    await state.clear()
    added, skipped = [], []
    for chunk in re.split(r"[\s,]+", message.text):
        ref = leads.parse_ref(chunk)
        if not ref or not ref.get("username"):
            continue
        username = ref["username"]
        existing = await db.fetchone("SELECT * FROM donors WHERE LOWER(username) = ?", (username.lower(),))
        if existing:
            if not existing["active"]:
                await db.execute("UPDATE donors SET active = 1, joined = 0 WHERE id = ?", (existing["id"],))
                added.append(username)
            else:
                skipped.append(username)
            continue
        await db.execute("INSERT INTO donors (username, added_by, active, joined, created_at) VALUES (?, ?, 1, 0, ?)", (username, me["id"], now_iso()))
        added.append(username)
    await scanner.reload_donors()
    text = f"Добавлено: {len(added)}" + (f" ({', '.join('@' + u for u in added[:10])})" if added else "")
    if skipped:
        text += f"\nУже были: {', '.join('@' + u for u in skipped[:10])}"
    if added and scanner.client:
        text += "\nСканер вступает в каналы с паузами по 8 секунд — это защита аккаунта от флуд-бана."
    await message.answer(text)
    await donors_view(message)


# ---------- чёрный список сущностей ----------

async def blacklist_view(target) -> None:
    rows = await db.fetchall("SELECT * FROM blacklist ORDER BY id DESC LIMIT 40")
    await _show(target, "<b>⛔ Чёрный список сущностей</b>\nКаналы и сайты, которые никогда не должны становиться лидами (конкуренты, скам, свои проекты). Нажмите, чтобы убрать.", blacklist_kb(rows))


@router.callback_query(AdmCb.filter((F.s == "bl") & (F.a == "list")), OWNER)
async def blacklist_list(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await blacklist_view(query)


@router.callback_query(AdmCb.filter((F.s == "bl") & (F.a == "del")), OWNER)
async def blacklist_delete(query: CallbackQuery, callback_data: AdmCb) -> None:
    await db.execute("DELETE FROM blacklist WHERE id = ?", (callback_data.id,))
    await query.answer("Убрано.")
    await blacklist_view(query)


@router.callback_query(AdmCb.filter((F.s == "bl") & (F.a == "add")), OWNER)
async def blacklist_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddBlacklist.text)
    await query.message.answer("Пришлите @username или ссылки списком. /cancel — отмена.")
    await query.answer()


@router.message(AddBlacklist.text, OWNER, F.text)
async def blacklist_save(message: Message, me: dict, state: FSMContext) -> None:
    await state.clear()
    count = 0
    for chunk in re.split(r"[\s,]+", message.text):
        ref = leads.parse_ref(chunk)
        if ref:
            await db.execute("INSERT OR IGNORE INTO blacklist (entity_key, reason, added_by, created_at) VALUES (?, 'через админ-панель', ?, ?)", (ref["entity_key"], me["id"], now_iso()))
            count += 1
    await message.answer(f"В чёрный список добавлено: {count}.")
    await blacklist_view(message)


# ---------- настройки ----------

async def settings_view(target) -> None:
    values = await st.all_settings()
    lines = ["<b>⚙️ Настройки</b>"]
    for key, label in st.LABELS.items():
        lines.append(f"<code>{key}</code> = <b>{h(values.get(key, ''))}</b> — {label}")
    await _show(target, "\n".join(lines), settings_kb())


@router.callback_query(AdmCb.filter((F.s == "set") & (F.a == "list")), OWNER)
async def settings_list(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await settings_view(query)


@router.callback_query(AdmCb.filter((F.s == "set") & (F.a == "edit")), OWNER)
async def settings_edit(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditSetting.kv)
    await query.message.answer("Отправьте <code>ключ значение</code>, например <code>sla_minutes 45</code> или <code>cadence_days 2,5,7</code>. /cancel — отмена.")
    await query.answer()


@router.message(EditSetting.kv, OWNER, F.text)
async def settings_save(message: Message, state: FSMContext) -> None:
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0] not in st.DEFAULTS:
        await message.answer("Неизвестный ключ. Список ключей — в сообщении выше.")
        return
    key, value = parts[0], parts[1].strip()
    if key.endswith(("_minutes", "_hours", "_days", "_start", "_end", "threshold", "max_active")) and key != "cadence_days" and not value.lstrip("-").isdigit():
        await message.answer("Это числовой параметр.")
        return
    if key == "cadence_days" and not re.fullmatch(r"\d+(,\d+)*", value):
        await message.answer("Формат каденции: числа через запятую, например 3,7,7")
        return
    await state.clear()
    await st.set_value(key, value)
    await message.answer(f"<code>{key}</code> = <b>{h(value)}</b>. Применено.")
    await settings_view(message)


# ---------- ИИ ----------

async def ai_view(target, note: str = "") -> None:
    info = await ai.health()
    if not info["enabled"]:
        state = "🔴 выключен — AI_API_KEY не задан в .env"
    elif info["streak"] >= 3:
        state = f"🔴 не отвечает ({info['streak']} ошибок подряд)"
    elif info["last_error"]:
        state = "🟡 были ошибки, сейчас работает"
    else:
        state = "🟢 работает"
    if not info["enabled"]:
        vision = "недоступно без ключа ИИ"
    elif not config.vision_possible:
        vision = "выключено в .env (AI_VISION_MODEL)"
    else:
        vision = "включено" if info["vision_on"] else "выключено"
    lines = [
        "<b>🤖 ИИ</b>",
        f"Состояние: {state}",
        f"Модель: <code>{h(info['model'])}</code> · сервис: <code>{h(info['base_url'])}</code>",
        f"Зрение: {vision}" + (f" (<code>{h(info['vision_model'])}</code>)" if config.vision_possible else ""),
        f"Расход за {h(season())}: <b>${info['spent']:.3f}</b> из ${info['cap']:.2f}",
        f"Запросов: {info['ok']} удачных · {info['fail']} с ошибкой (с момента запуска)",
    ]
    if info["thrifty"]:
        saved = sum(info["skipped"].values())
        line = f"Экономный режим: включён, лиды с оценкой ниже {info['min_score']} считаются формулами"
        if saved:
            line += f"\nНе отправлено запросов за месяц: {saved}"
        lines.append(line)
    else:
        lines.append("Экономный режим: выключен — в ИИ уходит каждый лид и каждый рекламный пост")
    if info["last_error"]:
        lines.append(f"Последняя ошибка: {h(info['last_error'])}")
    if config.ai_model_was_legacy:
        lines.append(f"⚠️ В .env указана снятая модель <code>{h(config.ai_model_was_legacy)}</code> — работаю на <code>{h(info['model'])}</code>.")
    if note:
        lines.append(f"\n{note}")
    await _show(target, "\n".join(lines), ai_kb(info["vision_on"], config.vision_possible, info["thrifty"]))


@router.callback_query(AdmCb.filter((F.s == "ai") & (F.a == "list")), OWNER)
async def ai_list(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await ai_view(query)


@router.callback_query(AdmCb.filter((F.s == "ai") & (F.a == "test")), OWNER)
async def ai_test(query: CallbackQuery) -> None:
    await query.answer("Проверяю…")
    result = await ai.self_test(with_vision=True)
    if not result["ok"]:
        note = f"❌ Проверка не прошла ({result.get('stage')}): {h(result['error'])}"
    else:
        note = f"✅ Текст: ответ за {result['ms']} мс, списано ${result.get('cost', 0):.4f}."
        if "vision" in result:
            icon = "✅" if result.get("vision_ok") else ("ℹ️" if "выключено" in str(result["vision"]) else "❌")
            note += f"\n{icon} Картинки: {h(str(result['vision']))}"
            if result.get("vision_ms"):
                note += f" ({result['vision_ms']} мс)"
    await ai_view(query, note)


@router.callback_query(AdmCb.filter((F.s == "ai") & (F.a == "vision")), OWNER)
async def ai_vision_toggle(query: CallbackQuery) -> None:
    new_value = "0" if await ai.vision_enabled() else "1"
    await st.set_value("ai_vision", new_value)
    await ai_view(query, "👁 Чтение картинок включено." if new_value == "1" else "👁 Чтение картинок выключено.")


@router.callback_query(AdmCb.filter((F.s == "ai") & (F.a == "thrifty")), OWNER)
async def ai_thrifty_toggle(query: CallbackQuery) -> None:
    new_value = "0" if await ai.thrifty() else "1"
    await st.set_value("ai_thrifty", new_value)
    note = (
        f"💰 Экономный режим включён. Лиды с оценкой ниже {await st.get_int('ai_min_score')} и посты "
        "с явной пометкой рекламы больше не тратят запросы."
        if new_value == "1"
        else "💰 Экономный режим выключен. Каждый лид и каждый рекламный пост идут в ИИ."
    )
    await ai_view(query, note)


@router.callback_query(AdmCb.filter((F.s == "ai") & (F.a == "usage")), OWNER)
async def ai_usage(query: CallbackQuery) -> None:
    rows = await ai.usage_breakdown()
    titles = {
        "analyze": "Анализ лидов", "ad": "Определение рекламы", "vision": "Чтение картинок",
        "draft": "Проверка сообщений", "selftest": "Проверки связи",
    }
    if not rows:
        await ai_view(query, "За этот месяц запросов к ИИ ещё не было.")
        return
    lines = [f"<b>📈 Расход ИИ за {h(season())}</b>"]
    for row in rows:
        failed = int(row["calls"]) - int(row["ok"] or 0)
        lines.append(
            f"{titles.get(row['purpose'], row['purpose'] or '—')}: ${row['cost']:.3f} · "
            f"{row['calls']} запр." + (f" ({failed} с ошибкой)" if failed else "") +
            f" · {row['tin']}→{row['tout']} токенов"
        )
    lines.append(f"\nИтого: <b>${sum(r['cost'] for r in rows):.3f}</b>")
    skipped = await ai.skipped_month()
    if skipped:
        lines.append(
            "Экономный режим сберёг запросов: "
            + ", ".join(f"{titles.get(key, key)} — {value}" for key, value in skipped.items())
        )
    await _show(query, "\n".join(lines), back_kb("ai"))


@router.callback_query(AdmCb.filter((F.s == "ai") & (F.a == "cap")), OWNER)
async def ai_cap(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditAiCap.value)
    await query.message.answer(
        f"Текущий месячный лимит: ${await st.get_float('ai_monthly_cap_usd'):.2f}\n"
        "Пришлите новую сумму в долларах, например <code>10</code>. 0 — без лимита. /cancel — отмена."
    )
    await query.answer()


@router.message(EditAiCap.value, OWNER, F.text)
async def ai_cap_save(message: Message, state: FSMContext) -> None:
    try:
        value = float(message.text.strip().replace(",", ".").lstrip("$"))
    except ValueError:
        await message.answer("Нужно число, например 10 или 7.5")
        return
    if value < 0:
        await message.answer("Лимит не может быть отрицательным.")
        return
    await state.clear()
    await st.set_value("ai_monthly_cap_usd", f"{value:g}")
    await ai_view(message, f"Лимит расхода: ${value:.2f} в месяц.")


# ---------- недельный отчёт ----------

@router.callback_query(AdmCb.filter(F.s == "rep"), OWNER)
async def weekly_report(query: CallbackQuery) -> None:
    await query.answer("Собираю…")
    await _show(query, await report.weekly(), back_kb("root"))


# ---------- дашборд ----------

@router.callback_query(AdmCb.filter(F.s == "dash"), OWNER)
async def dashboard(query: CallbackQuery) -> None:
    week = in_days(-7)
    month = in_days(-30)
    created_week = await db.scalar("SELECT COUNT(*) FROM leads WHERE created_at > ?", (week,)) or 0
    by_source = await db.fetchall("SELECT source, COUNT(*) AS c FROM leads WHERE created_at > ? GROUP BY source", (month,))
    by_status = await db.fetchall("SELECT status, COUNT(*) AS c FROM leads WHERE created_at > ? GROUP BY status ORDER BY c DESC", (month,))
    funnel = await db.fetchone(
        "SELECT SUM(claimed_at IS NOT NULL) AS claimed, SUM(contacted_at IS NOT NULL) AS contacted, SUM(replied_at IS NOT NULL) AS replied, "
        "SUM(status = 'WON') AS won, COALESCE(SUM(won_amount), 0) AS revenue, COALESCE(SUM(won_margin), 0) AS margin FROM leads WHERE created_at > ?",
        (month,),
    ) or {}
    released = await db.scalar("SELECT COUNT(*) FROM lead_events WHERE type = 'released' AND created_at > ?", (month,)) or 0
    dnc_hits = await db.scalar("SELECT COUNT(*) FROM lead_events WHERE type = 'dnc' AND created_at > ?", (month,)) or 0
    not_target = await db.fetchall("SELECT lost_reason, COUNT(*) AS c FROM leads WHERE status = 'NOT_TARGET' AND created_at > ? GROUP BY lost_reason ORDER BY c DESC LIMIT 5", (month,))
    ad_top = await db.fetchall("SELECT donor, COUNT(*) AS c FROM ad_posts WHERE created_at > ? GROUP BY donor ORDER BY c DESC LIMIT 5", (month,))

    contacted = funnel.get("contacted") or 0
    replied = funnel.get("replied") or 0
    lines = [
        "<b>📊 Дашборд · 30 дней</b>",
        f"Новых лидов: за неделю {created_week}",
        "Источники: " + (", ".join(f"{leads.SOURCE_RU.get(r['source'], r['source'])} {r['c']}" for r in by_source) or "—"),
        f"Воронка: взято {funnel.get('claimed') or 0} → контакт {contacted} → ответ {replied} ({(replied * 100 // contacted) if contacted else 0}%) → сделок {funnel.get('won') or 0}",
        f"Выручка: {int(funnel.get('revenue') or 0):,} ₽ · маржа {int(funnel.get('margin') or 0):,} ₽".replace(",", " "),
        f"Упущено по таймеру: {released} · «Просил не писать»: {dnc_hits}",
        "Статусы: " + (", ".join(f"{leads.STATUS_RU.get(r['status'], r['status'])} {r['c']}" for r in by_status) or "—"),
    ]
    if not_target:
        lines.append("Нецелевые: " + ", ".join(f"{h(r['lost_reason'] or '?')} {r['c']}" for r in not_target))
    if ad_top:
        lines.append("Доноры по улову: " + ", ".join(f"@{h(r['donor'])} {r['c']}" for r in ad_top))
    lines.append(f"ИИ за месяц: ${await ai.month_spent():.2f}")
    await _show(query, "\n".join(lines), back_kb("root"))
