"""Дневник КБЖУ на день: цели, учёт еды по фото/тексту/рецептам и подсказки."""

from __future__ import annotations

import datetime
from html import escape as _h
import io
from typing import Any, Dict, List, Optional, Tuple

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from services import fitness_nutrition, kitchen_ai
from utils.menu_match import match_menu_label
from utils.sender import safe_edit
from utils.time import local_date_str
from utils.user import ensure_user

router = Router()


class MealAddState(StatesGroup):
    waiting_input = State()
    confirm = State()


def _progress_bar(current: float, target: float, width: int = 10) -> str:
    if target <= 0:
        return "░" * width
    ratio = max(0.0, min(1.0, float(current) / float(target)))
    filled = max(0, min(width, round(ratio * width)))
    return "█" * filled + "░" * (width - filled)


def _format_macro_row(label: str, eaten: float, target: float) -> str:
    rem = max(0, int(round(target - eaten)))
    over = max(0, int(round(eaten - target)))
    tail = f"+{over} сверх" if over > 0 else f"ост. {rem} г"
    return f"• {label}: <b>{round(eaten):.0f}</b> / {int(round(target))} г ({tail})"


async def render_kbju_card(
    db,
    user: Dict[str, Any],
    *,
    now_utc: Optional[datetime.datetime] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    """Сформировать карточку личного КБЖУ на сегодня (текст + клавиатура)."""
    now = now_utc or datetime.datetime.utcnow()
    advice = await fitness_nutrition.get_daily_nutrition_advice(db, user, now_utc=now)
    targets = advice["targets"]
    eaten = advice["eaten"]
    remaining = advice["remaining"]
    over = advice["over"]
    meals: List[Dict[str, Any]] = advice["meals"]

    bar = _progress_bar(eaten["kcal"], targets["kcal"])
    kcal_status = (
        f"перебор на {over['kcal']} ккал"
        if over["kcal"] > 0
        else f"осталось {remaining['kcal']} ккал"
    )
    lines = [
        f"🔥 <b>Твой КБЖУ на сегодня</b> ({_h(advice['local_date'])})",
        f"Цель: {_h(targets['goal_label'])}",
        f"<code>[{bar}]</code> <b>{eaten['kcal']}</b> / {targets['kcal']} ккал ({kcal_status})",
    ]
    if targets.get("workout_bonus_kcal"):
        lines.append(f"🏃 Бонус за тренировку сегодня: +{targets['workout_bonus_kcal']} ккал и +10 г белка")
    if targets.get("is_estimated"):
        lines.append("<i>Параметры тела пока примерные — уточни рост/возраст/вес в «⚖ Умные весы».</i>")

    lines.append("")
    lines.append(_format_macro_row("🥩 Белок", eaten["protein_g"], targets["protein_g"]))
    lines.append(_format_macro_row("🥑 Жиры", eaten["fat_g"], targets["fat_g"]))
    lines.append(_format_macro_row("🌾 Углеводы", eaten["carbs_g"], targets["carbs_g"]))

    if advice["eat_more"]:
        lines.append("\n💡 <b>Что хорошо бы съесть:</b>\n• " + "\n• ".join(_h(x) for x in advice["eat_more"]))
    if advice["go_easy_on"]:
        lines.append("\n⚠️ <b>От чего лучше воздержаться:</b>\n• " + "\n• ".join(_h(x) for x in advice["go_easy_on"]))

    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Записать еду (фото / текст)", callback_data="kbju:add")],
        [
            InlineKeyboardButton(text="🍳 Что приготовить?", callback_data="kitchen:quick_pick"),
            InlineKeyboardButton(text="✨ ИИ-шеф", callback_data="kitchen:ai_chef"),
        ],
    ]

    if meals:
        lines.append("\n🍽 <b>Записано сегодня:</b>")
        del_btns = []
        for idx, meal in enumerate(meals[-6:], 1):
            lines.append(
                f"{idx}. {_h(meal['title'])} — {int(round(float(meal.get('kcal') or 0)))} ккал "
                f"(Б{float(meal.get('protein_g') or 0):g}/Ж{float(meal.get('fat_g') or 0):g}/У{float(meal.get('carbs_g') or 0):g})"
            )
            del_btns.append(
                InlineKeyboardButton(text=f"🗑 #{idx}", callback_data=f"kbju:del:{meal['id']}")
            )
        for i in range(0, len(del_btns), 3):
            rows.append(del_btns[i : i + 3])

    rows.append(
        [
            InlineKeyboardButton(text="⚖ Тело и весы", callback_data="fit:body"),
            InlineKeyboardButton(text="⬅️ Кухня", callback_data="kitchen:main"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("kbju"))
@router.message(lambda m: match_menu_label(m.text, {"кбжу", "мой кбжу", "калории"}))
async def cmd_kbju(message: types.Message, state: FSMContext, db) -> None:
    await state.clear()
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    text, kb = await render_kbju_card(db, user)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "kbju:today")
async def kbju_today_cb(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await state.clear()
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    text, kb = await render_kbju_card(db, user)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def _prompt_meal_input(target_message: types.Message, state: FSMContext) -> None:
    await state.set_state(MealAddState.waiting_input)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="kbju:cancel")]]
    )
    await target_message.answer(
        "🍽 <b>Запись еды в КБЖУ</b>\n\n"
        "Пришли <b>фото тарелки</b> или напиши <b>текстом</b>, что съел(а), например:\n"
        "• <code>Овсянка на овсяном молоке 250 г и банан</code>\n"
        "• <code>Тофу 150 г и гречка 150 г</code>\n"
        "• <code>Протеиновый коктейль 320 ккал б28 ж4 у38</code>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.message(Command("eat"))
async def cmd_eat(message: types.Message, state: FSMContext, db) -> None:
    await state.clear()
    await ensure_user(db, message.from_user.id, message.from_user.full_name)
    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=1)
    meal_text = parts[1].strip() if len(parts) > 1 else ""
    if not meal_text:
        await _prompt_meal_input(message, state)
        return
    await state.set_state(MealAddState.waiting_input)
    parsed = await fitness_nutrition.parse_meal_input(raw_text=meal_text)
    await _show_meal_preview(message, state, parsed)


