"""Админ-панель владельца: роутер собирается из разделов (был один файл на 977 строк)."""
from aiogram import Router

from app.handlers.admin import ai, blacklist, channels, dashboard, donors, dnc, employees, keys, settings

router = Router(name="admin")
router.include_routers(
    dashboard.router,
    employees.router,
    keys.router,
    dnc.router,
    donors.router,
    blacklist.router,
    settings.router,
    ai.router,
    channels.router,
)
