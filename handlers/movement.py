import datetime
import asyncio
from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils import texts
from utils.user import ensure_user

router = Router()
_focus_tasks: dict[int, asyncio.Task] = {}


class WeightState(StatesGroup):
    value = State()
    height = State()
    goal = State()
    target = State()


WARMUP_5 = (
    "Лёгкая разминка (5 минут):\n"
    "1) Круговые движения головой/плечами по 20–30 сек.\n"
    "2) Махи руками вперёд/назад 30 сек.\n"
    "3) Наклоны к носкам/вперёд 30–40 сек.\n"
    "4) Приседы без глубины 10–15 раз.\n"
    "5) Потянись вверх, подыши, выпей воды."
)

HOME_WORKOUT_10 = (
    "Домашняя тренировка 10–15 минут (без инвентаря):\n"
    "1) Разминка 2 мин (круговые суставы).\n"
    "2) Приседы или стульчик у стены — 3 подхода по 10–15.\n"
    "3) Отжимания от стола/стены — 3х10–15.\n"
    "4) Планка на локтях — 3х20–40 сек.\n"
    "5) Лёгкая растяжка спины/ног 2 мин.\n"
    "Если тяжело — убавь повторения, делай через день."
)

HOME_WORKOUT_20 = (
    "Тренировка 20 минут (дом):\n"
    "1) Разминка 3 мин: суставы + шаги на месте.\n"
    "2) Круг x3: 12 приседов, 10 отжиманий от пола/стола, 15 выпадов (пополам ноги), 30 сек планка.\n"
    "3) Отдых между кругами 1–2 мин.\n"
    "4) Растяжка 3 мин.\n"
    "Упрощай, если тяжело: меньше повторов, выше опора."
)


@router.callback_query(lambda c: c.data and c.data.startswith("move:warmup"))
async def move_warmup(callback: types.CallbackQuery) -> None:
    await callback.message.answer(WARMUP_5, reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("move:home"))
async def move_home(callback: types.CallbackQuery) -> None:
    await callback.message.answer(HOME_WORKOUT_10, reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("move:home10"))
async def move_home10(callback: types.CallbackQuery) -> None:
    await callback.message.answer(HOME_WORKOUT_10, reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("move:home20"))
async def move_home20(callback: types.CallbackQuery) -> None:
    await callback.message.answer(HOME_WORKOUT_20, reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("move:weight"))
async def move_weight(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    user_row = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    user = dict(user_row)
    weights = await repo.list_weights(db, user["id"], limit=30)
    latest = weights[0]["weight"] if weights else None
    trend30 = await repo.weight_trend(db, user["id"], days=30)
    height = user.get("height_cm") or 0
    goal = user.get("weight_goal") or "не задана"
    target = user.get("weight_target") or 0
    lines = ["⚖ Вес и цели (без оценок и стыда):"]
    if latest:
        lines.append(f"Текущий вес: {latest:.1f} кг")
    if trend30:
        arrow = "⬇️" if trend30 < 0 else "⬆️"
        lines.append(f"Динамика за 30 дней: {arrow} {trend30:.1f} кг")
    if height:
        lines.append(f"Рост: {height:.0f} см")
    if goal != "не задана":
        human_goal = {
            "loss": "чуть снизить вес",
            "keep": "поддерживать текущий вес",
            "gain": "немного набрать",
        }.get(goal, goal)
        goal_line = f"Цель: {human_goal}"
        if target:
            goal_line += f" (ориентир {target:.1f} кг, без жёстких рамок)"
        lines.append(goal_line)
    lines.append(
        "\nМожно вести заметки по весу, но главное — самочувствие.\n"
        "В качестве целей подойдут и вещи без цифр: «гулять 3 раза в неделю», «просыпаться бодрее»."
    )
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="➕ Записать вес", callback_data="weight:add")],
            [types.InlineKeyboardButton(text="Рост/цель", callback_data="weight:goal")],
        ]
    )
    await state.clear()
    await callback.message.answer("\n".join(lines), reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data == "weight:add")
async def weight_add_cb(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WeightState.value)
    await callback.message.answer("Введи текущий вес (кг), например 72.5", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.message(Command("weight"))
@router.message(WeightState.value)
async def save_weight(message: types.Message, state: FSMContext, db) -> None:
    text = message.text.strip().replace(",", ".")
    try:
        val = float(text)
        if val <= 0:
            raise ValueError
    except Exception:
        await message.answer(
            texts.error("вес должен быть числом. Пример: 72.3"),
        )
        return
    user_row = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    user = dict(user_row)
    await repo.add_weight(db, user["id"], val)
    history = await repo.list_weights(db, user["id"], limit=5)
    change_text = ""
    if len(history) >= 2:
        latest = history[0]["weight"]
        earliest = history[-1]["weight"]
        diff = latest - earliest
        arrow = "⬇️" if diff < 0 else "⬆️" if diff > 0 else "⇨"
        change_text = f"\nИзменение за {len(history)} записей: {arrow} {diff:.1f} кг"
    await message.answer(
        f"Записала вес: {val:.1f} кг.{change_text}\nДля тренировки жми в разделе Спорт.",
        reply_markup=main_menu_keyboard(),
    )
    await state.clear()


@router.callback_query(lambda c: c.data and c.data == "weight:goal")
async def weight_goal_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WeightState.height)
    await callback.message.answer("Введи рост (см), например 175.", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.message(WeightState.height)
async def set_height(message: types.Message, state: FSMContext, db) -> None:
    try:
        h = float(message.text.strip().replace(",", "."))
        if h < 100 or h > 250:
            raise ValueError
    except Exception:
        await message.answer(
            texts.error("рост должен быть числом, например 175."),
        )
        return
    await state.update_data(height=h)
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text="Сбросить вес", callback_data="weightgoal:loss"),
                types.InlineKeyboardButton(text="Поддерживать", callback_data="weightgoal:keep"),
                types.InlineKeyboardButton(text="Набрать", callback_data="weightgoal:gain"),
            ]
        ]
    )
    await state.set_state(WeightState.goal)
    await message.answer("Цель?", reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("weightgoal:"))
