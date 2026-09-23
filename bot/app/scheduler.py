"""Фоновый цикл: SLA-таймеры, каденция, старение очереди, план/разбор дня, лидерборд, бэкап, поиск по ключевым словам."""
import asyncio
import logging

from aiogram.types import FSInputFile

from app import runtime
from app import settings_store as st
from app.config import config
from app.db import db
from app.keyboards import open_kb, touch_kb
from app.services import dnc, keypool, leads, rating, report, search_poller
from app.services.ai import ai
from app.services.scanner import scanner
from app.statuses import CLAIMED, CONTACTED, REPLIED
from app.utils import day_key, fmt_time, h, hours_between, in_days, in_minutes, local_now, mention, now_iso, season

log = logging.getLogger(__name__)


async def scheduler_loop() -> None:
    await asyncio.sleep(5)
    while True:
        for job in (
            sla_jobs, senior_sla_jobs, cadence_jobs, cadence_escalation_jobs, aging_jobs, dnc_jobs,
            daily_jobs, search_job, ai_health_job, key_pool_job, reach_recheck_job,
        ):
            try:
                await job()
            except Exception as exc:  # noqa: BLE001 — один упавший job не должен останавливать остальные
                log.exception("Job %s упал: %s", job.__name__, exc)
        await asyncio.sleep(30)


async def _once_per_day(flag: str) -> bool:
    key = f"done:{flag}:{day_key()}"
    if await db.fetchone("SELECT 1 FROM settings WHERE key = ?", (key,)):
        return False
    await db.execute("INSERT INTO settings (key, value) VALUES (?, '1')", (key,))
    # Сравниваем только дату в конце ключа: при сравнении ключа целиком 'done:backup:сегодня'
    # оказывался меньше 'done:plan:позавчера' и сегодняшний флаг стирался, а задача шла по второму кругу.
    await db.execute("DELETE FROM settings WHERE key LIKE 'done:%' AND substr(key, -10) < ?", (in_days(-3)[:10],))
    return True


def _time_passed(hhmm: str, max_lag_minutes: int = 180) -> bool:
    """Время наступило и прошло не слишком давно: бот, поднятый ночью, не рассылает утренний план."""
    try:
        hour, minute = (int(part) for part in hhmm.split(":"))
    except ValueError:
        return False
    now = local_now()
    lag = (now.hour * 60 + now.minute) - (hour * 60 + minute)
    return 0 <= lag <= max_lag_minutes


async def in_work_hours() -> bool:
    return hours_between(await st.get_int("work_start"), await st.get_int("work_end"), local_now().hour)


async def in_quiet_hours() -> bool:
    return hours_between(await st.get_int("quiet_start"), await st.get_int("quiet_end"), local_now().hour)


# ---------- SLA ----------

async def sla_jobs() -> None:
    warn_minutes = await st.get_int("sla_warn_minutes")
    to_warn = await db.fetchall(
        "SELECT * FROM leads WHERE status = 'CLAIMED' AND sla_warned = 0 AND contact_deadline <= ?", (in_minutes(warn_minutes),)
    )
    for lead in to_warn:
        await db.execute("UPDATE leads SET sla_warned = 1 WHERE id = ?", (lead["id"],))
        await leads.dm(
            lead["assigned_to"],
            f"⏳ По лиду #{lead['id']} «{h(lead['title'])}» осталось {warn_minutes} минут. "
            f"Напишите клиенту и перешлите своё сообщение боту (кнопка «Написал — подтвердить»).",
            reply_markup=open_kb(lead["id"]),
        )
    expired = await db.fetchall("SELECT * FROM leads WHERE status = 'CLAIMED' AND contact_deadline <= ?", (now_iso(),))
    for lead in expired:
        await leads.release_by_timer(lead)


