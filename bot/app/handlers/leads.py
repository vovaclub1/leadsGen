"""Кнопки карточки: беру / написал / ответил / касание / нецелевой / просил не писать / передать / заметка / проверка текста."""
from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, MessageOriginHiddenUser, MessageOriginUser

from app import settings_store as st
from app.callbacks import LeadCb, MenuCb
from app.filters import SELLERS
from app.keyboards import BUDGETS, NOT_TARGET_REASONS, TIMINGS, budget_kb, confirm_dnc_kb, postpone_kb, reasons_kb, skip_kb, timing_kb
from app.services import draft_check, leads
from app.services.ai import ai
from app.utils import h, trunc

router = Router(name="leads")


class AddLead(StatesGroup):
    ref = State()


class Proof(StatesGroup):
    contact = State()
    reply = State()


class Handoff(StatesGroup):
    details = State()


class NoteSt(StatesGroup):
    text = State()


class DraftSt(StatesGroup):
    text = State()


CREATE_RESULT = {
    "blacklisted": "Эта сущность в чёрном списке агентства — лид не создан.",
    "dup_active": "Такой лид уже в работе: #{id} ({status}).",
    "cooldown": "Лид #{id} закрывали недавно — cooldown ещё действует.",
}


async def _lead_for(query_or_message, lead_id: int, me: dict, own_only: bool = True) -> dict | None:
    lead = await leads.get(lead_id)
    target = query_or_message
    if not lead:
        await _reply(target, "Лид не найден.")
        return None
    if own_only and lead["status"] != "NEW" and lead.get("assigned_to") != me["id"] and me["role"] == "sdr":
        await _reply(target, "Этот лид ведёт другой менеджер.")
        return None
    return lead


async def _reply(target, text: str, alert: bool = False, **kwargs) -> None:
    if isinstance(target, CallbackQuery):
        if alert:
            await target.answer(text, show_alert=True)
        else:
            await target.message.answer(text, **kwargs)
            await target.answer()
    else:
        await target.answer(text, **kwargs)


# ---------- создание вручную ----------

async def _create_from_text(message: Message, me: dict, text: str) -> bool:
    ref = leads.parse_ref(text)
    if not ref:
        return False
    wait = await message.answer("Собираю данные по площадке…")
    status, lead = await leads.create_lead(ref, source="manual", created_by=me["id"])
    if status == "created":
        await wait.edit_text(f"Лид #{lead['id']} создан и отправлен в группу. {leads.short_line(lead)}")
        if not await st.get("leads_group_id"):
            await leads.send_private_card(lead, me, prefix="Группа лидов не привязана — карточка только здесь.")
    else:
        template = CREATE_RESULT[status]
        await wait.edit_text(template.format(id=lead["id"] if lead else "", status=leads.STATUS_RU.get(lead["status"], "") if lead else ""))
    return True


@router.message(Command("add"), SELLERS)
async def add_cmd(message: Message, command: CommandObject, me: dict, state: FSMContext) -> None:
    if command.args and await _create_from_text(message, me, command.args):
        return
    await state.set_state(AddLead.ref)
    await message.answer("Пришлите @username, ссылку t.me/… или сайт. Можно просто переслать пост канала.")


@router.callback_query(MenuCb.filter(F.a == "add"), SELLERS)
async def add_cb(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddLead.ref)
    await query.message.answer("Пришлите @username, ссылку t.me/… или сайт. Можно просто переслать пост канала.")
    await query.answer()


def _forward_username(message: Message) -> str | None:
    origin = message.forward_origin
    chat = getattr(origin, "chat", None)
    if chat and getattr(chat, "username", None):
        return chat.username
    return None


@router.message(AddLead.ref, SELLERS)
async def add_ref(message: Message, me: dict, state: FSMContext) -> None:
    await state.clear()
    forwarded = _forward_username(message)
    text = f"@{forwarded}" if forwarded else (message.text or message.caption or "")
    if not await _create_from_text(message, me, text):
        await message.answer("Не распознал ссылку. Нужен @username, t.me/… или https://сайт. Попробуйте /add ещё раз.")


@router.message(StateFilter(None), SELLERS, F.chat.type == "private", F.forward_origin)
async def forwarded_post(message: Message, me: dict) -> None:
    username = _forward_username(message)
    if username:
        await _create_from_text(message, me, f"@{username}")
    else:
        await message.answer("Из этого пересланного сообщения не достать канал (скрытый источник). Пришлите @username вручную через /add.")


@router.message(StateFilter(None), SELLERS, F.chat.type == "private", F.text.regexp(r"(t\.me/|telegram\.me/|^@)"))
async def link_in_private(message: Message, me: dict) -> None:
    await _create_from_text(message, me, message.text)


# ---------- беру / открыть ----------

