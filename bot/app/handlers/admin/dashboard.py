"""Корень админ-панели, дашборд за 30 дней и недельный отчёт."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import settings_store as st
from app.callbacks import AdmCb
from app.config import config
from app.db import db
from app.filters import OWNER
from app.handlers.helpers import show as _show
from app.keyboards import admin_root, back_kb
from app.services import keypool, leads, report
from app.services.ai import ai
from app.services.scanner import scanner
from app.utils import h, in_days

router = Router(name="admin-dashboard")


async def root_text() -> str:
    users = await db.scalar("SELECT COUNT(*) FROM users WHERE status = 'active'") or 0
    pool = await keypool.pool_summary()
    queue_size = await db.scalar("SELECT COUNT(*) FROM leads WHERE status = 'NEW'") or 0
    active = await db.scalar("SELECT COUNT(*) FROM leads WHERE status IN ('CLAIMED','CONTACTED','REPLIED','HANDOFF','ACCEPTED')") or 0
    donors = await db.scalar("SELECT COUNT(*) FROM donors WHERE active = 1") or 0
    scanner_state = "работает" if scanner.client else ("нет сессии — python login_scanner.py" if scanner.configured else "выключен")
    return (
        "<b>⚙️ Админ-панель</b>\n"
        f"Сотрудников: {users} · В очереди: {queue_size} · В работе: {active}\n"
        f"Stat-ключей: {pool['stat'][2]} (осталось {pool['stat'][0]}/{pool['stat'][1]} в мес.) · Search-ключей: {pool['search'][2]} ({pool['search'][0]}/{pool['search'][1]})\n"
        f"Доноров: {donors} · Сканер: {scanner_state}\n"
        f"ИИ за месяц: ${await ai.month_spent():.2f} из ${await st.get_float('ai_monthly_cap_usd'):.0f}"
    )


@router.message(Command("admin"), OWNER)
async def admin_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(await root_text(), reply_markup=admin_root())


@router.callback_query(AdmCb.filter(F.s == "root"), OWNER)
async def admin_root_cb(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show(query, await root_text(), admin_root())


@router.callback_query(AdmCb.filter(F.s == "rep"), OWNER)
async def weekly_report(query: CallbackQuery) -> None:
    await query.answer("Собираю…")
    await _show(query, await report.weekly(), back_kb("root"))


@router.callback_query(AdmCb.filter(F.s == "dash"), OWNER)
async def dashboard(query: CallbackQuery) -> None:
    week = in_days(-7)
    month = in_days(-30)
    created_week = await db.scalar("SELECT COUNT(*) FROM leads WHERE created_at > ?", (week,)) or 0
    by_source = await db.fetchall("SELECT source, COUNT(*) AS c FROM leads WHERE created_at > ? GROUP BY source", (month,))
    by_status = await db.fetchall("SELECT status, COUNT(*) AS c FROM leads WHERE created_at > ? GROUP BY status ORDER BY c DESC", (month,))
    funnel = await db.fetchone(
        "SELECT SUM(claimed_at IS NOT NULL) AS claimed, SUM(contacted_at IS NOT NULL) AS contacted, SUM(replied_at IS NOT NULL) AS replied, "
        "SUM(status = 'WON') AS won, COALESCE(SUM(won_amount), 0) AS revenue, COALESCE(SUM(won_margin), 0) AS margin FROM leads WHERE created_at > ?",
        (month,),
    ) or {}
    released = await db.scalar("SELECT COUNT(*) FROM lead_events WHERE type = 'released' AND created_at > ?", (month,)) or 0
    dnc_hits = await db.scalar("SELECT COUNT(*) FROM lead_events WHERE type = 'dnc' AND created_at > ?", (month,)) or 0
    not_target = await db.fetchall("SELECT lost_reason, COUNT(*) AS c FROM leads WHERE status = 'NOT_TARGET' AND created_at > ? GROUP BY lost_reason ORDER BY c DESC LIMIT 5", (month,))
    ad_top = await db.fetchall("SELECT donor, COUNT(*) AS c FROM ad_posts WHERE created_at > ? GROUP BY donor ORDER BY c DESC LIMIT 5", (month,))

    contacted = funnel.get("contacted") or 0
    replied = funnel.get("replied") or 0
    lines = [
        "<b>📊 Дашборд · 30 дней</b>",
        f"Новых лидов: за неделю {created_week}",
        "Источники: " + (", ".join(f"{leads.SOURCE_RU.get(r['source'], r['source'])} {r['c']}" for r in by_source) or "—"),
        f"Воронка: взято {funnel.get('claimed') or 0} → контакт {contacted} → ответ {replied} ({(replied * 100 // contacted) if contacted else 0}%) → сделок {funnel.get('won') or 0}",
        f"Выручка: {int(funnel.get('revenue') or 0):,} ₽ · маржа {int(funnel.get('margin') or 0):,} ₽".replace(",", " "),
        f"Упущено по таймеру: {released} · «Просил не писать»: {dnc_hits}",
        "Статусы: " + (", ".join(f"{leads.STATUS_RU.get(r['status'], r['status'])} {r['c']}" for r in by_status) or "—"),
    ]
    if not_target:
        lines.append("Нецелевые: " + ", ".join(f"{h(r['lost_reason'] or '?')} {r['c']}" for r in not_target))
    if ad_top:
        lines.append("Доноры по улову: " + ", ".join(f"@{h(r['donor'])} {r['c']}" for r in ad_top))
    lines.append(f"ИИ за месяц: ${await ai.month_spent():.2f}")
    await _show(query, "\n".join(lines), back_kb("root"))
