"""Проверка клиента ИИ без обращения к настоящему API: ошибки, ретраи, расчёт цены, картинки, миграции.

Запуск:  BOT_TOKEN=1:x OWNER_ID=1 AI_API_KEY=sk-test DATA_DIR=/tmp/lh-ai python -m tests.test_ai
"""
import asyncio
import json
import os
import sys

import httpx

from app import settings_store as st
from app.config import _clean_base_url, config
from app.db import db
from app.services.ai import _parse_json, _tiny_png, ai
from app.utils import now_iso

CALLS: list[dict] = []
SCRIPT: list[tuple[int, dict | str]] = []


class FakeResponse:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.headers = {}

    @property
    def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


async def fake_post(self, url, json=None, headers=None):  # noqa: A002
    CALLS.append({"url": url, "body": json, "headers": headers})
    status, body = SCRIPT.pop(0) if SCRIPT else (200, ok_body("{}"))
    return FakeResponse(status, body)


def ok_body(content: str, prompt=1000, cached=200, completion=50) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": prompt,
            "prompt_cache_hit_tokens": cached,
            "prompt_cache_miss_tokens": prompt - cached,
            "completion_tokens": completion,
        },
    }


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        print(f"FAIL: {label} {detail}")
        sys.stdout.flush()
        os._exit(1)  # aiosqlite держит поток, обычный выход из корутины подвис бы
    print(f"ok: {label}{(' — ' + detail) if detail else ''}")


