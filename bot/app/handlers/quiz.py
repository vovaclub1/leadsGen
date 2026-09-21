"""Квиз новичка перед допуском к очереди (ТЗ 9.6): 10 вопросов, порог 8/10, пересдача через час.

Вопросы по умолчанию — встроенные; владелец может заменить их через /admin → Настройки →
quiz_questions (JSON-массив вида [{"q": "...", "a": ["...", ...], "correct": 0}, …]).
"""
import json

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app import settings_store as st
from app.db import db
from app.filters import SELLERS
from app.utils import now_iso, now_utc, parse_iso

router = Router(name="quiz")

PASS_SCORE = 8
RETRY_AFTER_MINUTES = 60

DEFAULT_QUESTIONS = [
    {"q": "Клиент дал вилку 3–8k, вы хотите назвать 5k, чтобы «закрыть быстрее». Как поступить?", "a": ["Назвать 5k — это внутри вилки", "Точную цену SDR не называет: вилку называете, цифру даёт старший", "Назвать 8k — больше маржа", "Промолчать и передать старшему"], "correct": 1},
    {"q": "Что нельзя делать в первом сообщении клиенту?", "a": ["Задать вопрос в конце", "Упомянуть персональный факт о канале", "Называть точную цену и слать ссылки «на примеры»", "Писать коротко, до 5 строк"], "correct": 2},
    {"q": "Как подтверждается первый контакт?", "a": ["Скриншотом переписки", "Словом «написал» в чате", "Пересылкой боту своего отправленного сообщения", "Ссылкой на диалог"], "correct": 2},
    {"q": "Сколько минут даётся на первый контакт после взятия лида?", "a": ["10", "30", "60", "До конца дня"], "correct": 1},
    {"q": "Клиент ответил: «скиньте прайс». Что делаете?", "a": ["Шлёте полный прайс", "Фиксируете ответ кнопкой «Клиент ответил» и передаёте старшему", "Игнорируете", "Называете минимальную цену"], "correct": 1},
    {"q": "Когда лид передаётся старшему?", "a": ["Сразу после взятия", "После первого касания", "Когда выяснили бюджет, задачу и сроки — после ответа клиента", "Через неделю касаний"], "correct": 2},
    {"q": "Клиент попросил больше не писать. Ваши действия?", "a": ["Удалить лид и забыть", "Кнопка «Просил не писать» — контакт уйдёт в красный список", "Написать с другого аккаунта", "Продолжить каденцию"], "correct": 1},
    {"q": "Контакт в оранжевом списке. Кто может писать?", "a": ["Любой SDR", "Только старший", "Никто никогда", "Только владелец"], "correct": 1},
    {"q": "Сколько активных лидов можно вести одновременно (по умолчанию)?", "a": ["2", "5", "10", "Без лимита"], "correct": 1},
    {"q": "Касание №2 по каденции — через сколько дней после первого контакта?", "a": ["1", "3", "7", "14"], "correct": 1},
]


class QuizSt(StatesGroup):
    answer = State()


async def questions() -> list[dict]:
    raw = await st.get("quiz_questions")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data and all(isinstance(q, dict) and q.get("q") and q.get("a") for q in data):
                return data
        except json.JSONDecodeError:
            pass
    return DEFAULT_QUESTIONS


def _options(question: dict) -> str:
    return "\n".join(f"{number}) {answer}" for number, answer in enumerate(question["a"], start=1))


async def _ask(message: Message, items: list[dict], index: int) -> None:
    question = items[index]
    await message.answer(f"Вопрос {index + 1}/{len(items)}:\n{question['q']}\n\n{_options(question)}\n\nОтветьте числом.")


@router.message(Command("quiz"), SELLERS)
async def quiz_start(message: Message, me: dict, state: FSMContext) -> None:
    if me["role"] != "sdr":
        await message.answer("Квиз нужен только SDR — остальным допуск к очереди открыт.")
        return
    if me.get("quiz_passed"):
        await message.answer("Квиз уже пройден — очередь доступна.")
        return
    failed_at = parse_iso(me.get("quiz_at"))
    if failed_at and (now_utc() - failed_at).total_seconds() < RETRY_AFTER_MINUTES * 60:
        await message.answer("Квиз провален. Пересдача через час — повторите /help, там все правила.")
        return
    items = await questions()
    await state.set_state(QuizSt.answer)
    await state.update_data(qi=0, correct=0, total=len(items))
    await _ask(message, items, 0)


@router.message(QuizSt.answer, F.text)
async def quiz_answer(message: Message, me: dict, state: FSMContext) -> None:
    data = await state.get_data()
    items = await questions()
    index = data.get("qi", 0)
    if index >= len(items):
        await state.clear()
        return
    try:
        choice = int((message.text or "").strip()) - 1
    except ValueError:
        choice = -1
    correct = 0 <= choice < len(items[index]["a"]) and choice == items[index]["correct"]
    data["correct"] = data.get("correct", 0) + correct
    await message.answer("✅ Верно." if correct else f"❌ Неверно. Правильный ответ: {items[index]['correct'] + 1}.")
    index += 1
    if index < len(items):
        await state.update_data(qi=index, correct=data["correct"])
        await _ask(message, items, index)
        return
    await state.clear()
    score = data["correct"]
    if score >= PASS_SCORE:
        await db.execute("UPDATE users SET quiz_passed = 1 WHERE id = ?", (me["id"],))
        await message.answer(f"Квиз сдан: {score}/{len(items)}. Допуск к очереди открыт — /queue.")
    else:
        await db.execute("UPDATE users SET quiz_at = ? WHERE id = ?", (now_iso(), me["id"]))
        await message.answer(f"Квиз не сдан: {score}/{len(items)}, нужно {PASS_SCORE}. Пересдача через час.")
