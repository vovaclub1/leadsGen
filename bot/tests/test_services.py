"""Проверка пула ключей, Trustat-клиента и разбора ответа ИИ без сети.

Запуск:  BOT_TOKEN=1:x OWNER_ID=1 DATA_DIR=/tmp/lh-svc python -m tests.test_services
"""
import asyncio
import sys

import httpx

from app.db import db
from app.services import keypool, trustat
from app.services import draft_check, scoring
from app.services.ai import _parse_json


class FakeTrustat:
    """Имитация api-public.trustat.me: по ключу решаем, что ответить."""

    def __init__(self):
        self.hits: list[tuple[str, str]] = []
        self.behaviour: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("X-API-Key", "")
        self.hits.append((key, request.url.path))
        status = self.behaviour.get(key, 200)
        if status == 200:
            if request.url.path.endswith("/stat"):
                return httpx.Response(200, json={"response": {"participants": 12000, "views24": 3400, "er24": 28.3, "month_growth": 512, "quality_score": 81}})
            if request.url.path.endswith("/usage/info"):
                # Формат по docs.trustat.me: расход приходит строкой «использовано/лимит».
                return httpx.Response(200, json={"response": {
                    "plan": "free", "period": "2026-09", "spent_requests": "3/50",
                    "spent_unique_channels": "1/10", "rps": 2, "scopes": ["channels", "stats"],
                }})
            if request.url.path.endswith("/posts/search"):
                # PostSummary не содержит ссылки — только channel_id и message_id.
                return httpx.Response(200, json={"response": {"total": 1, "limit": 20, "next_cursor": None, "posts": [
                    {"channel_id": 4242, "message_id": 15, "post_id": "4242_15", "text": "ищу каналы для рекламы"},
                ]}})
            return httpx.Response(200, json={"response": {"title": "X", "username": "x", "participants": 1}})
        if status == 426:
            return httpx.Response(426, json={"error": {"code": "quota_exceeded"}})
        if status == 401:
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})
        if status == 429:
            return httpx.Response(429, json={"error": {"code": "rate_limit"}})
        return httpx.Response(status, json={})


