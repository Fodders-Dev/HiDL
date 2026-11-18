"""Сервис обработки входящих голосовых и свободных текстовых сообщений."""

from __future__ import annotations

import datetime
import html
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from llm_client import LLMClient
from services import stt
from services.inbox_parser import (
    InboxParseResult,
    ParsedInboxItem,
    analyze_inbox_text,
    compute_one_time_last_sent,
)
from utils.pantry import format_quantity
from utils.time import format_date_display, local_date_str

KIND_LABELS = {
    "task": ("✅", "Задача"),
    "reminder": ("🔔", "Напоминание"),
    "shopping": ("🛒", "Покупки"),
    "diary": ("📓", "Дневник"),
    "idea": ("💡", "Идея"),
    "done": ("✔", "Сделано"),
    "pending_clarification": ("❓", "Уточнить"),
}


@dataclass
class InboxProcessOutcome:
    inbox_id: int
    status: str
    is_duplicate: bool
    source: str
    raw_text: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)
    clarifications: list[dict[str, Any]] = field(default_factory=list)
    llm_error: Optional[str] = None
    stt_error: Optional[str] = None
    used_llm: bool = False
    reply_text: str = ""
    reply_markup: Optional[InlineKeyboardMarkup] = None


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


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


def _format_action_line(action: dict[str, Any]) -> str:
    kind = action.get("item_kind") or "diary"
    icon, label = KIND_LABELS.get(kind, ("📝", "Запись"))
    title = _esc(action.get("title") or "")
    payload = _load_payload(action)

    if kind == "reminder":
        d = payload.get("resolved_date")
        t = payload.get("resolved_time")
        when = f"{format_date_display(d)} {t}".strip() if d else (t or "")
        return f"{icon} {label}: {title}" + (f" — {when}" if when else "")
    if kind == "shopping":
        qty = payload.get("quantity", 1)
        unit = payload.get("unit", "шт")
        return f"{icon} {label}: {title} ({_esc(format_quantity(qty, unit))})"
    return f"{icon} {label}: {title}"


