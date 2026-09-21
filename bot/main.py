"""Точка входа MORIER LeadHunter: бот, доступ по белому списку, планировщик, сканер каналов."""
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from app import runtime
from app.config import config
from app.db import db
from app.handlers import admin, common, leads, senior
from app.middleware import AccessMiddleware
from app.scheduler import scheduler_loop
from app.services.keypool import roll_periods
from app.services.scanner import scanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("leadhunter")

COMMON_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="queue", description="Очередь лидов"),
    BotCommand(command="my", description="Мои лиды"),
    BotCommand(command="me", description="Мои баллы и статистика"),
    BotCommand(command="top", description="Рейтинг команды"),
    BotCommand(command="add", description="Добавить лид вручную"),
    BotCommand(command="check", description="Проверить контакт по красному списку"),
    BotCommand(command="find", description="Карточка канала по @username"),
    BotCommand(command="help", description="Как работать"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]
OWNER_COMMANDS = [
    BotCommand(command="admin", description="Панель владельца"),
    BotCommand(command="handoffs", description="Передачи от SDR"),
    *COMMON_COMMANDS,
]


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(COMMON_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    try:
        await bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=config.owner_id))
    except Exception as exc:  # noqa: BLE001 — владелец ещё не открывал бота, Telegram не знает этот чат
        log.info("Команды владельца выставим после первого /start: %s", exc)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    access = AccessMiddleware()
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    # Порядок важен: у leads есть широкий перехват ссылок в личке, поэтому он последний.
    dp.include_routers(common.router, admin.router, senior.router, leads.router)
    return dp


async def on_startup(bot: Bot) -> None:
    await db.connect()
    await roll_periods()
    me = await bot.get_me()
    runtime.bot = bot
    log.info("Бот @%s запущен. Владелец: %s. БД: %s", me.username, config.owner_id, config.db_path)
    await set_commands(bot)
    await scanner.start()
    asyncio.create_task(scheduler_loop(), name="scheduler")


async def on_shutdown() -> None:
    if scanner.client:
        await scanner.client.disconnect()
    if db.conn:
        await db.conn.close()
    log.info("Остановлен.")


async def main() -> None:
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "my_chat_member"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
