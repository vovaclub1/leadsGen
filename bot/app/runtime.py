"""Единственное место, где живут объекты процесса: экземпляр бота и клиент сканера."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot

bot: "Bot | None" = None
