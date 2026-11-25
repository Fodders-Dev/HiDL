"""Обратная связь от пользователей (баги, идеи, скриншоты) напрямую автору бота."""

from __future__ import annotations

import html
import logging

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import get_owner_telegram_id
from keyboards.common import main_menu_keyboard
from utils.user import ensure_user

logger = logging.getLogger(__name__)
router = Router()


class FeedbackState(StatesGroup):
    waiting_message = State()


def _feedback_prompt_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="fb:cancel")]]
    )


@router.message(Command("feedback"))
async def feedback_command(message: types.Message, state: FSMContext, db) -> None:
    await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await state.set_state(FeedbackState.waiting_message)
    await message.answer(
        "💬 <b>Написать автору / сообщить о баге</b>\n\n"
        "Опиши проблему, идею или скинь скриншот с подписью одним сообщением — я перешлю это напрямую создателю HiDL.",
        reply_markup=_feedback_prompt_kb(),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "fb:start")
async def feedback_start_cb(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await state.set_state(FeedbackState.waiting_message)
    await callback.message.answer(
        "💬 <b>Написать автору / сообщить о баге</b>\n\n"
        "Опиши проблему, идею или скинь скриншот с подписью одним сообщением — я перешлю это напрямую создателю HiDL.",
        reply_markup=_feedback_prompt_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "fb:cancel")
async def feedback_cancel_cb(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    await state.clear()
    await callback.message.answer("Отменено.", reply_markup=main_menu_keyboard(user))
    await callback.answer()


@router.message(FeedbackState.waiting_message, lambda m: bool(m.text and m.text.startswith("/")))
async def feedback_slash_abort(message: types.Message, state: FSMContext) -> None:
    await state.clear()


@router.message(FeedbackState.waiting_message)
async def feedback_receive(message: types.Message, state: FSMContext, db) -> None:
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    raw_text = (message.text or message.caption or "").strip()
    if message.text and raw_text.casefold() in {"отмена", "назад"}:
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu_keyboard(user))
        return

    if not raw_text and not message.photo:
        await message.answer(
            "Пришли текст или скриншот с подписью (или нажми «Отмена»).",
            reply_markup=_feedback_prompt_kb(),
        )
        return

    await state.clear()
    owner_tg_id = get_owner_telegram_id()
    sender_name = html.escape(str(user.get("name") or message.from_user.full_name or "Пользователь"))
    username = f"@{html.escape(message.from_user.username)}" if getattr(message.from_user, "username", None) else "без username"
    mode = html.escape(str(user.get("profile_mode") or "adult"))
    header = (
        "💬 <b>Фидбек HiDL</b>\n"
        f"От: {sender_name} ({username}) · tg:<code>{int(message.from_user.id)}</code> · uid:<code>{int(user['id'])}</code> · режим:{mode}\n\n"
    )
    body = html.escape(raw_text) if raw_text else "<i>(скриншот без текста)</i>"
    full_text = header + body

    bot = getattr(message, "bot", None)
    if owner_tg_id > 0 and bot is not None:
        try:
            if message.photo:
                file_id = message.photo[-1].file_id
                await bot.send_photo(owner_tg_id, photo=file_id, caption=full_text[:1024], parse_mode="HTML")
            else:
                await bot.send_message(owner_tg_id, full_text, parse_mode="HTML")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to forward feedback to owner %s: %s", owner_tg_id, exc)
    else:
        logger.warning("Feedback received from %s, but owner_tg_id=%s or bot unavailable", message.from_user.id, owner_tg_id)

    await message.answer(
        "Спасибо! Передала сообщение автору 💛",
        reply_markup=main_menu_keyboard(user),
    )
