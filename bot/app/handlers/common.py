"""/start, меню, справка, рейтинг, привязка группы, проверка контакта."""
from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import settings_store as st
from app.callbacks import MenuCb
from app.filters import OWNER, SELLERS
from app.keyboards import ROLE_RU, main_menu, queue_item_kb
from app.services import dnc, leads, rating
from app.services.keypool import pool_summary
from app.services.scanner import scanner
from app.utils import h, mention

router = Router(name="common")


def help_text(role: str, sla: int, limit: int) -> str:
    base = (
        "<b>Как работать в LeadHunter</b>\n\n"
        f"1. Лид появляется в группе. Нажмите «Беру» — карточка и черновик придут в личку. Лимит активных лидов: {limit}.\n"
        f"2. У вас {sla} минут: напишите клиенту в Telegram, затем <b>перешлите боту своё отправленное сообщение</b> "
        "(кнопка «Написал — подтвердить»). Без пересылки контакт не засчитан, лид вернётся в очередь с −5.\n"
        "3. Перед отправкой прогоните текст через «Проверить сообщение»: бот ловит шаблонность, цены и лишние ссылки.\n"
        "4. Клиент молчит — бот напомнит о касании (3 → 7 → 7 дней). Ответил — перешлите его ответ («Клиент ответил»).\n"
        "5. Выяснили бюджет, задачу и сроки — «Передать старшему». Принят → +25, сделка → +50.\n"
        "6. Клиент просит не писать — жмите «Просил не писать». Это +2, а не минус. Написать контакту из красного списка — −15.\n\n"
        "<b>Баллы:</b> контакт +10 · ответ +15 · принят +25 · сделка +50 · нецелевой +1 · таймер −5\n"
        "<b>Команды:</b> /queue /my /me /top /add /check /find /cancel"
    )
    if role in ("owner", "senior"):
        base += "\n<b>Старшему:</b> /handoffs — передачи, /blacklist @канал причина — стоп-лист сущностей, /dnc @user причина — красный список"
    if role == "owner":
        base += "\n<b>Владельцу:</b> /admin — сотрудники, ключи, красный список, доноры, настройки, дашборд. /adjust @user ±N причина — ручные баллы. /bind в группе лидов, /bind_backup в чате для бэкапов."
    return base


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, me: dict, state: FSMContext) -> None:
    await state.clear()
    role = me["role"]
    greeting = f"Привет, {h(me.get('full_name') or 'коллега')}. Ваша роль: <b>{ROLE_RU[role]}</b>."
    if role == "buyer":
        greeting += "\nБайеру доступны /check @username (проверка красного списка) и /find @канал (карточка канала)."
    elif role == "owner":
        group = await st.get("leads_group_id")
        pool = await pool_summary()
        greeting += (
            f"\n\nГруппа лидов: {'привязана' if group else '<b>не привязана</b> — добавьте бота в группу и отправьте там /bind'}"
            f"\nСканер доноров: {'работает' if scanner.client else ('сессия не создана — python login_scanner.py' if scanner.configured else 'выключен (нет TG_API_ID)')}"
            f"\nAPI-ключи: Stat {pool['stat'][2]} · Search {pool['search'][2]}"
            f"\n\nНачните с /admin → Сотрудники, затем добавьте ключи и доноров."
        )
    await message.answer(greeting, reply_markup=main_menu(role))


@router.message(Command("help"))
async def help_cmd(message: Message, me: dict) -> None:
    await message.answer(help_text(me["role"], await st.get_int("sla_minutes"), await st.get_int("max_active")))


@router.callback_query(MenuCb.filter(F.a == "help"))
async def help_cb(query: CallbackQuery, me: dict) -> None:
    await query.message.answer(help_text(me["role"], await st.get_int("sla_minutes"), await st.get_int("max_active")))
    await query.answer()


@router.message(Command("id"))
async def my_id(message: Message) -> None:
    text = f"Ваш Telegram ID: <code>{message.from_user.id}</code>"
    if message.chat.type != "private":
        text += f"\nID этого чата: <code>{message.chat.id}</code>"
    await message.answer(text)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext, me: dict) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu(me["role"]))


@router.message(Command("menu"))
async def menu(message: Message, me: dict) -> None:
    await message.answer("Меню", reply_markup=main_menu(me["role"]))


# ---------- очередь и мои лиды ----------

async def render_queue() -> tuple[str, list[dict]]:
    rows = await leads.queue(limit=10)
    if not rows:
        return "Очередь пуста. Добавьте лид вручную: /add @канал", []
    return f"<b>Очередь ({len(rows)} из топа по скорингу)</b>", rows


@router.message(Command("queue"), SELLERS)
async def queue_cmd(message: Message) -> None:
    text, rows = await render_queue()
    await message.answer(text)
    for lead in rows:
        await message.answer(leads.short_line(lead), reply_markup=queue_item_kb(lead["id"]))


@router.callback_query(MenuCb.filter(F.a == "queue"), SELLERS)
async def queue_cb(query: CallbackQuery) -> None:
    text, rows = await render_queue()
    await query.message.answer(text)
    for lead in rows:
        await query.message.answer(leads.short_line(lead), reply_markup=queue_item_kb(lead["id"]))
    await query.answer()


