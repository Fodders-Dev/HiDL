from __future__ import annotations

import asyncio
import datetime
from html import escape as _h
import io
from typing import Any, Dict, Optional

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard, movement_menu_keyboard
from services import fitness_nutrition
from utils import texts
from utils.sender import safe_edit
from utils.time import local_date_str
from utils.user import ensure_user

router = Router()
_focus_tasks: dict[int, asyncio.Task] = {}


class WeightState(StatesGroup):
    value = State()
    height = State()
    goal = State()
    target = State()


class ScaleImportState(StatesGroup):
    waiting_input = State()


class FitProfileState(StatesGroup):
    height = State()
    age = State()
    sex = State()
    activity = State()
    goal = State()
    target = State()


class GearCustomState(StatesGroup):
    waiting_text = State()


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


def _goal_human(goal: str) -> str:
    return fitness_nutrition.GOAL_LABELS.get(
        str(goal or "").strip().lower(),
        "не задана",
    )


async def _render_body_card(db, user: Dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    user_id = int(user["id"])
    trend = await repo.body_metrics_trend(db, user_id, days=30)
    latest = trend.get("latest")
    height = float(user.get("height_cm") or 0)
    age = int(user.get("age_years") or 0)
    goal = str(user.get("weight_goal") or "loss")
    target = float(user.get("weight_target") or 0)

    local_date = local_date_str(datetime.datetime.utcnow(), user.get("timezone") or "UTC")
    week_stats = await repo.workout_stats_week(db, user_id, local_date)
    daily_targets = fitness_nutrition.calculate_daily_targets(
        user, latest, workout_kcal_today=int(week_stats.get("today_kcal_burned") or 0)
    )

    lines = ["⚖ <b>Умные весы и параметры тела</b> (строго личное, партнёр не видит)"]
    if latest:
        lines.append(f"• Вес: <b>{float(latest['weight']):.1f} кг</b>")
        extra_bits = []
        if latest.get("body_fat_pct") is not None:
            extra_bits.append(f"жир {float(latest['body_fat_pct']):.1f}%")
        if latest.get("muscle_mass_kg") is not None:
            extra_bits.append(f"мышцы {float(latest['muscle_mass_kg']):.1f} кг")
        if latest.get("water_pct") is not None:
            extra_bits.append(f"вода {float(latest['water_pct']):.1f}%")
        if latest.get("visceral_fat") is not None:
            extra_bits.append(f"висц. жир {float(latest['visceral_fat']):g}")
        if latest.get("bmi") is not None:
            extra_bits.append(f"ИМТ {float(latest['bmi']):.1f}")
        if extra_bits:
            lines.append("• Состав: " + " · ".join(extra_bits))
    else:
        lines.append("• Замеров пока нет — можно вбить числом или скинуть скриншот с весов.")

    profile_bits = []
    if height:
        profile_bits.append(f"рост {height:.0f} см")
    if age:
        profile_bits.append(f"{age} лет")
    if target:
        profile_bits.append(f"ориентир {target:.1f} кг")
    if profile_bits:
        lines.append("• Профиль: " + ", ".join(profile_bits))
    lines.append(f"• Цель: {_h(_goal_human(goal))}")
    lines.append(
        f"• Норма КБЖУ сейчас: <b>{daily_targets['kcal']} ккал</b> "
        f"(Б {daily_targets['protein_g']}г / Ж {daily_targets['fat_g']}г / У {daily_targets['carbs_g']}г)"
    )
    lines.append("\n" + _h(fitness_nutrition.format_body_trend_feedback(trend, goal)))

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📸 Загрузить весы (фото / текст)", callback_data="fit:scale_add")],
            [InlineKeyboardButton(text="⚙️ Анкета тела и цель", callback_data="fit:profile_setup")],
            [
                InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
                InlineKeyboardButton(text="🧰 Инвентарь", callback_data="fit:gear"),
            ],
            [InlineKeyboardButton(text="⬅️ Спорт", callback_data="fit:menu")],
        ]
    )
    return "\n".join(lines), kb


