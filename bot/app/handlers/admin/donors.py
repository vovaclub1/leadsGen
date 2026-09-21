"""Каналы-доноры сканера и кандидаты из рекомендаций Telegram."""
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.callbacks import AdmCb
from app.db import db
from app.filters import OWNER
from app.handlers.helpers import show as _show
from app.keyboards import donor_hints_kb, donor_item_kb, donors_kb
from app.services import leads
from app.services.scanner import scanner
from app.utils import fmt_date, h, now_iso

router = Router(name="admin-donors")


class AddDonor(StatesGroup):
    text = State()


async def donors_view(target) -> None:
    rows = await db.fetchall("SELECT * FROM donors WHERE active = 1 ORDER BY ads_found DESC, id")
    state_line = "Сканер работает." if scanner.client else ("Сканер не авторизован — выполните на сервере <code>python login_scanner.py</code>." if scanner.configured else "Сканер выключен: задайте TG_API_ID и TG_API_HASH в .env.")
    hints = await db.scalar("SELECT COUNT(*) FROM donor_hints WHERE status = 'new'") or 0
    text = f"<b>📡 Каналы-доноры</b>\nБот читает их посты и вылавливает рекламодателей. 📡 — вступил, ⏳ — ждёт вступления.\n{state_line}"
    await _show(target, text, donors_kb(rows, hints))


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "list")), OWNER)
async def donors_list(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await donors_view(query)


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "open")), OWNER)
async def donor_open(query: CallbackQuery, callback_data: AdmCb) -> None:
    row = await db.fetchone("SELECT * FROM donors WHERE id = ?", (callback_data.id,))
    if not row:
        await query.answer("Удалён.", show_alert=True)
        return
    recent = await db.fetchall("SELECT advertiser_key, created_at FROM ad_posts WHERE donor = ? ORDER BY id DESC LIMIT 5", (row["username"].lower(),))
    text = f"<b>@{h(row['username'])}</b> {h(row.get('title') or '')}\nНайдено реклам: {row['ads_found']} · {'в канале' if row['joined'] else 'ещё не вступил'}"
    if recent:
        text += "\n\nПоследние рекламодатели:\n" + "\n".join(f"• {h(r['advertiser_key'].split(':', 1)[-1])} — {fmt_date(r['created_at'])}" for r in recent)
    await _show(query, text, donor_item_kb(row["id"]))


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "del")), OWNER)
async def donor_delete(query: CallbackQuery, callback_data: AdmCb) -> None:
    await db.execute("UPDATE donors SET active = 0 WHERE id = ?", (callback_data.id,))
    await scanner.reload_donors()
    await query.answer("Убран из сканера.")
    await donors_view(query)


async def hints_view(target, note: str = "") -> None:
    rows = await db.fetchall("SELECT * FROM donor_hints WHERE status = 'new' ORDER BY votes DESC, subscribers DESC LIMIT 15")
    lines = ["<b>💡 Кандидаты в доноры</b>"]
    if note:
        lines.append(note)
    if rows:
        lines.append("Каналы, которые Telegram считает похожими на ваших доноров. Чем больше «похож на», тем ближе аудитория.")
        for row in rows:
            subs = f"{row['subscribers']:,}".replace(",", " ") + " подписчиков" if row.get("subscribers") else "подписчики неизвестны"
            lines.append(f"• <b>@{h(row['username'])}</b> — {h(row.get('title') or '')}\n  {subs} · похож на {h(row.get('sources') or '—')}")
    else:
        lines.append("Пока пусто. Нажмите «Искать ещё» — бот опросит Telegram по вашим действующим донорам.")
    await _show(target, "\n".join(lines), donor_hints_kb(rows))


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "hints")), OWNER)
async def donor_hints(query: CallbackQuery) -> None:
    await hints_view(query)


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "scan")), OWNER)
async def donor_hints_scan(query: CallbackQuery) -> None:
    await query.answer("Опрашиваю Telegram, это займёт полминуты…")
    result = await scanner.discover()
    if not result["ok"]:
        await hints_view(query, f"⚠️ {h(result['error'])}")
        return
    await hints_view(query, f"Опрошено доноров: {result['checked']}, новых кандидатов: {result['fresh']}.")


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "hadd")), OWNER)
async def donor_hint_add(query: CallbackQuery, callback_data: AdmCb, me: dict) -> None:
    row = await db.fetchone("SELECT * FROM donor_hints WHERE id = ?", (callback_data.id,))
    if not row:
        await query.answer("Кандидат уже обработан.", show_alert=True)
        return
    existing = await db.fetchone("SELECT id FROM donors WHERE LOWER(username) = ?", (row["username"].lower(),))
    if existing:
        await db.execute("UPDATE donors SET active = 1, joined = 0 WHERE id = ?", (existing["id"],))
    else:
        await db.execute(
            "INSERT INTO donors (username, title, joined, added_by, created_at) VALUES (?, ?, 0, ?, ?)",
            (row["username"], row.get("title"), me["id"], now_iso()),
        )
    await db.execute("UPDATE donor_hints SET status = 'added' WHERE id = ?", (row["id"],))
    await scanner.reload_donors()
    await query.answer(f"@{row['username']} добавлен — сканер вступит в канал.")
    await hints_view(query)


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "hhide")), OWNER)
async def donor_hint_hide(query: CallbackQuery, callback_data: AdmCb) -> None:
    await db.execute("UPDATE donor_hints SET status = 'hidden' WHERE id = ?", (callback_data.id,))
    await query.answer("Больше не предложу.")
    await hints_view(query)


@router.callback_query(AdmCb.filter((F.s == "don") & (F.a == "add")), OWNER)
async def donor_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddDonor.text)
    await query.message.answer("Пришлите каналы списком — @username или ссылки, по одному в строке или через запятую. /cancel — отмена.")
    await query.answer()


@router.message(AddDonor.text, OWNER, F.text)
async def donor_save(message: Message, me: dict, state: FSMContext) -> None:
    await state.clear()
    added, skipped = [], []
    for chunk in re.split(r"[\s,]+", message.text):
        ref = leads.parse_ref(chunk)
        if not ref or not ref.get("username"):
            continue
        username = ref["username"]
        existing = await db.fetchone("SELECT * FROM donors WHERE LOWER(username) = ?", (username.lower(),))
        if existing:
            if not existing["active"]:
                await db.execute("UPDATE donors SET active = 1, joined = 0 WHERE id = ?", (existing["id"],))
                added.append(username)
            else:
                skipped.append(username)
            continue
        await db.execute("INSERT INTO donors (username, added_by, active, joined, created_at) VALUES (?, ?, 1, 0, ?)", (username, me["id"], now_iso()))
        added.append(username)
    await scanner.reload_donors()
    text = f"Добавлено: {len(added)}" + (f" ({', '.join('@' + u for u in added[:10])})" if added else "")
    if skipped:
        text += f"\nУже были: {', '.join('@' + u for u in skipped[:10])}"
    if added and scanner.client:
        text += "\nСканер вступает в каналы с паузами по 8 секунд — это защита аккаунта от флуд-бана."
    await message.answer(text)
    await donors_view(message)