def build_inbox_keyboard(
    inbox_id: int,
    actions: list[dict[str, Any]],
    *,
    llm_error: Optional[str] = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    clarifying = [a for a in actions if a.get("status") == "clarifying"]
    if clarifying:
        first = clarifying[0]
        aid = first["id"]
        payload = _load_payload(first)
        candidates = payload.get("candidate_times") or []
        resolved_date = payload.get("resolved_date")
        if candidates:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🕒 {t}",
                        callback_data=f"inbox:clarify_time:{aid}:{t.replace(':', '_')}",
                    )
                    for t in candidates[:3]
                ]
            )
        elif not resolved_date:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Сегодня 19:00",
                        callback_data=f"inbox:clarify_preset:{aid}:today_19",
                    ),
                    InlineKeyboardButton(
                        text="Завтра 12:00",
                        callback_data=f"inbox:clarify_preset:{aid}:tomorrow_12",
                    ),
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(text="🕒 Своё время", callback_data=f"inbox:clarify_custom:{aid}"),
                InlineKeyboardButton(text="✅ Как задачу", callback_data=f"inbox:clarify_task:{aid}"),
                InlineKeyboardButton(text="✖️ Убрать", callback_data=f"inbox:clarify_drop:{aid}"),
            ]
        )

    active_or_clar = [a for a in actions if a.get("status") in {"active", "clarifying"}]
    ctrl_row: list[InlineKeyboardButton] = []
    if active_or_clar:
        ctrl_row.append(InlineKeyboardButton(text="✏️ Исправить", callback_data=f"inbox:edit:{inbox_id}"))
        ctrl_row.append(InlineKeyboardButton(text="↩️ Отменить всё", callback_data=f"inbox:undo:{inbox_id}"))
    if llm_error:
        ctrl_row.append(InlineKeyboardButton(text="🔁 Переразобрать", callback_data=f"inbox:retry_llm:{inbox_id}"))
    if ctrl_row:
        rows.append(ctrl_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_inbox_summary(
    inbox_row: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    llm_error: Optional[str] = None,
) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    lines: list[str] = []
    if inbox_row.get("source") == "voice" and inbox_row.get("raw_text"):
        quote = inbox_row["raw_text"].strip()
        if len(quote) > 160:
            quote = quote[:157] + "..."
        lines.append(f"🎙 Расшифровала: «{_esc(quote)}»")

    active = [a for a in actions if a.get("status") == "active"]
    clarifying = [a for a in actions if a.get("status") == "clarifying"]

    if active:
        lines.append("Записала:")
        for act in active:
            lines.append(_format_action_line(act))
    elif not clarifying:
        lines.append("Запись сохранена, активных пунктов сейчас нет.")

    if llm_error:
        lines.append("⚠️ Модель не ответила — разобрала по простым правилам, исходный текст сохранила.")

    if clarifying:
        first_payload = _load_payload(clarifying[0])
        title = _esc(first_payload.get("title") or clarifying[0].get("title") or "напоминание")
        question = _esc(first_payload.get("question") or "Уточни дату и время.")
        lines.append(f"❓ Напоминание «{title}»: {question}")

    kb = build_inbox_keyboard(inbox_row["id"], actions, llm_error=llm_error)
    return "\n".join(lines), (kb if kb.inline_keyboard else None)


async def _persist_parsed_items(
    db,
    user: dict[str, Any],
    inbox_row: dict[str, Any],
    parsed: InboxParseResult,
    local_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    clarifications: list[dict[str, Any]] = []
    source = inbox_row.get("source") or "text"
    inbox_id = int(inbox_row["id"])

    for item in parsed.items:
        kind = item.kind
        if kind == "task":
            item_id = await repo.add_day_plan_item(
                db,
                user["id"],
                local_date,
                item.title,
                category="inbox",
                is_important=False,
            )
            action_id = await repo.create_inbox_action(
                db,
                user_id=user["id"],
                inbox_message_id=inbox_id,
                item_kind="task",
                target_table="day_plan_items",
                target_id=item_id,
                title=item.title,
                source_quote=item.source_quote,
                payload={"plan_date": local_date, "model_note": item.model_note, "tags": item.tags},
                status="active",
            )
        elif kind == "shopping":
            shop_id = await repo.create_shopping_item(
                db,
                user["id"],
                item.title,
                qty=item.quantity,
                unit=item.unit,
                category="food",
                scope="household",
            )
            action_id = await repo.create_inbox_action(
                db,
                user_id=user["id"],
                inbox_message_id=inbox_id,
                item_kind="shopping",
                target_table="shopping_list",
                target_id=shop_id,
                title=item.title,
                source_quote=item.source_quote,
                payload={"quantity": item.quantity, "unit": item.unit, "model_note": item.model_note},
                status="active",
            )
        elif kind == "reminder":
            sched = item.schedule
            if sched and not sched.is_ambiguous and sched.resolved_date and sched.resolved_time:
                rem_id = await repo.create_custom_reminder(
                    db,
                    user_id=user["id"],
                    title=item.title,
                    reminder_time=sched.resolved_time,
                    frequency_days=9999,
                    target_weekday=None,
                )
                last_sent = compute_one_time_last_sent(sched.resolved_date, local_date, 9999)
                if last_sent:
                    await repo.set_custom_reminder_sent(db, rem_id, last_sent)
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="reminder",
                    target_table="custom_reminders",
                    target_id=rem_id,
                    title=item.title,
                    source_quote=item.source_quote,
                    payload={
                        "resolved_date": sched.resolved_date,
                        "resolved_time": sched.resolved_time,
                        "model_note": item.model_note,
                    },
                    status="active",
                )
            else:
                payload = {
                    "title": item.title,
                    "resolved_date": sched.resolved_date if sched else None,
                    "ambiguity_reason": sched.ambiguity_reason if sched else "vague_when",
                    "candidate_times": sched.candidate_times if sched else [],
                    "question": sched.question if sched else "На какую дату и время поставить напоминание?",
                    "model_note": item.model_note,
                }
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="pending_clarification",
                    target_table="pending",
                    target_id=None,
                    title=item.title,
                    source_quote=item.source_quote,
                    payload=payload,
                    status="clarifying",
                )
        else:
            entry_type = kind if kind in {"diary", "idea", "done"} else "diary"
            entry_id = await repo.create_journal_entry(
                db,
                user_id=user["id"],
                entry_type=entry_type,
                entry_date=local_date,
                source=source,
                user_text=item.source_quote,
                title=item.title,
                model_note=item.model_note,
                tags=item.tags,
                inbox_message_id=inbox_id,
            )
            action_id = await repo.create_inbox_action(
                db,
                user_id=user["id"],
                inbox_message_id=inbox_id,
                item_kind=entry_type,
                target_table="journal_entries",
                target_id=entry_id,
                title=item.title,
                source_quote=item.source_quote,
                payload={"model_note": item.model_note, "tags": item.tags},
                status="active",
            )

        action_row = await repo.get_inbox_action(db, user["id"], action_id)
        if action_row:
            actions.append(action_row)
            if action_row.get("status") == "clarifying":
                clarifications.append(action_row)

    return actions, clarifications


