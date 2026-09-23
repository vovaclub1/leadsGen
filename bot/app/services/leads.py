"""Жизненный цикл лида: создание → карточка в группе → взятие → контакт → каденция → передача → сделка."""
import json
import logging
import re

from aiogram.exceptions import TelegramAPIError

from app import runtime
from app import settings_store as st
from app.db import db
from app.keyboards import group_card_kb, private_card_kb, supervise_kb
from app.services import channels, dnc, enrich, gates, rating, scoring, trustat
from app.services.ai import ai, few_shot_examples
from app.statuses import (
    ACCEPTED, ACTIVE, CLAIMED, CONTACTED, HANDOFF, NEW, REPLIED, STATUS_RU, WON, is_open,
)
from app.utils import fmt_num, fmt_time, h, in_days, in_minutes, mention, minutes_left, now_iso, now_utc, parse_iso, trunc

log = logging.getLogger(__name__)
VERTICAL_RU = {
    "brawl": "Brawl Stars", "clash": "Clash / Minecraft", "dota": "Dota 2", "cs2": "CS2", "steam": "Steam",
    "streaming": "Стриминг", "gaming_news": "Игровые новости", "crypto": "Крипта", "news": "Новости",
    "trends": "Тренды", "auto_sport": "Авто / спорт", "other": "Другое",
}
RISK_RU = {"betting": "беттинг", "casino": "казино", "crypto": "крипто-сигналы", "adult": "18+"}
RISK_WORDS = {
    "betting": ("ставк", "букмекер", "бк ", "фрибет", "коэффициент"),
    "casino": ("казино", "слот", "рулетк", "бонус за депозит"),
    "crypto": ("сигнал", "памп", "x10", "гарантирован", "доходност"),
    "adult": ("18+", "onlyfans", "эскорт"),
}

TG_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?(?!joinchat|c/|\+)([A-Za-z][A-Za-z0-9_]{3,31})", re.I)
AT_RE = re.compile(r"(?<![\w/])@([A-Za-z][A-Za-z0-9_]{3,31})")
URL_RE = re.compile(r"https?://([^\s/]+)", re.I)
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def parse_ref(text: str | None) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    match = TG_RE.search(text) or AT_RE.search(text)
    if match:
        username = match.group(1)
        return {"kind": "channel", "username": username, "url": f"https://t.me/{username}", "entity_key": f"tg:{username.lower()}"}
    match = URL_RE.search(text)
    if match:
        domain = match.group(1).lower().removeprefix("www.")
        return {"kind": "site", "username": None, "url": text.split()[0], "entity_key": f"site:{domain}"}
    if USERNAME_RE.match(text):
        return {"kind": "channel", "username": text, "url": f"https://t.me/{text}", "entity_key": f"tg:{text.lower()}"}
    return None


async def get(lead_id: int) -> dict | None:
    return await db.fetchone("SELECT * FROM leads WHERE id = ?", (lead_id,))


async def user(user_id: int | None) -> dict | None:
    if not user_id:
        return None
    return await db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))


async def log_event(lead_id: int, user_id: int | None, event_type: str, payload: str | None = None) -> None:
    await db.execute(
        "INSERT INTO lead_events (lead_id, user_id, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (lead_id, user_id, event_type, payload, now_iso()),
    )


async def update(lead_id: int, fields: dict) -> None:
    if not fields:
        return
    fields = {**fields, "updated_at": now_iso()}
    assignments = ", ".join(f"{key} = ?" for key in fields)
    await db.execute(f"UPDATE leads SET {assignments} WHERE id = ?", (*fields.values(), lead_id))


def ai_data(lead: dict) -> dict:
    if not lead.get("ai_json"):
        return {}
    try:
        return json.loads(lead["ai_json"])
    except json.JSONDecodeError:
        return {}


def _heuristic_risk(*texts: str | None) -> str:
    blob = " ".join(t for t in texts if t).lower()
    for topic, words in RISK_WORDS.items():
        if any(word in blob for word in words):
            return topic
    return "none"


# ---------- создание ----------

