"""Входящие голосовые/текстовые сообщения, исправление разбора и личный дневник (/diary)."""

from __future__ import annotations

import datetime
import html
import io
import json
from typing import Any, Optional

from aiogram import F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from services import advice_service, coach_knowledge, fitness_nutrition, inbox_service, karma_service, pulse_service
from services.inbox_parser import resolve_temporal_expression
from utils.menu_match import match_menu_label
from utils.profile import is_student_mode
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


async def _existing_menu_user(db, telegram_id: int) -> dict[str, Any] | None:
    """Read a profile for reply-menu rendering without creating a user."""
    row = await repo.get_user_by_telegram_id(db, telegram_id)
    return dict(row) if row else None


async def handle_free_text_inbox(message: types.Message, db) -> None:
    """Единая точка входа для произвольного текста из natural_handler."""
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    night_notice = await pulse_service.check_night_violation(db, user)
    reply_msg = getattr(message, "reply_to_message", None)
    reply_context = str(
        getattr(reply_msg, "text", None) or getattr(reply_msg, "caption", None) or ""
    ).strip()[:400]
    outcome = await inbox_service.capture_and_process_text(
        db,
        user,
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=message.text or "",
        reply_context=reply_context,
    )
    reply_text = f"{night_notice}\n\n{outcome.reply_text}" if night_notice else outcome.reply_text
    await message.answer(
        reply_text,
        reply_markup=outcome.reply_markup or main_menu_keyboard(user),
    )


async def inbox_free_text(message: types.Message, state: FSMContext, db) -> None:
    """Делегирование свободного текста в inbox со сбросом временного FSM-состояния."""
    await state.clear()
    await handle_free_text_inbox(message, db)


@router.message(F.voice)
async def handle_voice_inbox(message: types.Message, state: FSMContext, db) -> None:
    """Принять голосовое сообщение, сохранить сырое, расшифровать и разобрать."""
    current = await state.get_state()
    if current and current.startswith("InboxEditState:"):
        await state.clear()
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    night_notice = await pulse_service.check_night_violation(db, user)
    outcome = await inbox_service.capture_and_process_voice(
        db,
        message.bot,
        user,
        chat_id=message.chat.id,
        message_id=message.message_id,
        voice=message.voice,
    )
    reply_text = f"{night_notice}\n\n{outcome.reply_text}" if night_notice else outcome.reply_text
    await message.answer(
        reply_text,
        reply_markup=outcome.reply_markup or main_menu_keyboard(user),
    )


_SCALE_CAPTION_KEYWORDS = (
    "вес",
    "весы",
    "жир",
    "мышц",
    "scale",
    "xiaomi",
    "имт",
    "bmi",
    "mi fit",
    "zepp",
)


def _is_image_document(doc: Any) -> tuple[bool, str]:
    if doc is None:
        return False, "image/jpeg"
    fsize = getattr(doc, "file_size", None)
    if fsize is not None and int(fsize) > 15 * 1024 * 1024:
        return False, "image/jpeg"
    mime = str(getattr(doc, "mime_type", "") or "").strip().lower()
    fname = str(getattr(doc, "file_name", "") or "").strip().lower()
    if mime.startswith("image/"):
        return True, mime
    if fname.endswith(".png"):
        return True, "image/png"
    if fname.endswith((".jpg", ".jpeg")):
        return True, "image/jpeg"
    if fname.endswith(".webp"):
        return True, "image/webp"
    return False, "image/jpeg"


async def _download_telegram_file_bytes(bot, file_obj) -> bytes:
    buf = io.BytesIO()
    dl = await bot.download(file_obj, destination=buf)
    img_bytes = buf.getvalue()
    if not img_bytes and dl is not None:
        if hasattr(dl, "getvalue"):
            img_bytes = dl.getvalue()
        elif isinstance(dl, (bytes, bytearray)):
            img_bytes = bytes(dl)
    return img_bytes


