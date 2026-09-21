"""Одноразовый вход аккаунта-сканера (MTProto). Запускать на сервере вручную: python login_scanner.py

Создаёт data/scanner.session. Используйте ОТДЕЛЬНЫЙ аккаунт, не личный: аккаунт будет
состоять во всех каналах-донорах и читать их посты. Бан этого аккаунта не затронет бота.
"""
import asyncio

from app.config import config


async def main() -> None:
    if not config.tg_api_id or not config.tg_api_hash:
        raise SystemExit("Заполните TG_API_ID и TG_API_HASH в .env (my.telegram.org → API development tools).")
    from telethon import TelegramClient

    client = TelegramClient(str(config.scanner_session), config.tg_api_id, config.tg_api_hash)
    await client.start()
    me = await client.get_me()
    print(f"Готово. Сканер авторизован как @{me.username or me.id}. Сессия: {config.scanner_session}.session")
    print("Теперь запускайте бота: python main.py — доноры из /admin → Доноры будут добавлены автоматически.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