@router.callback_query(LeadCb.filter(F.a == "claim"), SELLERS)
async def claim(query: CallbackQuery, callback_data: LeadCb, me: dict) -> None:
    ok, text = await leads.claim(callback_data.id, me)
    await query.answer(text, show_alert=not ok)


@router.callback_query(LeadCb.filter(F.a == "open"), SELLERS)
async def open_card(query: CallbackQuery, callback_data: LeadCb, me: dict, state: FSMContext) -> None:
    await state.clear()
    lead = await _lead_for(query, callback_data.id, me)
    if lead:
        await leads.send_private_card(lead, me)
        await query.answer()


@router.callback_query(LeadCb.filter(F.a == "dup"), SELLERS)
async def duplicate(query: CallbackQuery, callback_data: LeadCb, me: dict) -> None:
    lead = await leads.get(callback_data.id)
    if not lead or lead["status"] != "NEW":
        await query.answer("Лид уже не в очереди.", show_alert=True)
        return
    await query.answer(await leads.mark_duplicate(lead, me))


# ---------- нецелевой ----------

@router.callback_query(LeadCb.filter(F.a == "nt"), SELLERS)
async def not_target_menu(query: CallbackQuery, callback_data: LeadCb, me: dict) -> None:
    lead = await _lead_for(query, callback_data.id, me)
    if not lead:
        return
    if lead["status"] not in ("NEW", "CLAIMED", "CONTACTED", "REPLIED"):
        await query.answer("На этом этапе закрывает старший.", show_alert=True)
        return
    await query.message.answer(f"Почему лид #{lead['id']} нецелевой?", reply_markup=reasons_kb(lead["id"], "nt"))
    await query.answer()


@router.callback_query(LeadCb.filter(F.a == "ntr"), SELLERS)
async def not_target_reason(query: CallbackQuery, callback_data: LeadCb, me: dict) -> None:
    lead = await _lead_for(query, callback_data.id, me)
    if not lead:
        return
    if lead["status"] not in ("NEW", "CLAIMED", "CONTACTED", "REPLIED"):
        await query.answer("Лид уже закрыт.", show_alert=True)
        return
    label = NOT_TARGET_REASONS.get(callback_data.v, callback_data.v)
    text = await leads.close_not_target(lead, me, callback_data.v, label)
    await query.message.edit_text(text)
    await query.answer()


# ---------- просил не писать ----------

@router.callback_query(LeadCb.filter(F.a == "dnc"), SELLERS)
async def dnc_confirm(query: CallbackQuery, callback_data: LeadCb, me: dict) -> None:
    lead = await _lead_for(query, callback_data.id, me)
    if not lead:
        return
    await query.message.answer(
        f"Клиент по лиду #{lead['id']} попросил больше не писать? Контакт уйдёт в красный список навсегда, лид закроется.",
        reply_markup=confirm_dnc_kb(lead["id"]),
    )
    await query.answer()


@router.callback_query(LeadCb.filter(F.a == "dncok"), SELLERS)
async def dnc_apply(query: CallbackQuery, callback_data: LeadCb, me: dict) -> None:
    lead = await _lead_for(query, callback_data.id, me)
    if not lead or lead["status"] not in ("CLAIMED", "CONTACTED", "REPLIED", "NEW"):
        await query.answer("Лид уже закрыт.", show_alert=True)
        return
    await query.message.edit_text(await leads.close_dnc(lead, me))
    await query.answer()


# ---------- подтверждение контакта ----------

@router.callback_query(LeadCb.filter(F.a == "wrote"), SELLERS)
async def wrote(query: CallbackQuery, callback_data: LeadCb, me: dict, state: FSMContext) -> None:
    lead = await _lead_for(query, callback_data.id, me)
    if not lead:
        return
    if lead["status"] != "CLAIMED":
        await query.answer("Контакт уже подтверждён или лид не у вас.", show_alert=True)
        return
    await state.set_state(Proof.contact)
    await state.update_data(lead_id=lead["id"])
    await query.message.answer(
        "Перешлите сюда <b>своё сообщение</b>, которое вы отправили клиенту (откройте диалог с клиентом → удержите сообщение → Переслать → этот бот). "
        "Просто текст не подойдёт — нужна пересылка.\n/cancel — отмена."
    )
    await query.answer()


def _origin_user_id(message: Message) -> int | None:
    origin = message.forward_origin
    if isinstance(origin, MessageOriginUser):
        return origin.sender_user.id
    return None


