from __future__ import annotations

import datetime
import html
from typing import Any, Dict, List

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from services import household_service
from utils.profile import is_student_mode
from utils.sender import safe_edit
from utils.time import local_date_str
from utils.user import ensure_user

router = Router()


class HouseholdState(StatesGroup):
    waiting_request_text = State()


REQ_PRESETS: Dict[str, str] = {
    "buy": "Захвати, пожалуйста, по дороге продукты из списка покупок 🛒",
    "dishes": "Разбери/помой, пожалуйста, посуду, когда будет минутка 🍽",
    "trash": "Вынеси, пожалуйста, мусор 🗑",
    "cook": "Давай сегодня вместе решим и приготовим ужин 🍳",
}

_REQ_STATUS_LABELS: Dict[str, str] = {
    "open": "⏳ ждёт ответа",
    "accepted": "👍 принято",
    "snoozed": "⏰ через 30 мин",
    "done": "✅ готово",
}


async def _reject_if_student(target: types.Message | types.CallbackQuery, user: Dict[str, Any]) -> bool:
    if not is_student_mode(user):
        return False
    if isinstance(target, types.CallbackQuery):
        await target.answer("В режиме ученика общий дом отключён.", show_alert=True)
    else:
        await target.answer("В режиме ученика общий дом отключён.", reply_markup=main_menu_keyboard(user))
    return True


def _delivery_note(res: Dict[str, Any]) -> str:
    if int(res.get("partners_total") or 0) == 0:
        return "В доме пока только ты — пригласи партнёра в «⚙️ Настройки и инвайт»."
    if not res.get("notify_enabled", True):
        return "Уведомления дома сейчас выключены в настройках — запись сохранена в «Просьбы дома»."
    skipped = list(res.get("skipped_quiet") or [])
    if int(res.get("sent") or 0) > 0 and not skipped:
        return "Доставлено партнёру 💌"
    if skipped and int(res.get("sent") or 0) == 0:
        names = ", ".join(skipped)
        return f"Сохранено в доме, но пинг не отправляла ({names}: тихий режим или сон 😴)."
    if skipped:
        return f"Частично доставлено (тихий режим/сон у: {', '.join(skipped)})."
    return "Сохранено в доме."


def _request_action_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 Принято", callback_data=f"hh:req:act:{req_id}:accepted"),
                InlineKeyboardButton(text="⏰ Через 30 мин", callback_data=f"hh:req:act:{req_id}:snoozed"),
            ],
            [
                InlineKeyboardButton(text="✅ Готово", callback_data=f"hh:req:act:{req_id}:done"),
                InlineKeyboardButton(text="📋 Все просьбы", callback_data="hh:req:list"),
            ],
        ]
    )


async def _render_hub(db, user: Dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    hid = await repo.get_or_create_household(db, int(user["id"]))
    members = await repo.list_household_members(db, hid)
    open_reqs = await repo.list_household_requests(
        db, hid, kind="request", statuses=("open", "accepted", "snoozed")
    )
    member_names = ", ".join(html.escape(str(m.get("name") or f"Участник {m['id']}")) for m in members)
    lines = [
        "🏠 <b>Наш дом</b>",
        f"👥 Участники ({len(members)}): {member_names or 'только ты'}",
        f"📋 Активных просьб: <b>{len(open_reqs)}</b>",
        "",
        "Общие кухня, холодильник, дела, расходники и тёплая поддержка без лишнего шума:",
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🍳 Что сегодня готовим?", callback_data="hh:vote:start")],
            [
                InlineKeyboardButton(text="💌 Передать просьбу", callback_data="hh:req:new"),
                InlineKeyboardButton(text=f"📋 Просьбы ({len(open_reqs)})", callback_data="hh:req:list"),
            ],
            [InlineKeyboardButton(text="🤗 Забота и спасибо", callback_data="hh:care:menu")],
            [
                InlineKeyboardButton(text="🛒 Общие покупки", callback_data="kitchen:shoplist:household"),
                InlineKeyboardButton(text="❄️ Холодильник", callback_data="kitchen:fridge"),
            ],
            [
                InlineKeyboardButton(text="📅 Общие дела", callback_data="home:week"),
                InlineKeyboardButton(text="🧴 Расходники", callback_data="home:supplies"),
            ],
            [InlineKeyboardButton(text="📊 Итог недели дома", callback_data="hh:weekly")],
            [
                InlineKeyboardButton(text="⚙️ Настройки и инвайт", callback_data="settings:household"),
                InlineKeyboardButton(text="⬅️ Меню", callback_data="main:menu"),
            ],
        ]
    )
    return "\n".join(lines), kb