async def senior_sla_jobs() -> None:
    """ТЗ 8.4: передача должна быть принята за 2 рабочих часа, иначе узнаёт владелец."""
    hours = await st.get_int("senior_sla_hours")
    overdue = await db.fetchall(
        "SELECT * FROM leads WHERE status = 'HANDOFF' AND handoff_at <= ? AND aging_flags NOT LIKE '%senior_sla%'",
        (in_minutes(-hours * 60),),
    )
    for lead in overdue:
        await db.execute("UPDATE leads SET aging_flags = aging_flags || 'senior_sla,' WHERE id = ?", (lead["id"],))
        sdr = await leads.user(lead.get("assigned_to"))
        await _tell_owners(
            f"⏰ Передача по лиду #{lead['id']} «{h(lead['title'])}» от {mention(sdr)} не принята {hours} ч. "
            f"Разберите: /handoffs"
        )


# ---------- каденция ----------

async def cadence_jobs() -> None:
    if await in_quiet_hours():
        return
    due = await db.fetchall("SELECT * FROM leads WHERE status = 'CONTACTED' AND next_touch_at IS NOT NULL AND next_touch_at <= ?", (now_iso(),))
    for lead in due:
        # Следующее напоминание — завтра, если человек не нажмёт ни одну кнопку.
        await db.execute("UPDATE leads SET next_touch_at = ? WHERE id = ?", (in_days(1), lead["id"]))
        await leads.dm(
            lead["assigned_to"],
            f"🔔 Касание №{lead['touch_count'] + 1} по лиду #{lead['id']} «{h(lead['title'])}». "
            f"Клиент молчит с {fmt_time(lead['contacted_at'], with_date=True)}. Напишите второй раз — коротко, с новой пользой, без «напоминаю о себе».",
            reply_markup=touch_kb(lead["id"]),
        )


async def cadence_escalation_jobs() -> None:
    """ТЗ 7.2: просроченное касание > 48 ч — «зависший» лид старшему; > 5 дней — лид возвращается в очередь."""
    stall_hours = 48
    requeue_days = 5

    stalled = await db.fetchall(
        "SELECT * FROM leads WHERE status = 'CONTACTED' AND next_touch_at IS NOT NULL AND next_touch_at <= ? "
        "AND aging_flags NOT LIKE '%stall%'",
        (in_minutes(-stall_hours * 60),),
    )
    for lead in stalled:
        await db.execute("UPDATE leads SET aging_flags = aging_flags || 'stall,' WHERE id = ?", (lead["id"],))
        sdr = await leads.user(lead.get("assigned_to"))
        for senior in await leads.seniors():
            await leads.dm(
                senior["id"],
                f"🕒 Лид #{lead['id']} «{h(lead['title'])}» зависший: касание просрочено > {stall_hours} ч у {mention(sdr)}.",
                reply_markup=open_kb(lead["id"]),
            )

    expired = await db.fetchall(
        "SELECT * FROM leads WHERE status = 'CONTACTED' AND next_touch_at IS NOT NULL AND next_touch_at <= ?",
        (in_days(-requeue_days),),
    )
    for lead in expired:
        sdr_id = lead.get("assigned_to")
        await leads.update(lead["id"], {
            "status": "NEW", "assigned_to": None, "next_touch_at": None, "touch_count": 0, "aging_flags": "",
        })
        await leads.log_event(lead["id"], sdr_id, "requeued", f"{requeue_days}d без касания")
        await leads.dm(sdr_id, f"↩️ Лид #{lead['id']} «{h(lead['title'])}» вернулся в очередь: {requeue_days} дней без касания.")
        await leads.post_to_group(await leads.get(lead["id"]))


# ---------- старение очереди ----------

