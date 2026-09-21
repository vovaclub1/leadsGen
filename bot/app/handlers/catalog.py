"""Каталог каналов MORIER и разрешённая вилка цен (ТЗ 8.1): /catalog, /price."""
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.filters import SELLERS
from app.services import channels, leads

router = Router(name="catalog")

# Обратная карта: «Brawl Stars» → brawl. Живёт здесь, а не в services/channels,
# чтобы сервис не тянул зависимости жизненного цикла лидов.
VERT_BY_NAME = {name.lower(): key for key, name in leads.VERTICAL_RU.items()}


def resolve_vertical(raw: str | None) -> str | None:
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    if raw in leads.VERTICAL_RU:
        return raw
    return VERT_BY_NAME.get(raw)


def known_verticals() -> str:
    return ", ".join(leads.VERTICAL_RU[key] for key in leads.VERTICAL_RU)


@router.message(Command("catalog"), SELLERS)
async def catalog(message: Message, command: CommandObject, me: dict) -> None:
    vertical = resolve_vertical(command.args)
    if not vertical:
        await message.answer(f"Формат: /catalog <вертикаль>. Вертикали: {known_verticals()}.")
        return
    rows = await channels.list_vertical(vertical, include_c=me["role"] == "owner")
    if not rows:
        await message.answer(f"Каналов MORIER в вертикали «{leads.VERTICAL_RU.get(vertical, vertical)}» пока нет. Попросите старшего заполнить: админка → Каналы MORIER.")
        return
    is_owner = me["role"] == "owner"
    lines = [f"<b>Каталог MORIER · {leads.VERTICAL_RU.get(vertical, vertical)}</b>"]
    lines.extend(channels.format_channel(row, is_owner) for row in rows)
    if not is_owner:
        lines.append("\nТочную цену называет старший — вам разрешена вилка: /price " + vertical)
    await message.answer("\n".join(lines))


@router.message(Command("price"), SELLERS)
async def price(message: Message, command: CommandObject) -> None:
    vertical = resolve_vertical(command.args)
    if not vertical:
        await message.answer(f"Формат: /price <вертикаль>. Вертикали: {known_verticals()}.")
        return
    rng = await channels.price_range(vertical)
    if not rng:
        await message.answer("Вилка ещё не задана: в каталоге вертикали нет каналов с ценой.")
        return
    await message.answer(
        f"<b>Разрешённая вилка · {leads.VERTICAL_RU.get(vertical, vertical)}</b>\n"
        f"{rng[0]:,} – {rng[1]:,} ₽".replace(",", " ")
        + "\nЭто диапазон из каталога, а не точная цена. Точную цифру называет старший после проверки — SDR цену не называет."
    )