async def _analyze_and_persist(
    db,
    user: dict[str, Any],
    inbox_row: dict[str, Any],
    raw_text: str,
    now_utc: datetime.datetime,
    llm_client_instance: Optional[LLMClient] = None,
) -> InboxProcessOutcome:
    tz = user.get("timezone") or "UTC"
    local_date = inbox_row.get("local_date") or local_date_str(now_utc, tz)
    parsed = await analyze_inbox_text(
        raw_text,
        now_utc=now_utc,
        tz_str=tz,
        llm_client_instance=llm_client_instance,
    )

    actions, clarifications = await _persist_parsed_items(
        db, user, inbox_row, parsed, local_date
    )
    new_status = "needs_clarification" if clarifications else "processed"
    await repo.update_inbox_message(
        db,
        int(inbox_row["id"]),
        status=new_status,
        llm_provider=parsed.llm_provider,
        llm_model=parsed.llm_model,
        llm_raw_json=parsed.llm_raw_json,
        llm_error=parsed.llm_error or "",
    )
    updated_row = await repo.get_inbox_message(db, user["id"], int(inbox_row["id"])) or inbox_row
    reply_text, reply_markup = render_inbox_summary(
        updated_row, actions, llm_error=parsed.llm_error
    )
    return InboxProcessOutcome(
        inbox_id=int(inbox_row["id"]),
        status=new_status,
        is_duplicate=False,
        source=updated_row.get("source") or "text",
        raw_text=raw_text,
        actions=actions,
        clarifications=clarifications,
        llm_error=parsed.llm_error,
        used_llm=parsed.used_llm,
        reply_text=reply_text,
        reply_markup=reply_markup,
    )


async def capture_and_process_text(
    db,
    user: dict[str, Any],
    *,
    chat_id: int,
    message_id: Optional[int],
    text: str,
    now_utc: Optional[datetime.datetime] = None,
    llm_client_instance: Optional[LLMClient] = None,
) -> InboxProcessOutcome:
    now = now_utc or datetime.datetime.utcnow()
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    clean_text = (text or "").strip()

    row, created = await repo.create_inbox_message_if_new(
        db,
        user_id=user["id"],
        chat_id=chat_id,
        telegram_message_id=message_id,
        source="text",
        local_date=local_date,
        raw_text=clean_text,
        status="received",
    )
    if not created:
        return InboxProcessOutcome(
            inbox_id=int(row.get("id") or 0),
            status=str(row.get("status") or "processed"),
            is_duplicate=True,
            source="text",
            raw_text=str(row.get("raw_text") or clean_text),
            reply_text="Это сообщение я уже сохранила, дубликаты не создавала.",
        )

    return await _analyze_and_persist(
        db, user, row, clean_text, now, llm_client_instance=llm_client_instance
    )