async def render_my(me: dict) -> str:
    rows = await leads.my_leads(me["id"])
    if not rows:
        return "У вас нет активных лидов. Загляните в очередь: /queue"
    lines = [f"<b>Мои лиды ({len(rows)} из {await st.get_int('max_active')})</b>"]
    for lead in rows:
        lines.append(f"{leads.short_line(lead)} — {leads.STATUS_RU[lead['status']]} · /lead_{lead['id']}")
    return "\n".join(lines)


@router.message(Command("my"), SELLERS)
async def my_cmd(message: Message, me: dict) -> None:
    await message.answer(await render_my(me))


@router.callback_query(MenuCb.filter(F.a == "my"), SELLERS)
async def my_cb(query: CallbackQuery, me: dict) -> None:
    await query.message.answer(await render_my(me))
    await query.answer()


@router.message(F.text.regexp(r"^/lead_?(\d+)$").as_("match"), F.chat.type == "private")
async def open_lead(message: Message, me: dict, match) -> None:
    lead = await leads.get(int(match.group(1)))
    if not lead:
        await message.answer("Лид не найден.")
        return
    if me["role"] == "sdr" and lead.get("assigned_to") not in (None, me["id"]) and lead["status"] != "NEW":
        await message.answer("Этот лид ведёт другой менеджер.")
        return
    await leads.send_private_card(lead, me)


# ---------- рейтинг ----------

async def render_top(me: dict) -> str:
    board = await rating.leaderboard(limit=10)
    if not board:
        return "Пока никого в рейтинге."
    lines = ["<b>🏆 Рейтинг сезона</b>"]
    for index, row in enumerate(board, start=1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"{index}.")
        marker = " ← вы" if row["id"] == me["id"] else ""
        lines.append(f"{medal} {mention(row)} — {row['pts']} б. · сделок {row['wins'] or 0} · ответов {row['replies'] or 0}{marker}")
    return "\n".join(lines)


@router.message(Command("top"))
async def top_cmd(message: Message, me: dict) -> None:
    await message.answer(await render_top(me))


@router.callback_query(MenuCb.filter(F.a == "top"))
async def top_cb(query: CallbackQuery, me: dict) -> None:
    await query.message.answer(await render_top(me))
    await query.answer()


@router.message(Command("me"), SELLERS)
async def me_cmd(message: Message, me: dict) -> None:
    total = await rating.total(me["id"])
    place = await rating.rank(me["id"])
    history = await rating.history(me["id"], limit=8)
    active = len(await leads.my_leads(me["id"]))
    lines = [f"<b>{mention(me)}</b> · {ROLE_RU[me['role']]}", f"Баллы за сезон: <b>{total}</b>" + (f" · место {place}" if place else ""), f"Активных лидов: {active}"]
    if history:
        lines.append("\nПоследние начисления:")
        for row in history:
            lines.append(f"{row['delta']:+d} — {h(row['reason'])}" + (f" (лид #{row['lead_id']})" if row.get("lead_id") else ""))
    await message.answer("\n".join(lines))


# ---------- проверка контакта и поиск ----------

@router.message(Command("check"))
async def check_cmd(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Формат: /check @username — проверка красного списка. Текст сообщения проверяйте кнопкой «Проверить сообщение» на карточке.")
        return
    ref = leads.parse_ref(arg)
    username = ref["username"] if ref else None
    user_id = int(arg) if arg.isdigit() else None
    row = await dnc.check(username=username, user_id=user_id)
    if not row:
        await message.answer(f"✅ {h(arg)} — не в списках, писать можно.")
        return
    await message.answer(f"🚫 {h(arg)} — {dnc.badge(row)}")


@router.message(Command("find"))
async def find_cmd(message: Message, command: CommandObject) -> None:
    from app.services import trustat

    arg = (command.args or "").strip()
    ref = leads.parse_ref(arg)
    if not ref or not ref.get("username"):
        await message.answer("Формат: /find @канал")
        return
    stat, error = await trustat.channel_stat(ref["username"])
    if stat is None:
        reasons = {"quota": "квота ключей исчерпана", "no_key": "нет активных Stat-ключей", "not_found": "канал не найден в Trustat", "banned": "ключ заблокирован"}
        await message.answer(f"Не удалось получить статистику: {reasons.get(error, error)}")
        return
    await message.answer(f"<b>@{h(ref['username'])}</b>\n{trustat.format_stat(stat)}")


# ---------- привязка чатов (владелец) ----------

@router.message(Command("bind"), OWNER, F.chat.type.in_({"group", "supergroup"}))
async def bind_group(message: Message) -> None:
    await st.set_value("leads_group_id", str(message.chat.id))
    await message.answer("Эта группа теперь получает карточки лидов. Проверьте, что у бота есть право писать и редактировать сообщения.")


@router.message(Command("bind_backup"), OWNER)
async def bind_backup(message: Message) -> None:
    await st.set_value("backup_chat_id", str(message.chat.id))
    await message.answer("Сюда будут приходить ночные бэкапы базы (03:00).")


@router.message(Command("bind"), OWNER, F.chat.type == "private")
async def bind_hint(message: Message) -> None:
    await message.answer("Команду /bind нужно отправить внутри группы, куда добавлен бот.")