@router.message(Proof.contact, SELLERS)
async def proof_contact(message: Message, me: dict, state: FSMContext) -> None:
    data = await state.get_data()
    lead = await leads.get(data.get("lead_id", 0))
    if not lead or lead["status"] != "CLAIMED" or lead.get("assigned_to") != me["id"]:
        await state.clear()
        await message.answer("Лид уже не в статусе ожидания контакта.")
        return
    if message.forward_origin is None:
        await message.answer("Это не пересланное сообщение. Нужно именно переслать своё сообщение из диалога с клиентом.")
        return
    origin_id = _origin_user_id(message)
    if origin_id and origin_id != me["id"]:
        await message.answer("Это сообщение не от вас. Перешлите своё сообщение клиенту, а не ответ клиента.")
        return
    text = message.text or message.caption or ""
    if len(text.strip()) < 25:
        await message.answer("Слишком короткое сообщение для первого контакта. Перешлите основное сообщение клиенту.")
        return
    await state.clear()
    note = await leads.set_contacted(lead, me, text)
    issues, _ = await draft_check.check(text, lead)
    if issues:
        note += "\n\nЧто подтянуть в следующий раз:\n• " + "\n• ".join(issues[:3])
    if isinstance(message.forward_origin, MessageOriginHiddenUser):
        note += "\n\n(Источник пересылки скрыт настройками приватности — старший может выборочно проверить.)"
    await message.answer(note)
    await leads.send_private_card(await leads.get(lead["id"]), me)


# ---------- ответ клиента ----------

@router.callback_query(LeadCb.filter(F.a == "replied"), SELLERS)
async def replied(query: CallbackQuery, callback_data: LeadCb, me: dict, state: FSMContext) -> None:
    lead = await _lead_for(query, callback_data.id, me)
    if not lead:
        return
    if lead["status"] not in ("CONTACTED", "CLAIMED"):
        await query.answer("Ответ уже зафиксирован.", show_alert=True)
        return
    if lead["status"] == "CLAIMED":
        await query.answer("Сначала подтвердите первый контакт («Написал — подтвердить»).", show_alert=True)
        return
    await state.set_state(Proof.reply)
    await state.update_data(lead_id=lead["id"])
    await query.message.answer("Перешлите сюда ответ клиента (его сообщение из диалога). /cancel — отмена.")
    await query.answer()


@router.message(Proof.reply, SELLERS)
async def proof_reply(message: Message, me: dict, state: FSMContext) -> None:
    data = await state.get_data()
    lead = await leads.get(data.get("lead_id", 0))
    if not lead or lead["status"] != "CONTACTED" or lead.get("assigned_to") != me["id"]:
        await state.clear()
        await message.answer("Лид уже не в статусе «контакт установлен».")
        return
    if message.forward_origin is None:
        await message.answer("Нужна пересылка сообщения клиента, а не текст.")
        return
    if _origin_user_id(message) == me["id"]:
        await message.answer("Это ваше сообщение. Перешлите ответ клиента.")
        return
    await state.clear()
    await message.answer(await leads.set_replied(lead, me, message.text or message.caption))
    await leads.send_private_card(await leads.get(lead["id"]), me)


# ---------- касания и отложить ----------

@router.callback_query(LeadCb.filter(F.a == "touch"), SELLERS)
async def touch(query: CallbackQuery, callback_data: LeadCb, me: dict) -> None:
    lead = await _lead_for(query, callback_data.id, me)
    if not lead:
        return
    if lead["status"] != "CONTACTED":
        await query.answer("Касания считаются только в статусе «контакт установлен».", show_alert=True)
        return
    await query.answer(await leads.touch_done(lead, me), show_alert=True)


@router.callback_query(LeadCb.filter(F.a == "post"), SELLERS)
async def postpone_menu(query: CallbackQuery, callback_data: LeadCb, me: dict) -> None:
    lead = await _lead_for(query, callback_data.id, me)
    if lead:
        await query.message.answer(f"На сколько отложить лид #{lead['id']}?", reply_markup=postpone_kb(lead["id"]))
        await query.answer()


@router.callback_query(LeadCb.filter(F.a == "postd"), SELLERS)
async def postpone_apply(query: CallbackQuery, callback_data: LeadCb, me: dict) -> None:
    lead = await _lead_for(query, callback_data.id, me)
    if not lead:
        return
    await query.message.edit_text(await leads.postpone(lead, me, int(callback_data.v)))
    await query.answer()


# ---------- заметка ----------

@router.callback_query(LeadCb.filter(F.a == "note"), SELLERS)
async def note_start(query: CallbackQuery, callback_data: LeadCb, me: dict, state: FSMContext) -> None:
    lead = await _lead_for(query, callback_data.id, me)
    if lead:
        await state.set_state(NoteSt.text)
        await state.update_data(lead_id=lead["id"])
        await query.message.answer(f"Заметка к лиду #{lead['id']} — одним сообщением. /cancel — отмена.")
        await query.answer()


