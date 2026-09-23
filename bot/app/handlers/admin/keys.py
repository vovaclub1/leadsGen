"""API-ключи Trustat: добавление с проверкой, лимиты, роли, ключевые слова поиска."""
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.callbacks import AdmCb
from app.db import db
from app.filters import OWNER
from app.handlers.helpers import show as _show
from app.keyboards import back_kb, confirm_kb, key_kb, keys_kb, kw_item_kb, kw_skip_kb, limits_kb, scope_kb
from app.services import keypool, search_poller, trustat
from app.utils import fmt_date, h, now_iso

router = Router(name="admin-keys")


class AddKey(StatesGroup):
    key = State()
    limits = State()
    label = State()
    keywords = State()


class AddKeyword(StatesGroup):
    word = State()


async def keys_view(target) -> None:
    keys = await keypool.list_keys()
    pool = await keypool.pool_summary()
    text = (
        "<b>🔑 API-ключи Trustat</b>\n"
        f"Stat: {pool['stat'][2]} ключей, осталось {pool['stat'][0]} из {pool['stat'][1]} в месяц\n"
        f"Search: {pool['search'][2]} ключей, осталось {pool['search'][0]} из {pool['search'][1]}\n\n"
        "Бот держит 20% месячной квоты в резерве под передачи старшему. Ключи с ролью Search работают по ключевым словам "
        "раз в 2 дня, а слова, по которым давно ничего не находится, опрашиваются реже — экономят квоту для рабочих слов."
    )
    await _show(target, text, keys_kb(keys))


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "list")), OWNER)
async def keys_list(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await keys_view(query)


async def key_detail(target, key_id: int) -> None:
    key = await keypool.get_key(key_id)
    if not key:
        await _show(target, "Ключ удалён.", back_kb("key"))
        return
    words = await db.fetchall("SELECT * FROM keywords WHERE api_key_id = ? AND active = 1", (key_id,))
    text = (
        f"<b>{h(key['label'] or 'ключ')}</b> · …{key['key_tail']}\n"
        f"Статус: {key['status']} · Роли: {key['scopes']} · План: {h(key['plan'] or '—')}\n"
        f"Сегодня: {key['used_day']}/{key['limit_day'] or '∞'} · Месяц: {key['used_month']}/{key['limit_month'] or '∞'}\n"
    )
    if key.get("last_error"):
        text += f"Последняя ошибка: {h(key['last_error'])}\n"
    if words:
        # Trustat — платный провайдер №2 по ключу; MTProto (бесплатный провайдер №1, ТЗ 4.2) гоняет те же слова без квоты.
        text += "\nКлючевые слова:\n" + "\n".join(
            f"• {h(w['word'])} — Trustat {w['found']} · MTProto {w['mtproto_found'] or 0} · /kw_{w['id']}" for w in words
        )
    await _show(target, text, key_kb(key))


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "open")), OWNER)
async def key_open(query: CallbackQuery, callback_data: AdmCb) -> None:
    await key_detail(query, callback_data.id)


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a.in_({"enable", "disable"}))), OWNER)
async def key_toggle(query: CallbackQuery, callback_data: AdmCb) -> None:
    await keypool.set_status(callback_data.id, "active" if callback_data.a == "enable" else "disabled")
    await key_detail(query, callback_data.id)


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "probe")), OWNER)
async def key_probe(query: CallbackQuery, callback_data: AdmCb) -> None:
    key = await keypool.get_key(callback_data.id)
    if not key:
        await query.answer("Ключ удалён.", show_alert=True)
        return
    info, error = await trustat.probe_key(keypool.secret(key))
    if info is None:
        await query.answer(f"Ошибка: {error}", show_alert=True)
        return
    await query.answer("Ключ отвечает. Данные ниже.")
    # Раз уж сходили за квотой — приводим наши счётчики к тому, что говорит API.
    if info.get("limit_month") is not None:
        await keypool.sync_quota(key["id"], info["used_month"], info["limit_month"])
    await query.message.answer(f"<b>Квота ключа</b>\n{h(trustat.format_usage(info))}")


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "del")), OWNER)
async def key_delete_confirm(query: CallbackQuery, callback_data: AdmCb) -> None:
    await _show(query, "Удалить ключ? Его ключевые слова тоже отключатся.", confirm_kb("key", "deld", callback_data.id))


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "deld")), OWNER)
async def key_delete(query: CallbackQuery, callback_data: AdmCb) -> None:
    await keypool.delete_key(callback_data.id)
    await query.answer("Удалён.")
    await keys_view(query)


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "add")), OWNER)
async def key_add(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddKey.key)
    await query.message.answer("Пришлите API-ключ Trustat одним сообщением. Сообщение с ключом бот сразу удалит. /cancel — отмена.")
    await query.answer()


