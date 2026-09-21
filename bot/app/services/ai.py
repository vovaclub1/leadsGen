"""Клиент ИИ (DeepSeek и любой OpenAI-совместимый API): анализ лидов, классификация рекламы, чтение картинок."""
import asyncio
import base64
import json
import logging
import re
import struct
import time
import zlib

import httpx

from app import settings_store as st
from app.config import config
from app.db import db
from app.utils import day_key, now_iso, season, trunc

log = logging.getLogger(__name__)

RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024

VERTICALS = "brawl, clash, dota, cs2, steam, streaming, gaming_news, crypto, news, trends, auto_sport, other"

ANALYZE_SYSTEM = f"""Ты — аналитик агентства MORIER. Мы продаём рекламные размещения в Telegram-каналах (гейминг: Brawl Stars, CS2, Dota 2, Steam, стриминг; а также крипта, новости, тренды, авто/спорт; аудитория RU).
Твоя задача — по данным о канале/сайте/посте решить, является ли объект ПОТЕНЦИАЛЬНЫМ РЕКЛАМОДАТЕЛЕМ (тем, кто платит за рекламу своего продукта), а не площадкой, СМИ, агентством или скамом.

Клиент — это: магазины ключей и гифтов, донат-сервисы, пополнение Steam, игровые магазины и сервисы, VPN, приложения для геймеров, буст/аккаунты, железо и периферия, крипто-биржи и обменники, БК и казино, инфопродукты, бренды с молодой ЦА.
НЕ клиент: обычный контентный канал без продукта, новостное СМИ, другое рекламное агентство или биржа рекламы, явный скам/пирамида, личный блог без монетизации.

Ответь строго в формате json, без пояснений и без markdown. Пример структуры json:
{{
 "is_target": true,
 "kind": "advertiser|media|agency|scam|unknown",
 "niche": "коротко, напр. 'магазин ключей Steam'",
 "vertical": "одна из: {VERTICALS}",
 "product": "что продаёт",
 "why": "1–2 предложения, почему это (не) клиент",
 "personal_fact": "один конкретный факт об объекте для первого сообщения (что видно в описании/постах/размещении)",
 "draft": "первое сообщение клиенту от менеджера MORIER: до 5 строк, начинается с факта о клиенте (не с представления агентства), без цен, без слова 'агентство' в первой строке, в конце один вопрос",
 "risk_topic": "none|betting|casino|crypto|adult",
 "licensed_bookmaker": null,
 "contact_hint": null,
 "score_hint": 70
}}"""

AD_SYSTEM = """Ты определяешь, является ли пост в Telegram-канале РЕКЛАМНЫМ размещением стороннего продукта/канала (не самореклама канала, не репост новости).
Ответь строго в формате json. Пример: {"is_ad": true, "confidence": 0.9, "advertiser": "username рекламодателя без @ или домен сайта, иначе null", "product": "что рекламируют, коротко"}"""

VISION_SYSTEM = """Ты разбираешь креатив рекламного поста в Telegram. Тебе дают картинку из поста.
Опиши по-русски и очень сжато, максимум 4 строки:
1) весь текст, который видно на картинке (логотипы, названия, @username, домены, промокоды, цены) — дословно;
2) что рекламируется и какой это тип бизнеса.
Если на картинке нет ничего, кроме фото/мема без текста и бренда — ответь одной строкой: «Без текста и бренда»."""