def _render_gear_card(equipment: list[str]) -> tuple[str, InlineKeyboardMarkup]:
    eq_set = set(equipment)
    lines = [
        "🧰 <b>Мой инвентарь дома</b>",
        "Отметь всё, что реально есть под рукой — под это я буду подбирать 3-минутный микро-старт и домашние тренировки.\n",
        f"Сейчас выбрано: {_h(fitness_nutrition.format_equipment_labels(equipment))}",
    ]
    rows = []
    for key, emoji, label in fitness_nutrition.EQUIPMENT_CATALOG:
        mark = "✅ " if key in eq_set else "⬜ "
        rows.append([InlineKeyboardButton(text=f"{mark}{emoji} {label}", callback_data=f"fit:gear_toggle:{key}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить свой тренажёр", callback_data="fit:gear_custom")])
    rows.append(
        [
            InlineKeyboardButton(text="🫠 Лень начинать", callback_data="fit:lazy"),
            InlineKeyboardButton(text="🏋️ Тренировка", callback_data="fit:workout_menu"),
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Спорт", callback_data="fit:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# --- Legacy / basic movement handlers ---

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
    await state.clear()
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    text, kb = await _render_body_card(db, user)
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data == "weight:add")
async def weight_add_cb(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WeightState.value)
    await callback.message.answer("Введи текущий вес (кг), например 72.5", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.message(Command("weight"))
@router.message(WeightState.value)
async def save_weight(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").replace("/weight", "").strip().replace(",", ".")
    parsed = await fitness_nutrition.parse_smart_scale_input(raw_text=raw)
    if not parsed.get("ok"):
        try:
            val = float(raw)
            if val <= 0:
                raise ValueError
            parsed = {"ok": True, "weight": val, "source": "manual"}
        except Exception:
            await message.answer(
                texts.error("вес должен быть числом. Пример: 72.3"),
            )
            return
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    await repo.add_weight(
        db,
        int(user["id"]),
        float(parsed["weight"]),
        body_fat_pct=parsed.get("body_fat_pct"),
        muscle_mass_kg=parsed.get("muscle_mass_kg"),
        water_pct=parsed.get("water_pct"),
        visceral_fat=parsed.get("visceral_fat"),
        bmi=parsed.get("bmi"),
        source=str(parsed.get("source") or "manual"),
    )
    history = await repo.list_weights(db, int(user["id"]), limit=5)
    change_text = ""
    if len(history) >= 2:
        latest = history[0]["weight"]
        earliest = history[-1]["weight"]
        diff = latest - earliest
        arrow = "⬇️" if diff < 0 else "⬆️" if diff > 0 else "⇨"
        change_text = f"\nИзменение за {len(history)} записей: {arrow} {diff:.1f} кг"
    await message.answer(
        f"Записала вес: {float(parsed['weight']):.1f} кг.{change_text}\nДля тренировки жми в разделе Спорт.",
        reply_markup=main_menu_keyboard(user),
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


# --- Upgraded /fit: Smart scales, Profile wizard, Gear, Anti-laziness & Personal workouts ---

@router.message(Command("fit"))
async def fit_command(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🏋️ <b>Спорт и форма без давления</b>\nВыбирай, с чего начнём:",
        reply_markup=movement_menu_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "fit:menu")
async def fit_menu_cb(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(
        callback.message,
        "🏋️ <b>Спорт и форма без давления</b>\nВыбирай, с чего начнём:",
        reply_markup=movement_menu_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "fit:body")
async def fit_body_cb(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await state.clear()
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    text, kb = await _render_body_card(db, user)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data == "fit:scale_add")
async def fit_scale_add_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ScaleImportState.waiting_input)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="fit:scale_cancel")]]
    )
    await callback.message.answer(
        "📸 <b>Умные весы</b>\n\n"
        "Пришли <b>скриншот из приложения весов</b> (Mi Fit / Zepp / Picooc / Apple Health) "
        "или напиши замер <b>текстом</b>, например:\n"
        "• <code>84.2 жир 23.5% мышцы 58.1 вода 55%</code>\n"
        "• или просто <code>84.2</code>",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "fit:scale_cancel")