async def aging_jobs() -> None:
    if not await in_work_hours():
        return
    hot_minutes = await st.get_int("aging_hot_minutes")
    hot = await db.fetchall(
        "SELECT * FROM leads WHERE status = 'NEW' AND category = 'hot' AND aging_flags NOT LIKE '%ping%' AND created_at <= ?",
        (in_minutes(-hot_minutes),),
    )
    for lead in hot:
        await db.execute("UPDATE leads SET aging_flags = aging_flags || 'ping,' WHERE id = ?", (lead["id"],))
        sellers = await db.fetchall("SELECT id FROM users WHERE status = 'active' AND role IN ('sdr', 'senior')")
        for seller in sellers:
            await leads.dm(seller["id"], f"🔥 Горячий лид #{lead['id']} «{h(lead['title'])}» висит в очереди {hot_minutes} минут. Кто берёт?", reply_markup=open_kb(lead["id"]))

    senior_hours = await st.get_int("aging_senior_hours")
    stale = await db.fetchall(
        "SELECT * FROM leads WHERE status = 'NEW' AND aging_flags NOT LIKE '%senior%' AND created_at <= ?",
        (in_minutes(-senior_hours * 60),),
    )
    for lead in stale:
        await db.execute("UPDATE leads SET aging_flags = aging_flags || 'senior,' WHERE id = ?", (lead["id"],))
        for senior in await leads.seniors():
            await leads.dm(senior["id"], f"⚠️ Лид #{lead['id']} «{h(lead['title'])}» никто не взял за {senior_hours} ч. Разберите или закройте.", reply_markup=open_kb(lead["id"]))

    archive_days = await st.get_int("aging_archive_days")
    old = await db.fetchall("SELECT * FROM leads WHERE status = 'NEW' AND created_at <= ?", (in_days(-archive_days),))
    for lead in old:
        await leads.update(lead["id"], {"status": "ARCHIVED", "closed_at": now_iso()})
        # Карточку рисуем по свежей записи, иначе в группе останется статус «Новый».
        await leads.update_group_card(await leads.get(lead["id"]) or lead, f"📦 В архиве — {archive_days} дней без взятия")


# ---------- красный список ----------

async def dnc_jobs() -> None:
    for row in await dnc.expire_yellow():
        await dnc.remove(row["id"])
        for senior in await leads.seniors():
            await leads.dm(senior["id"], f"🟡 Жёлтый флаг снят: {'@' + row['username'] if row.get('username') else row['user_id']} — можно писать снова.")


# ---------- ежедневные ----------

async def daily_jobs() -> None:
    if _time_passed(await st.get("plan_time")) and await _once_per_day("plan"):
        await morning_plan()
    if _time_passed(await st.get("digest_time")) and await _once_per_day("digest"):
        await evening_digest()
    if local_now().weekday() == 0 and _time_passed("10:05") and await _once_per_day("board"):
        await weekly_board()
    if local_now().weekday() == 0 and _time_passed("10:15") and await _once_per_day("ownerreport"):
        await _tell_owners(await report.weekly())
    if local_now().weekday() == 0 and _time_passed("10:25") and await _once_per_day("discover"):
        await discover_donors()
    if local_now().weekday() == 0 and _time_passed("10:35") and await _once_per_day("chanstats"):
        await channel_stats_reminder()
    if _time_passed("03:00") and await _once_per_day("backup"):
        await backup()


async def channel_stats_reminder() -> None:
    """ТЗ 8.1: раз в неделю напоминаем обновить цифры в таблице каналов MORIER."""
    stale = await db.fetchall(
        "SELECT username, vertical FROM morier_channels WHERE stat_updated_at IS NULL OR stat_updated_at < ?",
        (in_days(-7),),
    )
    if not stale:
        return
    lines = ["📺 Пора обновить статистику каналов MORIER (7+ дней без обновления):"]
    lines.extend(f"• @{h(row['username'])} — {row['vertical']}" for row in stale[:15])
    lines.append("Обновление: /admin → Каналы MORIER → добавить заново той же строкой.")
    await _tell_owners("\n".join(lines))


async def morning_plan() -> None:
    sellers = await db.fetchall("SELECT * FROM users WHERE status = 'active' AND role IN ('sdr', 'senior')")
    hot_count = await db.scalar("SELECT COUNT(*) FROM leads WHERE status = 'NEW' AND category = 'hot'") or 0
    for seller in sellers:
        mine = await leads.my_leads(seller["id"])
        lines = [f"☀️ План на {local_now().strftime('%d.%m')}"]
        touches = [l for l in mine if l["status"] == CONTACTED and l.get("next_touch_at") and l["next_touch_at"] <= in_days(1)]
        pending = [l for l in mine if l["status"] == CLAIMED]
        replied = [l for l in mine if l["status"] == REPLIED]
        if pending:
            lines.append("⏱ Ждут первого контакта:\n" + "\n".join(leads.short_line(l) for l in pending))
        if replied:
            lines.append("💬 Ответили — квалифицируйте и передавайте:\n" + "\n".join(leads.short_line(l) for l in replied))
        if touches:
            lines.append("🔔 Касания сегодня:\n" + "\n".join(leads.short_line(l) for l in touches))
        lines.append(f"📋 В очереди горячих: {hot_count} — /queue")
        lines.append(f"🏆 Баллы за сезон: {await rating.total(seller['id'])}")
        await leads.dm(seller["id"], "\n\n".join(lines))


