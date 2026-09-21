"""Настройки бота: просмотр и правка параметров ключом значением."""
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app import settings_store as st
from app.callbacks import AdmCb
from app.filters import OWNER
from app.handlers.helpers import show as _show
from app.keyboards import settings_kb
from app.utils import h

router = Router(name="admin-settings")


class EditSetting(StatesGroup):
    kv = State()


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