async def set_goal(callback: types.CallbackQuery, state: FSMContext) -> None:
    goal = callback.data.split(":")[1]
    await state.update_data(goal=goal)
    await state.set_state(WeightState.target)
    await callback.message.answer(
        "Если хочешь, можешь задать ориентир по весу (в кг).\n"
        "Это не обязанность и не жёсткая норма — просто цифра, на которую удобно смотреть.\n"
        "Например: 70 или 72.5.",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer()


@router.message(WeightState.target)
async def set_target_weight(message: types.Message, state: FSMContext, db) -> None:
    try:
        target = float(message.text.strip().replace(",", "."))
    except Exception:
        await message.answer(
            texts.error("целевой вес должен быть числом."),
        )
        return
    data = await state.get_data()
    h = data.get("height")
    goal = data.get("goal", "keep")
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.update_user_body(db, user["id"], height_cm=h, weight_goal=goal, weight_target=target)
    await state.clear()
    goal_text = {
        "loss": "чуть снизить вес",
        "keep": "поддерживать текущий вес",
        "gain": "немного набрать",
    }.get(goal, "заботиться о самочувствии")
    await message.answer(
        f"Сохранила: рост {h:.0f} см, цель — {goal_text}, ориентир по весу {target:.1f} кг.\n"
        "Это не строгий план, а мягкий ориентир, который можно менять по ощущениям.",
        reply_markup=main_menu_keyboard(),
    )


# Фокус 20/10 диалог
@router.callback_query(lambda c: c.data and c.data == "move:focus")
async def focus_start(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    wellness = await repo.get_wellness(db, user["id"]) or {}
    work = wellness.get("focus_work", 20)
    rest = wellness.get("focus_rest", 10)
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text="1 раунд", callback_data=f"focusrun:1:{work}:{rest}"),
                types.InlineKeyboardButton(text="2 раунда", callback_data=f"focusrun:2:{work}:{rest}"),
                types.InlineKeyboardButton(text="3 раунда", callback_data=f"focusrun:3:{work}:{rest}"),
            ]
        ]
    )
    await callback.message.answer(f"Фокус {work}/{rest}. Сколько раундов делаем?", reply_markup=kb)
    await callback.answer()


async def _focus_session(bot, chat_id: int, rounds: int, work: int, rest: int, db=None, user_id: int | None = None, local_date: str | None = None):
    for i in range(1, rounds + 1):
        if chat_id in _focus_tasks and _focus_tasks[chat_id].cancelled():
            return
        await bot.send_message(chat_id, f"Раунд {i}/{rounds}: работаем {work} минут.")
        await asyncio.sleep(work * 60)
        if chat_id in _focus_tasks and _focus_tasks[chat_id].cancelled():
            return
        if i < rounds:
            await bot.send_message(chat_id, f"Раунд {i}/{rounds}: отдых {rest} минут.")
            await asyncio.sleep(rest * 60)
    if db and user_id:
        from db import repositories as repo
        try:
            await repo.add_points(db, user_id, 4, local_date=local_date)
        except Exception:
            pass
    await bot.send_message(chat_id, "Фокус-сессия завершена. Хорошая работа!", reply_markup=main_menu_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("focusrun:"))
async def focus_run(callback: types.CallbackQuery, db) -> None:
    parts = callback.data.split(":")
    rounds = int(parts[1])
    work = int(parts[2])
    rest = int(parts[3])
    from utils.user import ensure_user
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    from utils.time import local_date_str
    local_date = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    prev = _focus_tasks.pop(callback.message.chat.id, None)
    if prev:
        prev.cancel()
    task = asyncio.create_task(_focus_session(callback.message.bot, callback.message.chat.id, rounds, work, rest, db=db, user_id=user["id"], local_date=local_date))
    _focus_tasks[callback.message.chat.id] = task
    stop_kb = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="⏹ Стоп", callback_data="focus:stop")]]
    )
    await callback.answer("Запустила фокус-сессию.")
    await callback.message.answer(
        f"Стартую {rounds} раунд(ов) {work}/{rest}. Я напишу, когда пора отдыхать и заканчивать.",
        reply_markup=stop_kb,
    )


@router.callback_query(lambda c: c.data and c.data == "focus:stop")
async def focus_stop(callback: types.CallbackQuery) -> None:
    task = _focus_tasks.pop(callback.message.chat.id, None)
    if task:
        task.cancel()
    await callback.message.answer("Остановила фокус-сессию.", reply_markup=main_menu_keyboard())
    await callback.answer()