async def evening_digest() -> None:
    today = day_key()
    sellers = await db.fetchall("SELECT * FROM users WHERE status = 'active' AND role IN ('sdr', 'senior')")
    summary_lines = [f"🌙 Итоги {local_now().strftime('%d.%m')}"]
    for seller in sellers:
        stats = await db.fetchone(
            "SELECT SUM(type = 'contacted') AS contacts, SUM(type = 'replied') AS replies, SUM(type = 'claimed') AS claimed, "
            "SUM(type = 'released') AS released FROM lead_events WHERE user_id = ? AND created_at LIKE ?",
            (seller["id"], f"{today}%"),
        )
        points_today = await db.scalar("SELECT COALESCE(SUM(delta), 0) FROM points WHERE user_id = ? AND created_at LIKE ?", (seller["id"], f"{today}%")) or 0
        contacts, replies, claimed, released = (stats or {}).get("contacts") or 0, (stats or {}).get("replies") or 0, (stats or {}).get("claimed") or 0, (stats or {}).get("released") or 0
        personal = (
            f"🌙 Ваш день: взято {claimed}, контактов {contacts}, ответов {replies}, упущено по таймеру {released}. "
            f"Баллы за день: {points_today:+d}, за сезон: {await rating.total(seller['id'])}."
        )
        if contacts == 0 and claimed == 0:
            personal += "\nНи одного касания за день. Завтра — минимум три первых контакта."
        await leads.dm(seller["id"], personal)
        summary_lines.append(f"{mention(seller)}: {claimed} взято · {contacts} контактов · {replies} ответов · {points_today:+d}")
    new_today = await db.scalar("SELECT COUNT(*) FROM leads WHERE created_at LIKE ?", (f"{today}%",)) or 0
    summary_lines.append(f"\nНовых лидов за день: {new_today}. Расход ИИ за месяц: ${await ai.month_spent():.2f}")
    for boss in await db.fetchall("SELECT * FROM users WHERE status = 'active' AND role = 'owner'"):
        await leads.dm(boss["id"], "\n".join(summary_lines))


async def weekly_board() -> None:
    group_id = await st.get("leads_group_id")
    if not group_id or not runtime.bot:
        return
    board = await rating.leaderboard(limit=10)
    if not board:
        return
    lines = ["🏆 Рейтинг сезона " + local_now().strftime("%m.%Y")]
    for index, row in enumerate(board, start=1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"{index}.")
        lines.append(f"{medal} {mention(row)} — {row['pts']} б. · сделок {row['wins'] or 0} · ответов {row['replies'] or 0}")
    lines.append(f"⏱ Медиана первого контакта: {rating.fmt_minutes(await rating.median_first_contact())}")
    try:
        await runtime.bot.send_message(int(group_id), "\n".join(lines))
    except Exception as exc:  # noqa: BLE001
        log.info("Лидерборд не отправлен: %s", exc)


async def discover_donors() -> None:
    """Раз в неделю спрашиваем Telegram про каналы, похожие на наших доноров, и показываем владельцу лучших."""
    result = await scanner.discover()
    if not result.get("ok") or not result.get("fresh"):
        return
    top = await db.fetchall(
        "SELECT username, title, subscribers, votes, sources FROM donor_hints WHERE status = 'new' "
        "ORDER BY votes DESC, subscribers DESC LIMIT 5"
    )
    if not top:
        return
    lines = [f"💡 Нашёл {result['fresh']} новых каналов, похожих на ваших доноров:"]
    for row in top:
        subs = f", {row['subscribers'] // 1000}k подписчиков" if (row["subscribers"] or 0) >= 1000 else ""
        lines.append(f"• @{h(row['username'])} — {h(row['title'] or '')}{subs}\n  похож на {h(row['sources'] or '—')}")
    lines.append("Добавить или скрыть: /admin → Каналы-доноры → Кандидаты.")
    await _tell_owners("\n".join(lines))