async def _save_and_reply_scale(
    message: types.Message,
    db,
    user: dict[str, Any],
    scale_res: dict[str, Any],
    now_utc: datetime.datetime,
    *,
    explicit_scale: bool = False,
) -> bool:
    uid = int(user["id"])
    local_date = local_date_str(now_utc, user.get("timezone") or "UTC")
    is_partial = bool(scale_res.get("partial") or scale_res.get("weight") is None)
    merged = False
    karma_note = ""

    if is_partial:
        merged_row = await repo.merge_recent_weight_metrics(
            db,
            uid,
            body_fat_pct=scale_res.get("body_fat_pct"),
            muscle_mass_kg=scale_res.get("muscle_mass_kg"),
            water_pct=scale_res.get("water_pct"),
            visceral_fat=scale_res.get("visceral_fat"),
            bmi=scale_res.get("bmi"),
            now_utc=now_utc,
        )
        if merged_row is None:
            if explicit_scale:
                await message.answer(
                    "⚖ Вижу показатели состава тела, но не нашла недавний замер веса за последние 2 часа.\n"
                    "Сначала скинь первый скрин с весом или напиши вес числом (например <code>99.2</code>).",
                    parse_mode="HTML",
                )
                return True
            return False
        saved_row = merged_row
        merged = True
    else:
        await repo.add_weight(
            db,
            uid,
            float(scale_res["weight"]),
            body_fat_pct=scale_res.get("body_fat_pct"),
            muscle_mass_kg=scale_res.get("muscle_mass_kg"),
            water_pct=scale_res.get("water_pct"),
            visceral_fat=scale_res.get("visceral_fat"),
            bmi=scale_res.get("bmi"),
            source=str(scale_res.get("source") or "scale_photo"),
        )
        saved_row = {
            "weight": float(scale_res["weight"]),
            "body_fat_pct": scale_res.get("body_fat_pct"),
            "muscle_mass_kg": scale_res.get("muscle_mass_kg"),
            "water_pct": scale_res.get("water_pct"),
            "visceral_fat": scale_res.get("visceral_fat"),
            "bmi": scale_res.get("bmi"),
        }
        already_done = await repo.has_karma_event_today(db, uid, local_date, "weigh_in_done")
        if not already_done:
            await repo.add_points(db, uid, 3, local_date=local_date)
            kres = await karma_service.record_karma_event(
                db,
                user,
                domain="tasks",
                event_type="weigh_in_done",
                delta=3,
                reason="Замер на умных весах",
                now_utc=now_utc,
            )
            karma_note = f"✨ +3 кармы за дисциплину замера ({kres.get('score_after', 70)}/100)."

    trend = await repo.body_metrics_trend(db, uid, days=30)
    week_stats = await repo.workout_stats_week(db, uid, local_date)
    targets = fitness_nutrition.calculate_daily_targets(
        user,
        trend.get("latest") or saved_row,
        workout_kcal_today=int(week_stats.get("today_kcal_burned") or 0),
    )
    wellness_row = await repo.get_wellness(db, uid)
    meal_profile = str((dict(wellness_row) if wellness_row else {}).get("meal_profile") or "")
    nutrition_advice = await fitness_nutrition.get_daily_nutrition_advice(db, user, now_utc=now_utc)
    action_plan = coach_knowledge.build_body_action_plan(
        latest=trend.get("latest") or saved_row,
        trend=trend,
        week_workouts=week_stats,
        nutrition_advice=nutrition_advice,
        user=user,
        meal_profile=meal_profile,
        now_utc=now_utc,
    )

    msg_text = fitness_nutrition.format_scale_saved_summary(
        saved_row,
        trend,
        targets,
        str(user.get("weight_goal") or "loss"),
        merged=merged,
        low_res_warning=bool(scale_res.get("low_res_warning")),
        karma_note=karma_note,
        action_plan=action_plan,
    )

    rows = [
        [
            InlineKeyboardButton(text="⚖ Тело и весы", callback_data="fit:body"),
            InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
        ]
    ]
    row2 = []
    if not merged and any(saved_row.get(k) is None for k in ("muscle_mass_kg", "water_pct", "visceral_fat")):
        row2.append(InlineKeyboardButton(text="➕ Дополнить 2-м скрином", callback_data="fit:scale_add"))
    if action_plan.get("suggest_lazy_workout"):
        row2.append(InlineKeyboardButton(text="🫠 3 мин старт", callback_data="fit:lazy"))
    else:
        row2.append(InlineKeyboardButton(text="📚 База тренера", callback_data="fit:playbook"))
    if row2:
        rows.append(row2)
    await message.answer(msg_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    return True


async def _process_unsolicited_image(
    message: types.Message,
    state: FSMContext,
    db,
    user: dict[str, Any],
    img_bytes: bytes,
    image_mime: str,
) -> None:
    uid = int(user["id"])
    caption = (message.caption or "").strip()
    low_cap = caption.lower()
    now_utc = datetime.datetime.utcnow()

    # 1. Если ждём уточнение по недавней еде -> фото этикетки или тарелки сразу уточняет и сохраняет!
    pending = await repo.get_active_pending_meal(db, uid, now_utc=now_utc)
    if pending is not None:
        combined_cap = f"{pending['raw_text']} {caption}".strip()
        parsed = await fitness_nutrition.parse_meal_input(
            raw_text=combined_cap,
            image_bytes=img_bytes,
            image_mime=image_mime,
            db=db,
            user_id=uid,
            allow_clarification=False,
        )
        if parsed.get("ok"):
            committed = await inbox_service.commit_pending_meal(
                db, user, pending, refined_meal=parsed, status="refined", now_utc=now_utc
            )
            label_note = "\n🏷 <i>Этикетку запомнила на будущее.</i>" if parsed.get("is_nutrition_label") else ""
            rem_line = ""
            if not committed["advice"]["targets"].get("mindful_mode"):
                rem = committed["advice"]["remaining"]
                rem_line = f"\nОстаток на сегодня: <b>{rem['kcal']} ккал</b> · белок +{rem['protein_g']} г"
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✏️ Поправить", callback_data=f"kbju:refine:{committed['meal_id']}"),
                        InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
                    ]
                ]
            )
            await message.answer(
                f"✅ Уточнила по фото и записала: <b>{_esc(committed['title'])}</b>\n"
                f"🔥 <b>{committed['kcal']} ккал</b> (Б {committed['protein_g']:g}/Ж {committed['fat_g']:g}/У {committed['carbs_g']:g})"
                f"{rem_line}{label_note}",
                reply_markup=kb,
                parse_mode="HTML",
            )
            return

    # 2. Если в подписи явно про весы/замер тела -> распознаём умные весы (включая 2-й скрин)
    if any(w in low_cap for w in _SCALE_CAPTION_KEYWORDS):
        scale_res = await fitness_nutrition.parse_smart_scale_input(
            raw_text=caption,
            image_bytes=img_bytes,
            image_mime=image_mime,
            allow_partial=True,
        )
        if scale_res.get("ok"):
            if await _save_and_reply_scale(message, db, user, scale_res, now_utc, explicit_scale=True):
                return
        await message.answer(
            "⚖ Не смогла разобрать цифры с весов. Отправь отчёт Xiaomi <b>как файл (без сжатия)</b> или впиши вес текстом (например <code>99.2</code>).",
            parse_mode="HTML",
        )
        return

    # 3. Быстрый путь для длинного вертикального экспорта Xiaomi без подписи -> сразу проверяем весы
    checked_scale_first = False
    if not caption and fitness_nutrition.is_tall_scale_like_image(img_bytes):
        checked_scale_first = True
        scale_res = await fitness_nutrition.parse_smart_scale_input(
            raw_text="",
            image_bytes=img_bytes,
            image_mime=image_mime,
            require_scale_screen=True,
            allow_partial=True,
        )
        if scale_res.get("ok"):
            if await _save_and_reply_scale(message, db, user, scale_res, now_utc, explicit_scale=True):
                return

    # 4. Обычное фото еды или этикетки из чата -> показываем 1-тап превью КБЖУ
    from handlers import nutrition

    parsed_meal = await fitness_nutrition.parse_meal_input(
        raw_text=caption,
        image_bytes=img_bytes,
        image_mime=image_mime,
        db=db,
        user_id=uid,
        allow_clarification=False,
    )
    if parsed_meal.get("ok"):
        await nutrition._show_meal_preview(message, state, parsed_meal)
        return

    # 5. Если еда не распознана и экран весов ещё не проверяли -> авто-проверка на скрин весов (1-й или 2-й экран)
    if parsed_meal.get("error") == "no_meal_found" and not checked_scale_first:
        scale_res = await fitness_nutrition.parse_smart_scale_input(
            raw_text=caption,
            image_bytes=img_bytes,
            image_mime=image_mime,
            require_scale_screen=True,
            allow_partial=True,
        )
        if scale_res.get("ok"):
            handled = await _save_and_reply_scale(message, db, user, scale_res, now_utc, explicit_scale=False)
            if handled:
                return
            await message.answer(
                "⚖ Вижу показатели состава тела, но на этом скрине нет веса — скинь сначала верхний экран с весом или напиши число.",
            )
            return

    await nutrition._show_meal_preview(message, state, parsed_meal)


