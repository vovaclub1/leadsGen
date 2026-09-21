"""Каналы MORIER (ТЗ 8.1): таблица каналов агентства — каталог, вилка цен, флаги тематик."""
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.callbacks import AdmCb
from app.db import db
from app.filters import SENIOR_UP
from app.handlers.catalog import known_verticals, resolve_vertical
from app.handlers.helpers import show as _show
from app.keyboards import channel_item_kb, channels_kb
from app.services import channels, leads
from app.utils import fmt_date, h

router = Router(name="admin-channels")

ADD_FORMAT = (
    "@канал | вертикаль | подписчики | охват/24ч | цена_от-цена_до | закуп | A/B/C | флаги | админ\n"
    "Например: <code>@brawl_market | brawl | 45000 | 12000 | 3000-8000 | 1800 | A | betting,casino | @admin_brawl</code>\n"
    "Вертикали: {verticals}. Флаги (что канал принимает): betting, casino, crypto, adult, vpn. "
    "Последние поля можно опускать. Один канал — одна строка."
)


class AddChannel(StatesGroup):
    text = State()


async def channels_view(target, note: str = "") -> None:
    rows = await db.fetchall("SELECT * FROM morier_channels ORDER BY vertical, category, subscribers DESC")
    lines = [
        "<b>📺 Каналы MORIER</b>",
        "Каталог для /catalog и /price, матрица размещаемости в передачах. Закупочную цену видит только владелец.",
    ]
    if note:
        lines.append(note)
    if rows:
        lines.append("\n".join(f"• @{h(r['username'])} — {leads.VERTICAL_RU.get(r['vertical'], r['vertical'])} · {r['category']} · стата {fmt_date(r['stat_updated_at'])} · /mch_{r['id']}" for r in rows))
    else:
        lines.append("Пока пусто — добавьте первый канал.")
    await _show(target, "\n".join(lines), channels_kb(rows))


@router.callback_query(AdmCb.filter((F.s == "mch") & (F.a == "list")), SENIOR_UP)
async def channels_list(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await channels_view(query)


@router.callback_query(AdmCb.filter((F.s == "mch") & (F.a == "add")), SENIOR_UP)
async def channel_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddChannel.text)
    await query.message.answer("Формат одной строки:\n" + ADD_FORMAT.format(verticals=known_verticals()))
    await query.answer()


def _num(parts: list[str], index: int) -> int | None:
    if len(parts) <= index:
        return None
    digits = re.sub(r"[^\d]", "", parts[index])
    return int(digits) if digits else None


def parse_channel_line(line: str) -> dict | None:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 2:
        return None
    ref = leads.parse_ref(parts[0])
    vertical = resolve_vertical(parts[1])
    if not ref or not ref.get("username") or not vertical:
        return None
    fields: dict = {"username": ref["username"].lower(), "vertical": vertical}
    fields["subscribers"] = _num(parts, 2)
    fields["reach_24h"] = _num(parts, 3)
    if len(parts) > 4 and parts[4]:
        match = re.match(r"(\d+)\s*[-–—]\s*(\d+)", parts[4].replace(" ", ""))
        if match:
            fields["price_from"], fields["price_to"] = int(match.group(1)), int(match.group(2))
    fields["buy_price"] = _num(parts, 5)
    if len(parts) > 6 and parts[6].upper() in channels.CATEGORIES:
        fields["category"] = parts[6].upper()
    if len(parts) > 7 and parts[7]:
        fields["flags"] = ",".join(flag for flag in (p.strip().lower() for p in parts[7].split(",")) if flag in channels.FLAGS_RU)
    if len(parts) > 8 and parts[8]:
        fields["admin_contact"] = parts[8][:60]
    return fields


@router.message(AddChannel.text, SENIOR_UP, F.text)
async def channel_save(message: Message, state: FSMContext) -> None:
    await state.clear()
    added, failed = 0, []
    for line in message.text.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = parse_channel_line(line)
        if not fields:
            failed.append(line[:40])
            continue
        await channels.upsert(fields)
        added += 1
    text = f"Каналов добавлено/обновлено: {added}."
    if failed:
        text += "\nНе разобрал (нужны @канал и вертикаль): " + "; ".join(h(f) for f in failed[:5])
    await channels_view(message, text)


@router.message(F.text.regexp(r"^/mch_(\d+)$").as_("match"), SENIOR_UP)
async def channel_open(message: Message, me: dict, match) -> None:
    row = await db.fetchone("SELECT * FROM morier_channels WHERE id = ?", (int(match.group(1)),))
    if not row:
        await message.answer("Канал удалён.")
        return
    await message.answer(channels.format_channel(row, me["role"] == "owner"), reply_markup=channel_item_kb(row["id"]))


@router.callback_query(AdmCb.filter((F.s == "mch") & (F.a == "del")), SENIOR_UP)
async def channel_delete(query: CallbackQuery, callback_data: AdmCb) -> None:
    await channels.delete(callback_data.id)
    await query.answer("Удалён.")
    await channels_view(query)