@router.message(Command("house"))
async def house_command(message: types.Message, state: FSMContext, db) -> None:
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    if await _reject_if_student(message, user):
        return
    await state.clear()
    text, kb = await _render_hub(db, user)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "hh:hub")
async def house_hub_cb(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if await _reject_if_student(callback, user):
        return
    await state.clear()
    text, kb = await _render_hub(db, user)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# --- Dinner voting ---


@router.callback_query(lambda c: c.data == "hh:vote:start")
async def hh_vote_start_cb(callback: types.CallbackQuery, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if await _reject_if_student(callback, user):
        return
    now = datetime.datetime.utcnow()
    local_date = local_date_str(now, str(user.get("timezone") or "UTC"))
    hid = await repo.get_or_create_household(db, int(user["id"]))
    poll = await repo.get_latest_open_dinner_poll(db, hid, local_date)
    if not poll:
        options = await household_service.pick_dinner_poll_options(db, user, limit=3)
        poll = await repo.create_dinner_poll(db, hid, int(user["id"]), local_date, options)
    members = await repo.list_household_members(db, hid)
    text, kb = household_service.format_dinner_poll_card(poll, members)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("hh:vote:reroll:"))
async def hh_vote_reroll_cb(callback: types.CallbackQuery, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if await _reject_if_student(callback, user):
        return
    parts = (callback.data or "").split(":")
    old_pid = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    old_poll = await repo.get_dinner_poll(db, old_pid) if old_pid else None
    excl = [str(o.get("id") or "") for o in (old_poll or {}).get("options", [])]
    now = datetime.datetime.utcnow()
    local_date = local_date_str(now, str(user.get("timezone") or "UTC"))
    hid = await repo.get_or_create_household(db, int(user["id"]))
    options = await household_service.pick_dinner_poll_options(db, user, exclude_ids=excl, limit=3)
    poll = await repo.create_dinner_poll(db, hid, int(user["id"]), local_date, options)
    members = await repo.list_household_members(db, hid)
    text, kb = household_service.format_dinner_poll_card(poll, members)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Подобрала 3 других варианта 🎲")


async def _sync_decided_dinner_to_meal_plan(
    db,
    household_id: int,
    local_date: str,
    option: Dict[str, Any],
    *,
    acting_user_id: int,
) -> None:
    """Сохранить выбранное блюдо голосования в план питания дома (слот ужина на сегодня)."""
    from services import kitchen_ai

    rid = str(option.get("id") or "").strip()
    rec = await kitchen_ai.get_recipe_for_user(db, acting_user_id, rid) if rid else None
    title = str((rec or {}).get("title") or option.get("title") or "Ужин")
    t_min = int((rec or {}).get("time_minutes") or option.get("time_min") or 20)
    ings = list((rec or {}).get("ingredients") or [])
    members = await repo.list_household_members(db, household_id)
    target_uids = [int(m["id"]) for m in members] if members else [acting_user_id]
    for uid in target_uids:
        await repo.upsert_meal_plan_slot(
            db,
            uid,
            local_date,
            "dinner",
            title=title,
            recipe_id=rid,
            time_minutes=t_min,
            ingredients=ings,
            status="planned",
        )


@router.callback_query(lambda c: c.data and c.data.startswith("hh:vote:pick:"))
async def hh_vote_pick_cb(callback: types.CallbackQuery, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if await _reject_if_student(callback, user):
        return
    parts = (callback.data or "").split(":")
    try:
        pid = int(parts[3])
        idx = int(parts[4])
    except (IndexError, ValueError):
        await callback.answer()
        return
    hid = await repo.get_or_create_household(db, int(user["id"]))
    poll = await repo.decide_dinner_poll(db, pid, idx)
    if not poll:
        await callback.answer("Голосование не найдено.", show_alert=True)
        return
    members = await repo.list_household_members(db, hid)
    text, kb = household_service.format_dinner_poll_card(poll, members)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    opts = poll.get("options") or []
    if 0 <= idx < len(opts):
        local_date = str(poll.get("local_date") or local_date_str(datetime.datetime.utcnow(), str(user.get("timezone") or "UTC")))
        await _sync_decided_dinner_to_meal_plan(db, hid, local_date, opts[idx], acting_user_id=int(user["id"]))
        title = str(opts[idx].get("title") or "")
        actor = str(user.get("name") or "Партнёр")
        p_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📖 Открыть рецепт",
                        callback_data=f"kitchen:cook_view:{opts[idx]['id']}:2:all:0",
                    )
                ]
            ]
        )
        await household_service.notify_household_partners(
            getattr(callback, "bot", None),
            db,
            user,
            f"🎉 <b>{html.escape(actor)}</b> зафиксировал(а) блюдо на сегодня: <b>{html.escape(title)}</b>!",
            reply_markup=p_kb,
        )
    await callback.answer("Выбрано и добавлено в меню! 🎉")