async def fit_scale_cancel(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await state.clear()
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    text, kb = await _render_body_card(db, user)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Отменено")


async def _commit_scale_parsed(message: types.Message, state: FSMContext, db, parsed: Dict[str, Any]) -> None:
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="fit:scale_cancel")]]
    )
    if not parsed.get("ok"):
        err = parsed.get("error") or ""
        hint = (
            "ИИ-распознавание фото сейчас недоступно — введи вес и показатели текстом, например: <code>84.2 жир 23.5% мышцы 58.1</code>."
            if err in {"llm_not_configured", "vision_parse_failed"}
            else "Не смогла прочитать вес. Напиши число (например <code>84.2</code> или <code>84.2 жир 23% мышцы 58</code>)."
        )
        await message.answer(hint, reply_markup=cancel_kb, parse_mode="HTML")
        return

    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    user_id = int(user["id"])
    await repo.add_weight(
        db,
        user_id,
        float(parsed["weight"]),
        body_fat_pct=parsed.get("body_fat_pct"),
        muscle_mass_kg=parsed.get("muscle_mass_kg"),
        water_pct=parsed.get("water_pct"),
        visceral_fat=parsed.get("visceral_fat"),
        bmi=parsed.get("bmi"),
        source=str(parsed.get("source") or "manual"),
    )
    await state.clear()

    trend = await repo.body_metrics_trend(db, user_id, days=30)
    local_date = local_date_str(datetime.datetime.utcnow(), user.get("timezone") or "UTC")
    week_stats = await repo.workout_stats_week(db, user_id, local_date)
    targets = fitness_nutrition.calculate_daily_targets(
        user,
        trend.get("latest"),
        workout_kcal_today=int(week_stats.get("today_kcal_burned") or 0),
    )

    bits = [f"вес <b>{float(parsed['weight']):.1f} кг</b>"]
    if parsed.get("body_fat_pct") is not None:
        bits.append(f"жир {float(parsed['body_fat_pct']):.1f}%")
    if parsed.get("muscle_mass_kg") is not None:
        bits.append(f"мышцы {float(parsed['muscle_mass_kg']):.1f} кг")
    if parsed.get("water_pct") is not None:
        bits.append(f"вода {float(parsed['water_pct']):.1f}%")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚖ К карте тела", callback_data="fit:body"),
                InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
            ]
        ]
    )
    await message.answer(
        f"✅ Записала замер: {', '.join(bits)}.\n"
        f"{_h(fitness_nutrition.format_body_trend_feedback(trend, str(user.get('weight_goal') or 'loss')))}\n\n"
        f"🔥 Обновлённый ориентир на день: <b>{targets['kcal']} ккал</b> "
        f"(Б {targets['protein_g']}г / Ж {targets['fat_g']}г / У {targets['carbs_g']}г).",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.message(ScaleImportState.waiting_input, F.photo)
async def fit_scale_photo(message: types.Message, state: FSMContext, db) -> None:
    photo = message.photo[-1]
    buf = io.BytesIO()
    dl = await message.bot.download(photo, destination=buf)
    img_bytes = buf.getvalue()
    if not img_bytes and dl is not None:
        if hasattr(dl, "getvalue"):
            img_bytes = dl.getvalue()
        elif isinstance(dl, (bytes, bytearray)):
            img_bytes = bytes(dl)
    parsed = await fitness_nutrition.parse_smart_scale_input(
        raw_text=message.caption or "",
        image_bytes=img_bytes,
        image_mime="image/jpeg",
    )
    await _commit_scale_parsed(message, state, db, parsed)


@router.message(ScaleImportState.waiting_input, lambda m: bool(m.text and not m.text.startswith("/")))
async def fit_scale_text(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    parsed = await fitness_nutrition.parse_smart_scale_input(raw_text=raw)
    await _commit_scale_parsed(message, state, db, parsed)


@router.callback_query(lambda c: c.data == "fit:profile_setup")
async def fit_profile_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FitProfileState.height)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="fit:profile_cancel")]]
    )
    await callback.message.answer(
        "⚙️ <b>Настройка тела и КБЖУ</b>\n\nШаг 1/5. Введи свой <b>рост (см)</b>, например <code>176</code>:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "fit:profile_cancel")
async def fit_profile_cancel(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await state.clear()
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    text, kb = await _render_body_card(db, user)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Отменено")


@router.message(FitProfileState.height, lambda m: bool(m.text and not m.text.startswith("/")))
async def fit_profile_height(message: types.Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    if raw.lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    try:
        h = float(raw)
        if not (100.0 <= h <= 250.0):
            raise ValueError
    except ValueError:
        await message.answer(texts.error("рост должен быть числом от 100 до 250, например 176."))
        return
    await state.update_data(height_cm=h)
    await state.set_state(FitProfileState.age)
    await message.answer("Шаг 2/5. Сколько тебе <b>полных лет</b>? (например <code>28</code>)", parse_mode="HTML")


@router.message(FitProfileState.age, lambda m: bool(m.text and not m.text.startswith("/")))
async def fit_profile_age(message: types.Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    try:
        age = int(float(raw.replace(",", ".")))
        if not (14 <= age <= 100):
            raise ValueError
    except ValueError:
        await message.answer(texts.error("возраст должен быть от 14 до 100."))
        return
    await state.update_data(age_years=age)
    await state.set_state(FitProfileState.sex)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👨 Мужской", callback_data="fitsex:male"),
                InlineKeyboardButton(text="👩 Женский", callback_data="fitsex:female"),
            ],
            [InlineKeyboardButton(text="🙂 Усреднённо", callback_data="fitsex:neutral")],
        ]
    )
    await message.answer("Шаг 3/5. Выбери пол для формулы КБЖУ:", reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("fitsex:"))
async def fit_profile_sex(callback: types.CallbackQuery, state: FSMContext) -> None:
    sex = callback.data.split(":", 1)[1]
    await state.update_data(sex=sex)
    await state.set_state(FitProfileState.activity)
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"fitact:{key}")]
        for key, (label, _) in fitness_nutrition.ACTIVITY_MULTIPLIERS.items()
    ]
    await safe_edit(
        callback.message,
        "Шаг 4/5. Какая у тебя сейчас обычная бытовая активность?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("fitact:"))
async def fit_profile_activity(callback: types.CallbackQuery, state: FSMContext) -> None:
    act = callback.data.split(":", 1)[1]
    await state.update_data(activity_level=act)
    await state.set_state(FitProfileState.goal)
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"fitgoal:{key}")]
        for key, label in fitness_nutrition.GOAL_LABELS.items()
    ]
    await safe_edit(
        callback.message,
        "Шаг 5/5. Какая цель сейчас ближе?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("fitgoal:"))
async def fit_profile_goal(callback: types.CallbackQuery, state: FSMContext) -> None:
    goal = callback.data.split(":", 1)[1]
    await state.update_data(weight_goal=goal)
    await state.set_state(FitProfileState.target)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Пропустить ориентир веса", callback_data="fitgoal_skip")]]
    )
    await callback.message.answer(
        "Если хочешь, введи мягкий <b>ориентир по весу (кг)</b>, например <code>75</code>, или нажми «Пропустить».",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


async def _finish_fit_profile(
    target_msg: types.Message, state: FSMContext, db, *, tg_user_id: int, tg_full_name: str, weight_target: float | None
) -> None:
    data = await state.get_data()
    user = dict(await ensure_user(db, tg_user_id, tg_full_name))
    user_id = int(user["id"])
    await repo.update_user_body(
        db,
        user_id,
        height_cm=data.get("height_cm"),
        weight_goal=data.get("weight_goal"),
        weight_target=weight_target,
        age_years=data.get("age_years"),
        sex=data.get("sex"),
        activity_level=data.get("activity_level"),
    )
    await state.clear()
    fresh_user = dict(await repo.get_user(db, user_id))
    latest = await repo.get_latest_weight(db, user_id)
    targets = fitness_nutrition.calculate_daily_targets(fresh_user, latest)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🫠 Микро-старт 3 мин", callback_data="fit:lazy"),
                InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
            ],
            [InlineKeyboardButton(text="⚖ К карте тела", callback_data="fit:body")],
        ]
    )
    await target_msg.answer(
        f"✅ <b>Анкета сохранена!</b>\n"
        f"Цель: {_h(targets['goal_label'])}\n"
        f"Суточный ориентир КБЖУ: <b>{targets['kcal']} ккал</b> "
        f"(Б <b>{targets['protein_g']}г</b> / Ж <b>{targets['fat_g']}г</b> / У <b>{targets['carbs_g']}г</b>).",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "fitgoal_skip")
