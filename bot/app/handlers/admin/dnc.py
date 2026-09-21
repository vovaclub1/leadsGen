"""Красный список контактов: уровни red/orange/yellow, добавление и снятие флагов."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, MessageOriginUser

from app.callbacks import AdmCb
from app.db import db
from app.filters import SENIOR_UP
from app.handlers.helpers import show as _show
from app.keyboards import dnc_days_kb, dnc_item_kb, dnc_kb, dnc_level_kb
from app.services import dnc, leads
from app.utils import fmt_date, h

router = Router(name="admin-dnc")


class AddDnc(StatesGroup):
    ident = State()
    reason = State()


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
    await query.answer("Флаг снят.")
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