@router.callback_query(lambda c: c.data and c.data.startswith("hh:vote:"))
async def hh_vote_cast_cb(callback: types.CallbackQuery, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if await _reject_if_student(callback, user):
        return
    parts = (callback.data or "").split(":")
    try:
        pid = int(parts[2])
        idx = int(parts[3])
    except (IndexError, ValueError):
        await callback.answer()
        return
    hid = await repo.get_or_create_household(db, int(user["id"]))
    poll = await repo.vote_dinner_poll(db, pid, int(user["id"]), idx)
    if not poll:
        await callback.answer("Голосование не найдено.", show_alert=True)
        return
    members = await repo.list_household_members(db, hid)
    votes = poll.get("votes") or {}
    unanimous = (
        len(members) >= 2
        and all(str(int(m["id"])) in votes and int(votes[str(int(m["id"]))]) == idx for m in members)
    )
    if unanimous and poll.get("status") != "decided":
        poll = await repo.decide_dinner_poll(db, pid, idx) or poll

    text, kb = household_service.format_dinner_poll_card(poll, members)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")

    opts = poll.get("options") or []
    chosen_opt = opts[idx] if 0 <= idx < len(opts) else None
    actor = str(user.get("name") or "Партнёр")
    if len(members) >= 2 and chosen_opt:
        if unanimous:
            local_date = str(poll.get("local_date") or local_date_str(datetime.datetime.utcnow(), str(user.get("timezone") or "UTC")))
            await _sync_decided_dinner_to_meal_plan(db, hid, local_date, chosen_opt, acting_user_id=int(user["id"]))
            p_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📖 Открыть рецепт",
                            callback_data=f"kitchen:cook_view:{chosen_opt['id']}:2:all:0",
                        )
                    ]
                ]
            )
            await household_service.notify_household_partners(
                getattr(callback, "bot", None),
                db,
                user,
                f"🥳 <b>Совпадение!</b> Вы оба выбрали на сегодня: <b>{html.escape(str(chosen_opt['title']))}</b>",
                reply_markup=p_kb,
            )
            await callback.answer("Совпадение! Готовим это блюдо 🎉")
            return
        await household_service.notify_household_partners(
            getattr(callback, "bot", None),
            db,
            user,
            f"🍳 <b>{html.escape(actor)}</b> голосует за: <b>{html.escape(str(chosen_opt['title']))}</b>\nВыбирай в один тап:",
            reply_markup=kb,
        )
    await callback.answer("Голос учтён 👍")


# --- Partner requests ---


def _build_request_prompt_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Захвати продукты", callback_data="hh:req:preset:buy")],
            [InlineKeyboardButton(text="🍽 Помыть / убрать посуду", callback_data="hh:req:preset:dishes")],
            [InlineKeyboardButton(text="🗑 Вынести мусор", callback_data="hh:req:preset:trash")],
            [InlineKeyboardButton(text="🍳 Приготовим ужин вместе", callback_data="hh:req:preset:cook")],
            [InlineKeyboardButton(text="⬅️ Наш дом", callback_data="hh:hub")],
        ]
    )


