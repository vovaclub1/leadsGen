"""Проверка клиента Trustat на поддельном API, собранном строго по docs.trustat.me.

Ответы здесь повторяют реальные схемы: конверт {"status","response"}, PostSummary без поля link,
usage/info со строками «использовано/лимит», заголовки X-Quota-* и Retry-After.
"""
import asyncio
import sys

import httpx

from app.db import db
from app.services import keypool, search_poller, trustat
from app.utils import now_iso

OK = []


def check(name: str, condition: bool, got=None) -> None:
    OK.append(condition)
    print(("  ok  " if condition else "ФЕЙЛ ") + name + ("" if condition else f"  → получено: {got!r}"))


class Reply:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"status": "ok", "response": {}}
        self.headers = headers or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


CALLS: list[tuple[str, dict]] = []
ROUTES: dict[str, list[Reply]] = {}


async def fake_get(self, url, params=None, headers=None):
    path = url.split("/public/v1", 1)[-1]
    CALLS.append((path, params or {}))
    queue = ROUTES.get(path)
    if not queue:
        return Reply(404, {"status": "error", "error": {"code": "not_found"}})
    return queue.pop(0) if len(queue) > 1 else queue[0]


httpx.AsyncClient.get = fake_get


def envelope(response):
    return {"status": "ok", "response": response}