async def main() -> None:
    httpx.AsyncClient.post = fake_post
    await db.connect()
    await st.set_value("ai_monthly_cap_usd", "100")

    # --- миграция старой базы: колонки, которых не было в первой версии схемы ---
    cur = await db.conn.execute("PRAGMA table_info(keywords)")
    columns = {row[1] for row in await cur.fetchall()}
    await cur.close()
    check("миграция keywords.created_at", "created_at" in columns)
    await db.execute(
        "INSERT INTO keywords (word, api_key_id, active, created_at) VALUES (?, 1, 1, ?)", ("ищу рекламу", now_iso())
    )
    check("ключевое слово сохраняется", bool(await db.fetchone("SELECT 1 FROM keywords WHERE word = 'ищу рекламу'")))

    # --- нормализация base_url и подмена снятой модели ---
    # Проверяем саму нормализацию, а не переменную окружения: AI_BASE_URL в команде запуска теста нет.
    check("из base_url убран /chat/completions", _clean_base_url("https://api.deepseek.com/v1/chat/completions") == "https://api.deepseek.com/v1", _clean_base_url("https://api.deepseek.com/v1/chat/completions"))
    check("пустой base_url → адрес DeepSeek по умолчанию", _clean_base_url("") == "https://api.deepseek.com", config.ai_base_url)
    check("модель актуальная", config.ai_model == "deepseek-flash", config.ai_model)

    # --- успешный запрос: расчёт цены по кэшу и выходным токенам ---
    SCRIPT.append((200, ok_body('{"is_target": true}')))
    data = await ai.chat_json("sys", "user", purpose="analyze")
    check("json разобран", data == {"is_target": True}, str(data))
    row = await db.fetchone("SELECT * FROM ai_usage ORDER BY id DESC LIMIT 1")
    expected = 800 / 1e6 * config.ai_price_in + 200 / 1e6 * config.ai_price_cached + 50 / 1e6 * config.ai_price_out
    check("цена по кэш-хитам", abs(row["cost_usd"] - expected) < 1e-9, f"{row['cost_usd']:.8f} vs {expected:.8f}")
    check("режим размышления выключен", CALLS[-1]["body"].get("thinking") == {"type": "disabled"})

    # --- ответ в ```json ... ``` ---
    SCRIPT.append((200, ok_body('```json\n{"is_ad": true, "advertiser": "shop"}\n```')))
    data = await ai.classify_ad("текст рекламы", "donor")
    check("markdown-обёртка снимается", data == {"is_ad": True, "advertiser": "shop"}, str(data))

    # --- 429 → повтор и успех ---
    SCRIPT.extend([(429, "rate limit"), (200, ok_body('{"ok": true}'))])
    before = len(CALLS)
    data = await ai.chat_json("sys", "user", purpose="analyze")
    check("429 → повтор", data == {"ok": True} and len(CALLS) - before == 2, f"запросов: {len(CALLS) - before}")

    # --- понятные сообщения об ошибках ---
    for status, body, needle in (
        (401, "Authentication Fails", "ключ не принят"),
        (402, "Insufficient Balance", "нет средств"),
        (400, '{"error":{"message":"Model Not Exist"}}', "не найдена"),
    ):
        SCRIPT.append((status, body))
        await ai.chat_json("sys", "user", purpose="analyze")
        check(f"ошибка {status} по-русски", needle in (ai.last_error or ""), ai.last_error or "")

    failed = await db.fetchone("SELECT * FROM ai_usage WHERE ok = 0 ORDER BY id DESC LIMIT 1")
    check("неудачные запросы пишутся в журнал", failed is not None and bool(failed["error"]))

    # --- 5xx исчерпывает попытки и не зацикливается ---
    SCRIPT.extend([(500, "boom"), (500, "boom"), (500, "boom")])
    before = len(CALLS)
    await ai.chat_json("sys", "user", purpose="analyze")
    check("5xx — ровно 3 попытки", len(CALLS) - before == 3, f"{len(CALLS) - before}")

    # --- шлюз без поддержки thinking ---
    ai._send_thinking = True
    SCRIPT.extend([(400, "Unknown parameter: thinking"), (200, ok_body('{"ok": true}'))])
    data = await ai.chat_json("sys", "user", purpose="analyze")
    check("параметр thinking снимается", data == {"ok": True} and "thinking" not in CALLS[-1]["body"])

    # --- картинки ---
    ai._send_thinking = True
    check("зрение включено по умолчанию", await ai.vision_enabled())
    SCRIPT.append((200, ok_body("Магазин ключей Steam, на баннере @keysshop и промокод GG20")))
    note = await ai.read_image(_tiny_png(), caption="скидки")
    check("картинка прочитана", note is not None and "keysshop" in note, str(note))
    body = CALLS[-1]["body"]
    blocks = body["messages"][-1]["content"]
    check("картинка ушла как image_url", any(b.get("type") == "image_url" for b in blocks))
    check("картинка в base64 data-url", blocks[-1]["image_url"]["url"].startswith("data:image/png;base64,"))
    check("зрение использует свою модель", body["model"] == config.ai_vision_model, body["model"])

    SCRIPT.append((200, ok_body("Без текста и бренда")))
    check("пустой креатив отбрасывается", await ai.read_image(_tiny_png()) is None)

    await st.set_value("ai_vision", "0")
    check("выключенное зрение не тратит запросы", await ai.read_image(_tiny_png()) is None)
    await st.set_value("ai_vision", "1")

    await st.set_value("ai_vision_daily_cap", "1")
    check("дневной лимит картинок работает", await ai.read_image(_tiny_png()) is None)
    await st.set_value("ai_vision_daily_cap", "300")

    # --- самопроверка ---
    SCRIPT.extend([(200, ok_body('{"ok": true}')), (200, ok_body('{"seen": true}'))])
    result = await ai.self_test(with_vision=True)
    check("самопроверка проходит", result["ok"] and result.get("vision_ok"), str(result))

    SCRIPT.append((401, "Authentication Fails"))
    result = await ai.self_test()
    check("самопроверка ловит плохой ключ", not result["ok"] and "ключ не принят" in result["error"], str(result))

    # --- месячный лимит ---
    await st.set_value("ai_monthly_cap_usd", "0.0000001")
    before = len(CALLS)
    check("лимит расхода останавливает запросы", await ai.chat_json("s", "u", purpose="analyze") is None and len(CALLS) == before)
    await st.set_value("ai_monthly_cap_usd", "100")

    check("мусор вместо json не ломает разбор", _parse_json("привет, я не json") is None)
    check("json в середине текста находится", _parse_json('Вот ответ: {"a": 1} готово') == {"a": 1})

    breakdown = await ai.usage_breakdown()
    check("сводка расхода по задачам", any(r["purpose"] == "vision" for r in breakdown), str([r["purpose"] for r in breakdown]))
    print("\nAI OK")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())
