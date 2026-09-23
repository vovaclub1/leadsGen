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
from app.keyboards import ROLE_ICON, ROLE_RU, colleague_kb, confirm_kb, employee_kb, employees_kb, role_kb
from app.services import leads, rating
from app.utils import fmt_date, h, mention, now_iso

router = Router(name="admin-employees")

# ТЗ 7.4: у этих статусов клиент уже ответил — лид передаётся конкретному коллеге со всей
# историей, а не обратно в очередь (клиент не должен почувствовать смену человека).
HANDOFF_STATUSES = ("REPLIED", "HANDOFF", "ACCEPTED", "POSTPONED")
# CLAIMED/CONTACTED — контакта с клиентом мало или он ещё не разгорелся, лид просто возвращается в очередь.
QUEUE_STATUSES = ("CLAIMED", "CONTACTED")


class AddEmployee(StatesGroup):
    ident = State()


async def employees_view(target) -> None:
    users = await db.fetchall("SELECT * FROM users ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'senior' THEN 1 WHEN 'sdr' THEN 2 ELSE 3 END, created_at")
    await _show(target, "<b>👥 Сотрудники</b>\nТолько люди из этого списка могут пользоваться ботом.", employees_kb(users))


async def _colleagues_for(uid: int) -> list[dict]:
    return await db.fetchall("SELECT * FROM users WHERE status = 'active' AND role IN ('sdr', 'senior') AND id != ?", (uid,))


async def _reassign_on_exit(uid: int, actor: dict, colleague_id: int | None) -> tuple[int, int]:
    """ТЗ 7.4 «Отпуск и уход сотрудника» — общая логика для паузы и удаления.

    CLAIMED/CONTACTED уходят обратно в очередь с пометкой, кто вёл раньше.
    REPLIED и дальше (клиент уже ответил) переходят к коллеге со всей историей касаний.
    Если коллегу не выбирали (при удалении без активных передач-кандидатов) — лид достаётся actor.
    """
    employee = await db.fetchone("SELECT * FROM users WHERE id = ?", (uid,))
    queue_rows = await db.fetchall(
        f"SELECT * FROM leads WHERE assigned_to = ? AND status IN ({','.join('?' * len(QUEUE_STATUSES))})",
        (uid, *QUEUE_STATUSES),
    )
    for lead in queue_rows:
        combined = (lead.get("note") or "").strip()
        combined = f"{combined}\n• Ранее вёл {mention(employee)} — история касаний выше.".strip()
        await leads.update(lead["id"], {
            "status": "NEW", "assigned_to": None, "claimed_at": None, "contact_deadline": None,
            "next_touch_at": None, "sla_warned": 0, "note": combined[-3000:],
        })
        await leads.log_event(lead["id"], actor["id"], "reassigned_release", f"from={uid}")
        await leads.post_to_group(await leads.get(lead["id"]))

    handoff_rows = await db.fetchall(
        f"SELECT * FROM leads WHERE assigned_to = ? AND status IN ({','.join('?' * len(HANDOFF_STATUSES))})",
        (uid, *HANDOFF_STATUSES),
    )
    target_id = colleague_id or actor["id"]
    for lead in handoff_rows:
        await leads.update(lead["id"], {"assigned_to": target_id})
        await leads.log_event(lead["id"], actor["id"], "reassigned", f"from={uid} to={target_id}")
    if handoff_rows:
        colleague = actor if target_id == actor["id"] else await db.fetchone("SELECT * FROM users WHERE id = ?", (target_id,))
        await leads.dm(
            target_id,
            f"👤 Вам передано {len(handoff_rows)} лид(ов) от {mention(employee)}. История касаний, пересланные "
            f"сообщения и заметки — в карточках лидов. Клиент не должен заметить смену.",
        )
        for lead in handoff_rows:
            fresh = await leads.get(lead["id"])
            if fresh and colleague:
                await leads.send_private_card(fresh, colleague, prefix=f"↔️ Передан от {mention(employee)}")
    return len(queue_rows), len(handoff_rows)


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


# ---------- пауза / возврат доступа ----------

