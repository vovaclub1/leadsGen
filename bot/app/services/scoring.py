"""Скоринг v1 — прозрачные веса. Метки команды накапливаются в БД; модель v2 обучается на них отдельно.

ТЗ 5.5: «Финальный score = 0,6 × правила + 0,4 × ai_score (пока нет модели v2)». Правила считаются
здесь полностью формулами (_rules_score); ai_score — это score_hint от ИИ-анализа лида. Без ИИ (ключа
нет, лид не ушёл в анализ) блендить нечего — итог равен чистому правило-скорингу.
"""
from app import settings_store as st


def _rules_score(lead: dict, ad_count: int) -> int:
    score = 45

    subs = lead.get("subscribers") or 0
    # ТЗ 5.5 даёт плоский бонус за 1k–100k подписчиков. Осознанное отклонение: наш покупатель —
    # мелкий и средний проект (крупным мы не нужны — это дальше ловит гейт размера), поэтому бонус
    # перевёрнут — 5–20k ценнее 20–100k, а 100k+ не получает ничего. См. README, «Скоринг v1».
    if 5_000 <= subs < 20_000:
        score += 6
    elif 20_000 <= subs < 100_000:
        score += 3

    if (lead.get("avg_views") or 0) >= 20_000:
        score += 5

    if lead.get("source") == "scanner":
        score += 15
    elif lead.get("source") == "search":
        score += 10

    if ad_count >= 3:
        score += 10
    elif ad_count == 2:
        score += 5

    if lead.get("contact_username"):
        score += 5

    if (lead.get("risk_topic") or "none") != "none":
        score -= 5

    return max(0, min(100, score))


def compute(lead: dict, ai: dict | None, ad_count: int) -> int:
    rules = _rules_score(lead, ad_count)

    ai_score = None
    if ai:
        hint = ai.get("score_hint")
        if isinstance(hint, (int, float)):
            ai_score = max(0, min(100, int(hint)))

    final = round(0.6 * rules + 0.4 * ai_score) if ai_score is not None else rules

    if ai:
        if ai.get("is_target") is False:
            final = min(final, 30)
        if ai.get("kind") in ("media", "agency", "scam"):
            final = min(final, 20)

    return max(0, min(100, final))


async def category(score: int) -> str:
    hot = await st.get_int("hot_threshold")
    warm = await st.get_int("warm_threshold")
    if score >= hot:
        return "hot"
    if score >= warm:
        return "warm"
    return "cold"


CATEGORY_RU = {"hot": "🔥 ГОРЯЧИЙ", "warm": "🌤 ТЁПЛЫЙ", "cold": "❄️ ХОЛОДНЫЙ"}