@router.message(AddKey.key, OWNER, F.text)
async def key_receive(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    if len(raw) < 16 or " " in raw:
        await message.answer("Не похоже на ключ. Пришлите ещё раз или /cancel.")
        return
    if await db.fetchone("SELECT 1 FROM api_keys WHERE key_tail = ?", (raw[-4:],)):
        await message.answer("Ключ с таким окончанием уже есть. Если это другой ключ — продолжайте, иначе /cancel.")
    wait = await message.answer("Проверяю ключ…")
    info, error = await trustat.probe_key(raw)
    plan = None
    if info is None:
        await wait.edit_text(f"Trustat не принял ключ: {error}. Можно всё равно сохранить (если API временно недоступно) или /cancel.")
    else:
        plan = str(info.get("plan") or "") or None
        # API сам знает лимит и разрешённые разделы — подставляем их, чтобы владелец не гадал.
        await state.update_data(api_limit_month=info.get("limit_month"), api_packages=info.get("packages") or "")
        await wait.edit_text(f"<b>Ключ принят.</b>\n{h(trustat.format_usage(info))}")
    await state.update_data(raw_key=raw, plan=plan)
    data = await state.get_data()
    hint = data.get("api_packages")
    await message.answer(
        "Какие роли у ключа?" + (f"\nПо данным Trustat ключу доступно: <b>{h(hint)}</b>." if hint else ""),
        reply_markup=scope_kb(hint),
    )


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "scope")), OWNER)
async def key_scope(query: CallbackQuery, callback_data: AdmCb, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("raw_key"):
        await query.answer("Начните добавление заново.", show_alert=True)
        return
    await state.update_data(scopes=callback_data.v)
    await query.message.edit_text(
        f"Роли: {callback_data.v}. Лимиты запросов?", reply_markup=limits_kb(data.get("api_limit_month"))
    )
    await query.answer()


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "lim")), OWNER)
async def key_limits(query: CallbackQuery, callback_data: AdmCb, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("raw_key"):
        await query.answer("Начните добавление ��аново.", show_alert=True)
        return
    if callback_data.v == "custom":
        await state.set_state(AddKey.limits)
        await query.message.edit_text("Введите лимиты через запятую: <code>день,месяц</code> (0 — без лимита). Например <code>10,50</code>.")
        await query.answer()
        return
    day, month = (int(part) for part in callback_data.v.split(","))
    await state.update_data(limit_day=day, limit_month=month)
    await state.set_state(AddKey.label)
    await query.message.edit_text("Название ключа (например «аккаунт Макс» или «платный №1»):")
    await query.answer()


@router.message(AddKey.limits, OWNER, F.text)
async def key_limits_custom(message: Message, state: FSMContext) -> None:
    match = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", message.text or "")
    if not match:
        await message.answer("Формат: день,месяц — например 10,50")
        return
    await state.update_data(limit_day=int(match.group(1)), limit_month=int(match.group(2)))
    await state.set_state(AddKey.label)
    await message.answer("Название ключа:")


@router.message(AddKey.label, OWNER, F.text)
async def key_label(message: Message, state: FSMContext) -> None:
    await state.update_data(label=message.text.strip()[:60])
    data = await state.get_data()
    if "search" in data.get("scopes", ""):
        await state.set_state(AddKey.keywords)
        await message.answer(
            "Ключевые слова для поиска спроса, через запятую. Например:\n<code>ищу каналы для рекламы, куплю рекламу в телеграм, нужна реклама канала</code>",
            reply_markup=kw_skip_kb(),
        )
        return
    await _save_key(message, state, [])


async def _save_key(message: Message, state: FSMContext, words: list[str]) -> None:
    data = await state.get_data()
    await state.clear()
    key_id = await keypool.add_key(data["raw_key"], data.get("label") or "ключ", data.get("scopes", "stat"), int(data.get("limit_day", 0)), int(data.get("limit_month", 0)), data.get("plan"))
    for word in words:
        await db.execute("INSERT INTO keywords (word, api_key_id, active, created_at) VALUES (?, ?, 1, ?)", (word, key_id, now_iso()))
    await message.answer(f"Ключ «{h(data.get('label') or 'ключ')}» сохранён (#{key_id})." + (f" Ключевых слов: {len(words)}." if words else ""))
    await key_detail(message, key_id)


@router.message(AddKey.keywords, OWNER, F.text)
async def key_keywords(message: Message, state: FSMContext) -> None:
    words = [w.strip() for w in message.text.split(",") if len(w.strip()) >= 4][:10]
    await _save_key(message, state, words)


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "kwskip")), OWNER, AddKey.keywords)
async def key_keywords_skip(query: CallbackQuery, state: FSMContext) -> None:
    await _save_key(query.message, state, [])
    await query.answer()


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "kwadd")), OWNER)
async def keyword_add(query: CallbackQuery, callback_data: AdmCb, state: FSMContext) -> None:
    await state.set_state(AddKeyword.word)
    await state.update_data(key_id=callback_data.id)
    await query.message.answer("Ключевое слово или фраза (можно несколько через запятую):")
    await query.answer()