@router.message(StateFilter(None), F.photo)
async def handle_unsolicited_photo(message: types.Message, state: FSMContext, db) -> None:
    """Обработать фото тарелки, этикетки или умных весов прямо из общего чата."""
    if await state.get_state():
        return
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    if is_student_mode(user):
        return
    img_bytes = await _download_telegram_file_bytes(message.bot, message.photo[-1])
    if not img_bytes:
        await message.answer("Не удалось загрузить фото — попробуй ещё раз или напиши текстом.")
        return
    await _process_unsolicited_image(message, state, db, user, img_bytes, "image/jpeg")


@router.message(StateFilter(None), F.document)
async def handle_unsolicited_image_document(message: types.Message, state: FSMContext, db) -> None:
    """Обработать картинку, отправленную в общий чат как файл без сжатия (например, отчёт Xiaomi)."""
    if await state.get_state():
        return
    is_img, mime = _is_image_document(getattr(message, "document", None))
    if not is_img:
        return
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    if is_student_mode(user):
        return
    img_bytes = await _download_telegram_file_bytes(message.bot, message.document)
    if not img_bytes:
        await message.answer("Не удалось скачать файл изображения — попробуй ещё раз.")
        return
    await _process_unsolicited_image(message, state, db, user, img_bytes, mime)


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