def _tiny_png() -> bytes:
    """Картинка 32×32 для самопроверки зрения — без внешних зависимостей."""
    width = height = 32
    raw = b"".join(b"\x00" + bytes([32, 96, 208] * width) for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _sniff_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _image_block(data: bytes, detail: str = "low") -> dict | None:
    mime = _sniff_mime(data)
    if not mime or len(data) > MAX_IMAGE_BYTES:
        return None
    payload = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{payload}", "detail": detail}}


def _explain(status: int, body: str, model: str) -> str:
    low = body.lower()
    if status == 401:
        return "ключ не принят (401). Проверьте AI_API_KEY в .env — он должен начинаться с sk- и быть от того же сервиса, что AI_BASE_URL."
    if status == 402:
        return "на балансе DeepSeek нет средств (402). Пополните счёт в личном кабинете."
    if status == 403:
        return "доступ запрещён (403). Возможно, ключ отозван или закрыт по региону."
    if status == 404 or "model not exist" in low or "model_not_found" in low:
        return f"модель «{model}» не найдена. Актуальные имена DeepSeek: deepseek-flash и deepseek-v4-pro."
    if status == 400 and "image" in low:
        return f"модель «{model}» не принимает картинки. Для зрения нужна deepseek-flash."
    if status == 422 or status == 400:
        return f"запрос отклонён ({status}): {trunc(body, 200)}"
    if status == 429:
        return "слишком много запросов (429), сервис просит подождать."
    if status >= 500:
        return f"сервер ИИ отвечает ошибкой {status} — это на их стороне."
    return f"ошибка {status}: {trunc(body, 200)}"


class AIClient:
    def __init__(self):
        self.enabled = bool(config.ai_api_key)
        self.last_error: str | None = None
        self.last_error_at: str | None = None
        self.last_ok_at: str | None = None
        self.fail_streak = 0
        self.ok_count = 0
        self.fail_count = 0
        self._send_thinking = True
        self._cap_notified_month = ""
        if not self.enabled:
            log.warning("AI_API_KEY не задан — анализ лидов идёт на эвристиках")
        elif config.ai_model_was_legacy:
            log.warning(
                "AI_MODEL=%s больше не обслуживается DeepSeek — использую %s",
                config.ai_model_was_legacy, config.ai_model,
            )

    # ---------- расход ----------

    async def month_spent(self) -> float:
        value = await db.scalar("SELECT COALESCE(SUM(cost_usd), 0) FROM ai_usage WHERE month = ?", (season(),))
        return float(value or 0)

    async def over_cap(self) -> bool:
        cap = await st.get_float("ai_monthly_cap_usd")
        return cap > 0 and await self.month_spent() >= cap

    async def vision_calls_today(self) -> int:
        value = await db.scalar(
            "SELECT COUNT(*) FROM ai_usage WHERE purpose = 'vision' AND created_at LIKE ?", (f"{day_key()}%",)
        )
        return int(value or 0)

    async def vision_enabled(self) -> bool:
        if not config.vision_possible:
            return False
        return await st.get("ai_vision") != "0"

    async def usage_breakdown(self) -> list[dict]:
        return await db.fetchall(
            "SELECT purpose, COUNT(*) AS calls, SUM(ok) AS ok, COALESCE(SUM(cost_usd), 0) AS cost, "
            "COALESCE(SUM(tokens_in), 0) AS tin, COALESCE(SUM(tokens_out), 0) AS tout "
            "FROM ai_usage WHERE month = ? GROUP BY purpose ORDER BY cost DESC",
            (season(),),
        )

    # ---------- экономный режим ----------

    async def thrifty(self) -> bool:
        return await st.get("ai_thrifty") != "0"

    async def _note_skipped(self, purpose: str) -> None:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)",
            (f"ai_skipped:{season()}:{purpose}",),
        )

    async def skipped_month(self) -> dict[str, int]:
        rows = await db.fetchall("SELECT key, value FROM settings WHERE key LIKE ?", (f"ai_skipped:{season()}:%",))
        return {row["key"].rsplit(":", 1)[-1]: int(row["value"] or 0) for row in rows}

    async def should_classify_ad(self, explicit: bool, signals: int) -> bool:
        """Пост с пометкой «erid» или «#реклама» — реклама без вариантов. Переспрашивать ИИ незачем."""
        if not self.enabled:
            return False
        if not await self.thrifty():
            return True
        if explicit or signals >= 3:
            await self._note_skipped("ad")
            return False
        return True

    async def should_analyze(self, pre_score: int, ad_count: int) -> bool:
        """Повторных рекламодателей разбираем всегда, слабые одиночные лиды в экономном режиме — формулами."""
        if not self.enabled:
            return False
        if not await self.thrifty() or ad_count >= 2:
            return True
        floor = await st.get_int("ai_min_score")
        if floor > 0 and pre_score < floor:
            await self._note_skipped("analyze")
            log.info("Экономный режим: лид с предварительной оценкой %s не отправлен в ИИ", pre_score)
            return False
        return True

    async def winning_openers(self, limit: int = 3) -> list[str]:
        """Первые сообщения, после которых клиент ответил, — единственный честный образец «как надо»."""
        rows = await db.fetchall(
            "SELECT first_message FROM leads WHERE first_message IS NOT NULL AND TRIM(first_message) <> '' "
            "AND status IN ('REPLIED', 'ACCEPTED', 'WON') ORDER BY COALESCE(replied_at, updated_at) DESC LIMIT ?",
            (limit,),
        )
        return [trunc(row["first_message"], 500) for row in rows]

    async def _log_usage(self, purpose: str, usage: dict, ok: bool, error: str | None) -> float:
        prompt = int(usage.get("prompt_tokens") or 0)
        cached = usage.get("prompt_cache_hit_tokens")
        if cached is None:
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        cached = int(cached)
        miss = usage.get("prompt_cache_miss_tokens")
        miss = int(miss) if miss is not None else max(prompt - cached, 0)
        completion = int(usage.get("completion_tokens") or 0)
        cost = (
            miss / 1e6 * config.ai_price_in
            + cached / 1e6 * config.ai_price_cached
            + completion / 1e6 * config.ai_price_out
        )
        await db.execute(
            "INSERT INTO ai_usage (month, tokens_in, tokens_out, cost_usd, purpose, created_at, ok, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (season(), prompt, completion, cost, purpose, now_iso(), 1 if ok else 0, error),
        )
        return cost

    def _note_ok(self) -> None:
        self.last_ok_at = now_iso()
        self.fail_streak = 0
        self.ok_count += 1
        self.last_error = None

    def _note_fail(self, reason: str) -> None:
        self.last_error = reason
        self.last_error_at = now_iso()
        self.fail_streak += 1
        self.fail_count += 1

    # ---------- транспорт ----------

    async def _post(self, payload: dict, purpose: str) -> tuple[dict | None, str | None]:
        url = f"{config.ai_base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {config.ai_api_key}", "Content-Type": "application/json"}
        model = payload.get("model", config.ai_model)
        attempt = 0
        delay = 2.0
        while attempt < MAX_ATTEMPTS:
            attempt += 1
            body = dict(payload)
            if self._send_thinking and not config.ai_thinking:
                body["thinking"] = {"type": "disabled"}
            elif config.ai_thinking:
                body["thinking"] = {"type": "enabled"}
            try:
                async with httpx.AsyncClient(timeout=config.ai_timeout) as client:
                    response = await client.post(url, json=body, headers=headers)
            except httpx.TimeoutException:
                reason = f"таймаут {config.ai_timeout} с"
                if attempt >= MAX_ATTEMPTS:
                    return None, reason
                await asyncio.sleep(delay)
                delay *= 2
                continue
            except httpx.HTTPError as exc:
                reason = f"сеть недоступна: {type(exc).__name__}"
                if attempt >= MAX_ATTEMPTS:
                    return None, reason
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if response.status_code < 400:
                try:
                    return response.json(), None
                except ValueError:
                    return None, "сервис вернул не JSON"

            text = response.text or ""
            # Старые шлюзы не знают поля thinking — снимаем его и больше не отправляем.
            if response.status_code in (400, 422) and "thinking" in text.lower() and self._send_thinking:
                self._send_thinking = False
                log.info("Шлюз не принимает параметр thinking — отключаю его")
                attempt -= 1
                continue
            reason = _explain(response.status_code, text, model)
            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                pause = delay
                retry_after = response.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    pause = min(float(retry_after), 30.0)
                await asyncio.sleep(pause)
                delay *= 2
                continue
            return None, reason
        return None, "не удалось получить ответ ИИ"

    async def _chat(
        self, messages: list[dict], purpose: str, max_tokens: int, model: str | None = None, json_mode: bool = True
    ) -> tuple[str | None, str | None]:
        if not self.enabled:
            return None, "AI_API_KEY не задан"
        if await self.over_cap():
            return None, f"достигнут месячный лимит расхода ${await st.get_float('ai_monthly_cap_usd'):.2f}"
        payload: dict = {
            "model": model or config.ai_model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        data, error = await self._post(payload, purpose)
        if error:
            await self._log_usage(purpose, {}, ok=False, error=error)
            self._note_fail(error)
            log.warning("ИИ (%s): %s", purpose, error)
            return None, error

        await self._log_usage(purpose, data.get("usage") or {}, ok=True, error=None)
        self._note_ok()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return content.strip(), None

    # ---------- публичные методы ----------

    async def chat_json(
        self, system: str, user: str, purpose: str, max_tokens: int = 700, images: list[bytes] | None = None
    ) -> dict | None:
        blocks: list[dict] = [{"type": "text", "text": user}]
        model = None
        for image in images or []:
            block = _image_block(image)
            if block:
                blocks.append(block)
                model = config.ai_vision_model
        content: str | list[dict] = blocks if len(blocks) > 1 else user
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]

        text, error = await self._chat(messages, purpose, max_tokens, model=model)
        if error or not text:
            # DeepSeek изредка отдаёт пустой content в json-режиме — повторяем один раз.
            if not error:
                text, error = await self._chat(messages, purpose, max_tokens, model=model)
            if error or not text:
                return None
        parsed = _parse_json(text)
        if parsed is None:
            self._note_fail("ответ ИИ не разобрался как json")
            log.warning("ИИ (%s) вернул неразбираемый ответ: %s", purpose, trunc(text, 200))
        return parsed

    async def analyze_lead(self, lead: dict, examples: list[dict]) -> dict | None:
        parts = [
            f"Тип объекта: {lead.get('kind')}",
            f"Название: {lead.get('title') or '—'}",
            f"Username: @{lead.get('username')}" if lead.get("username") else f"URL: {lead.get('url') or '—'}",
            f"Подписчики: {lead.get('subscribers') or '—'}; средние просмотры поста: {lead.get('avg_views') or '—'}",
            f"Описание: {trunc(lead.get('about'), 800) or '—'}",
        ]
        if lead.get("recent_posts"):
            parts.append(f"Последние посты объекта:\n{trunc(lead['recent_posts'], 1200)}")
        if lead.get("source") == "scanner":
            parts.append(
                f"Объект найден как РЕКЛАМОДАТЕЛЬ: его реклама вышла в канале @{lead.get('donor')}. Текст рекламы:\n{trunc(lead.get('ad_text'), 900)}"
            )
        elif lead.get("source") == "search":
            parts.append(
                f"Объект найден по ключевому слову «{lead.get('keyword')}» — сам написал пост:\n{trunc(lead.get('ad_text'), 700)}"
            )
        if examples:
            lines = ["Примеры решений команды (учитывай их логику):"]
            for ex in examples:
                verdict = "КЛИЕНТ" if ex["status"] in ("WON", "REPLIED", "ACCEPTED") else f"НЕЦЕЛЕВОЙ ({ex.get('lost_reason') or 'без причины'})"
                lines.append(f"- {ex.get('title') or ex.get('username')}: {trunc(ex.get('about'), 120)} → {verdict}")
            parts.append("\n".join(lines))
        return await self.chat_json(ANALYZE_SYSTEM, "\n".join(parts), purpose="analyze")

    async def classify_ad(self, text: str, donor: str) -> dict | None:
        return await self.chat_json(AD_SYSTEM, f"Канал: @{donor}\nПост:\n{trunc(text, 1500)}", purpose="ad", max_tokens=200)

    async def read_image(self, image: bytes, caption: str = "") -> str | None:
        """Возвращает текст и смысл рекламного креатива. None — если зрение выключено или не сработало."""
        if not await self.vision_enabled():
            return None
        cap = await st.get_int("ai_vision_daily_cap")
        if cap > 0 and await self.vision_calls_today() >= cap:
            log.info("Дневной лимит разбора картинок (%s) исчерпан", cap)
            return None
        block = _image_block(image)
        if not block:
            return None
        user_blocks = [{"type": "text", "text": f"Подпись к посту: {trunc(caption, 400) or '—'}"}, block]
        messages = [
            {"role": "system", "content": VISION_SYSTEM},
            {"role": "user", "content": user_blocks},
        ]
        text, error = await self._chat(messages, "vision", max_tokens=300, model=config.ai_vision_model, json_mode=False)
        if error or not text:
            return None
        cleaned = text.strip()
        return None if cleaned.lower().startswith("без текста") else trunc(cleaned, 600)

    async def review_draft(self, draft: str, lead: dict) -> str | None:
        system = (
            "Ты — старший менеджер агентства MORIER. Оцени черновик первого сообщения потенциальному рекламодателю. "
            "Правила: начать с факта о клиенте, не с представления; не называть точную цену (только вилку по запросу); "
            "до 5 строк; один вопрос в конце; без канцелярита и лести. "
            'Ответь строго в формате json. Пример: {"verdict": "ok", "comment": "1–2 предложения, что поправить, по-русски"}'
        )
        parts = [f"Клиент: {lead.get('title')} (@{lead.get('username')}), ниша: {lead.get('niche')}"]
        winners = await self.winning_openers(3)
        if winners:
            # Общие правила знает любая модель. Ценность даёт тон именно этой команды на её же клиентах.
            parts.append(
                "Заходы этой команды, после которых клиент ответил — ориентируйся на их тон и структуру:\n"
                + "\n---\n".join(winners)
            )
        parts.append(f"Черновик:\n{trunc(draft, 1200)}")
        data = await self.chat_json(system, "\n\n".join(parts), purpose="draft", max_tokens=200)
        if not data:
            return None
        return f"{'Норм' if data.get('verdict') == 'ok' else 'Поправить'}: {data.get('comment', '')}".strip()

    # ---------- диагностика ----------

    async def self_test(self, with_vision: bool = False) -> dict:
        """Живой запрос к API. Используется кнопкой «Проверить ИИ» в админке."""
        if not self.enabled:
            return {"ok": False, "error": "AI_API_KEY не задан в .env", "stage": "текст"}
        before = await self.month_spent()
        started = time.monotonic()
        messages = [
            {"role": "system", "content": 'Отвечай строго в формате json. Пример: {"ok": true}'},
            {"role": "user", "content": 'Верни json {"ok": true}'},
        ]
        text, error = await self._chat(messages, "selftest", max_tokens=32)
        elapsed = int((time.monotonic() - started) * 1000)
        if error:
            return {"ok": False, "error": error, "stage": "текст", "ms": elapsed}
        if not _parse_json(text or ""):
            return {"ok": False, "error": f"модель ответила не json: {trunc(text, 120)}", "stage": "текст", "ms": elapsed}

        result = {"ok": True, "ms": elapsed, "model": config.ai_model, "cost": await self.month_spent() - before}
        if not with_vision:
            return result
        if not config.vision_possible:
            result["vision"] = "выключено: AI_VISION_MODEL пуст"
            return result
        started = time.monotonic()
        blocks = [{"type": "text", "text": 'Что на картинке? Ответь json: {"seen": true, "color": "цвет"}'}, _image_block(_tiny_png())]
        vision_text, vision_error = await self._chat(
            [{"role": "user", "content": blocks}], "selftest", max_tokens=64, model=config.ai_vision_model
        )
        result["vision_ms"] = int((time.monotonic() - started) * 1000)
        result["vision"] = "работает" if vision_text and not vision_error else f"ошибка: {vision_error or 'пустой ответ'}"
        result["vision_ok"] = bool(vision_text and not vision_error)
        return result

    async def health(self) -> dict:
        return {
            "enabled": self.enabled,
            "model": config.ai_model,
            "vision_model": config.ai_vision_model,
            "vision_on": await self.vision_enabled(),
            "base_url": config.ai_base_url,
            "thinking": config.ai_thinking,
            "spent": await self.month_spent(),
            "cap": await st.get_float("ai_monthly_cap_usd"),
            "thrifty": await self.thrifty(),
            "min_score": await st.get_int("ai_min_score"),
            "skipped": await self.skipped_month(),
            "ok": self.ok_count,
            "fail": self.fail_count,
            "streak": self.fail_streak,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_ok_at": self.last_ok_at,
        }


def _parse_json(content: str) -> dict | None:
    content = (content or "").strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I)
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.S)
        if match:
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
    return None


async def few_shot_examples() -> list[dict]:
    positive = await db.fetchall(
        "SELECT title, username, about, status, lost_reason FROM leads WHERE status IN ('WON','REPLIED','ACCEPTED') ORDER BY updated_at DESC LIMIT 3"
    )
    negative = await db.fetchall(
        "SELECT title, username, about, status, lost_reason FROM leads WHERE status = 'NOT_TARGET' ORDER BY updated_at DESC LIMIT 3"
    )
    return positive + negative


ai = AIClient()