async def _send_household_request(
    bot, db, actor_user: Dict[str, Any], text: str
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    hid = await repo.get_or_create_household(db, int(actor_user["id"]))
    actor_name = str(actor_user.get("name") or "Партнёр")
    req_row = await repo.create_household_request(
        db,
        hid,
        int(actor_user["id"]),
        actor_name,
        text,
        kind="request",
        status="open",
    )
    notify = await household_service.notify_household_partners_detailed(
        bot,
        db,
        actor_user,
        f"💌 <b>Просьба по дому от {html.escape(actor_name)}:</b>\n{html.escape(text)}",
        reply_markup=_request_action_kb(int(req_row["id"])),
    )
    return req_row, notify


@router.callback_query(lambda c: c.data == "hh:req:new")
async def hh_req_new_cb(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if await _reject_if_student(callback, user):
        return
    await state.set_state(HouseholdState.waiting_request_text)
    await safe_edit(
        callback.message,
        "💌 <b>Передать просьбу партнёру</b>\n\n"
        "Нажми быструю кнопку ниже или просто напиши текст сообщением (например: <code>Забери посылку на WB</code>).\n"
        "<i>Если у партнёра тихий режим или сон — не разбужу.</i>",
        reply_markup=_build_request_prompt_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("hh:req:preset:"))
async def hh_req_preset_cb(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if await _reject_if_student(callback, user):
        return
    key = (callback.data or "").split(":")[3]
    text = REQ_PRESETS.get(key)
    if not text:
        await callback.answer()
        return
    await state.clear()
    req_row, notify = await _send_household_request(getattr(callback, "bot", None), db, user, text)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Просьбы дома", callback_data="hh:req:list")],
            [InlineKeyboardButton(text="⬅️ Наш дом", callback_data="hh:hub")],
        ]
    )
    await safe_edit(
        callback.message,
        f"💌 Просьба #{req_row['id']} отправлена:\n<i>{html.escape(text)}</i>\n\n{_delivery_note(notify)}",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer("Просьба создана 💌")


@router.message(HouseholdState.waiting_request_text, lambda m: bool(m.text and not m.text.startswith("/")))
async def hh_req_text_input(message: types.Message, state: FSMContext, db) -> None:
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    if await _reject_if_student(message, user):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if raw.lower() in {"отмена", "назад"}:
        await state.clear()
        text, kb = await _render_hub(db, user)
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
        return
    await state.clear()
    req_row, notify = await _send_household_request(getattr(message, "bot", None), db, user, raw)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Просьбы дома", callback_data="hh:req:list")],
            [InlineKeyboardButton(text="⬅️ Наш дом", callback_data="hh:hub")],
        ]
    )
    await message.answer(
        f"💌 Просьба #{req_row['id']}:\n<i>{html.escape(raw)}</i>\n\n{_delivery_note(notify)}",
        reply_markup=kb,
        parse_mode="HTML",
    )