async def create_lead(
    ref: dict,
    source: str,
    created_by: int | None = None,
    donor: str | None = None,
    keyword: str | None = None,
    ad_text: str | None = None,
    ad_msg_id: int | None = None,
    ad_views: int | None = None,
    confidence: float | None = None,
    image_note: str | None = None,
) -> tuple[str, dict | None]:
    key = ref["entity_key"]
    if await db.fetchone("SELECT 1 FROM blacklist WHERE entity_key = ?", (key,)):
        return "blacklisted", None

    if source == "scanner" and donor:
        await db.execute(
            "INSERT INTO ad_posts (donor, msg_id, advertiser_key, text, confidence, views, created_at, image_note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (donor.lower(), ad_msg_id, key, trunc(ad_text, 1500), confidence, ad_views, now_iso(), image_note),
        )
        await db.execute("UPDATE donors SET ads_found = ads_found + 1 WHERE LOWER(username) = ?", (donor.lower(),))

    existing = await db.fetchone("SELECT * FROM leads WHERE entity_key = ? ORDER BY id DESC LIMIT 1", (key,))
    if existing:
        if is_open(existing["status"]):
            if source == "scanner":
                await update(existing["id"], {"ad_count": existing["ad_count"] + 1})
                await _notify_new_placement(existing, donor)
            return "dup_active", existing
        cooldown_key = "dnc_cooldown_days" if existing.get("lost_reason") == "dnc" else "cooldown_days"
        closed_at = parse_iso(existing.get("closed_at") or existing["updated_at"])
        if closed_at and (now_utc() - closed_at).days < await st.get_int(cooldown_key):
            return "cooldown", existing

    ad_count = await db.scalar(
        "SELECT COUNT(*) FROM ad_posts WHERE advertiser_key = ? AND created_at > ?", (key, in_days(-30))
    ) or 0
    lead_id = await db.insert(
        "INSERT INTO leads (entity_key, kind, username, url, title, source, donor, keyword, ad_text, ad_count, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (key, ref["kind"], ref.get("username"), ref.get("url"), ref.get("username") or ref.get("url"), source,
         donor.lower() if donor else None, keyword, trunc(ad_text, 1500), ad_count, created_by, now_iso(), now_iso()),
    )
    if source == "scanner":
        await db.execute("UPDATE ad_posts SET lead_id = ? WHERE advertiser_key = ? AND lead_id IS NULL", (lead_id, key))

    lead = await get(lead_id)
    try:
        extra = await enrich.enrich(lead)
    except Exception as exc:  # noqa: BLE001
        log.warning("Обогащение лида #%s: %s", lead_id, exc)
        extra = {}
    if extra:
        await update(lead_id, extra)
        lead = await get(lead_id)

    analysis = None
    # Сначала считаем лид формулами. В экономном режиме слабые одиночные лиды на этом и останавливаются.
    pre_score = scoring.compute({**lead, "risk_topic": _heuristic_risk(lead.get("about"), lead.get("ad_text"), lead.get("title"))}, None, ad_count)
    if await ai.should_analyze(pre_score, ad_count):
        try:
            analysis = await ai.analyze_lead(lead, await few_shot_examples())
        except Exception as exc:  # noqa: BLE001
            log.warning("ИИ-анализ лида #%s: %s", lead_id, exc)

    updates: dict = {}
    if analysis:
        updates["ai_json"] = json.dumps(analysis, ensure_ascii=False)
        updates["niche"] = trunc(analysis.get("niche"), 80) or None
        vertical = analysis.get("vertical")
        updates["vertical"] = vertical if vertical in VERTICAL_RU else "other"
        risk = analysis.get("risk_topic") or "none"
        updates["risk_topic"] = risk if risk in RISK_RU else "none"
        hint = analysis.get("contact_hint")
        if isinstance(hint, str) and not lead.get("contact_username") and USERNAME_RE.match(hint.lstrip("@")):
            updates["contact_username"] = hint.lstrip("@")
    else:
        updates["risk_topic"] = _heuristic_risk(lead.get("about"), lead.get("ad_text"), lead.get("title"))

    merged = {**lead, **updates}
    gate_cap, gate_reasons = await gates.evaluate(merged)
    score = scoring.compute(merged, analysis, ad_count)
    if gate_reasons:
        score = min(score, gate_cap)
        updates["gate_note"] = "; ".join(gate_reasons)
    updates["score"] = score
    updates["category"] = await scoring.category(score)

    flag = await dnc.check(username=merged.get("contact_username"), user_id=merged.get("contact_user_id"))
    if not flag and merged.get("username"):
        flag = await dnc.check(username=merged["username"])
    if flag:
        updates["contact_state"] = flag["level"]
    elif not merged.get("contact_username"):
        updates["contact_state"] = "none"

    await update(lead_id, updates)
    lead = await get(lead_id)
    await log_event(lead_id, created_by, "created", source)
    if gate_reasons:
        await log_event(lead_id, created_by, "gated", "; ".join(gate_reasons))
    # В группу попадают только лиды, стоящие очереди: горячие и тёплые от автоматических
    # источников. Холодные остаются в истории (/my, аналитика). Ручной /add — решение
    # живого человека, его карточка в группе всегда. Лид без контакта тоже едет в группу:
    # разбирать пост и искать связь может только человек.
    if source == "manual" or lead.get("kind") == "unknown" or lead.get("category") in ("hot", "warm"):
        await post_to_group(lead)
    return "created", lead