@router.callback_query(lambda c: c.data == "kbju:add")
async def kbju_add_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await _prompt_meal_input(callback.message, state)
    await callback.answer()


@router.callback_query(lambda c: c.data == "kbju:cancel")
async def kbju_add_cancel(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await state.clear()
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    text, kb = await render_kbju_card(db, user)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Отменено")


async def _show_meal_preview(message: types.Message, state: FSMContext, parsed: Dict[str, Any]) -> None:
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="kbju:cancel")]]
    )
    if not parsed.get("ok"):
        err = parsed.get("error") or ""
        msg = (
            "ИИ-распознавание фото сейчас недоступно — напиши блюдо текстом."
            if err in {"llm_not_configured", "vision_parse_failed"}
            else "Не смогла разобрать блюдо. Напиши чуть конкретнее (например: <code>Гречка 150 г и тофу 100 г</code>)."
        )
        await message.answer(msg, reply_markup=cancel_kb, parse_mode="HTML")
        return

    await state.set_state(MealAddState.confirm)
    await state.update_data(meal_preview=parsed)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Записать в КБЖУ", callback_data="kbju:confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="kbju:cancel")],
        ]
    )
    await message.answer(
        f"🍽 <b>Похоже на:</b> {_h(parsed['title'])}\n"
        f"🔥 <b>{int(round(parsed['kcal']))} ккал</b> · "
        f"Б <b>{parsed['protein_g']:g} г</b> / Ж <b>{parsed['fat_g']:g} г</b> / У <b>{parsed['carbs_g']:g} г</b>\n\n"
        "Записываем?",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.message(MealAddState.waiting_input, F.photo)
async def kbju_add_photo(message: types.Message, state: FSMContext) -> None:
    photo = message.photo[-1]
    buf = io.BytesIO()
    dl = await message.bot.download(photo, destination=buf)
    img_bytes = buf.getvalue()
    if not img_bytes and dl is not None:
        if hasattr(dl, "getvalue"):
            img_bytes = dl.getvalue()
        elif isinstance(dl, (bytes, bytearray)):
            img_bytes = bytes(dl)
    parsed = await fitness_nutrition.parse_meal_input(
        raw_text=message.caption or "",
        image_bytes=img_bytes,
        image_mime="image/jpeg",
    )
    await _show_meal_preview(message, state, parsed)


@router.message(MealAddState.waiting_input, lambda m: bool(m.text and not m.text.startswith("/")))
async def kbju_add_text(message: types.Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    parsed = await fitness_nutrition.parse_meal_input(raw_text=raw)
    await _show_meal_preview(message, state, parsed)


@router.callback_query(lambda c: c.data == "kbju:confirm")
async def kbju_confirm_cb(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    data = await state.get_data()
    preview = data.get("meal_preview") or {}
    if not preview or not preview.get("ok"):
        await state.clear()
        await callback.answer("Запись устарела, введи заново.", show_alert=True)
        return

    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    now_utc = datetime.datetime.utcnow()
    local_date = local_date_str(now_utc, user.get("timezone") or "UTC")
    await repo.add_meal_log(
        db,
        int(user["id"]),
        local_date,
        str(preview.get("title") or "Приём пищи"),
        meal_slot=str(preview.get("meal_slot") or "snack"),
        kcal=float(preview.get("kcal") or 0),
        protein_g=float(preview.get("protein_g") or 0),
        fat_g=float(preview.get("fat_g") or 0),
        carbs_g=float(preview.get("carbs_g") or 0),
        source="vision" if preview.get("used_llm") else "manual",
    )
    await state.clear()
    text, kb = await render_kbju_card(db, user, now_utc=now_utc)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Записала в КБЖУ ✅")


@router.callback_query(lambda c: c.data and c.data.startswith("kbju:log_recipe:"))
async def kbju_log_recipe_cb(callback: types.CallbackQuery, db) -> None:
    parts = (callback.data or "").split(":")
    rid = parts[2] if len(parts) > 2 else ""
    try:
        servings = max(0.5, min(6.0, float(parts[3]))) if len(parts) > 3 else 1.0
    except ValueError:
        servings = 1.0

    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    recipe = await kitchen_ai.get_recipe_for_user(db, int(user["id"]), rid)
    if not recipe:
        await callback.answer("Рецепт не найден.", show_alert=True)
        return

    per_serving = fitness_nutrition.estimate_recipe_nutrition(recipe)
    now_utc = datetime.datetime.utcnow()
    local_date = local_date_str(now_utc, user.get("timezone") or "UTC")
    kcal = round(float(per_serving["kcal"]) * servings)
    protein_g = round(float(per_serving["protein_g"]) * servings, 1)
    fat_g = round(float(per_serving["fat_g"]) * servings, 1)
    carbs_g = round(float(per_serving["carbs_g"]) * servings, 1)

    await repo.add_meal_log(
        db,
        int(user["id"]),
        local_date,
        str(recipe.get("title") or "Блюдо"),
        recipe_id=str(recipe.get("id") or ""),
        servings=servings,
        kcal=kcal,
        protein_g=protein_g,
        fat_g=fat_g,
        carbs_g=carbs_g,
        source="recipe",
    )
    advice = await fitness_nutrition.get_daily_nutrition_advice(db, user, now_utc=now_utc)
    rem = advice["remaining"]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔥 Мой КБЖУ сегодня", callback_data="kbju:today"),
                InlineKeyboardButton(text="⬅️ Кухня", callback_data="kitchen:main"),
            ]
        ]
    )
    await callback.message.answer(
        f"✅ Записала <b>{_h(recipe.get('title'))}</b> ({servings:g} порц.): "
        f"<b>{kcal} ккал</b> (Б {protein_g:g}г / Ж {fat_g:g}г / У {carbs_g:g}г).\n"
        f"Остаток на сегодня: <b>{rem['kcal']} ккал</b> · Белок +{rem['protein_g']} г.",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer("Съедено и учтено! 🍽")


@router.callback_query(lambda c: c.data and c.data.startswith("kbju:del:"))
async def kbju_delete_cb(callback: types.CallbackQuery, db) -> None:
    parts = (callback.data or "").split(":")
    try:
        log_id = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer()
        return
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    deleted = await repo.delete_meal_log(db, int(user["id"]), log_id)
    text, kb = await render_kbju_card(db, user)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Удалила запись" if deleted else "Запись не найдена")