@router.message(NoteSt.text, SELLERS, F.text)
async def note_save(message: Message, me: dict, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    lead = await leads.get(data.get("lead_id", 0))
    if not lead:
        await message.answer("Лид не найден.")
        return
    await leads.add_note(lead, me, message.text)
    await message.answer("Записал.")


# ---------- проверка текста ----------

@router.callback_query(LeadCb.filter(F.a == "chk"), SELLERS)
async def draft_start(query: CallbackQuery, callback_data: LeadCb, me: dict, state: FSMContext) -> None:
    lead = await _lead_for(query, callback_data.id, me)
    if lead:
        await state.set_state(DraftSt.text)
        await state.update_data(lead_id=lead["id"])
        await query.message.answer("Пришлите текст сообщения, которое собираетесь отправить клиенту. Проверю по чек-листу. /cancel — отмена.")
        await query.answer()


@router.message(DraftSt.text, SELLERS, F.text)
async def draft_review(message: Message, me: dict, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    lead = await leads.get(data.get("lead_id", 0)) or {}
    issues, good = await draft_check.check(message.text, lead)
    lines = []
    if issues:
        lines.append("<b>Поправить:</b>\n• " + "\n• ".join(issues))
    if good:
        lines.append("<b>Хорошо:</b> " + ", ".join(g.lower().rstrip(".") for g in good) + ".")
    if lead and ai.enabled:
        review = await ai.review_draft(message.text, lead)
        if review:
            lines.append(f"<b>Мнение ИИ:</b>\n{h(review)}")
    winners = await ai.winning_openers(1)
    if winners:
        lines.append(f"<b>Так заходили на клиента, который ответил:</b>\n<i>{h(winners[0])}</i>")
    if not issues:
        lines.append("Можно отправлять.")
    await message.answer("\n\n".join(lines))


# ---------- передача старшему ----------

@router.callback_query(LeadCb.filter(F.a == "hand"), SELLERS)
async def handoff_start(query: CallbackQuery, callback_data: LeadCb, me: dict, state: FSMContext) -> None:
    lead = await _lead_for(query, callback_data.id, me)
    if not lead:
        return
    if lead["status"] not in ("CLAIMED", "CONTACTED", "REPLIED"):
        await query.answer("Лид уже передан или закрыт.", show_alert=True)
        return
    if lead["status"] == "CLAIMED":
        await query.answer("Сначала подтвердите первый контакт. Передавать без диалога нельзя.", show_alert=True)
        return
    await state.set_state(Handoff.details)
    await state.update_data(lead_id=lead["id"], budget=None, timing=None)
    await query.message.answer(f"Передача лида #{lead['id']}. Бюджет клиента?", reply_markup=budget_kb(lead["id"]))
    await query.answer()


@router.callback_query(LeadCb.filter(F.a == "budget"), SELLERS, Handoff.details)
async def handoff_budget(query: CallbackQuery, callback_data: LeadCb, state: FSMContext) -> None:
    await state.update_data(budget=callback_data.v)
    await query.message.edit_text(f"Бюджет: {BUDGETS.get(callback_data.v, '?')}. Когда хочет запускаться?", reply_markup=timing_kb(callback_data.id))
    await query.answer()


@router.callback_query(LeadCb.filter(F.a == "timing"), SELLERS, Handoff.details)
async def handoff_timing(query: CallbackQuery, callback_data: LeadCb, state: FSMContext) -> None:
    await state.update_data(timing=callback_data.v)
    await query.message.edit_text(
        f"Сроки: {TIMINGS.get(callback_data.v, '?')}.\nТеперь одним сообщением: что клиент хочет, какие каналы обсуждали, возражения. Это увидит старший.",
        reply_markup=skip_kb("handskip", callback_data.id),
    )
    await query.answer()


async def _finish_handoff(state: FSMContext, me: dict, details: str, send) -> None:
    data = await state.get_data()
    await state.clear()
    lead = await leads.get(data.get("lead_id", 0))
    if not lead or lead["status"] not in ("CONTACTED", "REPLIED"):
        await send("Лид уже не в том статусе.")
        return
    note = f"Бюджет: {BUDGETS.get(data.get('budget'), 'не указан')} · Сроки: {TIMINGS.get(data.get('timing'), 'не указаны')}"
    if details:
        note += f"\n{trunc(details, 800)}"
    await send(await leads.handoff(lead, me, note))


@router.message(Handoff.details, SELLERS, F.text)
async def handoff_details(message: Message, me: dict, state: FSMContext) -> None:
    await _finish_handoff(state, me, message.text, message.answer)


@router.callback_query(LeadCb.filter(F.a == "handskip"), SELLERS, Handoff.details)
async def handoff_skip(query: CallbackQuery, me: dict, state: FSMContext) -> None:
    await _finish_handoff(state, me, "", query.message.answer)
    await query.answer()


@router.callback_query(LeadCb.filter(F.a.in_({"budget", "timing", "handskip"})))
async def handoff_stale(query: CallbackQuery) -> None:
    await query.answer("Этот шаг уже неактуален — начните передачу заново с карточки.", show_alert=True)
