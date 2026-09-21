"""Скоринг v1 — прозрачные веса. Метки команды накапливаются в БД; модель v2 обучается на них отдельно."""
from app import settings_store as st


def compute(lead: dict, ai: dict | None, ad_count: int) -> int:
    score = 45
    if ai:
        hint = ai.get("score_hint")
        if isinstance(hint, (int, float)):
            score = int(hint)
        if ai.get("is_target") is False:
            score = min(score, 30)
        if ai.get("kind") in ("media", "agency", "scam"):
            score = min(score, 20)

    subs = lead.get("subscribers") or 0
    if subs >= 100_000:
        score += 10
    elif subs >= 20_000:
        score += 6
    elif subs >= 5_000:
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


async def category(score: int) -> str:
    hot = await st.get_int("hot_threshold")
    warm = await st.get_int("warm_threshold")
    if score >= hot:
        return "hot"
    if score >= warm:
        return "warm"
    return "cold"


CATEGORY_RU = {"hot": "🔥 ГОРЯЧИЙ", "warm": "🌤 ТЁПЛЫЙ", "cold": "❄️ ХОЛОДНЫЙ"}
