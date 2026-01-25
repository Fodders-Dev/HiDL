import datetime

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.gender import done_button_label, button_label, g
from utils.time import local_date_str, tzinfo_from_string
from utils.user import ensure_user


router = Router()


class FocusCafeState(StatesGroup):
    task = State()
    duration = State()


def _duration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="25 мин", callback_data="cafe:duration:25"),
                InlineKeyboardButton(text="50 мин", callback_data="cafe:duration:50"),
            ],
            [
                InlineKeyboardButton(text="15 мин", callback_data="cafe:duration:15"),
                InlineKeyboardButton(text="Своя длительность", callback_data="cafe:duration:custom"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="cafe:cancel")],
        ]
    )


def _task_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="cafe:cancel")],
        ]
    )


def _custom_duration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="cafe:duration:back")],
        ]
    )


def _cooldown_text(user: dict, now_utc: datetime.datetime) -> str | None:
    cooldown = user.get("focus_cooldown_until") or ""
    if not cooldown:
        return None
    try:
        until = datetime.datetime.fromisoformat(cooldown)
    except Exception:
        return None
    if now_utc < until:
        tzinfo = tzinfo_from_string(user["timezone"])
        until_local = until.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
        return f"Пауза по фокусу до {until_local.strftime('%H:%M')}. Вернёмся позже."
    return None


@router.message(Command("cafe"))
@router.message(lambda m: m.text and "кафе" in m.text.lower())
async def cafe_start(message: types.Message, state: FSMContext, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await state.clear()
    now_utc = datetime.datetime.utcnow()
    cooldown_text = _cooldown_text(user, now_utc)
    if cooldown_text:
        await message.answer(cooldown_text)
        return

    active = await repo.get_active_focus_session(db, user["id"])
    if active:
        tzinfo = tzinfo_from_string(user["timezone"])
        end_ts = datetime.datetime.fromisoformat(active["end_ts"])
        end_local = end_ts.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
        text = (
            f"У тебя уже идёт сессия «{active['task_title']}».\n"
            f"Финиш примерно в {end_local.strftime('%H:%M')}."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=button_label(user, "✅ Завершить сейчас", "✅ Завершить сейчас", "✅ Завершить сейчас"),
                        callback_data=f"cafe:finish:done:{active['id']}",
                    ),
                    InlineKeyboardButton(
                        text=button_label(user, "❌ Прервать", "❌ Прервать", "❌ Прервать"),
                        callback_data=f"cafe:finish:fail:{active['id']}",
                    ),
                ]
            ]
        )
        await message.answer(text, reply_markup=kb)
        return

    await state.set_state(FocusCafeState.task)
    await message.answer(
        "Что берём в работу? Одной фразой, без деталей.",
        reply_markup=_task_keyboard(),
    )


@router.message(FocusCafeState.task)
async def cafe_task(message: types.Message, state: FSMContext) -> None:
    task = (message.text or "").strip()
    if not task:
        await message.answer("Нужна короткая формулировка задачи.")
        return
    await state.update_data(task=task)
    await state.set_state(FocusCafeState.duration)
    await message.answer("Сколько минут работаем?", reply_markup=_duration_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("cafe:duration:"))
