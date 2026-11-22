import datetime
import html
from typing import Tuple

from aiogram import types

from db import repositories as repo
from services import calendar_service, karma_service
from utils.finance import payday_summary
from utils.time import local_date_str, format_date_display, format_time_local
from utils.formatting import format_money
from utils.rows import row_to_dict
from utils.texts import gentle_streak
from utils.rows import rows_to_dicts
from utils.affirmations import random_affirmation_text
from utils.profile import is_student_mode


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


async def render_today(db, user) -> Tuple[str, types.InlineKeyboardMarkup]:
    """Build /today text and inline keyboard as dashboard."""
    user = dict(user)
    student = is_student_mode(user)
    now_utc = datetime.datetime.utcnow()
    local_date = local_date_str(now_utc, user["timezone"])
    adhd = user.get("adhd_mode") == 1

    if not student:
        await repo.ensure_user_tasks_for_date(db, user["id"], local_date)
        tasks = await repo.get_tasks_for_day(db, user["id"], local_date)
        routines = await repo.list_user_routines(db, user["id"])
    else:
        tasks, routines = [], []

    status_emoji = {"pending": "⏳", "done": "✅", "skip": "⏭", "later": "🔔"}
    routine_status = {row_to_dict(t).get("routine_id"): row_to_dict(t).get("status") for t in tasks}

    routine_lines = []
    first_pending_routine_row = None
    for r_raw in routines:
        r = row_to_dict(r_raw)
        if not r.get("routine_id"):
            continue
        st = routine_status.get(r.get("routine_id"), "pending")
        label = f"{status_emoji.get(st,'⏳')} {r.get('title')} ({r.get('reminder_time')})"
        if st == "skip":
            label = f"⏭ {r.get('title')} ({r.get('reminder_time')})"
        routine_lines.append(label)
        if st == "pending" and first_pending_routine_row is None:
            title_short = (r.get("title") or "Рутина")[:20]
            first_pending_routine_row = [
                types.InlineKeyboardButton(
                    text=f"✅ {title_short}",
                    callback_data=f"routine:{r.get('routine_id')}:{local_date}:done",
                ),
                types.InlineKeyboardButton(
                    text="⏭ Пропустить",
                    callback_data=f"routine:{r.get('routine_id')}:{local_date}:skip",
                ),
            ]
    if adhd and len(routine_lines) > 3:
        routine_lines = routine_lines[:3]

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

    if not student:
        await repo.ensure_regular_tasks(db, user["id"], local_date)
        reg_due = await repo.list_regular_tasks(db, user["id"], due_only=True, local_date=local_date, due_in_days=1)
        reg_done_today = await repo.list_regular_tasks_done_on_date(db, user["id"], local_date)
    else:
        reg_due, reg_done_today = [], []
    regular_lines = []
    today_date = datetime.date.fromisoformat(local_date)
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
        next_due = r.get("next_due_date") or ""
        due_label = "давно не обновлялось"
        if next_due:
            try:
                due_dt = datetime.date.fromisoformat(next_due)
                if (today_date - due_dt).days <= 7:
                    due_label = f"до {format_date_display(next_due)}"
            except Exception:
                due_label = f"до {format_date_display(next_due)}"
        regular_lines.append(f"{icon} {r.get('title')} — {due_label}")
    if reg_done_today:
        done_lines = [f"• {row_to_dict(r).get('title')} — следующая дата {format_date_display(row_to_dict(r).get('next_due_date'))}" for r in reg_done_today[:2]]
        regular_lines += ["Выполнено сегодня:"] + done_lines

    pause_note = ""
    if user.get("pause_until") and user["pause_until"] >= local_date:
        pause_note = "🧘 Сейчас включён щадящий режим: уведомлений меньше, можно вернуть /resume.\n\n"

    quiet_note = ""
    if user.get("quiet_mode"):
        quiet_note = "🔕 Тихий режим: шлёт только твои напоминания. Отключить в ⚙ Настройки.\n\n"

    bills_lines = []
    bills = [] if student else await repo.bills_due_soon(db, user["id"], local_date, days_ahead=3)
    if bills:
        bills_lines = []
        for b in bills[:3]:
            row = row_to_dict(b)
            bills_lines.append(
                f"• {row.get('title')} — до {format_date_display(row.get('due_date'))} (~{format_money(row.get('amount',0))} ₽)"
            )

    # day plan (важные/дополнительные дела)
    plan_items = rows_to_dicts(await repo.list_day_plan_items(db, user["id"], local_date))
    cal_events_today = await calendar_service.get_unified_events_for_date(db, user["id"], local_date)
    important_plan = [p for p in plan_items if p.get("is_important")]
    extra_plan = [p for p in plan_items if not p.get("is_important")]
    plan_done = [p for p in plan_items if p.get("done")]

    # продукты с подходящими сроками годности
    if student:
        wellness = None
        soon_pantry, expired_pantry = [], []
    else:
        wellness = await repo.get_wellness(db, user["id"])
        window_days = int((wellness or {}).get("expiring_window_days", 3))
        soon_pantry, expired_pantry = await repo.pantry_expiring(db, user["id"], local_date, window_days=window_days)
    soon_pantry_d = [row_to_dict(r) for r in soon_pantry]
    expired_pantry_d = [row_to_dict(r) for r in expired_pantry]

    # meds stats
    meds_total, meds_taken = (0, 0) if student else await repo.meds_stats_for_date(db, user["id"], local_date)

    # summary block
    points_week = await repo.points_week(db, user["id"], local_date)
    points_today = await repo.points_today(db, user["id"], local_date)
    streak = await repo.points_streak(db, user["id"], today=local_date)
    stats_r = [] if student else await repo.routine_stats(db, user["id"], days=1)
    stats_c = await repo.custom_stats(db, user["id"], days=1)
    done_today = sum(r["cnt"] for r in stats_r if r["status"] == "done") + sum(r["cnt"] for r in stats_c if r["status"] == "done")
    total_today = sum(r["cnt"] for r in stats_r) + sum(r["cnt"] for r in stats_c)
    important_total = min(3, len(routine_lines) + len(custom_lines) + len(regular_lines)) if adhd else total_today
    routine_done_cnt = sum(r["cnt"] for r in stats_r if r["status"] == "done")
    routine_total_cnt = sum(r["cnt"] for r in stats_r)
    karma_state = await repo.get_or_create_karma_state(db, user["id"])
    karma_score = int(karma_state.get("score") if karma_state.get("score") is not None else 70)
    karma_mode = karma_service.get_karma_mode(karma_score, user)
    checkins_today = await repo.list_pulse_checkins_for_date(db, user["id"], local_date)
    std_slots = {"wake", "afternoon", "winddown", "bedtime"} if student else {"wake", "lunch", "afternoon", "dinner", "winddown", "bedtime"}
    std_checkins = [c for c in checkins_today if c.get("slot_key") in std_slots]
    pulse_done = sum(1 for c in std_checkins if c.get("status") == "done")
    pulse_total = max(6, len(std_checkins))
    sick_badge = " · 🤒 ЧП-заморозка" if karma_service.is_sick_freeze_active(user, now_utc) else ""

    summary_lines = [
        f"{karma_mode['emoji']} Карма: {karma_score}/100 ({karma_mode['label']}) • Пульс дня: {pulse_done}/{pulse_total}{sick_badge}",
        f"✅ Прогресс: {done_today}/{important_total or total_today or 0} за сегодня",
        f"🌞 Рутины: {routine_done_cnt}/{routine_total_cnt}",
    ]
    finance_line = await payday_summary(db, user, local_date)
    home_summary = ""
    if reg_due:
        # В кратком резюме не перечисляем все задачи по дому, чтобы не
        # дублировать список ниже в блоке «Регулярка по дому».
        total_home = len(reg_due)
        max_titles = 1 if adhd else 2
        titles = []
        for r_raw in reg_due[:max_titles]:
            title = row_to_dict(r_raw).get("title")
            if title:
                titles.append(title)
        if titles:
            listed = ", ".join(titles)
            extra = total_home - len(titles)
            extra_note = f" и ещё {extra}" if extra > 0 else ""
            home_summary = (
                f"🧹 Дом: сегодня {total_home} "
                f"{_ru_plural(total_home, 'дело', 'дела', 'дел')} — {listed}{extra_note}."
            )
        else:
            home_summary = (
                f"🧹 Дом: сегодня {total_home} "
                f"{_ru_plural(total_home, 'дело', 'дела', 'дел')} по дому."
            )
    else:
        home_summary = "🧹 Дом: на эту неделю всё чисто, можешь выдохнуть."
    if finance_line and not student:
        summary_lines.append(finance_line)
    if home_summary and not student:
        summary_lines.append(home_summary)
    meal_plan_today = await repo.get_meal_plan_for_date(db, user["id"], local_date)
    if meal_plan_today and not student:
        m_icons = {"breakfast": "🥣", "lunch": "🍲", "dinner": "🍽"}
        meal_parts = [
            ("✅" if m.get("status") == "cooked" else m_icons.get(str(m.get("meal_slot")), "🍽"))
            + f" {html.escape(str(m.get('title') or ''))}"
            for m in meal_plan_today
            if m.get("title")
        ]
        if meal_parts:
            summary_lines.append("🍽 Меню дня: " + " · ".join(meal_parts[:3]))
    if plan_items:
        summary_lines.append(
            f"🎯 План на день: выполнено {len(plan_done)}/{len(plan_items)} дел "
            f"(важных {len(important_plan)}, доп. {len(extra_plan)})."
        )
    if (soon_pantry_d or expired_pantry_d) and not student:
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
    if meds_total and not student:
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
    today_heading = "План дня" if student else f"План на {format_date_display(local_date)}"
    blocks = [f"{pause_note}{quiet_note}<b>{today_heading}</b>\n" + "\n".join(summary_lines)]
    if routine_lines:
        blocks.append("<b>🌞 Рутины:</b>\n" + "\n".join(routine_lines))
    if custom_lines and not student:
        blocks.append("<b>🔔 Свои напоминания:</b>\n" + "\n".join(custom_lines))
    if regular_lines and not student:
        blocks.append("<b>🔁 Регулярка по дому:</b>\n" + "\n".join(regular_lines))
    if bills_lines and not student:
        blocks.append("<b>📅 Счета в ближайшие дни:</b>\n" + "\n".join(bills_lines))
    if cal_events_today:
        cal_lines = []
        for ev in cal_events_today[:5]:
            when = "Весь день" if ev.get("is_all_day") else f"{ev.get('start_time')}–{ev.get('end_time')}"
            cal_lines.append(f"• {when} · {html.escape(str(ev.get('title') or ''))}")
        event_heading = "📚 Занятия и ДЗ:" if student else "🗓 Календарь на сегодня:"
        blocks.append(f"<b>{event_heading}</b>\n" + "\n".join(cal_lines))
    elif student:
        blocks.append("<b>📚 Занятия и ДЗ:</b>\nНа сегодня занятий нет.")

    desk_homework = [
        item for item in plan_items
        if str(item.get("external_key") or "").startswith("desk:hw:")
    ] if student else []
    if student and desk_homework:
        homework_lines = []
        for item in desk_homework[:5]:
            icon = "✅" if item.get("done") else "⬜️"
            title = html.escape(str(item.get("title") or ""))
            url = str(item.get("external_url") or "")
            link = f' · <a href="{html.escape(url, quote=True)}">🔗 Desk</a>' if url.startswith(("https://", "http://")) else ""
            homework_lines.append(f"{icon} {title}{link}")
        blocks.append("<b>📝 Домашние задания Desk:</b>\n" + "\n".join(homework_lines))
    if plan_items:
        # Если слишком много пунктов, прячем детали (особенно в ADHD-режиме)
        visible_plan_items = [
            item for item in plan_items
            if not (student and str(item.get("external_key") or "").startswith("desk:hw:"))
        ]
        show_details = True
        if (len(visible_plan_items) if student else len(plan_items)) > 5:
            show_details = False
        
        if show_details:
            plan_lines = ["<b>🎯 План на день — детали:</b>"]
            for item in visible_plan_items:
                icon = "✅" if item.get("done") else "⬜️"
                kind = " (важное)" if item.get("is_important") else ""
                badge = " 🎓 <b>Desk</b>" if item.get("external_key", "").startswith("desk:") else ""
                title = html.escape(str(item.get("title") or ""))
                plan_lines.append(f"{icon} {title}{kind}{badge}")
            blocks.append("\n".join(plan_lines))
        # Если скрыли, верхняя строка саммари (строки 174-178) уже покажет статус

    if student:
        if custom_lines:
            blocks.append("<b>🔔 Свои напоминания:</b>\n" + "\n".join(custom_lines))

    # Footer: очки, стрик и щадящий фокус
    footer_line = f"🎯 Очки: {points_today} сегодня / {points_week} за неделю"
    if streak > 0:
        footer_line += f" • {gentle_streak(streak)}"
    blocks.append(footer_line)

    if adhd:
        blocks.append("<i>В щадящем фокусе показываю не больше 3 пунктов, остальное — по кнопкам.</i>")

    kb_buttons = []
    if first_pending_routine_row:
        kb_buttons.append(first_pending_routine_row)
    if not student:
        kb_buttons.append(
        [
            types.InlineKeyboardButton(text="🌙 Сон и карма", callback_data="pulse:hub"),
            types.InlineKeyboardButton(text="💡 Совет сейчас", callback_data="advice:more"),
        ]
        )
    has_any_reminders = bool(custom)
    kb_buttons.append([types.InlineKeyboardButton(text="🆘 Помощь сейчас", callback_data="help:menu")])
    if has_any_reminders:
        kb_buttons.append([types.InlineKeyboardButton(text="Напоминания", callback_data="rem:list")])
    else:
        kb_buttons.append([types.InlineKeyboardButton(text="➕ Добавить напоминание", callback_data="rem:add")])
    if student:
        kb_buttons.append([types.InlineKeyboardButton(text="📚 Учёба / Desk", callback_data="desk:main")])
        kb_buttons.append([types.InlineKeyboardButton(text="☕ Кафе фокуса", callback_data="more:focus")])
        kb_buttons.append([types.InlineKeyboardButton(text="😴 Сон и пауза", callback_data="pause:menu")])
    else:
        kb_buttons.append([types.InlineKeyboardButton(text="🧹 План по дому", callback_data="home:week")])
    kb_buttons.append([types.InlineKeyboardButton(text="📅 План на день", callback_data="dplan:list")])
    if not student:
        kb_buttons.append([types.InlineKeyboardButton(text="Финансы", callback_data="money:report")])
    kb_buttons.append([types.InlineKeyboardButton(text="Мои очки", callback_data="stats:view")])
    
    # Кнопка для просмотра скрытого плана - УДАЛЕНА, так как теперь есть кнопка "План на день" выше.

    if meds_total and not student:
        kb_buttons.append(
            [types.InlineKeyboardButton(text="Таблетки", callback_data="meds:today")]
        )
    if (soon_pantry_d or expired_pantry_d) and not student:
        kb_buttons.append(
            [types.InlineKeyboardButton(text="Продукты", callback_data="pantry:expiring")]
        )
    # Быстрые кнопки для плана дня - УДАЛЕНЫ
    # Теперь все дела доступны через интерактивную кнопку "📅 План на день"
    inline_kb = types.InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    return "\n\n".join(blocks), inline_kb