async def _notify_new_placement(lead: dict, donor: str | None) -> None:
    if not lead.get("assigned_to") or not runtime.bot:
        return
    try:
        await runtime.bot.send_message(
            lead["assigned_to"],
            f"📡 У вашего лида #{lead['id']} «{h(lead['title'])}» вышло новое размещение в @{h(donor)}. "
            f"Хороший повод написать: клиент сейчас в закупе.",
        )
    except TelegramAPIError:
        pass


# ---------- карточки ----------

SOURCE_RU = {"scanner": "сканер", "search": "поиск", "manual": "вручную"}


def source_line(lead: dict) -> str:
    if lead["source"] == "scanner":
        text = f"реклама в @{h(lead['donor'])}"
        if lead["ad_count"] >= 2:
            text += f" · {lead['ad_count']} размещений за 30 дней"
        return text
    if lead["source"] == "search":
        return f"ищет рекламу — ключевое слово «{h(lead['keyword'])}»"
    return "добавлен вручную"


def contact_line(lead: dict, reveal: bool = False) -> str:
    state = lead["contact_state"]
    if state in ("red", "orange", "yellow"):
        text = f"🚫 закрыт · {dnc.LEVEL_SHORT[state]} список"
        if state == "orange":
            text += " · только старший"
        if reveal and lead.get("contact_username"):
            text += f" · @{h(lead['contact_username'])}"
        return text
    if lead.get("contact_username"):
        return f"@{h(lead['contact_username'])}"
    if lead["kind"] == "user":
        return f"@{h(lead['username'])} — личный аккаунт"
    return "не указан в описании — ищите в закрепе или пишите через комментарии"


