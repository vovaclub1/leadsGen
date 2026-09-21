"""Чёрный список сущностей: каналы и сайты, которые никогда не становятся лидами."""
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.callbacks import AdmCb
from app.db import db
from app.filters import OWNER
from app.handlers.helpers import show as _show
from app.keyboards import blacklist_kb
from app.services import leads
from app.utils import now_iso

router = Router(name="admin-blacklist")


class AddBlacklist(StatesGroup):
    text = State()


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