async def capture_and_process_voice(
    db,
    bot,
    user: dict[str, Any],
    *,
    chat_id: int,
    message_id: Optional[int],
    voice,
    now_utc: Optional[datetime.datetime] = None,
    stt_fn: Optional[Callable[..., Any]] = None,
    llm_client_instance: Optional[LLMClient] = None,
) -> InboxProcessOutcome:
    now = now_utc or datetime.datetime.utcnow()
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    file_id = getattr(voice, "file_id", None)
    file_unique_id = getattr(voice, "file_unique_id", None)
    duration = getattr(voice, "duration", None)

    row, created = await repo.create_inbox_message_if_new(
        db,
        user_id=user["id"],
        chat_id=chat_id,
        telegram_message_id=message_id,
        source="voice",
        local_date=local_date,
        raw_text=None,
        telegram_file_id=file_id,
        telegram_file_unique_id=file_unique_id,
        duration_sec=duration,
        status="received",
    )
    if not created:
        return InboxProcessOutcome(
            inbox_id=int(row.get("id") or 0),
            status=str(row.get("status") or "processed"),
            is_duplicate=True,
            source="voice",
            raw_text=str(row.get("raw_text") or ""),
            reply_text="Это голосовое я уже сохранила, дубликаты не создавала.",
        )

    inbox_id = int(row["id"])
    max_sec = stt.max_voice_duration_sec()
    if duration is not None and int(duration) > max_sec:
        await repo.update_inbox_message(
            db, inbox_id, status="failed_stt", stt_error="too_long"
        )
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="failed_stt",
            is_duplicate=False,
            source="voice",
            stt_error="too_long",
            reply_text=(
                f"Голосовое длиннее {max_sec} сек — запись сохранила, но не смогла расшифровать. "
                "Пришли покороче или текстом."
            ),
        )

    if stt_fn is None and not stt.stt_enabled():
        await repo.update_inbox_message(
            db, inbox_id, status="failed_stt", stt_error="stt_not_configured"
        )
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="failed_stt",
            is_duplicate=False,
            source="voice",
            stt_error="stt_not_configured",
            reply_text=(
                "Голосовое сохранила, но сервис расшифровки (STT) пока не подключён. "
                "Напиши это же текстом — сразу разберу и запишу."
            ),
        )

    audio_bytes, dl_err = await stt.download_telegram_voice(bot, voice)
    if not audio_bytes:
        await repo.update_inbox_message(
            db, inbox_id, status="failed_stt", stt_error=dl_err or "download_failed"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔁 Повторить расшифровку", callback_data=f"inbox:retry_stt:{inbox_id}")]
            ]
        )
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="failed_stt",
            is_duplicate=False,
            source="voice",
            stt_error=dl_err or "download_failed",
            reply_text="Запись сохранила, но не смогла скачать аудио из Telegram. Можем повторить или напиши текстом.",
            reply_markup=kb,
        )

    mime_type = getattr(voice, "mime_type", None) or "audio/ogg"
    filename = "voice.mp4" if "mp4" in mime_type else "voice.ogg"
    if stt_fn is not None:
        raw_res = await stt_fn(audio_bytes, filename=filename, mime_type=mime_type)
        if isinstance(raw_res, stt.STTResult):
            stt_res = raw_res
        elif isinstance(raw_res, str) and raw_res.strip():
            stt_res = stt.STTResult(ok=True, text=raw_res.strip(), provider="custom")
        else:
            stt_res = stt.STTResult(ok=False, error="empty_transcript")
    else:
        stt_res = await stt.transcribe_voice(audio_bytes, filename=filename, mime_type=mime_type)

    if not stt_res.ok or not stt_res.text.strip():
        await repo.update_inbox_message(
            db,
            inbox_id,
            status="failed_stt",
            stt_provider=stt_res.provider,
            stt_error=stt_res.error or "stt_failed",
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔁 Повторить расшифровку", callback_data=f"inbox:retry_stt:{inbox_id}")]
            ]
        )
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="failed_stt",
            is_duplicate=False,
            source="voice",
            stt_error=stt_res.error or "stt_failed",
            reply_text=(
                "Голосовое сохранила, но расшифровать сейчас не получилось. "
                "Можно нажать «Повторить» или прислать текстом."
            ),
            reply_markup=kb,
        )

    transcript = stt_res.text.strip()
    await repo.update_inbox_message(
        db,
        inbox_id,
        raw_text=transcript,
        stt_provider=stt_res.provider,
        stt_error="",
        status="transcribed",
    )
    updated_row = await repo.get_inbox_message(db, user["id"], inbox_id) or row
    return await _analyze_and_persist(
        db,
        user,
        updated_row,
        transcript,
        now,
        llm_client_instance=llm_client_instance,
    )