def render_card(lead: dict, mode: str = "group", assignee: dict | None = None, reveal: bool = False, matrix: str | None = None) -> str:
    data = ai_data(lead)
    head = f"{scoring.CATEGORY_RU[lead['category']]} · #{lead['id']}"
    if lead.get("niche"):
        head += f" · {h(lead['niche'])}"
    name = h(lead.get("title") or lead.get("username") or lead.get("url"))
    ident = f"@{h(lead['username'])}" if lead.get("username") else h(lead.get("url") or "")
    second = f"<b>{name}</b> · {ident}"
    stats = []
    if lead.get("subscribers"):
        stats.append(f"{fmt_num(lead['subscribers'])} подп.")
    if lead.get("avg_views"):
        stats.append(f"~{fmt_num(lead['avg_views'])} просм/пост")
    if stats:
        second += " · " + " · ".join(stats)

    lines = [head, second]
    if lead.get("vertical"):
        lines.append(f"Вертикаль: {VERTICAL_RU.get(lead['vertical'], lead['vertical'])}")
    lines.append("Источник: " + source_line(lead))
    if lead.get("gate_note"):
        lines.append(f"Гейты: {h(trunc(lead['gate_note'], 200))}")
    if data.get("why"):
        lines.append(f"Почему клиент: {h(data['why'])}")
    elif lead.get("about"):
        lines.append(f"Описание: {h(trunc(lead['about'], 200))}")
    if data.get("product"):
        lines.append(f"Продукт: {h(data['product'])}")
    lines.append("Контакт: " + contact_line(lead, reveal=reveal))
    if lead["kind"] == "unknown":
        lines.append("<i>Контакт в посте не найден — открой пост по ссылке и найди связь вручную</i>")
    if data.get("personal_fact"):
        lines.append(f"Факт для первого сообщения: {h(data['personal_fact'])}")
    if lead["risk_topic"] != "none":
        warning = f"⚠️ Тематика-риск: {RISK_RU.get(lead['risk_topic'], lead['risk_topic'])} — бонус за нишу не начисляется"
        if lead["risk_topic"] == "betting" and data.get("licensed_bookmaker") is not True:
            warning += ", проверьте лицензию"
        lines.append(warning)
    if not data and not ai.enabled:
        lines.append("<i>ИИ-анализ выключен — оцените вручную</i>")

    if mode == "group" and lead["status"] != NEW:
        lines.append(f"\nСтатус: {STATUS_RU.get(lead['status'], lead['status'])}")

    if mode in ("private", "handoff"):
        lines.append(f"\nСтатус: <b>{STATUS_RU.get(lead['status'], lead['status'])}</b>")
        if lead["status"] == CLAIMED:
            lines.append(f"⏱ Первый контакт до {fmt_time(lead['contact_deadline'])} · осталось {max(0, minutes_left(lead['contact_deadline']))} мин")
        if lead["status"] == CONTACTED and lead.get("next_touch_at"):
            lines.append(f"Следующее касание: {fmt_time(lead['next_touch_at'], with_date=True)} (№{lead['touch_count'] + 1})")
        if lead["status"] == CLAIMED and data.get("draft"):
            lines.append(f"\n<b>Черновик первого сообщения</b> — перепишите под себя, шаблоны клиенты чуют:\n<i>{h(data['draft'])}</i>")
        if lead.get("note"):
            lines.append(f"\n🗒 Заметки:\n{h(lead['note'])}")

    if mode == "handoff":
        lines.append(f"\n<b>Передача от {mention(assignee)}</b>")
        if lead.get("handoff_note"):
            lines.append(h(lead["handoff_note"]))
        if lead.get("first_message"):
            lines.append(f"Первое сообщение SDR:\n<i>{h(trunc(lead['first_message'], 400))}</i>")
        if matrix:
            lines.append(f"\n<b>Матрица размещаемости</b>:\n{matrix}")
        if lead.get("trustat_json"):
            try:
                lines.append(trustat.format_stat(json.loads(lead["trustat_json"])))
            except json.JSONDecodeError:
                pass
    return "\n".join(lines)


async def post_to_group(lead: dict) -> None:
    group_id = await st.get("leads_group_id")
    if not group_id or not runtime.bot:
        return
    try:
        message = await runtime.bot.send_message(
            int(group_id), render_card(lead, "group"), reply_markup=group_card_kb(lead["id"]) if lead["status"] == "NEW" else None
        )
        await db.execute("UPDATE leads SET group_msg_id = ? WHERE id = ?", (message.message_id, lead["id"]))
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить карточку в группу: %s", exc)


async def update_group_card(lead: dict, footer: str | None = None) -> None:
    group_id = await st.get("leads_group_id")
    if not group_id or not lead.get("group_msg_id") or not runtime.bot:
        return
    text = render_card(lead, "group")
    if footer:
        text += f"\n{footer}"
    try:
        await runtime.bot.edit_message_text(text, chat_id=int(group_id), message_id=lead["group_msg_id"], reply_markup=None)
    except TelegramAPIError as exc:
        log.info("Карточка в группе не обновлена: %s", exc)


async def send_private_card(lead: dict, to_user: dict, prefix: str | None = None) -> None:
    if not runtime.bot:
        return
    reveal = to_user["role"] in ("owner", "senior")
    assignee = await user(lead.get("assigned_to"))
    mode = "handoff" if lead["status"] in (HANDOFF, ACCEPTED) and reveal else "private"
    matrix = await channels.placement_matrix(lead.get("vertical"), lead.get("risk_topic", "none")) if mode == "handoff" else None
    text = render_card(lead, mode, assignee=assignee, reveal=reveal, matrix=matrix)
    if prefix:
        text = f"{prefix}\n\n{text}"
    try:
        await runtime.bot.send_message(to_user["id"], text, reply_markup=private_card_kb(lead, to_user["role"]))
    except TelegramAPIError as exc:
        log.warning("Личная карточка не доставлена %s: %s", to_user["id"], exc)


