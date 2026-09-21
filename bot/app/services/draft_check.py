"""Проверка черновика первого сообщения по чек-листу SDR-инструкции — до отправки клиенту."""
import re

from app.services import dnc

PRICE_RE = re.compile(r"\d[\d\s]{2,}\s?(₽|руб|р\.|k\b|к\b|тыс)", re.I)
RANGE_RE = re.compile(r"\bот\b.*\bдо\b|\d\s?[–-]\s?\d", re.I)
INTRO_RE = re.compile(r"агентств|меня зовут|мы —|мы -|представля", re.I)
LINK_RE = re.compile(r"https?://|t\.me/", re.I)


async def check(draft: str, lead: dict) -> tuple[list[str], list[str]]:
    issues, good = [], []
    lines = [line for line in draft.splitlines() if line.strip()]
    low = draft.lower()

    if len(lines) > 5:
        issues.append(f"Длинно: {len(lines)} строк, норма — до 5.")
    else:
        good.append("Длина в норме.")

    if PRICE_RE.search(draft) and not RANGE_RE.search(draft):
        issues.append("Похоже на точную цену. SDR называет только вилку — и только если спросили.")

    if lines and INTRO_RE.search(lines[0]):
        issues.append("Первая строка — представление. Начните с факта о клиенте, представьтесь во второй.")
    else:
        good.append("Начало не с представления.")

    anchors = [lead.get("title") or "", lead.get("username") or "", lead.get("niche") or ""]
    anchors = [a.lower() for a in anchors if len(a) >= 4]
    if anchors and not any(a in low for a in anchors):
        issues.append("Не вижу персонального факта: упомяните канал, продукт или размещение клиента.")
    else:
        good.append("Есть привязка к клиенту.")

    if "?" not in draft[-160:]:
        issues.append("Нет вопроса в конце — клиенту нечего отвечать.")
    else:
        good.append("Есть вопрос в конце.")

    if len(LINK_RE.findall(draft)) > 1:
        issues.append("Больше одной ссылки — выглядит как спам, Telegram такие сообщения режет.")

    if "здравствуйте, мы" in low or "добрый день, мы" in low:
        issues.append("«Здравствуйте, мы…» — шаблон, который клиенты не дочитывают.")

    for mention in set(re.findall(r"@([A-Za-z][A-Za-z0-9_]{3,31})", draft)):
        row = await dnc.check(username=mention)
        if row and row["level"] == "red":
            issues.append(f"@{mention} в красном списке — этому человеку писать нельзя.")

    return issues, good