async def main():
    await db.connect()
    fake = FakeTrustat()
    transport = httpx.MockTransport(fake.handler)

    original_client = httpx.AsyncClient

    class PatchedClient(original_client):
        def __init__(self, *a, **k):
            k["transport"] = transport
            super().__init__(*a, **k)

    httpx.AsyncClient = PatchedClient
    trustat.httpx.AsyncClient = PatchedClient

    # --- 1. Добавление ключей, шифрование, хвост
    k1 = await keypool.add_key("KEY-ONE-AAAA", "первый", "stat", 10, 50, "free")
    k2 = await keypool.add_key("KEY-TWO-BBBB", "второй", "stat", 10, 50, "free")
    k3 = await keypool.add_key("KEY-SEARCH-CCCC", "поиск", "search", 0, 100, "free")
    rows = await keypool.list_keys()
    assert [r["key_tail"] for r in rows] == ["AAAA", "BBBB", "CCCC"], rows
    assert all(r["key_enc"] != "KEY-ONE-AAAA" for r in rows), "ключ лежит в БД открытым текстом"
    assert keypool.secret(rows[0]) == "KEY-ONE-AAAA"
    print("ok: ключи добавлены и зашифрованы")

    # --- 2. Обычный запрос идёт через ключ с наибольшим остатком, счётчики растут
    payload, err = await trustat.channel_stat("@cool_shop")
    assert err is None and payload["participants"] == 12000, (payload, err)
    used = await db.fetchone("SELECT used_day, used_month FROM api_keys WHERE id = ?", (k1,))
    assert (used["used_day"], used["used_month"]) == (1, 1), used
    print("ok: stat через пул, счётчики:", dict(used))

    # --- 3. 426 у первого ключа → он exhausted, запрос прозрачно уходит на второй
    # (у второго искусственно меньше остатка, чтобы пул выбрал первый)
    await db.execute("UPDATE api_keys SET used_month = 5 WHERE id = ?", (k2,))
    fake.behaviour["KEY-ONE-AAAA"] = 426
    payload, err = await trustat.channel_stat("@cool_shop")
    assert err is None and payload, (payload, err)
    s1 = await db.fetchone("SELECT status, last_error FROM api_keys WHERE id = ?", (k1,))
    assert s1["status"] == "exhausted", s1
    assert fake.hits[-1][0] == "KEY-TWO-BBBB", fake.hits[-2:]
    print("ok: 426 → переключение на второй ключ, первый:", dict(s1))

    # --- 4. 401 у второго → banned, пул пуст → ошибка без падения
    fake.behaviour["KEY-TWO-BBBB"] = 401
    payload, err = await trustat.channel_stat("@cool_shop")
    assert payload is None and err == "banned", (payload, err)
    s2 = await db.fetchone("SELECT status FROM api_keys WHERE id = ?", (k2,))
    assert s2["status"] == "banned"
    print("ok: 401 → ключ banned, пул отвечает ошибкой:", err)

    # --- 5. Резерв 20%: при остатке ≤ 20% обычные запросы не идут, приоритетные — идут
    await db.execute("UPDATE api_keys SET status='active', used_day=0, used_month=45 WHERE id = ?", (k1,))
    fake.behaviour.pop("KEY-ONE-AAAA")
    normal = await keypool.pick("stat")
    priority = await keypool.pick("stat", priority=True)
    assert normal is None and priority is not None, (normal, priority)
    print("ok: резерв квоты — обычный запрос отклонён, приоритетный пропущен")

    # --- 6. 429 → пауза и повтор на том же ключе
    await db.execute("UPDATE api_keys SET used_month=0 WHERE id = ?", (k1,))
    calls = {"n": 0}
    real_handler = fake.handler

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={})
        return real_handler(request)

    transport_flaky = httpx.MockTransport(flaky)
    trustat.httpx.AsyncClient = type("C", (original_client,), {"__init__": lambda self, *a, **k: original_client.__init__(self, *a, **{**k, "transport": transport_flaky})})
    payload, err = await trustat.channel_stat("@cool_shop")
    assert err is None and calls["n"] == 2, (err, calls)
    trustat.httpx.AsyncClient = PatchedClient
    print("ok: 429 → повтор, успех со второй попытки")

    # --- 7. Search-ключ не используется для stat и наоборот
    assert await keypool.pick("search", priority=True) is not None
    assert (await keypool.pick("search", priority=True))["id"] == k3
    payload, err = await trustat.posts_search("ищу каналы для рекламы", 0, key_id=k3)
    assert err is None and payload["posts"][0]["channel_id"] == 4242, (payload, err)
    print("ok: search по своему ключу")

    # --- 8. Проверка ключа при добавлении
    info, err = await trustat.probe_key("KEY-ONE-AAAA")
    assert err is None and info["plan"] == "free", (info, err)
    assert info["limit_month"] == 50 and info["packages"] == "stat", info
    print("ok: probe_key →", info)

    # --- 9. Сводка пула
    summary = await keypool.pool_summary()
    print("ok: сводка:", summary)
    assert summary["stat"][2] >= 1 and summary["search"][2] == 1

    # --- 10. Разбор JSON от ИИ (с ```json``` и с мусором вокруг)
    assert _parse_json('```json\n{"is_target": true, "score_hint": 80}\n```')["is_target"] is True
    assert _parse_json('Вот ответ: {"is_ad": false, "confidence": 0.2} спасибо')["is_ad"] is False
    assert _parse_json("не json") is None
    print("ok: разбор JSON ИИ")

    # --- 11. Эвристический скоринг и чек-лист черновика
    lead = {"kind": "tg", "subscribers": 25000, "avg_views": 6000, "about": "Магазин ключей Steam, гифты, пополнение. Реклама: @ads_manager", "source": "scanner", "username": "steam_keys"}
    score = scoring.compute(lead, None, ad_count=2)
    cat = await scoring.category(score)
    print("ok: score без ИИ:", score, cat)
    assert 0 <= score <= 100 and cat in ("hot", "warm", "cold")
    problems, good = await draft_check.check(
        "Здравствуйте! Мы агентство MORIER, предлагаем рекламу от 5000 руб. https://morier.ru https://t.me/x https://x.ru", lead
    )
    print("ok: чек-лист — поправить:", problems)
    joined = " ".join(problems).lower()
    assert "цен" in joined and "ссыл" in joined, problems

    await db.conn.close()
    print("\nSERVICES OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print("FAIL:", exc)
        sys.exit(1)
