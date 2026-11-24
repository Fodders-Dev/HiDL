import calendar
import datetime
from typing import Optional, Tuple

from db import repositories as repo
from utils.time import format_date_display


def _clamp_month_day(year: int, month: int, day: int) -> datetime.date:
    last_day = calendar.monthrange(year, month)[1]
    return datetime.date(year=year, month=month, day=min(max(1, day), last_day))


def _payday_bounds(local_date: str, payday_day: int) -> Tuple[str, str, int]:
    """Return (start_iso, next_payday_iso, days_left) based on local date and payday day."""
    today = datetime.date.fromisoformat(local_date)
    day = payday_day if 1 <= payday_day <= 31 else 1
    this_month_payday = _clamp_month_day(today.year, today.month, day)
    if today >= this_month_payday:
        start = this_month_payday
        year = today.year + 1 if today.month == 12 else today.year
        month = 1 if today.month == 12 else today.month + 1
        next_payday = _clamp_month_day(year, month, day)
    else:
        month = 12 if today.month == 1 else today.month - 1
        year = today.year - 1 if today.month == 1 else today.year
        start = _clamp_month_day(year, month, day)
        next_payday = this_month_payday
    days_left = (next_payday - today).days or 0
    return start.isoformat(), next_payday.isoformat(), max(days_left, 0)


async def payday_summary(db, user: dict, local_date: str) -> Optional[str]:
    """Build short finance summary until payday for /today and money menu."""
    budget = await repo.get_budget(db, user["id"])
    if not budget or not budget["payday_day"]:
        return None
    payday_day = int(budget["payday_day"])
    start_iso, end_iso, days_left = _payday_bounds(local_date, payday_day)
    rows = await repo.expenses_between(db, user["id"], start_iso, end_iso, categories=None)
    spent = float(rows or 0)
    limit = float(budget["food_budget"] if budget["food_budget"] else (budget["monthly_limit"] or 0))
    if limit <= 0:
        return None
    raw_remaining = limit - spent
    safe_per_day = int(raw_remaining // days_left) if days_left > 0 and raw_remaining > 0 else 0
    next_display = format_date_display(end_iso)
    if raw_remaining > 0:
        return f"💰 До {next_display}: осталось {raw_remaining:.0f} ₽ из {limit:.0f} (~{safe_per_day} ₽/день)."
    if raw_remaining < 0:
        return f"💰 Бюджет превышен на {abs(raw_remaining):.0f} ₽. До {next_display} — экономим."
    return f"💰 Бюджет выбран полностью. До {next_display} — экономим."
