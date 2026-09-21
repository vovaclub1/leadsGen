"""Экономный режим ИИ, обучение черновиков, недельный отчёт и автопоиск доноров — без сети.

Запуск:  BOT_TOKEN=1:x OWNER_ID=1 AI_API_KEY=sk-test DATA_DIR=/tmp/lh-growth python -m tests.test_growth
"""
import asyncio
import sys
import types

import httpx

from app import settings_store as st
from app.db import db
from app.keyboards import donor_hints_kb, donors_kb
from app.services import report
from app.services.ai import ai
from app.services.scanner import scanner
from app.utils import in_days, now_iso, season

PROMPTS: list[str] = []


class FakeResponse:
    status_code = 200
    headers: dict = {}
    text = ""

    def json(self) -> dict:
        return {
            "choices": [{"message": {"content": '{"verdict": "fix", "comment": "Начните с факта"}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


async def fake_post(self, url, json=None, headers=None):  # noqa: A002
    PROMPTS.append(json["messages"][-1]["content"])
    return FakeResponse()


class FakeChat:
    def __init__(self, username, title, subscribers, megagroup=False):
        self.username, self.title, self.participants_count, self.megagroup = username, title, subscribers, megagroup


class FakeClient:
    """Отдаёт рекомендации Telegram по донору, у которого спросили последним."""

    def __init__(self, recommendations: dict):
        self.recommendations = recommendations
        self.seed = None

    async def get_entity(self, username):
        self.seed = username
        return types.SimpleNamespace(username=username)

    async def __call__(self, request):
        return types.SimpleNamespace(chats=self.recommendations[self.seed])


async def main() -> None:
    httpx.AsyncClient.post = fake_post
    await db.connect()

    # --- экономный режим ---
    assert await ai.should_classify_ad(True, 1) is False, "пост с erid не должен идти в ИИ"
    assert await ai.should_classify_ad(False, 3) is False, "три промо-признака — реклама и без ИИ"
    assert await ai.should_classify_ad(False, 1) is True, "спорный пост обязан дойти до ИИ"
    print("ok: разметка рекламы не переспрашивает ИИ там, где и так ясно")

    assert await ai.should_analyze(40, 0) is False, "слабый одиночный лид считается формулами"
    assert await ai.should_analyze(60, 0) is True, "сильный лид идёт в ИИ"
    assert await ai.should_analyze(40, 2) is True, "повторный рекламодатель разбирается всегда"
    print("ok: слабые лиды мимо ИИ, повторные рекламодатели — всегда в ИИ")

    skipped = await ai.skipped_month()
    assert skipped.get("ad") == 2 and skipped.get("analyze") == 1, skipped
    print("ok: сэкономленные запросы посчитаны —", skipped)

    await st.set_value("ai_thrifty", "0")
    assert await ai.should_analyze(1, 0) is True and await ai.should_classify_ad(True, 9) is True
    await st.set_value("ai_thrifty", "1")
    print("ok: выключенный режим пропускает всё в ИИ")

    # --- черновик учится на удачных заходах ---
    assert await ai.winning_openers() == [], "на пустой базе образцов быть не может"
    await db.execute(
        "INSERT INTO leads (entity_key, kind, username, title, source, status, first_message, claimed_at, contacted_at, replied_at, created_at, updated_at) "
        "VALUES ('ch:w', 'channel', 'won', 'Победа', 'scanner', 'WON', 'Видел ваше размещение у Иванова — кто ведёт закупку?', ?, ?, ?, ?, ?)",
        (in_days(-3), in_days(-3), in_days(-1), in_days(-4), now_iso()),
    )
    await db.execute(
        "INSERT INTO leads (entity_key, kind, username, title, source, status, first_message, created_at, updated_at) "
        "VALUES ('ch:n', 'channel', 'new', 'Новый', 'manual', 'NEW', 'Этот заход ещё не сработал', ?, ?)",
        (in_days(-2), now_iso()),
    )
    assert await ai.winning_openers() == ["Видел ваше размещение у Иванова — кто ведёт закупку?"]
    verdict = await ai.review_draft("Здравствуйте! Меня зовут Пётр.", {"title": "Тест", "username": "t", "niche": "крипта"})
    assert verdict == "Поправить: Начните с факта", verdict
    assert "Видел ваше размещение" in PROMPTS[-1], "удачный заход обязан попасть в промпт"
    assert "ещё не сработал" not in PROMPTS[-1], "неотвеченный заход образцом быть не может"
    print("ok: проверка черновика учится на сообщениях, после которых ответили")

    # --- недельный отчёт ---
    await db.execute(
        "INSERT OR REPLACE INTO users (id, username, full_name, role, status, created_at) VALUES (7, 'ivan', 'Иван', 'sdr', 'active', ?)",
        (now_iso(),),
    )
    await db.execute("UPDATE leads SET assigned_to = 7")
    await db.execute("INSERT INTO points (user_id, season, delta, reason, created_at) VALUES (7, ?, 12, 'contact', ?)", (season(), in_days(-2)))
    await db.execute("INSERT INTO donors (username, joined, ads_found, active, created_at) VALUES ('deadchannel', 1, 0, 1, ?)", (in_days(-60),))
    text = await report.weekly()
    assert "Пришло лидов:</b> 2 (сканер 1, вручную 1)" in text, text
    assert "@ivan" in text and "+12 б." in text, text
    assert "deadchannel" in text, "донор без лидов за месяц обязан попасть в отчёт"
    assert "Отчёт за неделю" in text
    print("ok: недельный отчёт собирает воронку, людей и молчащих доноров")

    # --- автопоиск доноров ---
    assert (await scanner.discover())["ok"] is False, "без запущенного сканера искать нечем"
    for name in ("crypto_daily", "invest_news"):
        await db.execute("INSERT INTO donors (username, joined, active, created_at) VALUES (?, 1, 1, ?)", (name, now_iso()))
    await db.execute("INSERT INTO donor_hints (username, status, created_at) VALUES ('rejected_one', 'hidden', ?)", (now_iso(),))
    scanner.client = FakeClient({
        "deadchannel": [],
        "crypto_daily": [FakeChat("bitcoin_ru", "Биткоин Россия", 54000), FakeChat("trader_chat", "Чат", 9000, megagroup=True), FakeChat("rejected_one", "Отклонён", 1000)],
        "invest_news": [FakeChat("bitcoin_ru", "Биткоин Россия", 54000), FakeChat("stocks_pro", "Акции Про", 12000), FakeChat("crypto_daily", "Свой же донор", 80000)],
    })
    result = await scanner.discover()
    assert result["fresh"] == 2, result
    hints = {row["username"]: row for row in await db.fetchall("SELECT * FROM donor_hints WHERE status = 'new'")}
    assert set(hints) == {"bitcoin_ru", "stocks_pro"}, "чаты, свои доноры и отклонённые в кандидаты не идут"
    assert hints["bitcoin_ru"]["votes"] == 2, "канал, который советуют два донора, должен быть выше"
    assert (await scanner.discover())["fresh"] == 0, "повторный поиск не плодит дубли"
    print("ok: автопоиск отсеял чаты, своих и отклонённых —", {k: v["votes"] for k, v in hints.items()})

    rows = await db.fetchall("SELECT * FROM donor_hints WHERE status = 'new'")
    assert donor_hints_kb(rows).inline_keyboard and donors_kb([], len(rows)).inline_keyboard
    print("ok: клавиатуры кандидатов и доноров собираются")

    scanner.client = None
    await db.conn.close()
    print("\nGROWTH OK")


async def run() -> int:
    try:
        await main()
        return 0
    except AssertionError as exc:
        print("FAIL:", exc)
        return 1
    finally:
        # Открытое соединение aiosqlite держит свой поток и не даёт процессу завершиться.
        if db.conn:
            await db.conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