async def _delete_action_target(db, user_id: int, action: dict[str, Any]) -> None:
    table = action.get("target_table")
    tid = action.get("target_id")
    if not tid:
        return
    tid_int = int(tid)
    if table == "day_plan_items":
        await repo.delete_day_plan_item(db, user_id, tid_int)
    elif table == "shopping_list":
        await repo.delete_shopping_item(db, user_id, tid_int, scope="household")
    elif table == "custom_reminders":
        await repo.delete_custom_reminder(db, user_id, tid_int)
    elif table == "journal_entries":
        await repo.delete_journal_entry(db, user_id, tid_int, hard=False)


async def undo_inbox_message(db, user: dict[str, Any], inbox_id: int) -> int:
    actions = await repo.list_inbox_actions(db, user["id"], inbox_id, include_undone=False)
    undone_count = 0
    for act in actions:
        await _delete_action_target(db, user["id"], act)
        await repo.update_inbox_action(db, user["id"], act["id"], status="undone")
        undone_count += 1
    await repo.update_inbox_message(db, inbox_id, status="undone")
    return undone_count


async def delete_inbox_action(db, user: dict[str, Any], action_id: int) -> Optional[dict[str, Any]]:
    action = await repo.get_inbox_action(db, user["id"], action_id)
    if not action or action.get("status") == "undone":
        return None
    await _delete_action_target(db, user["id"], action)
    await repo.update_inbox_action(db, user["id"], action_id, status="undone")
    await _sync_parent_inbox_status(db, user["id"], int(action["inbox_message_id"]))
    return action


