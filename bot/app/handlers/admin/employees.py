"""Сотрудники: добавление, роли, пауза доступа, удаление с передачей лидов."""
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, MessageOriginUser

from app import runtime
from app.callbacks import AdmCb
from app.db import db
from app.filters import OWNER
from app.handlers.helpers import show as _show
from app.keyboards import ROLE_ICON, ROLE_RU, confirm_kb, employee_kb, employees_kb, role_kb
from app.services import leads, rating
from app.utils import fmt_date, h, mention, now_iso

router = Router(name="admin-employees")


class AddEmployee(StatesGroup):
    ident = State()


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
