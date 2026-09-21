"""ИИ: состояние, проверка, зрение, экономный режим, лимит расхода, разбивка по задачам."""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app import settings_store as st
from app.callbacks import AdmCb
from app.config import config
from app.filters import OWNER
from app.handlers.helpers import show as _show
from app.keyboards import ai_kb, back_kb
from app.services.ai import ai
from app.utils import h, season

router = Router(name="admin-ai")


class EditAiCap(StatesGroup):
    value = State()


async def ai_view(target, note: str = "") -> None:
    info = await ai.health()
    if not info["enabled"]:
        state = "🔴 выключен — AI_API_KEY не задан в .env"
    elif info["streak"] >= 3:
        state = f"🔴 не отвечает ({info['streak']} ошибок подряд)"
    elif info["last_error"]:
        state = "🟡 были ошибки, сейчас работает"
    else:
        state = "🟢 работает"
    if not info["enabled"]:
        vision = "недоступно без ключа ИИ"
    elif not config.vision_possible:
        vision = "выключено в .env (AI_VISION_MODEL)"
    else:
        vision = "включено" if info["vision_on"] else "выключено"
    lines = [
        "<b>🤖 ИИ</b>",
        f"Состояние: {state}",
        f"Модель: <code>{h(info['model'])}</code> · сервис: <code>{h(info['base_url'])}</code>",
        f"Зрение: {vision}" + (f" (<code>{h(info['vision_model'])}</code>)" if config.vision_possible else ""),
        f"Расход за {h(season())}: <b>${info['spent']:.3f}</b> из ${info['cap']:.2f}",
        f"Запросов: {info['ok']} удачных · {info['fail']} с ошибкой (с момента запуска)",
    ]
    if info["thrifty"]:
        saved = sum(info["skipped"].values())
        line = f"Экономный режим: включён, лиды с оценкой ниже {info['min_score']} считаются формулами"
        if saved:
            line += f"\nНе отправлено запросов за месяц: {saved}"
        lines.append(line)
    else:
        lines.append("Экономный режим: выключен — в ИИ уходит каждый лид и каждый рекламный пост")
    if info["last_error"]:
        lines.append(f"Последняя ошибка: {h(info['last_error'])}")
    if config.ai_model_was_legacy:
        lines.append(f"⚠️ В .env указана снятая модель <code>{h(config.ai_model_was_legacy)}</code> — работаю на <code>{h(info['model'])}</code>.")
    if note:
        lines.append(f"\n{note}")
    await _show(target, "\n".join(lines), ai_kb(info["vision_on"], config.vision_possible, info["thrifty"]))


@router.callback_query(AdmCb.filter((F.s == "ai") & (F.a == "list")), OWNER)
async def ai_list(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await ai_view(query)


@router.callback_query(AdmCb.filter((F.s == "ai") & (F.a == "test")), OWNER)
async def ai_test(query: CallbackQuery) -> None:
    await query.answer("Проверяю…")
    result = await ai.self_test(with_vision=True)
    if not result["ok"]:
        note = f"❌ Проверка не прошла ({result.get('stage')}): {h(result['error'])}"
    else:
        note = f"✅ Текст: ответ за {result['ms']} мс, списано ${result.get('cost', 0):.4f}."
        if "vision" in result:
            icon = "✅" if result.get("vision_ok") else ("ℹ️" if "выключено" in str(result["vision"]) else "❌")
            note += f"\n{icon} Картинки: {h(str(result['vision']))}"
            if result.get("vision_ms"):
                note += f" ({result['vision_ms']} мс)"
    await ai_view(query, note)


@router.callback_query(AdmCb.filter((F.s == "ai") & (F.a == "vision")), OWNER)
async def ai_vision_toggle(query: CallbackQuery) -> None:
    new_value = "0" if await ai.vision_enabled() else "1"
    await st.set_value("ai_vision", new_value)
    await ai_view(query, "👁 Чтение картинок включено." if new_value == "1" else "👁 Чтение картинок выключено.")


@router.callback_query(AdmCb.filter((F.s == "ai") & (F.a == "thrifty")), OWNER)
async def ai_thrifty_toggle(query: CallbackQuery) -> None:
    new_value = "0" if await ai.thrifty() else "1"
    await st.set_value("ai_thrifty", new_value)
    note = (
        f"💰 Экономный режим включён. Лиды с оценкой ниже {await st.get_int('ai_min_score')} и посты "
        "с явной пометкой рекламы больше не тратят запросы."
        if new_value == "1"
        else "💰 Экономный режим выключен. Каждый лид и каждый рекламный пост идут в ИИ."
    )
    await ai_view(query, note)


@router.callback_query(AdmCb.filter((F.s == "ai") & (F.a == "usage")), OWNER)
async def ai_usage(query: CallbackQuery) -> None:
    rows = await ai.usage_breakdown()
    titles = {
        "analyze": "Анализ лидов", "ad": "Определение рекламы", "vision": "Чтение картинок",
        "draft": "Проверка сообщений", "selftest": "Проверки связи",
    }
    if not rows:
        await ai_view(query, "За этот месяц запросов к ИИ ещё не было.")
        return
    lines = [f"<b>📈 Расход ИИ за {h(season())}</b>"]
    for row in rows:
        failed = int(row["calls"]) - int(row["ok"] or 0)
        lines.append(
            f"{titles.get(row['purpose'], row['purpose'] or '—')}: ${row['cost']:.3f} · "
            f"{row['calls']} запр." + (f" ({failed} с ошибкой)" if failed else "") +
            f" · {row['tin']}→{row['tout']} токенов"
        )
    lines.append(f"\nИтого: <b>${sum(r['cost'] for r in rows):.3f}</b>")
    skipped = await ai.skipped_month()
    if skipped:
        lines.append(
            "Экономный режим сберёг запросов: "
            + ", ".join(f"{titles.get(key, key)} — {value}" for key, value in skipped.items())
        )
    await _show(query, "\n".join(lines), back_kb("ai"))


@router.callback_query(AdmCb.filter((F.s == "ai") & (F.a == "cap")), OWNER)
async def ai_cap(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EditAiCap.value)
    await query.message.answer(
        f"Текущий месячный лимит: ${await st.get_float('ai_monthly_cap_usd'):.2f}\n"
        "Пришлите новую сумму в долларах, например <code>10</code>. 0 — без лимита. /cancel — отмена."
    )
    await query.answer()


@router.message(EditAiCap.value, OWNER, F.text)
async def ai_cap_save(message: Message, state: FSMContext) -> None:
    try:
        value = float(message.text.strip().replace(",", ".").lstrip("$"))
    except ValueError:
        await message.answer("Нужно число, например 10 или 7.5")
        return
    if value < 0:
        await message.answer("Лимит не может быть отрицательным.")
        return
    await state.clear()
    await st.set_value("ai_monthly_cap_usd", f"{value:g}")
    await ai_view(message, f"Лимит расхода: ${value:.2f} в месяц.")
