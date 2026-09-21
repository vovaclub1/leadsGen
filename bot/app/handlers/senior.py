"""Старший менеджер: приём передач, возврат, WON/LOST, ручные баллы, чёрный список сущностей."""
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.callbacks import LeadCb, MenuCb
from app.db import db
from app.filters import OWNER, SENIOR_UP
from app.keyboards import LOST_REASONS, handoff_kb, reasons_kb, skip_kb
from app.services import leads, rating
from app.utils import h, mention, now_iso

router = Router(name="senior")


class ReturnSt(StatesGroup):
    comment = State()


class WonSt(StatesGroup):
    amount = State()
    margin = State()


# ---------- список передач ----------

async def send_handoffs(target: Message) -> None:
    rows = await leads.handoffs()
    if not rows:
        await target.answer("Передач нет.")
        return
    await target.answer(f"<b>Передачи ({len(rows)})</b>")
    for lead in rows:
        sdr = await leads.user(lead.get("assigned_to"))
        await target.answer(f"{leads.short_line(lead)}\nот {mention(sdr)} · {h(lead.get('handoff_note') or '')}", reply_markup=handoff_kb(lead["id"]))


@router.message(Command("handoffs"), SENIOR_UP)
async def handoffs_cmd(message: Message) -> None:
    await send_handoffs(message)


@router.callback_query(MenuCb.filter(F.a == "handoffs"), SENIOR_UP)
async def handoffs_cb(query: CallbackQuery) -> None:
    await send_handoffs(query.message)
    await query.answer()


# ---------- принять / вернуть ----------

@router.callback_query(LeadCb.filter(F.a == "acc"), SENIOR_UP)
async def accept(query: CallbackQuery, callback_data: LeadCb, me: dict) -> None:
    lead = await leads.get(callback_data.id)
    if not lead or lead["status"] != "HANDOFF":
        await query.answer("Лид уже принят или закрыт.", show_alert=True)
        return
    text = await leads.accept(lead, me)
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(text)
    await leads.send_private_card(await leads.get(lead["id"]), me)
    await query.answer()


@router.callback_query(LeadCb.filter(F.a == "ret"), SENIOR_UP)
async def return_start(query: CallbackQuery, callback_data: LeadCb, state: FSMContext) -> None:
    lead = await leads.get(callback_data.id)
    if not lead or lead["status"] != "HANDOFF":
        await query.answer("Лид уже принят или закрыт.", show_alert=True)
        return
    await state.set_state(ReturnSt.comment)
    await state.update_data(lead_id=lead["id"])
    await query.message.answer(f"Что SDR должен доделать по лиду #{lead['id']}? Одним сообщением.")
    await query.answer()