def _build_item_detail_view(action: dict[str, Any], user: dict[str, Any] | None = None) -> tuple[str, InlineKeyboardMarkup]:
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
        if target_k != kind and not (target_k == "shopping" and user and user.get("profile_mode") == "student")
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
    if action not in {"retitle", "clarify_custom"}:
        current = await state.get_state()
        if current and current.startswith("InboxEditState:"):
            await state.clear()
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
        text, kb = _build_item_detail_view(act, user)
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
        text, kb = _build_item_detail_view(updated, user)
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
        inbox_id = int(act["inbox_message_id"])
        await state.set_state(InboxEditState.rename)
        await state.update_data(edit_action_id=aid, edit_inbox_id=inbox_id)
        await callback.message.answer(
            "Напиши новый короткий текст для этого пункта:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"inbox:edit:{inbox_id}")]]
            ),
        )
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
        inbox_id = int(act["inbox_message_id"])
        await state.set_state(InboxEditState.clarify_when)
        await state.update_data(clarify_action_id=aid, clarify_inbox_id=inbox_id)
        await callback.message.answer(
            "На какую дату и время поставить? Например: «завтра в 14:00» или «20:30».",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"inbox:summary:{inbox_id}")]]
            ),
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

    if action == "confirm" and len(parts) > 2:
        aid = int(parts[2])
        ok, msg = await inbox_service.confirm_inbox_action(db, user, aid)
        await safe_edit(callback.message, msg, reply_markup=None)
        await callback.answer("Выполнено!" if ok else "Не найдено")
        return

    if action == "reject" and len(parts) > 2:
        aid = int(parts[2])
        ok = await inbox_service.reject_inbox_action(db, user, aid)
        await safe_edit(
            callback.message,
            "❌ Отменено — ничего менять не стала." if ok else "Действие уже закрыто.",
            reply_markup=None,
        )
        await callback.answer("Отменено.")
        return

    await callback.answer()