async def backup() -> None:
    chat_id = await st.get("backup_chat_id")
    if not chat_id or not runtime.bot:
        return
    await db.conn.commit()
    snapshot = config.data_dir / f"backup-{day_key()}.sqlite3"
    try:
        await db.conn.execute("VACUUM INTO ?", (str(snapshot),))
        await runtime.bot.send_document(int(chat_id), FSInputFile(snapshot), caption=f"Бэкап базы LeadHunter {day_key()}")
    except Exception as exc:  # noqa: BLE001
        log.warning("Бэкап не отправлен: %s", exc)
    finally:
        if snapshot.exists():
            snapshot.unlink()


# ---------- здоровье ИИ ----------

_ai_alerted_at: float = 0.0
_cap_alerted_month: str = ""


async def ai_health_job() -> None:
    """Молчащий ИИ — самая дорогая поломка: лиды идут, но без анализа и черновиков. Владелец узнаёт сразу."""
    global _ai_alerted_at, _cap_alerted_month
    if not ai.enabled:
        return
    now_ts = local_now().timestamp()

    cap = await st.get_float("ai_monthly_cap_usd")
    spent = await ai.month_spent()
    if cap > 0 and spent >= cap and _cap_alerted_month != season():
        _cap_alerted_month = season()
        await _tell_owners(
            f"💸 Месячный лимит ИИ исчерпан: потрачено ${spent:.2f} из ${cap:.2f}.\n"
            "Анализ лидов переключился на эвристики. Поднимите лимит: /admin → ИИ → Лимит расхода."
        )

    if ai.fail_streak >= 3 and now_ts - _ai_alerted_at > 3600:
        _ai_alerted_at = now_ts
        await _tell_owners(
            f"⚠️ ИИ не отвечает: {ai.fail_streak} ошибок подряд.\nПричина: {h(ai.last_error or 'неизвестна')}\n"
            "Пока лиды скорятся по эвристикам. Проверка: /admin → ИИ → Проверить ИИ."
        )


async def _tell_owners(text: str) -> None:
    for boss in await db.fetchall("SELECT id FROM users WHERE status = 'active' AND role = 'owner'"):
        await leads.dm(boss["id"], text)


# ---------- поиск спроса ----------

_last_search_check: float = 0.0


async def search_job() -> None:
    global _last_search_check
    now_ts = local_now().timestamp()
    if now_ts - _last_search_check < 1800 or not await in_work_hours():
        return
    _last_search_check = now_ts
    created = await search_poller.run_due()
    if created:
        log.info("Поиск по ключевым словам дал %s новых лидов", created)


# ---------- пул API-ключей ----------

_key_pool_alerted_at: str = ""


async def key_pool_job() -> None:
    """ТЗ 2.1a: уведомление владельцу при остатке пула < 20% и за 3 дня до исчерпания по темпу."""
    global _key_pool_alerted_at
    alerts = await keypool.low_quota_alerts()
    if not alerts:
        return
    key = f"{season()}:{day_key()}"
    if _key_pool_alerted_at == key:
        return
    _key_pool_alerted_at = key
    await _tell_owners("\n".join(alerts))


# ---------- реальный охват размещений (ТЗ 4.1) ----------

_last_reach_check: float = 0.0


async def reach_recheck_job() -> None:
    """Просмотры рекламного поста перечитываются через 24 и 48 ч — реальный охват, а не заявленный."""
    global _last_reach_check
    now_ts = local_now().timestamp()
    if now_ts - _last_reach_check < 1800:
        return
    _last_reach_check = now_ts
    await scanner.recheck_views()
