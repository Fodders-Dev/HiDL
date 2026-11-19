from __future__ import annotations

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from handlers import focus_cafe, kitchen
from utils.sender import safe_edit
from utils.user import ensure_user

router = Router()


def _help_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="😵 Мало сил", callback_data="help:low")],
            [InlineKeyboardButton(text="⏳ Прокрастинация", callback_data="help:procrast")],
            [InlineKeyboardButton(text="🧹 Бардак", callback_data="help:mess")],
            [InlineKeyboardButton(text="🍽 Не знаю, что есть", callback_data="help:food")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="main:menu")],
        ]
    )


def _help_low_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Сделал микро‑шаг", callback_data="help:ack")],
            [InlineKeyboardButton(text="🏋️ Разминка 5 мин", callback_data="move:warmup")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="help:menu")],
        ]
    )


def _help_mess_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Быстрый сценарий", callback_data="home:quickmenu")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="help:menu")],
        ]
    )


def _help_procrast_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="☕ Старт фокуса", callback_data="help:cafe")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="help:menu")],
        ]
    )


async def _render_help_menu(message: types.Message) -> None:
    text = "Что сейчас сложнее всего? Выбери один вариант — дам короткий следующий шаг."
    await safe_edit(message, text, reply_markup=_help_menu_keyboard())


@router.message(Command("help_now"))
@router.message(lambda m: m.text and "помощ" in m.text.lower() and "сейчас" in m.text.lower())
async def help_now_entry(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Что сейчас сложнее всего? Выбери один вариант — дам короткий следующий шаг.",
        reply_markup=_help_menu_keyboard(),
    )


@router.callback_query(lambda c: c.data == "help:menu")
async def help_now_menu(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _render_help_menu(callback.message)
    await callback.answer()


@router.callback_query(lambda c: c.data == "help:low")
async def help_now_low(callback: types.CallbackQuery) -> None:
    text = (
        "Минимум сил — значит минимум шагов. Попробуй так:\n"
        "• стакан воды\n"
        "• открыть окно на 2–3 минуты\n"
        "• 2 минуты мягкой разминки\n"
        "Выбери, что сделаешь прямо сейчас."
    )
    await safe_edit(callback.message, text, reply_markup=_help_low_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data == "help:ack")
async def help_now_ack(callback: types.CallbackQuery) -> None:
    await callback.answer("Отлично. Этого достаточно для старта.")


@router.callback_query(lambda c: c.data == "help:mess")
async def help_now_mess(callback: types.CallbackQuery) -> None:
    text = "Давай быстро на 10–15 минут. Выбери сценарий — я поведу по шагам."
    await safe_edit(callback.message, text, reply_markup=_help_mess_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data == "help:procrast")
async def help_now_procrast(callback: types.CallbackQuery) -> None:
    text = "Давай сделаем одну короткую фокус‑сессию. Нужна задача и 15–25 минут."
    await safe_edit(callback.message, text, reply_markup=_help_procrast_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data == "help:cafe")
async def help_now_cafe(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await focus_cafe.cafe_start(
        callback.message,
        state,
        db,
        tg_user_id=callback.from_user.id,
        tg_full_name=callback.from_user.full_name,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "help:food")
async def help_now_food(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    profile = await kitchen._get_meal_profile(db, user["id"])  # noqa: SLF001
    quick = kitchen.get_quick_recipes(profile, limit=3, max_minutes=30)
    rows = [
        [
            InlineKeyboardButton(
                text=kitchen._recipe_button_text(r),  # noqa: SLF001
                callback_data=f"kitchen:cook_view:{r['id']}:2:all:0",
            )
        ]
        for r in quick
    ]
    rows.append([InlineKeyboardButton(text="📖 Все рецепты", callback_data="kitchen:recipes")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="help:menu")])
    lines = [f"🍽 Быстрые варианты до 30 мин ({kitchen._diet_label(profile)}):"]  # noqa: SLF001
    for r in quick:
        lines.append(
            f"• {kitchen._h(r.get('title'))} ({int(r.get('time_minutes') or 15)} мин) — {kitchen._h(r.get('desc', ''))}"  # noqa: SLF001
        )
    if not quick:
        lines.append("Пока не нашла подходящих рецептов — посмотри всю книгу.")
    await safe_edit(
        callback.message,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()
