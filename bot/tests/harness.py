"""Сквозной прогон бота без Telegram: реальный Dispatcher + фейковая сессия.

Запуск:  BOT_TOKEN=1:x OWNER_ID=1 DATA_DIR=/tmp/lh-test python -m tests.harness
"""
import asyncio
import logging
import sys
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, GetChat, GetMe, SendDocument, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, MessageOriginUser, Update, User

logging.basicConfig(level=logging.WARNING, stream=sys.stdout)

OWNER = User(id=1, is_bot=False, first_name="Максим", username="maksimmorier")
SDR = User(id=2, is_bot=False, first_name="Иван", username="ivan_sdr")
SENIOR = User(id=3, is_bot=False, first_name="Оля", username="olya_senior")
STRANGER = User(id=77, is_bot=False, first_name="Чужой", username="stranger")
PRIVATE = {u.id: Chat(id=u.id, type="private", first_name=u.first_name, username=u.username) for u in (OWNER, SDR, SENIOR, STRANGER)}
GROUP = Chat(id=-100123, type="supergroup", title="MORIER Лиды")


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.counter = 0

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.counter += 1
        if isinstance(method, (SendMessage, EditMessageText)):
            chat_id = method.chat_id
            chat = PRIVATE.get(chat_id) if isinstance(chat_id, int) and chat_id > 0 else GROUP
            if chat is None:
                chat = Chat(id=chat_id, type="private")
            mid = method.message_id if isinstance(method, EditMessageText) and method.message_id else self.counter
            msg = Message(message_id=mid, date=datetime.now(timezone.utc), chat=chat, text=method.text, reply_markup=method.reply_markup).as_(bot)
            self.calls.append(msg)
            return msg
        self.calls.append(method)
        if isinstance(method, SendDocument):
            return Message(message_id=self.counter, date=datetime.now(timezone.utc), chat=GROUP, text="doc").as_(bot)
        if isinstance(method, GetMe):
            return User(id=999, is_bot=True, first_name="LeadHunter", username="lh_test_bot")
        if isinstance(method, GetChat):
            cid = method.chat_id
            if isinstance(cid, str):
                return Chat(id=-1001111, type="channel", title="Cool Shop", username=cid.lstrip("@")).as_(bot)
            return Chat(id=cid, type="private", first_name="Новый", username="newbie").as_(bot)
        return True

    def sent(self, chat_id=None):
        out = [m for m in self.calls if isinstance(m, Message)]
        if chat_id is not None:
            out = [m for m in out if m.chat.id == chat_id]
        return out

    def last(self, chat_id=None):
        s = self.sent(chat_id)
        return s[-1] if s else None

    def reset(self):
        self.calls.clear()


session = FakeSession()
uid = 0


def _next():
    global uid
    uid += 1
    return uid


async def send(dp, bot, user, text, chat=None, forward=False):
    chat = chat or PRIVATE[user.id]
    kwargs = {}
    if forward:
        kwargs["forward_origin"] = MessageOriginUser(type="user", date=datetime.now(timezone.utc), sender_user=user)
    msg = Message(message_id=_next(), date=datetime.now(timezone.utc), chat=chat, from_user=user, text=text, **kwargs)
    await dp.feed_update(bot, Update(update_id=_next(), message=msg))


async def click(dp, bot, user, message, button_text=None, data=None):
    if data is None:
        found = None
        for row in message.reply_markup.inline_keyboard:
            for b in row:
                if button_text.lower() in b.text.lower():
                    found = b
                    break
            if found:
                break
        assert found, f"Кнопка «{button_text}» не найдена. Есть: {buttons(message)}"
        data = found.callback_data
    fake_msg = Message(
        message_id=message.message_id, date=datetime.now(timezone.utc), chat=message.chat,
        from_user=User(id=999, is_bot=True, first_name="bot"), text=message.text, reply_markup=message.reply_markup,
    )
    cq = CallbackQuery(id=str(_next()), from_user=user, chat_instance="x", message=fake_msg, data=data)
    await dp.feed_update(bot, Update(update_id=_next(), callback_query=cq))


def buttons(message):
    return [b.text for r in (message.reply_markup.inline_keyboard if message.reply_markup else []) for b in r]


def show(label, m):
    if m is None:
        print(f"--- {label}: <нет сообщения>")
        return
    print(f"--- {label}:\n{m.text[:600]}\n    кнопки: {buttons(m)}")


