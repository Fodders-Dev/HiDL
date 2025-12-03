import datetime
from typing import Tuple

from aiogram import types

from db import repositories as repo
from utils.finance import payday_summary
from utils.time import local_date_str, format_date_display, format_time_local
from utils.formatting import format_money
from utils.rows import row_to_dict
from utils.texts import gentle_streak
from utils.rows import rows_to_dicts
from utils.affirmations import random_affirmation_text


async def render_today(db, user) -> Tuple[str, types.InlineKeyboardMarkup]:
    """Build /today text and inline keyboard as dashboard."""
    user = dict(user)
    now_utc = datetime.datetime.utcnow()
    local_date = local_date_str(now_utc, user["timezone"])
    adhd = user.get("adhd_mode") == 1

    await repo.ensure_user_tasks_for_date(db, user["id"], local_date)
    tasks = await repo.get_tasks_for_day(db, user["id"], local_date)
    routines = await repo.list_user_routines(db, user["id"])

    status_emoji = {"pending": "⏳", "done": "✅", "skip": "⏭", "later": "🔔"}
    routine_status = {row_to_dict(t).get("routine_id"): row_to_dict(t).get("status") for t in tasks}

    routine_lines = []
    routine_kb = []
    for r_raw in routines:
        r = row_to_dict(r_raw)
        if not r.get("routine_id"):
            continue
        st = routine_status.get(r.get("routine_id"), "pending")
        label = f"{status_emoji.get(st,'⏳')} {r.get('title')} ({r.get('reminder_time')})"
        if st == "skip":
            label = f"⏭ {r.get('title')} ({r.get('reminder_time')})"
        routine_lines.append(label)
        routine_kb.append(
            [
                types.InlineKeyboardButton(
                    text=f"✅ {r.get('title')}",
                    callback_data=f"routine:{r.get('routine_id')}:{local_date}:done",
                ),
                types.InlineKeyboardButton(
                    text=f"⏭ {r.get('title')}",
                    callback_data=f"routine:{r.get('routine_id')}:{local_date}:skip",
                ),
            ]
        )
    if adhd and len(routine_lines) > 3:
        routine_lines = routine_lines[:3]
        routine_kb = routine_kb[:3]

    custom = await repo.list_custom_reminders(db, user["id"])
    custom_status = await repo.custom_statuses_for_date(db, user["id"], local_date)
    custom_lines = []
    custom_kb = []
    for c_raw in custom:
        c = row_to_dict(c_raw)
        status = custom_status.get(c.get("id"), "pending")
        if status != "done":  # не показываем уже закрытые
            custom_lines.append(f"• {c.get('title')} — {c.get('reminder_time')}")
            custom_kb.append(
                [
                    types.InlineKeyboardButton(
                        text=f"✅ {c.get('title','')[:18]}",
                        callback_data=f"custom:{c.get('id')}:{local_date}:done",
                    ),
                    types.InlineKeyboardButton(
                        text=f"↪️ {c.get('title','')[:18]}",
                        callback_data=f"custom:{c.get('id')}:{local_date}:later",
                    ),
                ]
            )
    if adhd and len(custom_lines) > 3:
        custom_lines = custom_lines[:3]
        custom_kb = custom_kb[:3]
    elif len(custom_lines) > 3:
        custom_lines = custom_lines[:3]
        custom_kb = custom_kb[:3]

    await repo.ensure_regular_tasks(db, user["id"], local_date)
    reg_due = await repo.list_regular_tasks(db, user["id"], due_only=True, local_date=local_date, due_in_days=1)
    reg_done_today = await repo.list_regular_tasks_done_on_date(db, user["id"], local_date)
    regular_lines = []
    regular_kb = []
    for r_raw in reg_due[:3]:
        r = row_to_dict(r_raw)
        zone = r.get("zone") or "misc"
        icon = {
            "kitchen": "🍳",
            "bathroom": "🚿",
            "bedroom": "🛏",
            "hallway": "🚪",
            "laundry": "🧺",
            "fridge": "🧊",
            "misc": "🧰",
        }.get(zone, "🧰")
        regular_lines.append(f"{icon} {r.get('title')} — до {format_date_display(r.get('next_due_date'))}")
        regular_kb.append(
            [
                types.InlineKeyboardButton(text=f"✅ {r.get('title','')[:16]}", callback_data=f"hweek:done:{r.get('id')}"),
                types.InlineKeyboardButton(text="↪️ +1", callback_data=f"hweek:later:1:{r.get('id')}"),
                types.InlineKeyboardButton(text="+3", callback_data=f"hweek:later:3:{r.get('id')}"),
                types.InlineKeyboardButton(text="+7", callback_data=f"hweek:later:7:{r.get('id')}"),
            ]
        )
    if reg_done_today:
        done_lines = [f"• {row_to_dict(r).get('title')} — следующая дата {format_date_display(row_to_dict(r).get('next_due_date'))}" for r in reg_done_today[:2]]
        regular_lines += ["Выполнено сегодня:"] + done_lines

    pause_note = ""
    if user.get("pause_until") and user["pause_until"] >= local_date:
        pause_note = "🧘 Сейчас включён щадящий режим: уведомлений меньше, можно вернуть /resume.\n\n"

    bills_lines = []
    bills = await repo.bills_due_soon(db, user["id"], local_date, days_ahead=3)
    if bills:
        bills_lines = []
        for b in bills[:3]:
            row = row_to_dict(b)
            bills_lines.append(
                f"• {row.get('title')} — до {format_date_display(row.get('due_date'))} (~{format_money(row.get('amount',0))} ₽)"
            )

    # day plan (важные/дополнительные дела)
    plan_items = rows_to_dicts(await repo.list_day_plan_items(db, user["id"], local_date))
    important_plan = [p for p in plan_items if p.get("is_important")]
    extra_plan = [p for p in plan_items if not p.get("is_important")]
    plan_done = [p for p in plan_items if p.get("done")]

    # продукты с подходящими сроками годности
    wellness = await repo.get_wellness(db, user["id"])
    window_days = int((wellness or {}).get("expiring_window_days", 3))
    soon_pantry, expired_pantry = await repo.pantry_expiring(db, user["id"], local_date, window_days=window_days)
    soon_pantry_d = [row_to_dict(r) for r in soon_pantry]
    expired_pantry_d = [row_to_dict(r) for r in expired_pantry]

    # meds stats
    meds_total, meds_taken = await repo.meds_stats_for_date(db, user["id"], local_date)

    # summary block
    points7 = await repo.points_window(db, user["id"], days=7)
    points_today = await repo.points_today(db, user["id"], local_date)
    streak = await repo.points_streak(db, user["id"], today=local_date)
    stats_r = await repo.routine_stats(db, user["id"], days=1)
    stats_c = await repo.custom_stats(db, user["id"], days=1)
    done_today = sum(r["cnt"] for r in stats_r if r["status"] == "done") + sum(r["cnt"] for r in stats_c if r["status"] == "done")
    total_today = sum(r["cnt"] for r in stats_r) + sum(r["cnt"] for r in stats_c)
    important_total = min(3, len(routine_lines) + len(custom_lines) + len(regular_lines)) if adhd else total_today
    routine_done_cnt = sum(r["cnt"] for r in stats_r if r["status"] == "done")
    routine_total_cnt = sum(r["cnt"] for r in stats_r)
    summary_lines = [
        f"🎯 Очки: сегодня {points_today}, за 7 дней {points7}, стрик {streak} дн.",
        f"✅ Прогресс: {done_today}/{important_total or total_today or 0} задач за сегодня",
        f"🌞 Рутины: {routine_done_cnt}/{routine_total_cnt} пунктов за сегодня",
        gentle_streak(streak),
    ]
    finance_line = await payday_summary(db, user, local_date)
    home_summary = ""
    if reg_due:
        names = ", ".join(r["title"] for r in reg_due[:2])
        extra = "" if len(reg_due) <= 2 else f" и ещё {len(reg_due)-2}"
        home_summary = f"🧹 Дом: сегодня {len(reg_due)} дела — {names}{extra}."
    else:
        home_summary = "🧹 Дом: на эту неделю всё чисто, можно отдохнуть."
    if finance_line:
        summary_lines.append(finance_line)
    if home_summary:
        summary_lines.append(home_summary)
    if plan_items:
        summary_lines.append(
            f"🎯 План на день: выполнено {len(plan_done)}/{len(plan_items)} дел "
            f"(важных {len(important_plan)}, доп. {len(extra_plan)})."
        )
    if soon_pantry_d or expired_pantry_d:
        names = [p.get("name") for p in soon_pantry_d[:2] + expired_pantry_d[:2] if p.get("name")]
        listed = ", ".join(names)
        total_pantry = len(soon_pantry_d) + len(expired_pantry_d)
        extra_note = ""
        if total_pantry > len(names):
            extra_note = f" и ещё {total_pantry - len(names)}"
        summary_lines.append(
            f"🧊 Продукты: у {len(soon_pantry_d)} вещей скоро срок, "
            f"у {len(expired_pantry_d)} уже истёк — {listed}{extra_note}."
        )
    if meds_total:
        # показываем только краткий итог и 1–2 ближайших приёма;
        # детальный список можно открыть по кнопке «Таблетки».
        meds_line = (
            f"💊 Таблетки: сегодня {meds_total} напоминаний, уже отмечено {meds_taken}/{meds_total}."
        )
        try:
            logs = await repo.list_med_logs_for_date(db, user["id"], local_date)
        except Exception:
            logs = []
        upcoming = [dict(l) for l in logs if not l.get("taken_at")]
        if not upcoming:
            upcoming = [dict(l) for l in logs]
        if upcoming:
            nearest = upcoming[:2]
            details_parts = []
            for log in nearest:
                t = (log.get("planned_time") or "")[:5]
                name = log.get("name") or "курс"
                details_parts.append(f"{t} — {name}")
            meds_line += " Ближайшие: " + "; ".join(details_parts) + ". Остальное — в кнопке «Таблетки» ниже."
        summary_lines.append(meds_line)
    # мягкая мысль дня по настройкам аффирмаций
    affirm_line = ""
    affirm_mode = (wellness or {}).get("affirm_mode", "off")
    if affirm_mode and affirm_mode != "off":
        local_time = format_time_local(now_utc, user["timezone"])
        try:
            hour = int(local_time.split(":")[0])
        except Exception:
            hour = 12
        show = False
        if affirm_mode in {"morning", "both"} and hour < 12:
            show = True
        if affirm_mode in {"evening", "both"} and hour >= 18:
            show = True
        if show:
            thought = random_affirmation_text()
            if thought:
                affirm_line = f"💬 Мысль дня: «{thought}»"
                summary_lines.append(affirm_line)
    blocks = [f"{pause_note}<b>План на {format_date_display(local_date)}</b>\n" + "\n".join(summary_lines)]
    if routine_lines:
        blocks.append("<b>🌞 Рутины:</b>\n" + "\n".join(routine_lines))
    if custom_lines:
        blocks.append("<b>🔔 Свои напоминания:</b>\n" + "\n".join(custom_lines))
    if regular_lines:
        blocks.append("<b>🔁 Регулярка по дому:</b>\n" + "\n".join(regular_lines))
    if bills_lines:
        blocks.append("<b>📅 Счета в ближайшие дни:</b>\n" + "\n".join(bills_lines))
    if plan_items:
        plan_lines = ["<b>🎯 План на день — детали:</b>"]
        for item in plan_items:
            icon = "✅" if item.get("done") else "⬜️"
            kind = " (важное)" if item.get("is_important") else ""
            plan_lines.append(f"{icon} {item.get('title')}{kind}")
        blocks.append("\n".join(plan_lines))

    kb_buttons = []
    kb_buttons.append([types.InlineKeyboardButton(text="Напоминания", callback_data="rem:list")])
    kb_buttons.append([types.InlineKeyboardButton(text="📅 План по дому", callback_data="home:week")])
    kb_buttons.append([types.InlineKeyboardButton(text="Финансы", callback_data="money:report")])
    kb_buttons.append([types.InlineKeyboardButton(text="Мои очки", callback_data="stats:view")])
    if meds_total:
        kb_buttons.append(
            [types.InlineKeyboardButton(text="Таблетки", callback_data="meds:today")]
        )
    if soon_pantry_d or expired_pantry_d:
        kb_buttons.append(
            [types.InlineKeyboardButton(text="Продукты", callback_data="pantry:expiring")]
        )
    # быстрые кнопки для плана дня (ограничим тремя пунктами)
    if plan_items:
        for item in plan_items[:3]:
            if item.get("done"):
                continue
            title = (item.get("title") or "")[:24]
            kb_buttons.append(
                [
                    types.InlineKeyboardButton(
                        text=f"🎯 {title}",
                        callback_data=f"dplan:done:{item.get('id')}",
                    )
                ]
            )
    inline_kb = types.InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    return "\n\n".join(blocks), inline_kb