@router.message(AddKeyword.word, OWNER, F.text)
async def keyword_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    words = [w.strip() for w in message.text.split(",") if len(w.strip()) >= 4][:10]
    for word in words:
        await db.execute("INSERT INTO keywords (word, api_key_id, active, created_at) VALUES (?, ?, 1, ?)", (word, data["key_id"], now_iso()))
    await message.answer(f"Добавлено слов: {len(words)}.")
    await key_detail(message, data["key_id"])


@router.message(F.text.regexp(r"^/kw_(\d+)$").as_("match"), OWNER)
async def keyword_open(message: Message, match) -> None:
    word = await db.fetchone("SELECT * FROM keywords WHERE id = ?", (int(match.group(1)),))
    if not word:
        await message.answer("Слово не найдено.")
        return
    streak = word["empty_streak"] or 0
    pace = (
        f"тишина {streak} заход(ов) подряд — опрашиваю раз в {search_poller.interval_for(streak).days} дн."
        if streak
        else "находит посты — опрашиваю раз в 2 дня"
    )
    await message.answer(
        f"«{h(word['word'])}»\n"
        f"Trustat (провайдер №2): найдено {word['found']} · последний запуск: {fmt_date(word['last_run']) if word['last_run'] else 'ещё не было'} · {pace}\n"
        f"MTProto (провайдер №1, бесплатный): найдено {word['mtproto_found'] or 0} · последний запуск: "
        f"{fmt_date(word['mtproto_last_run']) if word.get('mtproto_last_run') else 'ещё не было'}",
        reply_markup=kw_item_kb(word["id"], word["api_key_id"]),
    )


@router.callback_query(AdmCb.filter((F.s == "key") & (F.a == "kwdel")), OWNER)
async def keyword_delete(query: CallbackQuery, callback_data: AdmCb) -> None:
    word = await db.fetchone("SELECT * FROM keywords WHERE id = ?", (callback_data.id,))
    if word:
        await db.execute("UPDATE keywords SET active = 0 WHERE id = ?", (callback_data.id,))
        await query.answer("Удалено.")
        await key_detail(query, word["api_key_id"])
    else:
        await query.answer("Уже удалено.", show_alert=True)
