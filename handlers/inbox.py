"""Входящие голосовые/текстовые сообщения, исправление разбора и личный дневник (/diary)."""

from __future__ import annotations

import datetime
import html
import json
from typing import Any, Optional

from aiogram import F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from services import inbox_service
from services.inbox_parser import resolve_temporal_expression
from utils.sender import safe_edit
from utils.time import format_date_display, local_date_str
from utils.user import ensure_user

router = Router()


class InboxEditState(StatesGroup):
    rename = State()
    clarify_when = State()


def _esc(val: Any) -> str:
    return html.escape(str(val or ""))


def _load_payload(action: dict[str, Any]) -> dict[str, Any]:
    raw = action.get("payload_json")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


async def handle_free_text_inbox(message: types.Message, db) -> None:
    """Единая точка входа для произвольного текста из natural_handler."""
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    outcome = await inbox_service.capture_and_process_text(
        db,
        user,
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=message.text or "",
    )
    await message.answer(
        outcome.reply_text,
        reply_markup=outcome.reply_markup or main_menu_keyboard(),
    )


@router.message(F.voice)
async def handle_voice_inbox(message: types.Message, state: FSMContext, db) -> None:
    """Принять голосовое сообщение, сохранить сырое, расшифровать и разобрать."""
    current = await state.get_state()
    if current and current.startswith("InboxEditState:"):
        await state.clear()
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    outcome = await inbox_service.capture_and_process_voice(
        db,
        message.bot,
        user,
        chat_id=message.chat.id,
        message_id=message.message_id,
        voice=message.voice,
    )
    await message.answer(
        outcome.reply_text,
        reply_markup=outcome.reply_markup or main_menu_keyboard(),
    )