_SHOWN_ADVICE_BY_USER: dict[int, set[str]] = {}


def _advice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💡 Ещё совет", callback_data="advice:more"),
                InlineKeyboardButton(text="📅 Сегодня", callback_data="today:menu"),
            ],
            [InlineKeyboardButton(text="🌙 Сон и карма", callback_data="pulse:hub")],
        ]
    )


@router.message(Command("advice"))
async def cmd_advice(message: types.Message, state: FSMContext, db) -> None:
    await state.clear()
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    uid = int(user["id"])
    excluded = _SHOWN_ADVICE_BY_USER.setdefault(uid, set())
    reply_html, meta = await advice_service.build_situational_advice_reply(
        db, user, "", exclude_ids=excluded
    )
    card_id = str((meta.get("card") or {}).get("id") or "")
    if card_id:
        excluded.add(card_id)
    if len(excluded) >= 12:
        excluded.clear()
    await message.answer(reply_html, reply_markup=_advice_keyboard())


@router.callback_query(lambda c: c.data and c.data == "advice:more")
async def cb_advice_more(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    uid = int(user["id"])
    excluded = _SHOWN_ADVICE_BY_USER.setdefault(uid, set())
    reply_html, meta = await advice_service.build_situational_advice_reply(
        db, user, "", exclude_ids=excluded
    )
    card_id = str((meta.get("card") or {}).get("id") or "")
    if card_id:
        excluded.add(card_id)
    if len(excluded) >= 12:
        excluded.clear()
    await safe_edit(callback.message, reply_html, reply_markup=_advice_keyboard())
    await callback.answer()


@router.message(InboxEditState.rename, lambda m: m.text and not m.text.startswith("/"))
async def inbox_rename_input(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    aid = data.get("edit_action_id")
    inbox_id = data.get("edit_inbox_id")
    new_title = (message.text or "").strip()
    if not new_title:
        await message.answer("Напиши непустой текст.")
        return
    if new_title.lower() in {"отмена", "отменить"}:
        await state.clear()
        if inbox_id:
            user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
            actions = await repo.list_inbox_actions(db, user["id"], int(inbox_id), include_undone=False)
            text, kb = _build_edit_list_view(int(inbox_id), actions)
            await message.answer(text, reply_markup=kb)
        else:
            await message.answer("Отменила правку.", reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)))
        return
    await state.clear()
    if not aid:
        await message.answer("Не нашла пункт для правки.", reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)))
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    updated = await inbox_service.rename_inbox_action(db, user, int(aid), new_title)
    if not updated:
        await message.answer("Пункт уже удалён или не найден.", reply_markup=main_menu_keyboard(user))
        return
    text, kb = _build_item_detail_view(updated, user)
    await message.answer(text, reply_markup=kb)


