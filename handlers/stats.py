import datetime
from collections import defaultdict

from aiogram import Router, types
from aiogram.filters import Command

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.time import local_date_str
from utils.tone import tone_message
from utils.time import format_date_display
from utils.user import ensure_user
from utils.texts import gentle_streak
from utils.affirmations import random_affirmation_text

router = Router()


def _aggregate(rows):
    by_date = defaultdict(lambda: {"done": 0, "total": 0})
    for row in rows:
        r = dict(row)
        date = r["routine_date"] if "routine_date" in r.keys() else r["reminder_date"]
        status = r["status"]
        count = r["cnt"]
        by_date[date]["total"] += count
        if status == "done":
            by_date[date]["done"] += count
    return by_date


def _streak(by_date, today_str: str) -> int:
    """Count consecutive days backwards where all tasks done (done==total>0)."""
    streak = 0
    today = datetime.date.fromisoformat(today_str)
    while True:
        day = today - datetime.timedelta(days=streak)
        day_str = day.isoformat()
        stats = by_date.get(day_str)
        if not stats:
            break
        if stats["total"] == 0 or stats["done"] < stats["total"]:
            break
        streak += 1
    return streak


@router.message(Command("stats"))
async def stats(message: types.Message, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)

    now_utc = datetime.datetime.utcnow()
    local_today = local_date_str(now_utc, user["timezone"])

    routine_rows = await repo.routine_stats(db, user["id"], days=7)
    custom_rows = await repo.custom_stats(db, user["id"], days=7)

    routine_by_date = _aggregate(routine_rows)
    custom_by_date = _aggregate(custom_rows)

    def lines(by_date, label):
        if not by_date:
            return [f"{label}: нет данных за последние 7 дней."]
        out = []
        for date in sorted(by_date.keys(), reverse=True):
            d = by_date[date]
            out.append(f"{format_date_display(date)}: {d['done']}/{d['total']}")
        return out

    routine_streak = _streak(routine_by_date, local_today)
    custom_streak = _streak(custom_by_date, local_today)

    routine_summary = lines(routine_by_date, "Рутины")
    custom_summary = lines(custom_by_date, "Свои напоминания")

    routine_total = sum(v["total"] for v in routine_by_date.values())
    routine_done = sum(v["done"] for v in routine_by_date.values())
    custom_total = sum(v["total"] for v in custom_by_date.values())
    custom_done = sum(v["done"] for v in custom_by_date.values())
    today_points = await repo.points_today(db, user["id"], local_date=local_today)
    points7 = await repo.points_window(db, user["id"], days=7)
    home_cnt, home_pts = await repo.home_stats_window(db, user["id"], days=7)
    user_full = await repo.get_user(db, user["id"])
    points_month = user_full["points_month"]
    points_total = user_full["points_total"]

    achievements = []
    if routine_streak >= 3:
        achievements.append("🔥 Стрик рутин 3+ дней")
    if routine_streak >= 7:
        achievements.append("🏅 Стрик рутин 7+ дней")
    if custom_done >= 5:
        achievements.append("✅ Свои напоминания: 5+ выполнено за неделю")
    if (routine_done + custom_done) >= 10:
        achievements.append("🎯 10+ задач закрыто за неделю")

    text = (
        "Статистика за 7 дней:\n\n"
        f"Рутины: {routine_done}/{routine_total} (стрик полных дней: {routine_streak})\n"
        + "\n".join(routine_summary)
        + "\n\n"
        f"Свои напоминания: {custom_done}/{custom_total} (стрик: {custom_streak})\n"
        + "\n".join(custom_summary)
    )
    text += f"\n\nОчки: сегодня — {today_points}, за 7 дней — {points7}, за месяц — {points_month}, всего — {points_total}"
    text += f"\nДом: за 7 дней {home_cnt} дел, очков {home_pts}. "
    if home_cnt == 0:
        text += "Если не до уборки — нормально. Можно начать с одного пункта."
    elif home_cnt < 4:
        text += "Даже пара дел в неделю — это движение, квартира уже легче дышит."
    else:
        text += "Отличный темп — квартира точно благодарит."
    text += "\n\n" + gentle_streak(routine_streak)
    if achievements:
        text += "\n\nАчивки:\n" + "\n".join(f"- {a}" for a in achievements)
    tone = "neutral"
    wellness = await repo.get_wellness(db, user["id"])
    if wellness:
        tone = wellness["tone"]
    if points7 < 10:
        tone = "soft"
    elif points7 > 40:
        tone = "pushy"
    # Иногда добавляем аффирмацию поддержки
    extra = None
    if points7 < 10 or routine_streak <= 1:
        extra = random_affirmation_text("self_worth")
    elif routine_streak >= 7:
        extra = random_affirmation_text("motivation")
    if extra:
        text += f"\n\n<i>{extra}</i>"
    await message.answer(tone_message(tone, text), reply_markup=main_menu_keyboard())


