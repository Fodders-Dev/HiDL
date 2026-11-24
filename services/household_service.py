"""Сервис уведомлений и кооперации внутри общего дома (household)."""

from __future__ import annotations

import datetime
import html
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

if TYPE_CHECKING:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from services import karma_service, kitchen_ai
from utils.formatting import format_money
from utils.rows import rows_to_dicts
from utils.time import is_in_time_window, local_date_str

CARE_NUDGES: Dict[str, Dict[str, Any]] = {
    "water": {
        "label": "💧 Попить воды",
        "kind": "nudge_water",
        "karma": 0,
        "msg": "💧 <b>{actor}</b> нежно напоминает: пора выпить стакан воды!",
    },
    "break": {
        "label": "☕ Выдохнуть 5 мин",
        "kind": "nudge_break",
        "karma": 0,
        "msg": "☕ <b>{actor}</b> подсказывает: сделай паузу на 5 минут, потянись и отдохни.",
    },
    "hug": {
        "label": "🫂 Обнимашка",
        "kind": "nudge_hug",
        "karma": 0,
        "msg": "🫂 <b>{actor}</b> передаёт тебе тёплую обнимашку!",
    },
    "thanks": {
        "label": "💛 Спасибо (+5 кармы)",
        "kind": "nudge_thanks",
        "karma": 5,
        "msg": "💛 <b>{actor}</b> говорит огромное спасибо за заботу и помощь по дому! (+5 кармы)",
    },
}


def is_partner_quiet_or_paused(partner: Dict[str, Any], now_utc: datetime.datetime) -> bool:
    """Проверить, стоит ли сейчас воздержаться от уведомления партнёра."""
    if int(partner.get("quiet_mode") or 0):
        return True
    if partner.get("sleep_confirmed_at"):
        return True
    if karma_service.is_sick_freeze_active(partner, now_utc):
        return True
    tz = str(partner.get("timezone") or "UTC")
    local_date = local_date_str(now_utc, tz)
    pause_until = str(partner.get("pause_until") or "").strip()
    if pause_until and local_date <= pause_until:
        return True
    sleep_hhmm = str(partner.get("sleep_target_sleep") or partner.get("sleep_time") or "").strip()
    wake_hhmm = str(partner.get("sleep_target_wake") or partner.get("wake_up_time") or "").strip()
    if sleep_hhmm and wake_hhmm and is_in_time_window(now_utc, tz, sleep_hhmm, wake_hhmm):
        return True
    return False