async def dm(user_id: int | None, text: str, reply_markup=None) -> None:
    if not user_id or not runtime.bot:
        return
    try:
        await runtime.bot.send_message(user_id, text, reply_markup=reply_markup)
    except TelegramAPIError as exc:
        log.info("DM %s не доставлено: %s", user_id, exc)


async def seniors(exclude: int | None = None) -> list[dict]:
    rows = await db.fetchall("SELECT * FROM users WHERE status = 'active' AND role IN ('owner', 'senior')")
    return [row for row in rows if row["id"] != exclude]


# ---------- взятие и освобождение ----------

async def claim(lead_id: int, me: dict) -> tuple[bool, str]:
    lead = await get(lead_id)
    if not lead:
        return False, "Лид не найден."
    if lead["status"] != NEW:
        return False, "Лид уже взят или закрыт."
    if me["role"] == "sdr" and not me.get("quiz_passed"):
        return False, "Сначала пройдите квиз новичка: /quiz. Без него очередь закрыта."
    if lead["contact_state"] in ("red", "orange") and me["role"] == "sdr":
        return False, "Контакт закрыт для SDR — этот лид берёт только старший."
    if await db.fetchone("SELECT 1 FROM lead_events WHERE lead_id = ? AND user_id = ? AND type = 'released'", (lead_id, me["id"])):
        return False, "Вы уже упускали этот лид по таймеру — он для других."
    active = await db.scalar(
        f"SELECT COUNT(*) FROM leads WHERE assigned_to = ? AND status IN ({','.join('?' * len(ACTIVE))})", (me["id"], *ACTIVE)
    )
    limit = await st.get_int("max_active")
    if active >= limit:
        return False, f"У вас {active} активных лидов — лимит {limit}. Закройте или передайте часть."

    sla = await st.get_int("sla_minutes")
    cursor = await db.execute(
        "UPDATE leads SET status = 'CLAIMED', assigned_to = ?, claimed_at = ?, contact_deadline = ?, sla_warned = 0, updated_at = ? "
        "WHERE id = ? AND status = 'NEW'",
        (me["id"], now_iso(), in_minutes(sla), now_iso(), lead_id),
    )
    if cursor.rowcount == 0:
        return False, "Кто-то успел раньше."
    await log_event(lead_id, me["id"], "claimed")
    lead = await get(lead_id)
    await update_group_card(lead, f"✅ Взял {mention(me)} · первый контакт до {fmt_time(lead['contact_deadline'])}")
    await send_private_card(lead, me, prefix=f"Лид ваш. Таймер пошёл: {sla} минут на первое сообщение и подтверждение.")
    return True, "Лид ваш — карточка и черновик в личке."


async def release_by_timer(lead: dict) -> None:
    cursor = await db.execute(
        "UPDATE leads SET status = 'NEW', assigned_to = NULL, claimed_at = NULL, contact_deadline = NULL, sla_warned = 0, updated_at = ? "
        "WHERE id = ? AND status = 'CLAIMED'",
        (now_iso(), lead["id"]),
    )
    if cursor.rowcount == 0:
        return
    await log_event(lead["id"], lead["assigned_to"], "released")
    await rating.add(lead["assigned_to"], "timer", lead["id"])
    await dm(
        lead["assigned_to"],
        f"⏱ Лид #{lead['id']} «{h(lead['title'])}» освобождён: {await st.get_int('sla_minutes')} минут без подтверждённого контакта. "
        f"−{abs(rating.POINTS['timer'])} баллов. Лид снова в очереди.",
    )
    fresh = await get(lead["id"])
    await update_group_card(fresh, "⏱ Освобождён по таймеру — снова в очереди (см. новую карточку ниже)")
    await post_to_group(fresh)


# ---------- контакт и каденция ----------