def _build_edit_list_view(
    inbox_id: int, actions: list[dict[str, Any]]
) -> tuple[str, InlineKeyboardMarkup]:
    if not actions:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"inbox:summary:{inbox_id}")]
            ]
        )
        return "Активных пунктов по этому сообщению уже нет.", kb

    lines = ["<b>Что исправить?</b> Выбери пункт:"]
    rows: list[list[InlineKeyboardButton]] = []
    for act in actions:
        kind = act.get("item_kind") or "diary"
        icon, label = inbox_service.KIND_LABELS.get(kind, ("📝", "Запись"))
        title = str(act.get("title") or "")
        lines.append(f"• {icon} {label}: {_esc(title)}")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {title[:28]}",
                    callback_data=f"inbox:item:{act['id']}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(text="↩️ Отменить всё", callback_data=f"inbox:undo:{inbox_id}"),
            InlineKeyboardButton(text="⬅️ Готово", callback_data=f"inbox:summary:{inbox_id}"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _build_item_detail_view(action: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    kind = action.get("item_kind") or "diary"
    icon, label = inbox_service.KIND_LABELS.get(kind, ("📝", "Запись"))
    payload = _load_payload(action)
    model_note = str(payload.get("model_note") or "").strip()

    lines = [
        f"<b>{icon} {label}</b>: {_esc(action.get('title'))}",
        f"Твои слова: «{_esc(action.get('source_quote'))}»",
    ]
    if model_note:
        lines.append(f"Пометка модели (гипотеза): {_esc(model_note)}")

    aid = int(action["id"])
    inbox_id = int(action["inbox_message_id"])
    reclass_targets = [
        ("task", "✅ Задача"),
        ("shopping", "🛒 Покупка"),
        ("diary", "📓 Дневник"),
        ("idea", "💡 Идея"),
        ("done", "✔ Сделано"),
    ]
    reclass_btns = [
        InlineKeyboardButton(text=lbl, callback_data=f"inbox:reclass:{aid}:{target_k}")
        for target_k, lbl in reclass_targets
        if target_k != kind
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(reclass_btns), 2):
        rows.append(reclass_btns[i : i + 2])
    rows.append(
        [
            InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"inbox:retitle:{aid}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"inbox:delitem:{aid}"),
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data=f"inbox:edit:{inbox_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _refresh_summary_message(message: types.Message, db, user: dict[str, Any], inbox_id: int) -> None:
    row = await repo.get_inbox_message(db, user["id"], inbox_id)
    if not row:
        await safe_edit(message, "Запись не найдена.")
        return
    actions = await repo.list_inbox_actions(db, user["id"], inbox_id, include_undone=False)
    text, kb = inbox_service.render_inbox_summary(row, actions, llm_error=row.get("llm_error") or None)
    await safe_edit(message, text, reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("inbox:"))
async def inbox_callbacks(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)

    if action == "summary" and len(parts) > 2:
        inbox_id = int(parts[2])
        await _refresh_summary_message(callback.message, db, user, inbox_id)
        await callback.answer()
        return

    if action == "undo" and len(parts) > 2:
        inbox_id = int(parts[2])
        cnt = await inbox_service.undo_inbox_message(db, user, inbox_id)
        await safe_edit(callback.message, f"↩️ Отменила запись ({cnt} п.), исходное сообщение сохранено в архиве.")
        await callback.answer("Отменила")
        return

    if action == "edit" and len(parts) > 2:
        inbox_id = int(parts[2])
        actions = await repo.list_inbox_actions(db, user["id"], inbox_id, include_undone=False)
        text, kb = _build_edit_list_view(inbox_id, actions)
        await safe_edit(callback.message, text, reply_markup=kb)
        await callback.answer()
        return

    if action == "item" and len(parts) > 2:
        aid = int(parts[2])
        act = await repo.get_inbox_action(db, user["id"], aid)
        if not act or act.get("status") == "undone":
            await callback.answer("Пункт не найден.", show_alert=True)
            return
        text, kb = _build_item_detail_view(act)
        await safe_edit(callback.message, text, reply_markup=kb)
        await callback.answer()
        return

    if action == "reclass" and len(parts) > 3:
        aid = int(parts[2])
        new_kind = parts[3]
        updated = await inbox_service.reclassify_inbox_action(db, user, aid, new_kind)
        if not updated:
            await callback.answer("Не удалось изменить тип.", show_alert=True)
            return
        text, kb = _build_item_detail_view(updated)
        await safe_edit(callback.message, text, reply_markup=kb)
        await callback.answer("Перенесла")
        return

    if action == "delitem" and len(parts) > 2:
        aid = int(parts[2])
        deleted = await inbox_service.delete_inbox_action(db, user, aid)
        if not deleted:
            await callback.answer("Уже удалено.")
            return
        inbox_id = int(deleted["inbox_message_id"])
        actions = await repo.list_inbox_actions(db, user["id"], inbox_id, include_undone=False)
        text, kb = _build_edit_list_view(inbox_id, actions)
        await safe_edit(callback.message, text, reply_markup=kb)
        await callback.answer("Удалила")
        return

    if action == "retitle" and len(parts) > 2:
        aid = int(parts[2])
        act = await repo.get_inbox_action(db, user["id"], aid)
        if not act:
            await callback.answer("Пункт не найден.", show_alert=True)
            return
        await state.set_state(InboxEditState.rename)
        await state.update_data(edit_action_id=aid, edit_inbox_id=int(act["inbox_message_id"]))
        await callback.message.answer("Напиши новый короткий текст для этого пункта:")
        await callback.answer()
        return

    if action == "clarify_time" and len(parts) > 3:
        aid = int(parts[2])
        hhmm = parts[3].replace("_", ":")
        resolved = await inbox_service.resolve_clarification_action(
            db, user, aid, resolved_time=hhmm
        )
        if not resolved:
            await callback.answer("Уточнение не найдено.", show_alert=True)
            return
        await _refresh_summary_message(callback.message, db, user, int(resolved["inbox_message_id"]))
        await callback.answer(f"Поставила на {hhmm}")
        return

    if action == "clarify_preset" and len(parts) > 3:
        aid = int(parts[2])
        preset = parts[3]
        now_utc = datetime.datetime.utcnow()
        today = local_date_str(now_utc, user.get("timezone") or "UTC")
        if preset == "today_19":
            t_date, t_time = today, "19:00"
        else:
            t_date = (datetime.date.fromisoformat(today) + datetime.timedelta(days=1)).isoformat()
            t_time = "12:00"
        resolved = await inbox_service.resolve_clarification_action(
            db, user, aid, resolved_date=t_date, resolved_time=t_time
        )
        if not resolved:
            await callback.answer("Уточнение не найдено.", show_alert=True)
            return
        await _refresh_summary_message(callback.message, db, user, int(resolved["inbox_message_id"]))
        await callback.answer(f"Поставила на {format_date_display(t_date)} {t_time}")
        return

    if action == "clarify_custom" and len(parts) > 2:
        aid = int(parts[2])
        act = await repo.get_inbox_action(db, user["id"], aid)
        if not act:
            await callback.answer("Уточнение не найдено.", show_alert=True)
            return
        await state.set_state(InboxEditState.clarify_when)
        await state.update_data(clarify_action_id=aid, clarify_inbox_id=int(act["inbox_message_id"]))
        await callback.message.answer(
            "На какую дату и время поставить? Например: «завтра в 14:00» или «20:30»."
        )
        await callback.answer()
        return

    if action == "clarify_task" and len(parts) > 2:
        aid = int(parts[2])
        resolved = await inbox_service.resolve_clarification_action(db, user, aid, as_task=True)
        if not resolved:
            await callback.answer("Уточнение не найдено.", show_alert=True)
            return
        await _refresh_summary_message(callback.message, db, user, int(resolved["inbox_message_id"]))
        await callback.answer("Добавила как задачу")
        return

    if action == "clarify_drop" and len(parts) > 2:
        aid = int(parts[2])
        resolved = await inbox_service.resolve_clarification_action(db, user, aid, drop=True)
        if not resolved:
            await callback.answer("Уточнение не найдено.", show_alert=True)
            return
        await _refresh_summary_message(callback.message, db, user, int(resolved["inbox_message_id"]))
        await callback.answer("Убрала")
        return

    if action == "retry_stt" and len(parts) > 2:
        inbox_id = int(parts[2])
        outcome = await inbox_service.retry_inbox_stt(db, callback.message.bot, user, inbox_id)
        await safe_edit(callback.message, outcome.reply_text, reply_markup=outcome.reply_markup)
        await callback.answer()
        return

    if action == "retry_llm" and len(parts) > 2:
        inbox_id = int(parts[2])
        outcome = await inbox_service.retry_inbox_parse(db, user, inbox_id)
        await safe_edit(callback.message, outcome.reply_text, reply_markup=outcome.reply_markup)
        await callback.answer()
        return

    await callback.answer()


@router.message(InboxEditState.rename)
async def inbox_rename_input(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    aid = data.get("edit_action_id")
    new_title = (message.text or "").strip()
    if not new_title:
        await message.answer("Напиши непустой текст.")
        return
    await state.clear()
    if not aid:
        await message.answer("Не нашла пункт для правки.", reply_markup=main_menu_keyboard())
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    updated = await inbox_service.rename_inbox_action(db, user, int(aid), new_title)
    if not updated:
        await message.answer("Пункт уже удалён или не найден.", reply_markup=main_menu_keyboard())
        return
    text, kb = _build_item_detail_view(updated)
    await message.answer(text, reply_markup=kb)


@router.message(InboxEditState.clarify_when)
async def inbox_clarify_when_input(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    aid = data.get("clarify_action_id")
    if not aid:
        await state.clear()
        await message.answer("Черновик не найден.", reply_markup=main_menu_keyboard())
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    act = await repo.get_inbox_action(db, user["id"], int(aid))
    if not act:
        await state.clear()
        await message.answer("Уточнение не найдено.", reply_markup=main_menu_keyboard())
        return

    payload = _load_payload(act)
    date_hint = payload.get("resolved_date") or ""
    now_utc = datetime.datetime.utcnow()
    sched = resolve_temporal_expression(
        date_hint=date_hint,
        time_hint="",
        raw_fragment=message.text or "",
        now_utc=now_utc,
        tz_str=user.get("timezone") or "UTC",
    )
    if sched.is_ambiguous or not sched.resolved_date or not sched.resolved_time:
        await message.answer(
            sched.question or "Не поняла дату и время. Напиши, например: «завтра в 14:00»."
        )
        return

    await state.clear()
    resolved = await inbox_service.resolve_clarification_action(
        db,
        user,
        int(aid),
        resolved_date=sched.resolved_date,
        resolved_time=sched.resolved_time,
    )
    if not resolved:
        await message.answer("Не удалось сохранить напоминание.", reply_markup=main_menu_keyboard())
        return
    inbox_id = int(resolved["inbox_message_id"])
    row = await repo.get_inbox_message(db, user["id"], inbox_id)
    actions = await repo.list_inbox_actions(db, user["id"], inbox_id, include_undone=False)
    if row:
        text, kb = inbox_service.render_inbox_summary(row, actions, llm_error=row.get("llm_error") or None)
        await message.answer(text, reply_markup=kb or main_menu_keyboard())
    else:
        await message.answer(
            f"🔔 Поставила напоминание на {format_date_display(sched.resolved_date)} {sched.resolved_time}.",
            reply_markup=main_menu_keyboard(),
        )


# --- Личный дневник (/diary), поиск, экспорт и удаление ---

_DIARY_TABS = [
    ("diary", "📓 Дневник"),
    ("idea", "💡 Идеи"),
    ("done", "✔ Сделано"),
    ("raw", "📥 Сырые"),
]


async def _render_diary_view(
    db, user: dict[str, Any], *, tab: str = "diary", query: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = []
    tab_row = [
        InlineKeyboardButton(
            text=(f"[{lbl}]" if key == tab and not query else lbl),
            callback_data=f"diary:tab:{key}",
        )
        for key, lbl in _DIARY_TABS
    ]
    rows.append(tab_row)

    if query:
        entries = await repo.search_journal_entries(db, user["id"], query, limit=10)
        lines = [f"<b>🔍 Поиск в дневнике:</b> «{_esc(query)}»"]
        if not entries:
            lines.append("Ничего не нашлось.")
        else:
            for e in entries:
                src_icon = "🎙" if e.get("source") == "voice" else "💬"
                lines.append(
                    f"• {format_date_display(e['entry_date'])} {src_icon} <b>{_esc(e['title'])}</b>\n"
                    f"  «{_esc(e['user_text'])}»"
                )
    elif tab == "raw":
        msgs = await repo.list_inbox_messages(db, user["id"], limit=10)
        lines = ["<b>📥 Сырые входящие (последние):</b>"]
        if not msgs:
            lines.append("Пока пусто.")
        else:
            for m in msgs:
                src_icon = "🎙" if m.get("source") == "voice" else "💬"
                txt = (m.get("raw_text") or "").strip() or "(аудио без расшифровки)"
                if len(txt) > 90:
                    txt = txt[:87] + "..."
                lines.append(
                    f"• {format_date_display(m['local_date'])} {src_icon} [{_esc(m['status'])}] {_esc(txt)}"
                )
    else:
        entry_type = tab if tab in {"diary", "idea", "done"} else "diary"
        title_map = {"diary": "📓 Дневник", "idea": "💡 Идеи", "done": "✔ Сделано"}
        entries = await repo.list_journal_entries(db, user["id"], entry_type=entry_type, limit=10)
        lines = [f"<b>{title_map[entry_type]} (последние):</b>"]
        if not entries:
            lines.append("Пока пусто. Напиши или надиктуй мысль в чат — я сохраню.")
        else:
            del_row: list[InlineKeyboardButton] = []
            for e in entries:
                src_icon = "🎙" if e.get("source") == "voice" else "💬"
                note = (e.get("model_note") or "").strip()
                line = f"• {format_date_display(e['entry_date'])} {src_icon} {_esc(e['user_text'])}"
                if note:
                    line += f"\n  <i>Заметка модели: {_esc(note)}</i>"
                lines.append(line)
                if len(del_row) < 4:
                    del_row.append(
                        InlineKeyboardButton(
                            text=f"🗑 #{e['id']}",
                            callback_data=f"diary:del:{e['id']}:{tab}",
                        )
                    )
            if del_row:
                rows.append(del_row)

    rows.append(
        [
            InlineKeyboardButton(text="📤 Экспорт JSON", callback_data="diary:export"),
            InlineKeyboardButton(text="🗑 Очистить дневник", callback_data="diary:wipe_confirm"),
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="main:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("diary"))
async def cmd_diary(message: types.Message, state: FSMContext, db) -> None:
    await state.clear()
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    text, kb = await _render_diary_view(db, user, tab="diary", query=query)
    await message.answer(text, reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("diary:"))
async def diary_callbacks(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await state.clear()
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)

    if action == "tab":
        tab = parts[2] if len(parts) > 2 else "diary"
        text, kb = await _render_diary_view(db, user, tab=tab)
        await safe_edit(callback.message, text, reply_markup=kb)
        await callback.answer()
        return

    if action == "del" and len(parts) > 2:
        eid = int(parts[2])
        tab = parts[3] if len(parts) > 3 else "diary"
        await repo.delete_journal_entry(db, user["id"], eid, hard=False)
        text, kb = await _render_diary_view(db, user, tab=tab)
        await safe_edit(callback.message, text, reply_markup=kb)
        await callback.answer("Удалила запись")
        return

    if action == "export":
        payload = await repo.export_user_personal_data(db, user["id"])
        raw_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        local_date = local_date_str(datetime.datetime.utcnow(), user.get("timezone") or "UTC")
        doc = BufferedInputFile(raw_bytes, filename=f"hidl_diary_{local_date}.json")
        await callback.message.answer_document(doc, caption="Экспорт твоих личных записей (JSON).")
        await callback.answer("Готово")
        return

    if action == "wipe_confirm":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, удалить всё", callback_data="diary:wipe_yes")],
                [InlineKeyboardButton(text="⬅️ Отмена", callback_data="diary:tab:diary")],
            ]
        )
        await safe_edit(
            callback.message,
            "Удалить все личные записи дневника, идей и сырые входящие сообщения? Действие необратимо.",
            reply_markup=kb,
        )
        await callback.answer()
        return

    if action == "wipe_yes":
        counts = await repo.delete_user_personal_data(db, user["id"])
        await safe_edit(
            callback.message,
            f"Удалила: записей дневника — {counts['journal_entries']}, "
            f"входящих сообщений — {counts['inbox_messages']}.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Меню", callback_data="main:menu")]]
            ),
        )
        await callback.answer("Очистила")
        return

    await callback.answer()