@router.message(ReturnSt.comment, SENIOR_UP, F.text)
async def return_apply(message: Message, me: dict, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    lead = await leads.get(data.get("lead_id", 0))
    if not lead or lead["status"] != "HANDOFF":
        await message.answer("Лид уже не в передаче.")
        return
    await message.answer(await leads.return_to_sdr(lead, me, message.text))


# ---------- WON / LOST ----------

@router.callback_query(LeadCb.filter(F.a == "won"), SENIOR_UP)
async def won_start(query: CallbackQuery, callback_data: LeadCb, state: FSMContext) -> None:
    lead = await leads.get(callback_data.id)
    if not lead or lead["status"] != "ACCEPTED":
        await query.answer("Сначала примите лид.", show_alert=True)
        return
    await state.set_state(WonSt.amount)
    await state.update_data(lead_id=lead["id"])
    await query.message.answer(f"Сумма сделки по #{lead['id']} в рублях (число):")
    await query.answer()


@router.message(WonSt.amount, SENIOR_UP, F.text)
async def won_amount(message: Message, state: FSMContext) -> None:
    digits = re.sub(r"[^\d]", "", message.text or "")
    if not digits:
        await message.answer("Нужно число, например 45000.")
        return
    await state.update_data(amount=int(digits))
    await state.set_state(WonSt.margin)
    await message.answer("Маржа агентства в рублях (число) — или пропустите.", reply_markup=skip_kb("marginskip"))


async def _finish_won(state: FSMContext, me: dict, margin: int | None, send) -> None:
    data = await state.get_data()
    await state.clear()
    lead = await leads.get(data.get("lead_id", 0))
    if not lead or lead["status"] != "ACCEPTED":
        await send("Лид уже не в работе у старшего.")
        return
    await send(await leads.won(lead, me, int(data.get("amount") or 0), margin))


@router.message(WonSt.margin, SENIOR_UP, F.text)
async def won_margin(message: Message, me: dict, state: FSMContext) -> None:
    digits = re.sub(r"[^\d]", "", message.text or "")
    await _finish_won(state, me, int(digits) if digits else None, message.answer)


@router.callback_query(LeadCb.filter(F.a == "marginskip"), SENIOR_UP, WonSt.margin)
async def won_margin_skip(query: CallbackQuery, me: dict, state: FSMContext) -> None:
    await _finish_won(state, me, None, query.message.answer)
    await query.answer()


@router.callback_query(LeadCb.filter(F.a == "lost"), SENIOR_UP)
async def lost_menu(query: CallbackQuery, callback_data: LeadCb) -> None:
    lead = await leads.get(callback_data.id)
    if not lead or lead["status"] != "ACCEPTED":
        await query.answer("Сначала примите лид.", show_alert=True)
        return
    await query.message.answer(f"Почему лид #{lead['id']} потерян?", reply_markup=reasons_kb(lead["id"], "lost"))
    await query.answer()


@router.callback_query(LeadCb.filter(F.a == "lostr"), SENIOR_UP)
async def lost_apply(query: CallbackQuery, callback_data: LeadCb, me: dict) -> None:
    lead = await leads.get(callback_data.id)
    if not lead or lead["status"] != "ACCEPTED":
        await query.answer("Лид уже закрыт.", show_alert=True)
        return
    await query.message.edit_text(await leads.lost(lead, me, callback_data.v, LOST_REASONS.get(callback_data.v, callback_data.v)))
    await query.answer()


# ---------- ручные баллы и чёрный список ----------

@router.message(Command("adjust"), OWNER)
async def adjust(message: Message, command: CommandObject) -> None:
    match = re.match(r"@?(\w+)\s+([+-]?\d+)\s*(.*)", command.args or "", re.S)
    if not match:
        await message.answer("Формат: /adjust @username ±N причина")
        return
    username, delta, reason = match.group(1), int(match.group(2)), match.group(3).strip() or "ручная корректировка"
    target = await db.fetchone("SELECT * FROM users WHERE LOWER(username) = ?", (username.lower(),))
    if not target:
        await message.answer("Сотрудник с таким username не найден среди добавленных.")
        return
    await rating.add(target["id"], "manual", created_by=message.from_user.id, delta=delta, note=reason)
    await leads.dm(target["id"], f"Владелец скорректировал ваши баллы: {delta:+d} — {h(reason)}")
    await message.answer(f"{mention(target)}: {delta:+d}. Итого за сезон: {await rating.total(target['id'])}")


@router.message(Command("blacklist"), SENIOR_UP)
async def blacklist(message: Message, command: CommandObject, me: dict) -> None:
    parts = (command.args or "").split(maxsplit=1)
    ref = leads.parse_ref(parts[0]) if parts else None
    if not ref:
        await message.answer("Формат: /blacklist @канал причина — сущность больше никогда не станет лидом.")
        return
    reason = parts[1] if len(parts) > 1 else "без причины"
    await db.execute(
        "INSERT OR REPLACE INTO blacklist (entity_key, reason, added_by, created_at) VALUES (?, ?, ?, ?)",
        (ref["entity_key"], reason, me["id"], now_iso()),
    )
    open_lead = await db.fetchone("SELECT * FROM leads WHERE entity_key = ? AND status IN ('NEW') ORDER BY id DESC", (ref["entity_key"],))
    if open_lead:
        await leads.update(open_lead["id"], {"status": "NOT_TARGET", "lost_reason": "blacklist", "closed_at": now_iso()})
        await leads.update_group_card(await leads.get(open_lead["id"]), f"⛔ В чёрном списке · {mention(me)}")
    await message.answer(f"⛔ {h(ref['entity_key'])} в чёрном списке: {h(reason)}")