async def rename_inbox_action(
    db, user: dict[str, Any], action_id: int, new_title: str
) -> Optional[dict[str, Any]]:
    clean_title = (new_title or "").strip()
    if not clean_title:
        return None
    action = await repo.get_inbox_action(db, user["id"], action_id)
    if not action or action.get("status") == "undone":
        return None

    table = action.get("target_table")
    tid = action.get("target_id")
    if tid:
        tid_int = int(tid)
        if table == "day_plan_items":
            await repo.update_day_plan_item_title(db, user["id"], tid_int, clean_title)
        elif table == "shopping_list":
            await repo.update_shopping_item_name(db, user["id"], tid_int, clean_title, scope="household")
        elif table == "custom_reminders":
            await repo.update_custom_reminder_title(db, user["id"], tid_int, clean_title)
        elif table == "journal_entries":
            await repo.update_journal_entry(db, user["id"], tid_int, title=clean_title)

    payload = _load_payload(action)
    if "title" in payload:
        payload["title"] = clean_title
    await repo.update_inbox_action(
        db,
        user["id"],
        action_id,
        title=clean_title,
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    return await repo.get_inbox_action(db, user["id"], action_id)


async def reclassify_inbox_action(
    db,
    user: dict[str, Any],
    action_id: int,
    new_kind: str,
    *,
    now_utc: Optional[datetime.datetime] = None,
) -> Optional[dict[str, Any]]:
    if new_kind not in {"task", "shopping", "diary", "idea", "done"}:
        return None
    action = await repo.get_inbox_action(db, user["id"], action_id)
    if not action or action.get("status") == "undone":
        return None

    now = now_utc or datetime.datetime.utcnow()
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    inbox_row = await repo.get_inbox_message(db, user["id"], int(action["inbox_message_id"]))
    source = (inbox_row or {}).get("source") or "text"
    payload = _load_payload(action)

    await _delete_action_target(db, user["id"], action)

    if new_kind == "task":
        new_id = await repo.add_day_plan_item(
            db, user["id"], local_date, action["title"], category="inbox", is_important=False
        )
        new_table = "day_plan_items"
    elif new_kind == "shopping":
        qty = float(payload.get("quantity") or 1.0)
        unit = str(payload.get("unit") or "шт")
        new_id = await repo.create_shopping_item(
            db, user["id"], action["title"], qty=qty, unit=unit, category="food", scope="household"
        )
        new_table = "shopping_list"
    else:
        new_id = await repo.create_journal_entry(
            db,
            user_id=user["id"],
            entry_type=new_kind,
            entry_date=local_date,
            source=source,
            user_text=action["source_quote"],
            title=action["title"],
            model_note=str(payload.get("model_note") or ""),
            tags=payload.get("tags") if isinstance(payload.get("tags"), list) else [],
            inbox_message_id=int(action["inbox_message_id"]),
        )
        new_table = "journal_entries"

    await repo.update_inbox_action(
        db,
        user["id"],
        action_id,
        item_kind=new_kind,
        target_table=new_table,
        target_id=new_id,
        status="active",
    )
    await _sync_parent_inbox_status(db, user["id"], int(action["inbox_message_id"]))
    return await repo.get_inbox_action(db, user["id"], action_id)


async def resolve_clarification_action(
    db,
    user: dict[str, Any],
    action_id: int,
    *,
    resolved_date: Optional[str] = None,
    resolved_time: Optional[str] = None,
    as_task: bool = False,
    drop: bool = False,
    now_utc: Optional[datetime.datetime] = None,
) -> Optional[dict[str, Any]]:
    action = await repo.get_inbox_action(db, user["id"], action_id)
    if not action or action.get("status") != "clarifying":
        return None

    now = now_utc or datetime.datetime.utcnow()
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    inbox_id = int(action["inbox_message_id"])

    if drop:
        await repo.update_inbox_action(db, user["id"], action_id, status="undone")
        await _sync_parent_inbox_status(db, user["id"], inbox_id)
        return await repo.get_inbox_action(db, user["id"], action_id)

    payload = _load_payload(action)
    title = payload.get("title") or action.get("title") or "Напоминание"

    if as_task:
        target_date = resolved_date or payload.get("resolved_date") or local_date
        item_id = await repo.add_day_plan_item(
            db, user["id"], target_date, title, category="inbox", is_important=False
        )
        await repo.update_inbox_action(
            db,
            user["id"],
            action_id,
            item_kind="task",
            target_table="day_plan_items",
            target_id=item_id,
            title=title,
            payload_json=json.dumps({"plan_date": target_date}, ensure_ascii=False),
            status="active",
        )
        await _sync_parent_inbox_status(db, user["id"], inbox_id)
        return await repo.get_inbox_action(db, user["id"], action_id)

    eff_date = resolved_date or payload.get("resolved_date") or local_date
    eff_time = resolved_time
    if not eff_time:
        return None

    rem_id = await repo.create_custom_reminder(
        db,
        user_id=user["id"],
        title=title,
        reminder_time=eff_time,
        frequency_days=9999,
        target_weekday=None,
    )
    last_sent = compute_one_time_last_sent(eff_date, local_date, 9999)
    if last_sent:
        await repo.set_custom_reminder_sent(db, rem_id, last_sent)

    new_payload = {
        "resolved_date": eff_date,
        "resolved_time": eff_time,
        "model_note": payload.get("model_note", ""),
    }
    await repo.update_inbox_action(
        db,
        user["id"],
        action_id,
        item_kind="reminder",
        target_table="custom_reminders",
        target_id=rem_id,
        title=title,
        payload_json=json.dumps(new_payload, ensure_ascii=False),
        status="active",
    )
    await _sync_parent_inbox_status(db, user["id"], inbox_id)
    return await repo.get_inbox_action(db, user["id"], action_id)


async def retry_inbox_stt(
    db,
    bot,
    user: dict[str, Any],
    inbox_id: int,
    *,
    now_utc: Optional[datetime.datetime] = None,
    stt_fn: Optional[Callable[..., Any]] = None,
    llm_client_instance: Optional[LLMClient] = None,
) -> InboxProcessOutcome:
    row = await repo.get_inbox_message(db, user["id"], inbox_id)
    if not row:
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="missing",
            is_duplicate=False,
            source="voice",
            reply_text="Не нашла эту запись.",
        )
    if row.get("raw_text"):
        return await retry_inbox_parse(
            db, user, inbox_id, now_utc=now_utc, llm_client_instance=llm_client_instance
        )
    file_id = row.get("telegram_file_id")
    if not file_id:
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="failed_stt",
            is_duplicate=False,
            source="voice",
            stt_error="missing_file_id",
            reply_text="У записи нет аудиофайла — пришли текстом.",
        )

    voice_stub = SimpleNamespace(
        file_id=file_id,
        file_unique_id=row.get("telegram_file_unique_id"),
        duration=row.get("duration_sec"),
        mime_type="audio/ogg",
    )
    audio_bytes, dl_err = await stt.download_telegram_voice(bot, voice_stub)
    if not audio_bytes:
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="failed_stt",
            is_duplicate=False,
            source="voice",
            stt_error=dl_err or "download_failed",
            reply_text="Не смогла скачать аудио. Попробуй ещё раз или напиши текстом.",
        )

    if stt_fn is not None:
        raw_res = await stt_fn(audio_bytes, filename="voice.ogg", mime_type="audio/ogg")
        stt_res = (
            raw_res
            if isinstance(raw_res, stt.STTResult)
            else stt.STTResult(ok=bool(raw_res), text=str(raw_res or "").strip())
        )
    else:
        stt_res = await stt.transcribe_voice(audio_bytes, filename="voice.ogg", mime_type="audio/ogg")

    if not stt_res.ok or not stt_res.text.strip():
        await repo.update_inbox_message(
            db, inbox_id, status="failed_stt", stt_error=stt_res.error or "stt_failed"
        )
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="failed_stt",
            is_duplicate=False,
            source="voice",
            stt_error=stt_res.error or "stt_failed",
            reply_text="Пока всё ещё не удалось расшифровать аудио. Пришли текстом — я сразу запишу.",
        )

    transcript = stt_res.text.strip()
    await repo.update_inbox_message(
        db,
        inbox_id,
        raw_text=transcript,
        stt_provider=stt_res.provider,
        stt_error="",
        status="transcribed",
    )
    updated_row = await repo.get_inbox_message(db, user["id"], inbox_id) or row
    now = now_utc or datetime.datetime.utcnow()
    return await _analyze_and_persist(
        db, user, updated_row, transcript, now, llm_client_instance=llm_client_instance
    )


async def retry_inbox_parse(
    db,
    user: dict[str, Any],
    inbox_id: int,
    *,
    now_utc: Optional[datetime.datetime] = None,
    llm_client_instance: Optional[LLMClient] = None,
) -> InboxProcessOutcome:
    row = await repo.get_inbox_message(db, user["id"], inbox_id)
    if not row or not row.get("raw_text"):
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="missing",
            is_duplicate=False,
            source="text",
            reply_text="Не нашла исходный текст для переразбора.",
        )
    await undo_inbox_message(db, user, inbox_id)
    now = now_utc or datetime.datetime.utcnow()
    return await _analyze_and_persist(
        db, user, row, str(row["raw_text"]), now, llm_client_instance=llm_client_instance
    )


async def _sync_parent_inbox_status(db, user_id: int, inbox_id: int) -> None:
    actions = await repo.list_inbox_actions(db, user_id, inbox_id, include_undone=False)
    if not actions:
        await repo.update_inbox_message(db, inbox_id, status="undone")
    elif any(a.get("status") == "clarifying" for a in actions):
        await repo.update_inbox_message(db, inbox_id, status="needs_clarification")
    else:
        await repo.update_inbox_message(db, inbox_id, status="processed")