async def fit_profile_skip_target(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await _finish_fit_profile(
        callback.message,
        state,
        db,
        tg_user_id=callback.from_user.id,
        tg_full_name=callback.from_user.full_name,
        weight_target=None,
    )
    await callback.answer("Сохранила!")


@router.message(FitProfileState.target, lambda m: bool(m.text and not m.text.startswith("/")))
async def fit_profile_target(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    if raw.lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    try:
        wt = float(raw)
        if not (30.0 <= wt <= 250.0):
            raise ValueError
    except ValueError:
        await message.answer(texts.error("целевой вес должен быть числом от 30 до 250."))
        return
    await _finish_fit_profile(
        message,
        state,
        db,
        tg_user_id=message.from_user.id,
        tg_full_name=message.from_user.full_name,
        weight_target=wt,
    )


# --- Home Equipment ---

@router.callback_query(lambda c: c.data == "fit:gear")
async def fit_gear_cb(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await state.clear()
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    eq = repo.get_user_equipment(user)
    text, kb = _render_gear_card(eq)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("fit:gear_toggle:"))
async def fit_gear_toggle(callback: types.CallbackQuery, db) -> None:
    key = callback.data.split(":", 2)[2]
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    eq = repo.get_user_equipment(user)
    if key in eq:
        eq = [x for x in eq if x != key]
    else:
        eq.append(key)
    await repo.update_user_body(db, int(user["id"]), equipment=eq)
    text, kb = _render_gear_card(eq)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Обновила инвентарь")


@router.callback_query(lambda c: c.data == "fit:gear_custom")
async def fit_gear_custom_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GearCustomState.waiting_text)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="fit:gear")]]
    )
    await callback.message.answer(
        "Напиши, какой ещё инвентарь или тренажёр у тебя есть (например: <code>скамья, гиря 16 кг</code>):",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(GearCustomState.waiting_text, lambda m: bool(m.text and not m.text.startswith("/")))
async def fit_gear_custom_save(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    eq = repo.get_user_equipment(user)
    for part in raw.split(","):
        clean = part.strip()
        if clean and clean not in eq:
            eq.append(clean[:40])
    await repo.update_user_body(db, int(user["id"]), equipment=eq)
    await state.clear()
    text, kb = _render_gear_card(eq)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# --- Anti-Laziness Micro-Start & Personal Workouts ---

@router.callback_query(lambda c: c.data and (c.data == "fit:lazy" or c.data.startswith("fit:lazy:")))
async def fit_lazy_cb(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    parts = (callback.data or "").split(":")
    try:
        var = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        var = 0
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    user_id = int(user["id"])
    eq = repo.get_user_equipment(user)
    local_date = local_date_str(datetime.datetime.utcnow(), user.get("timezone") or "UTC")
    week_stats = await repo.workout_stats_week(db, user_id, local_date)
    workout = fitness_nutrition.build_lazy_start_workout(
        eq, recent_focus_areas=week_stats.get("recent_focus_areas"), variation=var
    )
    await state.update_data(pending_workout=workout, pending_workout_lazy=True)

    lines = [
        f"🫠 <b>{_h(workout['title'])}</b>",
        f"🧰 Инвентарь: {_h(workout['equipment_used'])} · ~{workout['kcal_burned']} ккал",
        "",
    ]
    for idx, step in enumerate(workout["steps"], 1):
        lines.append(f"{idx}. {_h(step)}")
    lines.append(f"\n🤝 <i>{_h(workout['deal_text'])}</i>")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"✅ Сделал(а) {workout['duration_min']} мин (+3 кармы)",
                    callback_data="fit:done:lazy",
                )
            ],
            [InlineKeyboardButton(text="🔥 Втянулся — ещё 10 мин!", callback_data="fit:workout:10:auto")],
            [InlineKeyboardButton(text="🔄 Другой вариант", callback_data=f"fit:lazy:{var + 1}")],
            [
                InlineKeyboardButton(text="🧰 Инвентарь", callback_data="fit:gear"),
                InlineKeyboardButton(text="⬅️ Спорт", callback_data="fit:menu"),
            ],
        ]
    )
    await safe_edit(callback.message, "\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data == "fit:workout_menu")
async def fit_workout_menu_cb(callback: types.CallbackQuery, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    eq = repo.get_user_equipment(user)
    eq_line = fitness_nutrition.format_equipment_labels(eq)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚡ 10 мин · Авто", callback_data="fit:workout:10:auto"),
                InlineKeyboardButton(text="🔥 20 мин · Авто", callback_data="fit:workout:20:auto"),
                InlineKeyboardButton(text="💪 30 мин · Авто", callback_data="fit:workout:30:auto"),
            ],
            [
                InlineKeyboardButton(text="15м Верх/спина", callback_data="fit:workout:15:upper"),
                InlineKeyboardButton(text="15м Ноги/кор", callback_data="fit:workout:15:lower"),
            ],
            [
                InlineKeyboardButton(text="15м Пресс/осанка", callback_data="fit:workout:15:core"),
                InlineKeyboardButton(text="🫠 Лень (3–5м)", callback_data="fit:lazy"),
            ],
            [
                InlineKeyboardButton(text="🧰 Сменить инвентарь", callback_data="fit:gear"),
                InlineKeyboardButton(text="⬅️ Спорт", callback_data="fit:menu"),
            ],
        ]
    )
    await safe_edit(
        callback.message,
        f"🏋️ <b>Персональная тренировка под твой дом</b>\nИнвентарь: {_h(eq_line)}\n\nВыбери формат:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("fit:workout:"))
async def fit_workout_generate_cb(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    parts = (callback.data or "").split(":")
    try:
        dur = int(parts[2]) if len(parts) > 2 else 15
    except ValueError:
        dur = 15
    focus = parts[3] if len(parts) > 3 else "auto"

    await callback.answer("Собираю тренировку...")
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    workout = await fitness_nutrition.generate_personal_workout(
        db, user, duration_min=dur, focus_area=focus
    )
    await state.update_data(pending_workout=workout, pending_workout_lazy=False)

    lines = [
        f"🏋️ <b>{_h(workout['title'])}</b>",
        f"🧰 {_h(workout['equipment_used'])} · ~{workout['kcal_burned']} ккал",
        "",
    ]
    for idx, step in enumerate(workout["steps"], 1):
        lines.append(f"{idx}. {_h(step)}")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Тренировка выполнена (+5 кармы)", callback_data="fit:done:workout")],
            [
                InlineKeyboardButton(text="🔄 Другой вариант", callback_data=f"fit:workout:{dur}:{focus}"),
                InlineKeyboardButton(text="⬅️ К выбору", callback_data="fit:workout_menu"),
            ],
        ]
    )
    await safe_edit(callback.message, "\n".join(lines), reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("fit:done:"))