async def set_contacted(lead: dict, me: dict, first_message: str | None) -> str:
    cadence = await st.cadence()
    await update(lead["id"], {
        "status": CONTACTED, "contacted_at": now_iso(), "first_message": trunc(first_message, 2000),
        "touch_count": 1, "next_touch_at": in_days(cadence[0]), "contact_deadline": None,
    })
    await log_event(lead["id"], me["id"], "contacted")
    points = await rating.add(me["id"], "contact", lead["id"])
    note = f"✅ Контакт подтверждён, +{points} баллов. Следующее касание через {cadence[0]} дн. — напомню."
    if lead["contact_state"] in ("red", "orange") or (lead["contact_state"] == "yellow" and me["role"] == "sdr"):
        penalty = await rating.add(me["id"], "wrote_red", lead["id"])
        note += f"\n\n⛔ Контакт был в {dnc.LEVEL_SHORT[lead['contact_state']]} списке: {penalty} баллов. Старшие уведомлены."
        for senior in await seniors(exclude=me["id"]):
            await dm(senior["id"], f"⛔ {mention(me)} написал закрытому контакту по лиду #{lead['id']} «{h(lead['title'])}».")
    await supervise_newbie(lead, me, first_message)
    fresh = await get(lead["id"])
    await update_group_card(fresh, f"✉️ Контакт установлен · {mention(me)}")
    return note


async def supervise_newbie(lead: dict, me: dict, first_message: str | None) -> None:
    """ТЗ 9.6: первые 5 лидов новичка старший смотрит после отправки (не до — чтобы не ломать SLA)."""
    if not first_message:
        return
    accepted = await db.scalar(
        "SELECT COUNT(*) FROM leads WHERE assigned_to = ? AND status IN ('ACCEPTED', 'WON')", (me["id"],)
    ) or 0
    if accepted >= await st.get_int("supervised_leads"):
        return
    for senior in await seniors(exclude=me["id"]):
        await dm(
            senior["id"],
            f"👀 Новичок {mention(me)} — первое сообщение по лиду #{lead['id']} «{h(lead['title'])}» "
            f"(уже отправлено клиенту):\n\n<i>{h(trunc(first_message, 600))}</i>",
            reply_markup=supervise_kb(lead["id"], me["id"]),
        )


async def set_replied(lead: dict, me: dict, reply_text: str | None) -> str:
    await update(lead["id"], {"status": REPLIED, "replied_at": now_iso(), "next_touch_at": None})
    await log_event(lead["id"], me["id"], "replied", trunc(reply_text, 500))
    points = await rating.add(me["id"], "replied", lead["id"])
    fresh = await get(lead["id"])
    await update_group_card(fresh, f"💬 Клиент ответил · {mention(me)}")
    return f"💬 Ответ зафиксирован, +{points} баллов. К��алифицируйте (бюджет, что хочет, когда) и передавайте старшему."


async def touch_done(lead: dict, me: dict) -> str:
    cadence = await st.cadence()
    count = lead["touch_count"] + 1
    if count > len(cadence):
        await update(lead["id"], {"touch_count": count, "next_touch_at": None})
        await log_event(lead["id"], me["id"], "touch", str(count))
        return f"Касание №{count} записано. Каденция исчерпана — если ответа нет, отметьте «Нецелевой» или передайте старшему с пометкой «молчит»."
    next_days = cadence[min(count - 1, len(cadence) - 1)]
    await update(lead["id"], {"touch_count": count, "next_touch_at": in_days(next_days)})
    await log_event(lead["id"], me["id"], "touch", str(count))
    return f"Касание №{count} записано. Следующее — через {next_days} дн."


async def postpone(lead: dict, me: dict, days: int) -> str:
    await update(lead["id"], {"next_touch_at": in_days(days)})
    await log_event(lead["id"], me["id"], "postponed", str(days))
    return f"Отложено на {days} дн. Напомню {fmt_time(in_days(days), with_date=True)}."


async def add_note(lead: dict, me: dict, text: str) -> None:
    stamp = fmt_time(now_iso(), with_date=True)
    combined = (lead.get("note") or "").strip()
    combined = f"{combined}\n• {stamp} {mention(me)}: {trunc(text, 500)}".strip()
    await update(lead["id"], {"note": combined[-3000:]})


# ---------- закрытие ----------