async def main() -> None:
    await db.connect()
    await keypool.add_key("secret-key-1234", "тест", "stat,search", 0, 500, "pro")
    key_id = 1

    # --- разбор строк квоты ---
    check("«120/500» разбирается", trustat._parse_quota("120/500") == (120, 500), trustat._parse_quota("120/500"))
    check("безлимит «7/-» не ломает", trustat._parse_quota("7/-") == (7, 0), trustat._parse_quota("7/-"))
    check("мусор игнорируется", trustat._parse_quota("нет данных") is None)

    # --- заголовки квоты синхронизируют счётчики ---
    ROUTES["/channels/durov"] = [Reply(200, envelope({"username": "durov", "participants": 10}), {"X-Quota-Requests": "498/500"})]
    payload, error = await trustat.channel_info("@durov")
    key = await keypool.get_key(key_id)
    check("карточка канала получена", payload and payload.get("username") == "durov", (payload, error))
    check("расход подтянут из заголовка", key["used_month"] == 498, key["used_month"])

    # На остатке 2 из 500 пул уходит в резерв: последние 20% квоты бережём под приоритетные вызовы.
    ROUTES["/channels/reserve"] = [Reply(200, envelope({"username": "reserve"}))]
    payload, error = await trustat.channel_info("reserve")
    check("остаток квоты бережётся под приоритет", payload is None and error == "no_key", (payload, error))
    payload, _ = await trustat.channel_info("reserve", priority=True)
    check("приоритетный вызов резерв проходит", payload and payload.get("username") == "reserve", payload)

    await db.execute("UPDATE api_keys SET used_month = 0 WHERE id = ?", (key_id,))
    ROUTES["/channels/durov"] = [Reply(200, envelope({"username": "durov"}), {"X-Quota-Requests": "500/500"})]
    await trustat.channel_info("durov")
    key = await keypool.get_key(key_id)
    check("исчерпанный ключ выключается", key["status"] == "exhausted", key["status"])
    await keypool.set_status(key_id, "active")
    await db.execute("UPDATE api_keys SET used_month = 0 WHERE id = ?", (key_id,))

    # --- 429 ждёт столько, сколько сказал сервер ---
    ROUTES["/channels/x"] = [Reply(429, {}, {"Retry-After": "0"}), Reply(200, envelope({"username": "x"}))]
    payload, _ = await trustat.channel_info("x")
    check("после 429 запрос повторяется", payload and payload.get("username") == "x", payload)

    # --- 503 не сжигает ключ ---
    ROUTES["/channels/y"] = [Reply(503, {}), Reply(200, envelope({"username": "y"}))]
    payload, _ = await trustat.channel_info("y")
    key = await keypool.get_key(key_id)
    check("503 переживается без бана ключа", payload and key["status"] == "active", (payload, key["status"]))

    # --- ссылки со слэшами идут через /lookup ---
    CALLS.clear()
    ROUTES["/lookup"] = [Reply(200, envelope({"results": [{"channel_id": 777, "username": "adshop"}]}))]
    ROUTES["/channels/777"] = [Reply(200, envelope({"username": "adshop"}))]
    payload, _ = await trustat.channel_info("t.me/adshop/1024")
    paths = [c[0] for c in CALLS]
    check("ссылка резолвится через lookup", paths == ["/lookup", "/channels/777"], paths)
    check("канал по ссылке найден", payload and payload.get("username") == "adshop", payload)

    CALLS.clear()
    ROUTES["/channels/simple"] = [Reply(200, envelope({"username": "simple"}))]
    await trustat.channel_info("@simple")
    check("обычный username не тратит lookup", [c[0] for c in CALLS] == ["/channels/simple"], [c[0] for c in CALLS])

    # --- параметры поиска в пределах документации ---
    CALLS.clear()
    ROUTES["/posts/search"] = [Reply(200, envelope({"total": 0, "limit": 100, "next_cursor": None, "posts": []}))]
    await trustat.posts_search("куплю рекламу", 1700000000, key_id=key_id, limit=500)
    params = CALLS[-1][1]
    check("limit прижат к потолку 100", params["limit"] == 100, params["limit"])
    check("ищем и в чатах тоже", params["peer_type"] == "all", params.get("peer_type"))
    check("репосты скрыты", params["hide_forwards"] == "true", params.get("hide_forwards"))

    # --- поиск превращает посты в лиды через батч каналов ---
    posts = [
        {"channel_id": 111, "message_id": 5, "post_id": "111_5", "text": "Ищу каналы для рекламы", "views": 900},
        {"channel_id": 111, "message_id": 6, "post_id": "111_6", "text": "Второй пост того же автора"},
        {"channel_id": 222, "message_id": 9, "post_id": "222_9", "text": "Куплю размещение"},
    ]
    ROUTES["/posts/search"] = [Reply(200, envelope({"total": 3, "limit": 50, "next_cursor": None, "posts": posts}))]
    ROUTES["/channels/batch"] = [Reply(200, envelope({"requested": 2, "found": 2, "channels": [
        {"channel_id": 111, "username": "buyads", "title": "Закупка"},
        {"channel_id": 222, "username": "mediaplan", "title": "Медиаплан"},
    ]}))]
    await db.execute("INSERT INTO keywords (word, api_key_id, active, created_at) VALUES ('куплю рекламу', ?, 1, ?)", (key_id, now_iso()))
    row = await db.fetchone("SELECT * FROM keywords WHERE id = 1")
    created = await search_poller.run_keyword(row)
    check("из постов созданы лиды", created == 2, created)
    names = [r["username"] for r in await db.fetchall("SELECT username FROM leads ORDER BY username")]
    check("лиды — авторы постов", names == ["buyads", "mediaplan"], names)
    check("один автор считается один раз", len(names) == len(set(names)), names)
    batch_params = [p for path, p in CALLS if path == "/channels/batch"][-1]
    check("батч ушёл одним запросом", batch_params["ids"] == "111,222", batch_params["ids"])

    # --- без батча выручают ссылки из текста ---
    await db.execute("DELETE FROM leads")
    await db.execute("UPDATE keywords SET last_run = NULL WHERE id = 1")
    ROUTES["/posts/search"] = [Reply(200, envelope({"posts": [
        {"channel_id": 333, "text": "Нужна реклама, пишите t.me/backup_lead"},
    ]}))]
    ROUTES["/channels/batch"] = [Reply(426, {"status": "error", "error": {"code": "quota"}})]
    row = await db.fetchone("SELECT * FROM keywords WHERE id = 1")
    created = await search_poller.run_keyword(row)
    fallback = [r["username"] for r in await db.fetchall("SELECT username FROM leads")]
    check("при отказе батча берём ссылку из текста", created == 1 and fallback == ["backup_lead"], (created, fallback))

    # --- голый @упоминание в тексте — тоже контакт ---
    await keypool.set_status(key_id, "active")  # 426 из прошлого сценария исчерпал ключ
    await db.execute("UPDATE api_keys SET used_month = 0 WHERE id = ?", (key_id,))
    await db.execute("DELETE FROM leads")
    await db.execute("UPDATE keywords SET last_run = NULL WHERE id = 1")
    ROUTES["/posts/search"] = [Reply(200, envelope({"posts": [
        {"channel_id": 444, "text": "Ищу каналы для посевов, предложения @game_studio"},
    ]}))]
    row = await db.fetchone("SELECT * FROM keywords WHERE id = 1")
    created = await search_poller.run_keyword(row)
    fallback = [r["username"] for r in await db.fetchall("SELECT username FROM leads")]
    check("голый @username вытаскивается из текста", created == 1 and fallback == ["game_studio"], (created, fallback))

    # --- пагинация: вторая страница не теряется ---
    await keypool.set_status(key_id, "active")
    await db.execute("UPDATE api_keys SET used_month = 0 WHERE id = ?", (key_id,))
    await db.execute("DELETE FROM leads")
    await db.execute("UPDATE keywords SET last_run = NULL WHERE id = 1")
    ROUTES["/posts/search"] = [
        Reply(200, envelope({"posts": [{"channel_id": 551, "text": "страница один"}], "next_cursor": "page2"})),
        Reply(200, envelope({"posts": [{"channel_id": 552, "text": "страница два"}], "next_cursor": None})),
    ]
    ROUTES["/channels/batch"] = [Reply(200, envelope({"channels": [
        {"channel_id": 551, "username": "lead_p1"},
        {"channel_id": 552, "username": "lead_p2"},
    ]}))]
    row = await db.fetchone("SELECT * FROM keywords WHERE id = 1")
    created = await search_poller.run_keyword(row)
    pages = [p for path, p in CALLS if path == "/posts/search"][-2:]  # CALLS копится с начала файла
    names = sorted(r["username"] for r in await db.fetchall("SELECT username FROM leads"))
    check("обе страницы выдачи обработаны", created == 2 and names == ["lead_p1", "lead_p2"], (created, names))
    check("курсор передан во второй запрос", len(pages) == 2 and pages[1].get("cursor") == "page2", pages)

    # --- usage/info: строки, а не числа ---
    ROUTES["/usage/info"] = [Reply(200, envelope({
        "plan": "pro", "period": "2026-09", "spent_requests": "120/500",
        "spent_unique_channels": "40/200", "spent_listing": "0/1000",
        "rps": 5, "scopes": ["channels", "stats", "posts"],
    }))]
    info, error = await trustat.probe_key("secret-key-1234")
    check("лимит вынут из строки", info and info["limit_month"] == 500, info)
    check("пакеты определены по scopes", info and info["packages"] == "stat,search", info and info.get("packages"))
    text = trustat.format_usage(info)
    check("в сводке виден остаток", "осталось 380" in text, text)
    check("в сводке видны каналы", "уникальные каналы: 40 из 200" in text, text)

    ROUTES["/usage/info"] = [Reply(200, envelope({"plan": "free", "scopes": ["stats"]}))]
    info, _ = await trustat.probe_key("secret-key-1234")
    check("ключ только со статистикой → пакет stat", info["packages"] == "stat", info["packages"])

    # --- поля статистики совпадают со схемой ChannelStat ---
    stat = {"participants": 125000, "views24": 31000, "er24": 24, "month_growth": 4200, "quality_score": 78.5}
    line = trustat.format_stat(stat)
    check("статистика читается человеком", "125 000" in line and "ER24 24%" in line and "качество 78.5" in line, line)

    print()
    print("TRUSTAT OK" if all(OK) else f"ЕСТЬ ОШИБКИ: {OK.count(False)}")


async def run() -> int:
    try:
        await main()
        return 0 if all(OK) else 1
    finally:
        if db.conn:
            await db.conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