@router.callback_query(lambda c: c.data == "stats:view")
async def stats_view(callback: types.CallbackQuery, db) -> None:
    await stats(callback.message, db)
    await callback.answer()


@router.message(Command("weekly_report"))
async def weekly_report(message: types.Message, db) -> None:
    """Сводка за 7 дней: рутины/напоминания + деньги и лимиты."""
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)

    now_utc = datetime.datetime.utcnow()
    local_today = local_date_str(now_utc, user["timezone"])

    # Задачи
    routine_rows = await repo.routine_stats(db, user["id"], days=7)
    custom_rows = await repo.custom_stats(db, user["id"], days=7)
    routine_by_date = _aggregate(routine_rows)
    custom_by_date = _aggregate(custom_rows)
    routine_done = sum(v["done"] for v in routine_by_date.values())
    routine_total = sum(v["total"] for v in routine_by_date.values())
    custom_done = sum(v["done"] for v in custom_by_date.values())
    custom_total = sum(v["total"] for v in custom_by_date.values())
    routine_streak = _streak(routine_by_date, local_today)

    # Деньги
    expenses = await repo.expenses_last_days(db, user["id"], days=7)
    per_cat = defaultdict(float)
    total = 0.0
    for e in expenses:
        per_cat[e["category"]] += e["amount"]
        total += e["amount"]
    budget = await repo.get_budget(db, user["id"])
    month_total = await repo.monthly_expense_sum(db, user["id"])
    cat_limits = await repo.list_budget_categories(db, user["id"])
    cat_lines = []
    for c in cat_limits:
        spent_cat = await repo.category_expense_sum(db, user["id"], c["category"], days=30)
        cat_lines.append(f"{c['category']}: {spent_cat:.0f}/{c['limit_amount']:.0f}")

    text = (
        "Сводка за 7 дней:\n"
        f"Рутины: {routine_done}/{routine_total} (стрик полных дней: {routine_streak})\n"
        f"Свои напоминания: {custom_done}/{custom_total}\n"
        "\nДеньги за 7 дней:\n"
        + ("\n".join(f"- {cat}: {amt:.0f}" for cat, amt in per_cat.items()) if per_cat else "нет записей")
        + f"\nМесяц: {month_total:.0f}"
    )
    if budget and budget["monthly_limit"] > 0:
        text += f" / лимит {budget['monthly_limit']:.0f}"
    if cat_lines:
        text += "\nКатегорные лимиты:\n" + "\n".join(cat_lines)
    text += "\n\n➕ Записать трату — кнопкой ниже."

    tone = "neutral"
    wellness = await repo.get_wellness(db, user["id"])
    if wellness:
        tone = wellness["tone"]
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="➕ Записать трату", callback_data="money:spent")]
        ]
    )
    await message.answer(tone_message(tone, text), reply_markup=kb)