async def close_not_target(lead: dict, me: dict, reason: str, reason_label: str) -> str:
    await update(lead["id"], {"status": "NOT_TARGET", "lost_reason": reason, "closed_at": now_iso(), "next_touch_at": None, "contact_deadline": None})
    await log_event(lead["id"], me["id"], "not_target", reason)
    points = await rating.add(me["id"], "not_target", lead["id"])
    fresh = await get(lead["id"])
    await update_group_card(fresh, f"❌ Нецелевой · {h(reason_label)} · {mention(me)}")
    return f"Лид #{lead['id']} закрыт как нецелевой ({reason_label}), +{points}. Эта метка учит скоринг."


async def close_dnc(lead: dict, me: dict) -> str:
    contact = lead.get("contact_username") or (lead.get("username") if lead["kind"] == "user" else None)
    if not contact and not lead.get("contact_user_id"):
        # Контакт так и не нашли — блокируем сам канал, иначе через закреп/комментарии напишут ещё раз.
        contact = lead.get("username")
    if contact or lead.get("contact_user_id"):
        await dnc.add("red", f"просил не писать (лид #{lead['id']})", me["id"], username=contact, user_id=lead.get("contact_user_id"))
    # Канал больше не должен попадать в очередь ни из сканера, ни из поиска, ни вручную.
    await db.execute(
        "INSERT OR IGNORE INTO blacklist (entity_key, reason, added_by, created_at) VALUES (?, ?, ?, ?)",
        (lead["entity_key"], f"просил не писать (лид #{lead['id']})", me["id"], now_iso()),
    )
    await update(lead["id"], {"status": "NOT_TARGET", "lost_reason": "dnc", "closed_at": now_iso(), "next_touch_at": None, "contact_deadline": None, "contact_state": "red"})
    await log_event(lead["id"], me["id"], "dnc")
    points = await rating.add(me["id"], "dnc_honest", lead["id"])
    fresh = await get(lead["id"])
    await update_group_card(fresh, f"🚫 Просил не писать · контакт в красном списке · {mention(me)}")
    return f"Контакт добавлен в красный список, лид закрыт. +{points} за честность — это важнее любого лида."


async def mark_duplicate(lead: dict, me: dict) -> str:
    await update(lead["id"], {"status": "DUPLICATE", "closed_at": now_iso()})
    await log_event(lead["id"], me["id"], "duplicate")
    fresh = await get(lead["id"])
    await update_group_card(fresh, f"♻️ Дубликат · {mention(me)}")
    return "Отмечен как дубликат."


# ---------- передача старшему ----------

async def handoff(lead: dict, me: dict, note: str) -> str:
    fields = {"status": "HANDOFF", "handoff_at": now_iso(), "handoff_note": note, "next_touch_at": None, "contact_deadline": None}
    if lead.get("username") and lead["kind"] in ("channel", "chat"):
        stat, error = await trustat.channel_stat(lead["username"], priority=True)
        if stat:
            fields["trustat_json"] = json.dumps(stat, ensure_ascii=False)
        elif error:
            log.info("Trustat для #%s: %s", lead["id"], error)
    await update(lead["id"], fields)
    await log_event(lead["id"], me["id"], "handoff", note)
    fresh = await get(lead["id"])
    for senior in await seniors(exclude=me["id"]):
        await send_private_card(fresh, senior, prefix="📨 Новая передача от SDR")
    await update_group_card(fresh, f"⬆️ Передан старшему · {mention(me)}")
    return "Передано. Старшие получили карточку; после принятия вам начислится +25."


async def accept(lead: dict, senior: dict) -> str:
    await update(lead["id"], {"status": "ACCEPTED", "accepted_by": senior["id"], "accepted_at": now_iso()})
    await log_event(lead["id"], senior["id"], "accepted")
    if lead.get("assigned_to"):
        points = await rating.add(lead["assigned_to"], "accepted", lead["id"])
        await dm(lead["assigned_to"], f"✅ Старший {mention(senior)} принял лид #{lead['id']} «{h(lead['title'])}». +{points} баллов.")
    fresh = await get(lead["id"])
    await update_group_card(fresh, f"✅ Принят старшим {mention(senior)}")
    return "Принято. Теперь лид в вашей воронке: WON / LOST на карточке."