@router.message(InboxEditState.clarify_when, lambda m: m.text and not m.text.startswith("/"))
async def inbox_clarify_when_input(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    aid = data.get("clarify_action_id")
    clarify_inbox_id = data.get("clarify_inbox_id")
    raw_input = (message.text or "").strip()
    if raw_input.lower() in {"отмена", "отменить"}:
        await state.clear()
        await message.answer("Отменила уточнение.", reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)))
        return
    if not aid:
        await state.clear()
        await message.answer("Черновик не найден.", reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)))
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    act = await repo.get_inbox_action(db, user["id"], int(aid))
    if not act:
        await state.clear()
        await message.answer("Уточнение не найдено.", reply_markup=main_menu_keyboard(user))
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
        await message.answer("Не удалось сохранить напоминание.", reply_markup=main_menu_keyboard(user))
        return
    inbox_id = int(resolved["inbox_message_id"])
    row = await repo.get_inbox_message(db, user["id"], inbox_id)
    actions = await repo.list_inbox_actions(db, user["id"], inbox_id, include_undone=False)
    if row:
        text, kb = inbox_service.render_inbox_summary(row, actions, llm_error=row.get("llm_error") or None)
        await message.answer(text, reply_markup=kb or main_menu_keyboard(user))
    else:
        await message.answer(
            f"🔔 Поставила напоминание на {format_date_display(sched.resolved_date)} {sched.resolved_time}.",
            reply_markup=main_menu_keyboard(user),
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
        entries = await repo.search_user_notes(db, user["id"], query, limit=10)
        lines = [f"<b>🔍 Поиск в дневнике:</b> «{_esc(query)}»"]
        if not entries:
            lines.append("Ничего не нашлось.")
        else:
            for idx, e in enumerate(entries, 1):
                src_icon = "🎙" if e.get("source") == "voice" else "💬"
                title = (e.get("title") or "").strip()
                utext = (e.get("user_text") or "").strip()
                if title and title.lower() != utext.lower():
                    lines.append(
                        f"{idx}. {format_date_display(e['entry_date'])} {src_icon} <b>{_esc(title)}</b>\n"
                        f"   «{_esc(utext)}»"
                    )
                else:
                    lines.append(
                        f"{idx}. {format_date_display(e['entry_date'])} {src_icon} «{_esc(utext or title)}»"
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
            task_row: list[InlineKeyboardButton] = []
            for idx, e in enumerate(entries, 1):
                src_icon = "🎙" if e.get("source") == "voice" else "💬"
                note = (e.get("model_note") or "").strip()
                line = f"{idx}. {format_date_display(e['entry_date'])} {src_icon} {_esc(e['user_text'])}"
                if note:
                    line += f"\n  <i>Заметка модели: {_esc(note)}</i>"
                lines.append(line)
                if len(del_row) < 4:
                    del_row.append(
                        InlineKeyboardButton(
                            text=f"🗑 {idx}",
                            callback_data=f"diary:del:{e['id']}:{tab}",
                        )
                    )
                    if entry_type == "idea":
                        task_row.append(
                            InlineKeyboardButton(
                                text=f"➡️ В задачу {idx}",
                                callback_data=f"diary:to_task:{e['id']}",
                            )
                        )
            if task_row:
                rows.append(task_row)
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
@router.message(StateFilter(None), lambda m: match_menu_label(m.text, {"дневник"}))
async def cmd_diary(message: types.Message, state: FSMContext, db) -> None:
    await state.clear()
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=1) if raw.startswith("/") else [raw]
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

    if action == "to_task" and len(parts) > 2:
        eid = int(parts[2])
        entry = await repo.get_journal_entry(db, user["id"], eid)
        if not entry:
            await callback.answer("Запись не найдена", show_alert=True)
            return
        local_date = local_date_str(datetime.datetime.utcnow(), user.get("timezone") or "UTC")
        task_title = str(entry.get("title") or entry.get("user_text") or "Идея").strip()
        await repo.add_day_plan_item(
            db, user["id"], local_date, task_title, category="idea", is_important=True
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📋 План на день", callback_data="dplan:list"),
                    InlineKeyboardButton(text="💡 К идеям", callback_data="diary:tab:idea"),
                ]
            ]
        )
        await callback.message.answer(
            f"✅ Перевела идею в важную задачу на сегодня:\n<b>{_esc(task_title)}</b>",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await callback.answer("Добавлено в задачи на сегодня!")
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