async def cafe_pick_duration(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    action = callback.data.split(":")[2]
    if action == "custom":
        await state.set_state(FocusCafeState.duration)
        await callback.message.answer(
            "Сколько минут? Напиши число (например 35).",
            reply_markup=_custom_duration_keyboard(),
        )
        await callback.answer()
        return
    if action == "back":
        await state.set_state(FocusCafeState.duration)
        await callback.message.answer(
            "Сколько минут работаем?",
            reply_markup=_duration_keyboard(),
        )
        await callback.answer()
        return
    try:
        minutes = int(action)
    except ValueError:
        await callback.answer("Не поняла длительность.")
        return
    await _start_session(callback, state, db, minutes)


@router.message(FocusCafeState.duration)
async def cafe_custom_duration(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip()
    try:
        minutes = int(raw)
    except ValueError:
        await message.answer(
            "Нужно число минут (например 35).",
            reply_markup=_custom_duration_keyboard(),
        )
        return
    if minutes < 5 or minutes > 180:
        await message.answer(
            "Давай в пределах 5–180 минут.",
            reply_markup=_custom_duration_keyboard(),
        )
        return
    await _start_session(message, state, db, minutes)


async def _start_session(event, state: FSMContext, db, minutes: int) -> None:
    data = await state.get_data()
    task = data.get("task")
    if not task:
        if isinstance(event, types.CallbackQuery):
            await event.message.answer("Сначала задай задачу.")
            await event.answer()
        else:
            await event.answer("Сначала задай задачу.")
        return
    user = await ensure_user(db, event.from_user.id, event.from_user.full_name)
    now_utc = datetime.datetime.utcnow()
    start_ts = now_utc
    checkin_ts = now_utc + datetime.timedelta(minutes=max(5, minutes // 2))
    end_ts = now_utc + datetime.timedelta(minutes=minutes)
    session_id = await repo.create_focus_session(
        db,
        user["id"],
        task,
        minutes,
        start_ts.isoformat(),
        checkin_ts.isoformat(),
        end_ts.isoformat(),
    )
    await state.clear()

    tzinfo = tzinfo_from_string(user["timezone"])
    end_local = end_ts.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
    text = (
        f"Старт. «{task}» на {minutes} мин.\n"
        f"Напишу в середине и в конце. Финиш примерно в {end_local.strftime('%H:%M')}."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button_label(user, "⏹ Прервать", "⏹ Прервать", "⏹ Прервать"),
                    callback_data=f"cafe:finish:fail:{session_id}",
                )
            ]
        ]
    )
    if isinstance(event, types.CallbackQuery):
        await event.message.answer(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("cafe:checkin:"))
async def cafe_checkin(callback: types.CallbackQuery, db) -> None:
    _, _, status, session_id = callback.data.split(":")
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    session = await repo.get_active_focus_session(db, user["id"])
    if not session or str(session["id"]) != session_id:
        await callback.answer("Сессия уже закрыта.")
        return
    await repo.mark_focus_checkin_response(db, session["id"], status)
    if status == "ok":
        await callback.answer(g(user, "Держу курс.", "Держу курс.", "Держу курс."))
    else:
        await callback.answer("Поняла. Вернёмся мягко, я напомню в конце.")


@router.callback_query(lambda c: c.data and c.data.startswith("cafe:finish:"))
async def cafe_finish(callback: types.CallbackQuery, db) -> None:
    _, _, result, session_id = callback.data.split(":")
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    session = await repo.get_active_focus_session(db, user["id"])
    if not session or str(session["id"]) != session_id:
        await callback.answer("Сессия уже закрыта.")
        return
    await repo.complete_focus_session(db, session["id"], result)
    now_utc = datetime.datetime.utcnow()
    local_date = local_date_str(now_utc, user["timezone"])

    points = 0
    if result == "done":
        points = 3
        await repo.update_focus_strikes(db, user["id"], -100)
        await repo.set_focus_cooldown(db, user["id"], None)
    elif result == "partial":
        points = 1
        await repo.update_focus_strikes(db, user["id"], -1)
    else:
        strikes = await repo.update_focus_strikes(db, user["id"], 1)
        if strikes >= 2:
            cooldown_until = (
                now_utc + datetime.timedelta(hours=6)
            ).isoformat()
            await repo.set_focus_cooldown(db, user["id"], cooldown_until)
            tzinfo = tzinfo_from_string(user["timezone"])
            until_local = (
                datetime.datetime.fromisoformat(cooldown_until)
                .replace(tzinfo=datetime.timezone.utc)
                .astimezone(tzinfo)
            )
            cooldown_note = f"\nПауза по фокусу до {until_local.strftime('%H:%M')}."
        else:
            cooldown_note = ""
    if result != "fail":
        cooldown_note = ""

    if points:
        await repo.add_points(db, user["id"], points, local_date=local_date)

    text = {
        "done": f"Записала. +{points} очк. Хорошая работа.",
        "partial": f"Отметила. +{points} очк. Всё равно сдвиг.",
        "fail": "Принято. Без давления — вернёмся, когда будет ресурс.",
    }.get(result, "Принято.")
    text += cooldown_note
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="☕ Ещё сессию", callback_data="cafe:restart")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="main:menu")],
        ]
    )
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data == "cafe:restart")
async def cafe_restart(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FocusCafeState.task)
    await callback.message.answer("Что берём в работу?")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data == "cafe:cancel")
async def cafe_cancel(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("Главное меню:", reply_markup=main_menu_keyboard())
    await callback.answer()