async def return_to_sdr(lead: dict, senior: dict, comment: str) -> str:
    status = REPLIED if lead.get("replied_at") else CONTACTED
    await update(lead["id"], {"status": status, "handoff_at": None})
    await log_event(lead["id"], senior["id"], "returned", comment)
    if lead.get("assigned_to"):
        penalty = await rating.add(lead["assigned_to"], "rework", lead["id"])
        owner = await user(lead["assigned_to"])
        fresh = await get(lead["id"])
        if owner:
            await send_private_card(fresh, owner, prefix=f"↩️ Старший вернул лид с комментарием:\n<i>{h(comment)}</i>\n({penalty} балла за доработку)")
    return "Возвращено SDR."


async def won(lead: dict, senior: dict, amount: int, margin: int | None) -> str:
    await update(lead["id"], {"status": WON, "closed_at": now_iso(), "won_amount": amount, "won_margin": margin})
    await log_event(lead["id"], senior["id"], "won", f"{amount}/{margin}")
    if lead.get("assigned_to"):
        points = await rating.add(lead["assigned_to"], "won", lead["id"])
        text = f"🏆 Сделка по лиду #{lead['id']} «{h(lead['title'])}» закрыта на {amount:,} ��. +{points} баллов!".replace(",", " ")
        # ТЗ 9.2: повторная сделка с тем же клиентом — отдельный бонус.
        repeat = await db.scalar(
            "SELECT COUNT(*) FROM leads WHERE entity_key = ? AND status = 'WON' AND id != ?", (lead["entity_key"], lead["id"])
        ) or 0
        if repeat:
            bonus = await rating.add(lead["assigned_to"], "won_repeat", lead["id"])
            text += f"\n🔁 Это уже не первый deal с этим клиентом: +{bonus} за повторную сделку."
        await dm(lead["assigned_to"], text)
    fresh = await get(lead["id"])
    await update_group_card(fresh, f"🏆 СДЕЛКА · {amount:,} ₽ · SDR {mention(await user(lead.get('assigned_to')))} · закрыл {mention(senior)}".replace(",", " "))
    return "Сделка записана. Баллы начислены SDR."


async def lost(lead: dict, senior: dict, reason: str, reason_label: str) -> str:
    await update(lead["id"], {"status": "LOST", "closed_at": now_iso(), "lost_reason": reason})
    await log_event(lead["id"], senior["id"], "lost", reason)
    if lead.get("assigned_to"):
        await dm(lead["assigned_to"], f"💤 Лид #{lead['id']} «{h(lead['title'])}» закрыт как потерянный: {reason_label}.")
    fresh = await get(lead["id"])
    await update_group_card(fresh, f"💤 Потерян · {h(reason_label)} · {mention(senior)}")
    return "Записано как LOST."


# ---------- выборки ----------

async def queue(limit: int = 10) -> list[dict]:
    return await db.fetchall("SELECT * FROM leads WHERE status = 'NEW' ORDER BY score DESC, created_at ASC LIMIT ?", (limit,))


async def my_leads(user_id: int) -> list[dict]:
    return await db.fetchall(
        f"SELECT * FROM leads WHERE assigned_to = ? AND status IN ({','.join('?' * len(ACTIVE))}) ORDER BY "
        "CASE status WHEN 'CLAIMED' THEN 0 WHEN 'REPLIED' THEN 1 WHEN 'CONTACTED' THEN 2 ELSE 3 END, next_touch_at",
        (user_id, *ACTIVE),
    )


async def handoffs() -> list[dict]:
    return await db.fetchall("SELECT * FROM leads WHERE status = 'HANDOFF' ORDER BY handoff_at")


def short_line(lead: dict) -> str:
    ident = f"@{h(lead['username'])}" if lead.get("username") else h(lead.get("url") or "")
    extra = ""
    if lead["status"] == CLAIMED:
        extra = f" · ⏱ до {fmt_time(lead['contact_deadline'])}"
    elif lead["status"] == CONTACTED and lead.get("next_touch_at"):
        extra = f" · касание {fmt_time(lead['next_touch_at'], with_date=True)}"
    return f"#{lead['id']} {scoring.CATEGORY_RU[lead['category']].split()[0]} <b>{h(lead['title'])}</b> {ident} · {h(lead.get('niche') or '')}{extra}"