async def _render_requests_list(db, user: Dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    hid = await repo.get_or_create_household(db, int(user["id"]))
    reqs = await repo.list_household_requests(
        db, hid, kind="request", statuses=("open", "accepted", "snoozed"), limit=8
    )
    rows: List[List[InlineKeyboardButton]] = []
    if not reqs:
        lines = ["📋 <b>Просьбы дома</b>", "", "Сейчас открытых просьб нет — можно выдохнуть 🌿"]
    else:
        lines = ["📋 <b>Активные просьбы дома</b>", ""]
        for r in reqs:
            rid = int(r["id"])
            st = _REQ_STATUS_LABELS.get(str(r.get("status") or "open"), str(r.get("status") or "open"))
            sender = html.escape(str(r.get("from_name") or "Кто-то"))
            body = html.escape(str(r.get("text") or ""))
            lines.append(f"• <b>#{rid}</b> от {sender} ({st}):\n  {body}")
            rows.append(
                [
                    InlineKeyboardButton(text=f"👍 #{rid}", callback_data=f"hh:req:act:{rid}:accepted"),
                    InlineKeyboardButton(text=f"⏰ #{rid}", callback_data=f"hh:req:act:{rid}:snoozed"),
                    InlineKeyboardButton(text=f"✅ #{rid}", callback_data=f"hh:req:act:{rid}:done"),
                ]
            )
    rows.append([InlineKeyboardButton(text="💌 Новая просьба", callback_data="hh:req:new")])
    rows.append([InlineKeyboardButton(text="⬅️ Наш дом", callback_data="hh:hub")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(lambda c: c.data == "hh:req:list")
async def hh_req_list_cb(callback: types.CallbackQuery, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if await _reject_if_student(callback, user):
        return
    text, kb = await _render_requests_list(db, user)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("hh:req:act:"))
async def hh_req_action_cb(callback: types.CallbackQuery, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if await _reject_if_student(callback, user):
        return
    parts = (callback.data or "").split(":")
    try:
        req_id = int(parts[3])
        action = parts[4]
    except (IndexError, ValueError):
        await callback.answer()
        return
    if action not in {"accepted", "snoozed", "done"}:
        await callback.answer()
        return
    hid = await repo.get_or_create_household(db, int(user["id"]))
    updated = await repo.update_household_request_status(db, hid, req_id, action)
    if not updated:
        await callback.answer("Просьба не найдена.", show_alert=True)
        return
    actor = str(user.get("name") or "Партнёр")
    if action == "done":
        today = local_date_str(datetime.datetime.utcnow(), str(user.get("timezone") or "UTC"))
        await repo.add_points(db, int(user["id"]), 3, local_date=today)

    status_human = _REQ_STATUS_LABELS.get(action, action)
    await household_service.notify_household_partners(
        getattr(callback, "bot", None),
        db,
        user,
        f"🏠 <b>{html.escape(actor)}</b> обновил(а) просьбу #{req_id} ({status_human}):\n<i>{html.escape(str(updated.get('text') or ''))}</i>",
    )
    text, kb = await _render_requests_list(db, user)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Готово! (+3 очка) ✅" if action == "done" else f"Статус: {status_human}")


# --- Warm care nudges & thanks ---


@router.callback_query(lambda c: c.data == "hh:care:menu")
async def hh_care_menu_cb(callback: types.CallbackQuery, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if await _reject_if_student(callback, user):
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💧 Попить воды", callback_data="hh:care:send:water"),
                InlineKeyboardButton(text="☕ Выдохнуть 5 мин", callback_data="hh:care:send:break"),
            ],
            [
                InlineKeyboardButton(text="🫂 Обнимашка", callback_data="hh:care:send:hug"),
                InlineKeyboardButton(text="💛 Спасибо (+5 кармы)", callback_data="hh:care:send:thanks"),
            ],
            [InlineKeyboardButton(text="⬅️ Наш дом", callback_data="hh:hub")],
        ]
    )
    await safe_edit(
        callback.message,
        "🤗 <b>Забота и спасибо</b>\n\n"
        "Тёплый пинг партнёру без навязчивости (анти-спам пауза 2 часа; учитывает сон и тихий режим).",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("hh:care:send:"))
async def hh_care_send_cb(callback: types.CallbackQuery, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if await _reject_if_student(callback, user):
        return
    key = (callback.data or "").split(":")[3]
    res = await household_service.send_care_nudge(getattr(callback, "bot", None), db, user, key)
    if res.get("cooldown"):
        await callback.answer("Недавно уже отправляла — пауза 2 часа ⏳", show_alert=True)
        return
    if not res.get("ok"):
        await callback.answer()
        return
    karma_note = " Партнёру начислено +5 кармы 💛" if int(res.get("karma_given") or 0) > 0 else ""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Наш дом", callback_data="hh:hub")]]
    )
    await safe_edit(
        callback.message,
        f"🤗 <b>{html.escape(str(res['label']))}</b>\n\n{_delivery_note(res)}{karma_note}",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer("Отправлено 💛")


# --- Weekly summary ---


@router.callback_query(lambda c: c.data == "hh:weekly")
async def hh_weekly_cb(callback: types.CallbackQuery, db) -> None:
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if await _reject_if_student(callback, user):
        return
    text = await household_service.build_household_weekly_summary(db, user)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🍳 Что сегодня готовим?", callback_data="hh:vote:start")],
            [InlineKeyboardButton(text="⬅️ Наш дом", callback_data="hh:hub")],
        ]
    )
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()