async def fit_workout_done_cb(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    kind = callback.data.split(":")[2] if len(callback.data.split(":")) > 2 else "workout"
    is_lazy = kind == "lazy"
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    data = await state.get_data()
    workout = data.get("pending_workout")
    if not isinstance(workout, dict):
        eq = repo.get_user_equipment(user)
        workout = (
            fitness_nutrition.build_lazy_start_workout(eq)
            if is_lazy
            else fitness_nutrition._build_offline_workout(eq, duration_min=15)  # noqa: SLF001
        )
    res = await fitness_nutrition.complete_workout(db, user, workout, is_lazy_start=is_lazy)
    await state.clear()

    week = res["week_stats"]
    advice = res["daily_advice"]
    targets = advice["targets"]
    msg_head = (
        "🔥 <b>Сработало! Порог лени пробит!</b>"
        if is_lazy
        else f"💪 <b>Отличная работа: {_h(res['title'])}!</b>"
    )
    lines = [
        msg_head,
        f"⏱ {res['duration_min']} мин · сожжено ~{res['kcal_burned']} ккал",
        f"💓 Карма ритма: +{res['karma_delta']} → <b>{res.get('score_after', '—')}/100</b>",
        f"📊 За 7 дней: {week['count']} тр. ({week['total_minutes']} мин)",
        f"🍽 Норма на сегодня с учётом движения: <b>{targets['kcal']} ккал</b> · Белок {targets['protein_g']} г",
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔥 Мой КБЖУ сегодня", callback_data="kbju:today"),
                InlineKeyboardButton(text="⬅️ Спорт", callback_data="fit:menu"),
            ]
        ]
    )
    await safe_edit(callback.message, "\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await callback.answer("Засчитала тренировку! 💪")


@router.callback_query(lambda c: c.data == "fit:stats")
async def fit_stats_cb(callback: types.CallbackQuery, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    user_id = int(user["id"])
    local_date = local_date_str(datetime.datetime.utcnow(), user.get("timezone") or "UTC")
    week = await repo.workout_stats_week(db, user_id, local_date)
    trend = await repo.body_metrics_trend(db, user_id, days=30)
    advice = await fitness_nutrition.get_daily_nutrition_advice(db, user)

    lines = [
        "📊 <b>Прогресс формы (строго личный)</b>",
        f"🏋️ За 7 дней: <b>{week['count']}</b> трен. · <b>{week['total_minutes']} мин</b> · ~{week['kcal_burned']} ккал",
        f"🫠 Микро-стартов против лени: {week['lazy_starts']}",
        "",
        "⚖ " + _h(fitness_nutrition.format_body_trend_feedback(trend, str(user.get("weight_goal") or "loss"))),
        f"🔥 Сегодня КБЖУ: {_h(advice['summary_line'])}",
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🫠 Микро-старт", callback_data="fit:lazy"),
                InlineKeyboardButton(text="⚖ Умные весы", callback_data="fit:body"),
            ],
            [InlineKeyboardButton(text="⬅️ Спорт", callback_data="fit:menu")],
        ]
    )
    await safe_edit(callback.message, "\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await callback.answer()


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
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
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
