"""Общие хелперы ответов: цель может быть callback'ом или обычным сообщением."""
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup


async def reply(target, text: str, alert: bool = False, **kwargs) -> None:
    """Ответить в callback (alert или новым сообщением) либо обычным answer."""
    if isinstance(target, CallbackQuery):
        if alert:
            await target.answer(text, show_alert=True)
        else:
            await target.message.answer(text, **kwargs)
            await target.answer()
    else:
        await target.answer(text, **kwargs)


async def show(target, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Отредактировать сообщение с кнопками, если это callback, иначе отправить новое."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb)
        except TelegramAPIError:
            await target.message.answer(text, reply_markup=kb)
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb)