async def notify_household_partners_detailed(
    bot,
    db,
    actor_user: Dict[str, Any],
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Бережно отправить уведомление участникам дома и вернуть статистику доставки/тихих часов."""
    actor_id = int(actor_user["id"])
    household_id = await repo.get_or_create_household(db, actor_id)
    household = await repo.get_household(db, household_id)
    if not household:
        return {"sent": 0, "skipped_quiet": [], "partners_total": 0, "notify_enabled": False}
    settings = household.get("settings") or {}
    notify_on = bool(settings.get("notify_partner", True))

    now = now_utc or datetime.datetime.utcnow()
    members = await repo.list_household_members(db, household_id)
    partners = [m for m in members if int(m["id"]) != actor_id]
    sent = 0
    skipped_quiet: List[str] = []
    if not notify_on:
        return {
            "sent": 0,
            "skipped_quiet": [],
            "partners_total": len(partners),
            "notify_enabled": False,
        }
    for member in partners:
        m_name = str(member.get("name") or f"Участник {member['id']}")
        if is_partner_quiet_or_paused(member, now):
            skipped_quiet.append(m_name)
            continue
        tg_id = member.get("telegram_id")
        if bot is None or not text or not tg_id:
            continue
        try:
            send_kwargs: Dict[str, Any] = {"parse_mode": "HTML"}
            if reply_markup is not None:
                send_kwargs["reply_markup"] = reply_markup
            await bot.send_message(int(tg_id), text, **send_kwargs)
            sent += 1
        except Exception:
            continue
    return {
        "sent": sent,
        "skipped_quiet": skipped_quiet,
        "partners_total": len(partners),
        "notify_enabled": True,
    }


async def notify_household_partners(
    bot,
    db,
    actor_user: Dict[str, Any],
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> int:
    """Бережно отправить уведомление остальным участникам дома (учитывая тихие часы и ЧП)."""
    res = await notify_household_partners_detailed(
        bot, db, actor_user, text, reply_markup=reply_markup, now_utc=now_utc
    )
    return int(res["sent"])


async def pick_dinner_poll_options(
    db,
    user: Dict[str, Any],
    *,
    exclude_ids: Optional[Iterable[str]] = None,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Подобрать 3 лучших рецепта из холодильника дома для совместного голосования."""
    uid = int(user["id"])
    profile = await kitchen_ai.get_user_meal_profile(db, uid)
    all_recipes = await kitchen_ai.load_recipes_for_user(db, uid)
    eligible = [r for r in all_recipes if kitchen_ai.recipe_allowed_for_profile(r, profile)] or list(all_recipes)
    pantry = rows_to_dicts(await repo.list_pantry_items(db, uid))
    excl = {str(x) for x in (exclude_ids or ())}

    scored: List[tuple[tuple[float, int, int, int, int], Dict[str, Any]]] = []
    for r in eligible:
        rid = str(r.get("id") or "")
        tags = set(r.get("tags") or [])
        match = kitchen_ai.match_recipe_to_pantry(r, pantry)
        cov = float(match.get("coverage") or 0.0)
        miss_cnt = len(match.get("missing") or [])
        t_min = int(r.get("time_minutes") or 40)
        slot_bonus = 1 if ("dinner" in tags or "lunch" in tags) else 0
        quick_bonus = 1 if ("quick" in tags or t_min <= 30) else 0
        opt = {
            "id": rid,
            "title": str(r.get("title") or "Блюдо"),
            "time_min": t_min,
            "coverage_pct": int(round(cov * 100)),
            "missing_count": miss_cnt,
        }
        scored.append(((cov, slot_bonus, quick_bonus, -miss_cnt, -t_min), opt))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    fresh = [opt for _, opt in scored if opt["id"] not in excl]
    if len(fresh) >= limit:
        return fresh[:limit]
    seen = {o["id"] for o in fresh}
    for _, opt in scored:
        if opt["id"] not in seen:
            fresh.append(opt)
            seen.add(opt["id"])
        if len(fresh) >= limit:
            break
    return fresh[:limit]


def format_dinner_poll_card(
    poll: Dict[str, Any], members: List[Dict[str, Any]]
) -> tuple[str, InlineKeyboardMarkup]:
    """Сформировать текст и клавиатуру голосования «Что сегодня готовим?»."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    options: List[Dict[str, Any]] = list(poll.get("options") or [])
    votes: Dict[str, int] = dict(poll.get("votes") or {})
    status = str(poll.get("status") or "open")
    pid = int(poll["id"])
    member_names = {str(m["id"]): str(m.get("name") or f"Участник {m['id']}") for m in members}
    voters_by_idx: Dict[int, List[str]] = {i: [] for i in range(len(options))}
    for uid_str, opt_idx in votes.items():
        if int(opt_idx) in voters_by_idx:
            voters_by_idx[int(opt_idx)].append(member_names.get(str(uid_str), f"Участник {uid_str}"))

    num_icons = ("1️⃣", "2️⃣", "3️⃣", "4️⃣")
    lines = ["🍳 <b>Что сегодня готовим?</b>"]
    if len(members) < 2:
        lines.append("<i>Можно выбирать для себя или пригласить партнёра в «Наш дом», чтобы голосовать вдвоём.</i>")
    else:
        lines.append("Выберите вариант — как только голоса совпадут, блюдо зафиксируется автоматически!")
    lines.append("")

    winner_idx: Optional[int] = None
    winner_id = str(poll.get("winner_recipe_id") or "")
    for i, opt in enumerate(options):
        if winner_id and str(opt.get("id")) == winner_id:
            winner_idx = i
        cov_pct = int(opt.get("coverage_pct") or 0)
        miss = int(opt.get("missing_count") or 0)
        pantry_txt = "всё есть дома ✅" if miss == 0 else f"дома ~{cov_pct}% (не хватает {miss})"
        t_min = int(opt.get("time_min") or 0)
        time_txt = f" · {t_min} мин" if t_min else ""
        voters = voters_by_idx.get(i) or []
        vote_txt = f" — 👍 {', '.join(html.escape(v) for v in voters)}" if voters else ""
        mark = " 🏆" if winner_idx == i and status == "decided" else ""
        lines.append(
            f"{num_icons[i]} <b>{html.escape(str(opt.get('title') or ''))}</b>{mark}\n"
            f"   <i>{pantry_txt}{time_txt}</i>{vote_txt}"
        )

    rows: List[List[InlineKeyboardButton]] = []
    if status == "decided" and winner_idx is not None:
        win_opt = options[winner_idx]
        lines.append("")
        lines.append(f"🎉 <b>Решено! Готовим:</b> {html.escape(str(win_opt.get('title') or ''))}")
        rows.append(
            [
                InlineKeyboardButton(
                    text="📖 Открыть рецепт",
                    callback_data=f"kitchen:cook_view:{win_opt['id']}:2:all:0",
                )
            ]
        )
    else:
        vote_row = [
            InlineKeyboardButton(text=num_icons[i], callback_data=f"hh:vote:{pid}:{i}")
            for i in range(len(options))
        ]
        if vote_row:
            rows.append(vote_row)
        # Button to lock the leading or first option immediately
        best_idx = max(range(len(options)), key=lambda idx: len(voters_by_idx.get(idx) or [])) if options else 0
        if options:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"✅ Берём вариант {best_idx + 1}",
                        callback_data=f"hh:vote:pick:{pid}:{best_idx}",
                    )
                ]
            )

    rows.append(
        [
            InlineKeyboardButton(text="🎲 Другие 3", callback_data=f"hh:vote:reroll:{pid}"),
            InlineKeyboardButton(text="⬅️ Наш дом", callback_data="hh:hub"),
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def send_care_nudge(
    bot,
    db,
    actor_user: Dict[str, Any],
    nudge_key: str,
    *,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Отправить заботливый пинг или благодарность (+5 кармы) с защитой от спама и тихими часами."""
    spec = CARE_NUDGES.get(nudge_key)
    if not spec:
        return {"ok": False, "error": "unknown_nudge"}
    now = now_utc or datetime.datetime.utcnow()
    actor_id = int(actor_user["id"])
    actor_name = str(actor_user.get("name") or "Партнёр")
    hid = await repo.get_or_create_household(db, actor_id)

    if await repo.recent_household_nudge_exists(
        db, hid, actor_id, spec["kind"], within_hours=2, now_utc=now
    ):
        return {"ok": False, "cooldown": True, "label": spec["label"]}

    members = await repo.list_household_members(db, hid)
    partners = [m for m in members if int(m["id"]) != actor_id]
    await repo.create_household_request(
        db,
        hid,
        actor_id,
        actor_name,
        spec["label"],
        kind=spec["kind"],
        status="sent",
        now_utc=now,
    )

    karma_delta = int(spec.get("karma") or 0)
    if karma_delta > 0:
        for p in partners:
            await karma_service.record_karma_event(
                db,
                p,
                domain="home",
                event_type="partner_thanks",
                delta=karma_delta,
                reason=f"Спасибо по дому от {actor_name}",
                now_utc=now,
            )

    text = spec["msg"].format(actor=html.escape(actor_name))
    notify = await notify_household_partners_detailed(bot, db, actor_user, text, now_utc=now)
    return {
        "ok": True,
        "cooldown": False,
        "label": spec["label"],
        "karma_given": karma_delta if partners else 0,
        **notify,
    }


async def build_household_weekly_summary(
    db, user: Dict[str, Any], *, now_utc: Optional[datetime.datetime] = None
) -> str:
    """Собрать тёплый итог недели для участников дома."""
    now = now_utc or datetime.datetime.utcnow()
    if now.tzinfo is not None:
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    uid = int(user["id"])
    tz = str(user.get("timezone") or "UTC")
    local_today = local_date_str(now, tz)
    today_dt = datetime.date.fromisoformat(local_today)
    since_date = (today_dt - datetime.timedelta(days=6)).isoformat()
    since_utc_iso = (now - datetime.timedelta(days=7)).isoformat()
    ym = today_dt.strftime("%Y-%m")

    hid = await repo.get_or_create_household(db, uid)
    members = await repo.list_household_members(db, hid)
    act = await repo.count_household_weekly_activity(
        db, hid, since_date=since_date, since_utc_iso=since_utc_iso
    )
    supplies = rows_to_dicts(await repo.list_supplies(db, uid))
    low_supplies = [s["name"] for s in supplies if (s.get("status") or "full") in {"low", "empty"}]
    soon_pantry, expired_pantry = await repo.pantry_expiring(db, uid, local_today, window_days=3)
    month_exp = await repo.household_monthly_expenses_summary(db, hid, year_month=ym)

    lines = [
        "📊 <b>Итог недели нашего дома</b>",
        f"👥 Участники: {', '.join(html.escape(str(m.get('name') or '')) for m in members)}",
        "",
        f"🧹 Закрыто домашних дел за 7 дней: <b>{act['done_tasks_total']}</b>",
    ]
    if act["done_tasks_by_actor"]:
        parts = [f"{html.escape(name)}: {cnt}" for name, cnt in act["done_tasks_by_actor"].items()]
        lines.append("   • Вклад: " + " · ".join(parts))
    lines.append(
        f"💌 Просьбы дома: выполнено <b>{act['done_requests']}</b>, открыто <b>{act['open_requests']}</b>"
    )
    lines.append(
        f"💛 Забота и «спасибо» друг другу: <b>{act['nudges_count']}</b> (благодарностей: {act['thanks_count']})"
    )
    lines.append(
        f"💰 Общие траты за 7 дней: <b>{format_money(act['expenses_7d'])} ₽</b> "
        f"(за месяц: {format_money(month_exp.get('total') or 0)} ₽)"
    )
    if low_supplies:
        lines.append(f"🧴 Кончаются расходники ({len(low_supplies)}): {', '.join(html.escape(x) for x in low_supplies[:4])}")
    else:
        lines.append("🧴 Бытовая химия и расходники в порядке ✅")
    exp_cnt = len(soon_pantry) + len(expired_pantry)
    if exp_cnt:
        lines.append(f"🧊 Продукты с истекающим сроком: <b>{exp_cnt}</b> — стоит заглянуть в «Что приготовить сейчас?»")
    else:
        lines.append("🧊 В холодильнике без срочной просрочки ✅")
    return "\n".join(lines)