@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a == "resume")), OWNER)
async def employee_resume(query: CallbackQuery, callback_data: AdmCb, me: dict) -> None:
    if callback_data.id == me["id"]:
        await query.answer("Себя нельзя.", show_alert=True)
        return
    await db.execute("UPDATE users SET status = 'active' WHERE id = ?", (callback_data.id,))
    await query.answer("Доступ возвращён.")
    await employees_view(query)


@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a == "pause")), OWNER)
async def employee_pause_start(query: CallbackQuery, callback_data: AdmCb, me: dict) -> None:
    if callback_data.id == me["id"]:
        await query.answer("Себя нельзя.", show_alert=True)
        return
    uid = callback_data.id
    handoff_count = await db.scalar(
        f"SELECT COUNT(*) FROM leads WHERE assigned_to = ? AND status IN ({','.join('?' * len(HANDOFF_STATUSES))})",
        (uid, *HANDOFF_STATUSES),
    ) or 0
    if not handoff_count:
        await _reassign_on_exit(uid, me, colleague_id=None)
        await db.execute("UPDATE users SET status = 'paused' WHERE id = ?", (uid,))
        await query.answer("Доступ приостановлен.")
        await employees_view(query)
        return
    colleagues = await _colleagues_for(uid)
    if not colleagues:
        await query.answer(
            f"У сотрудника {handoff_count} лид(ов), где клиент уже ответил, а активных коллег для передачи нет. "
            "Сначала добавьте ещё сотрудника, потом ставьте паузу.",
            show_alert=True,
        )
        return
    await _show(
        query,
        f"У сотрудника {handoff_count} лид(ов), где клиент уже ответил. Кому передать их со всей историей на время паузы?",
        colleague_kb(uid, colleagues, action="pauseto"),
    )
    await query.answer()


@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a == "pauseto")), OWNER)
async def employee_pause_finish(query: CallbackQuery, callback_data: AdmCb, me: dict) -> None:
    await _reassign_on_exit(callback_data.id, me, colleague_id=int(callback_data.v))
    await db.execute("UPDATE users SET status = 'paused' WHERE id = ?", (callback_data.id,))
    await query.answer("Доступ приостановлен, лиды переданы.")
    await employees_view(query)


# ---------- удаление ----------

@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a == "del")), OWNER)
async def employee_delete_confirm(query: CallbackQuery, callback_data: AdmCb, me: dict) -> None:
    if callback_data.id == me["id"]:
        await query.answer("Себя нельзя.", show_alert=True)
        return
    await _show(
        query,
        "Удалить сотрудника? Лиды в ожидании контакта/только с контактом вернутся в очередь с пометкой, кто вёл. "
        "Если есть лиды, где клиент уже ответил, дальше попросим выбрать, кому передать их историю.",
        confirm_kb("emp", "deld", callback_data.id),
    )


@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a == "deld")), OWNER)
async def employee_delete(query: CallbackQuery, callback_data: AdmCb, me: dict) -> None:
    if callback_data.id == me["id"]:
        await query.answer("Себя нельзя.", show_alert=True)
        return
    uid = callback_data.id
    handoff_count = await db.scalar(
        f"SELECT COUNT(*) FROM leads WHERE assigned_to = ? AND status IN ({','.join('?' * len(HANDOFF_STATUSES))})",
        (uid, *HANDOFF_STATUSES),
    ) or 0
    colleagues = await _colleagues_for(uid) if handoff_count else []
    if handoff_count and colleagues:
        await _show(
            query,
            f"У удаляемого сотрудника {handoff_count} лид(ов), где клиент уже ответил. Кому передать их со всей историей? "
            "(Остальные лиды вернутся в очередь, сотрудник будет удалён.)",
            colleague_kb(uid, colleagues, action="delto"),
        )
        await query.answer()
        return
    await _reassign_on_exit(uid, me, colleague_id=None)
    await db.execute("DELETE FROM users WHERE id = ?", (uid,))
    await query.answer("Удалён.")
    await employees_view(query)


@router.callback_query(AdmCb.filter((F.s == "emp") & (F.a == "delto")), OWNER)
async def employee_delete_finish(query: CallbackQuery, callback_data: AdmCb, me: dict) -> None:
    uid = callback_data.id
    await _reassign_on_exit(uid, me, colleague_id=int(callback_data.v))
    await db.execute("DELETE FROM users WHERE id = ?", (uid,))
    await query.answer("Удалён, лиды переданы.")
    await employees_view(query)