async def main():
    import main as entry
    from app import runtime
    from app import settings_store as st
    from app.db import db

    bot = Bot("123:test", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = entry.build_dispatcher()
    await db.connect()
    runtime.bot = bot

    # 1. Чужой стучится
    await send(dp, bot, STRANGER, "/start")
    show("чужой /start", session.last(77))
    assert "Нет доступа" in session.last(77).text

    # 2. Владелец входит
    session.reset()
    await send(dp, bot, OWNER, "/start")
    show("владелец /start", session.last(1))
    assert "Владелец" in session.last(1).text

    # 3. Админка → сотрудники → добавить по ID → роль
    await send(dp, bot, OWNER, "/admin")
    admin_msg = session.last(1)
    show("/admin", admin_msg)
    await click(dp, bot, OWNER, admin_msg, "Сотрудники")
    emp = session.last(1)
    show("Сотрудники", emp)
    await click(dp, bot, OWNER, emp, "Добавить")
    show("Добавить сотрудника", session.last(1))
    await send(dp, bot, OWNER, "2")
    role_msg = session.last(1)
    show("Выбор роли", role_msg)
    await click(dp, bot, OWNER, role_msg, "SDR")
    show("Роль выбрана", session.last(1))
    await send(dp, bot, OWNER, "/admin")
    await click(dp, bot, OWNER, session.last(1), "Сотрудники")
    await click(dp, bot, OWNER, session.last(1), "Добавить")
    await send(dp, bot, OWNER, "3")
    await click(dp, bot, OWNER, session.last(1), "Старший")
    users = await db.fetchall("SELECT id, role, status FROM users ORDER BY id")
    print("users:", users)
    assert [u["role"] for u in users] == ["owner", "sdr", "senior"]

    # 4. Привязка группы
    session.reset()
    await send(dp, bot, OWNER, "/bind", chat=GROUP)
    show("/bind в группе", session.last(GROUP.id))
    assert await st.get("leads_group_id") == str(GROUP.id)

    # 5. Ключи
    session.reset()
    await send(dp, bot, OWNER, "/admin")
    await click(dp, bot, OWNER, session.last(1), "Ключи")
    show("Ключи", session.last(1))

    # 6. Красный список через админку
    await send(dp, bot, OWNER, "/admin")
    await click(dp, bot, OWNER, session.last(1), "Красный")
    dnc_msg = session.last(1)
    show("Красный список", dnc_msg)
    await click(dp, bot, OWNER, dnc_msg, "Добавить")
    show("Добавить в красный", session.last(1))
    await send(dp, bot, OWNER, "@angry_client")
    step = session.last(1)
    show("после ввода контакта", step)
    for _ in range(3):
        if step.reply_markup:
            names = buttons(step)
            pick = next((n for n in names if "расн" in n), names[0])
            await click(dp, bot, OWNER, step, pick)
        else:
            await send(dp, bot, OWNER, "написал агрессивно, просил не писать")
        step = session.last(1)
        show("шаг", step)
        if await db.fetchone("SELECT 1 FROM dnc_contacts"):
            break
    dnc_rows = await db.fetchall("SELECT username, level, reason FROM dnc_contacts")
    print("dnc rows:", dnc_rows)
    assert dnc_rows, "красный список пуст"

    # 7. SDR: квиз новичка → лид → взять → проверка → контакт пересылкой
    session.reset()
    await send(dp, bot, SDR, "/start")
    show("SDR /start", session.last(2))
    from app.handlers.quiz import questions as quiz_questions

    await send(dp, bot, SDR, "/quiz")
    show("квиз — первый вопрос", session.last(2))
    for question in await quiz_questions():
        await send(dp, bot, SDR, str(question["correct"] + 1))
    show("квиз сдан", session.last(2))
    assert "Допуск" in session.last(2).text, session.last(2).text
    await send(dp, bot, SDR, "/add @cool_shop_channel")
    show("SDR /add", session.last(2))
    group_card = session.last(GROUP.id)
    show("карточка в группе", group_card)
    assert group_card and "Беру" in " ".join(buttons(group_card))
    await click(dp, bot, SDR, group_card, "Беру")
    private_card = session.last(2)
    show("карточка в личке", private_card)
    assert "Написал" in " ".join(buttons(private_card))
    await click(dp, bot, SDR, private_card, "Проверить")
    show("проверка — запрос текста", session.last(2))
    await send(dp, bot, SDR, "Добрый день! Увидел ваше размещение в канале про гаджеты — заходит хорошо. Мы в MORIER подбираем каналы под такие продукты. Подскажите, планируете ещё размещения в этом месяце?")
    show("результат проверки", session.last(2))
    await click(dp, bot, SDR, private_card, "Написал")
    show("запрос пересылки", session.last(2))
    await send(dp, bot, SDR, "Добрый день! Увидел ваше размещение...", forward=True)
    show("контакт подтверждён", session.last(2))
    lead = await db.fetchone("SELECT * FROM leads WHERE username='cool_shop_channel'")
    print("lead status:", lead["status"], "assigned:", lead["assigned_to"])
    assert lead["status"] == "CONTACTED"

    # 8. Ответил → передать → принять → WON
    await send(dp, bot, SDR, f"/lead_{lead['id']}")
    card = session.last(2)
    show("/lead_N", card)
    await click(dp, bot, SDR, card, "ответил")
    await send(dp, bot, SDR, "Да, интересно, какие условия?", forward=True)
    show("ответ зафиксирован", session.last(2))
    await send(dp, bot, SDR, f"/lead_{lead['id']}")
    card = session.last(2)
    await click(dp, bot, SDR, card, "Передать")
    step = session.last(2)
    show("передача — шаг 1", step)
    for _ in range(3):
        if step.reply_markup and buttons(step):
            await click(dp, bot, SDR, step, buttons(step)[0])
            step = session.last(2)
            show("передача — шаг", step)
        else:
            break
    await send(dp, bot, SDR, "Хочет 3 размещения в октябре, бюджет ~60к")
    show("передано", session.last(2))
    senior_msg = session.last(3)
    show("старшему пришло", senior_msg)
    assert senior_msg and "Принять" in " ".join(buttons(senior_msg))
    await click(dp, bot, SENIOR, senior_msg, "Принять")
    show("принято", session.last(3))
    await send(dp, bot, SENIOR, "/handoffs")
    show("/handoffs", session.last(3))
    await send(dp, bot, SENIOR, f"/lead_{lead['id']}")
    scard = session.last(3)
    show("карточка у старшего", scard)
    await click(dp, bot, SENIOR, scard, "WON")
    show("WON — сумма", session.last(3))
    await send(dp, bot, SENIOR, "60000")
    show("WON — следующий шаг", session.last(3))
    if "марж" in session.last(3).text.lower():
        await send(dp, bot, SENIOR, "35")
        show("WON готово", session.last(3))
    lead = await db.fetchone("SELECT status, won_amount, won_margin FROM leads WHERE id=?", (lead["id"],))
    print("final:", lead)
    assert lead["status"] == "WON"

    # 9. Команды SDR
    for cmd in ("/me", "/top", "/my", "/queue", "/check @angry_client", "/help"):
        await send(dp, bot, SDR, cmd)
        show(f"SDR {cmd}", session.last(2))

    # 10. Владелец: разделы админки
    for section in ("Дашборд", "Настройки", "ИИ", "Доноры", "Чёрный"):
        await send(dp, bot, OWNER, "/admin")
        try:
            await click(dp, bot, OWNER, session.last(1), section)
            show(section, session.last(1))
        except AssertionError as e:
            print("SKIP", section, e)
    await send(dp, bot, OWNER, "/admin")
    await click(dp, bot, OWNER, session.last(1), "Доноры")
    dmsg = session.last(1)
    if any("Добавить" in b for b in buttons(dmsg)):
        await click(dp, bot, OWNER, dmsg, "Добавить")
        show("Добавить донора", session.last(1))
        await send(dp, bot, OWNER, "@gadget_news @tech_daily")
        show("доноры добавлены", session.last(1))
        print("donors:", await db.fetchall("SELECT username, active FROM donors"))
    await send(dp, bot, OWNER, "/adjust @ivan_sdr +5 помог коллеге")
    show("/adjust", session.last(1))
    pts = await db.fetchone("SELECT SUM(delta) AS p FROM points WHERE user_id=?", (2,))
    print("points after adjust:", pts["p"])
    assert pts["p"] == 90, f"ожидал 90 баллов, получил {pts['p']}"
    await send(dp, bot, SENIOR, "/blacklist @spam_channel казино")
    show("/blacklist", session.last(3))
    session.reset()
    await send(dp, bot, SDR, "/admin")
    show("SDR /admin", session.last(2))
    await send(dp, bot, SDR, "/cancel")
    show("/cancel", session.last(2))

    # 11. Планировщик
    from app import scheduler
    for job in (scheduler.sla_jobs, scheduler.cadence_jobs, scheduler.aging_jobs, scheduler.dnc_jobs,
                scheduler.morning_plan, scheduler.evening_digest, scheduler.weekly_board, scheduler.backup):
        await job()
        print("job ok:", job.__name__)
    show("утренний план SDR", session.last(2))
    show("вечерний разбор владельцу", session.last(1))

    await db.conn.close()
    print("\nHARNESS OK")


if __name__ == "__main__":
    asyncio.run(main())
