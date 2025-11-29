import datetime
from typing import Optional, Tuple

from db import repositories as repo
from utils.time import format_date_display


def _payday_bounds(local_date: str, payday_day: int) -> Tuple[str, str, int]:
    """Return (start_iso, next_payday_iso, days_left) based on local date and payday day."""
    today = datetime.date.fromisoformat(local_date)
    day = payday_day if 1 <= payday_day <= 31 else 1
    # determine current period start
    if today.day >= day:
        start = datetime.date(year=today.year, month=today.month, day=day)
        # next month payday
        year = today.year + 1 if today.month == 12 else today.year
        month = 1 if today.month == 12 else today.month + 1
        next_payday = datetime.date(year=year, month=month, day=min(day, (datetime.date(year, month, 1) - datetime.timedelta(days=1)).day))
    else:
        # period started previous month
        month = 12 if today.month == 1 else today.month - 1
        year = today.year - 1 if today.month == 1 else today.year
        start = datetime.date(year=year, month=month, day=min(day, (datetime.date(today.year, today.month, 1) - datetime.timedelta(days=1)).day))
        next_payday = datetime.date(year=today.year, month=today.month, day=min(day, (datetime.date(today.year, today.month, 1) - datetime.timedelta(days=1)).day))
    days_left = (next_payday - today).days or 0
    # include today in range
    return start.isoformat(), next_payday.isoformat(), max(days_left, 0)


async def payday_summary(db, user: dict, local_date: str) -> Optional[str]:
    """Build short finance summary until payday for /today and money menu."""
    budget = await repo.get_budget(db, user["id"])
    if not budget or not budget["payday_day"]:
        return None
    payday_day = int(budget["payday_day"])
    start_iso, end_iso, days_left = _payday_bounds(local_date, payday_day)
    # filter categories for еды/быта если есть
    rows = await repo.expenses_between(db, user["id"], start_iso, end_iso, categories=None)
    spent = rows
    limit = budget["food_budget"] if budget["food_budget"] else budget["monthly_limit"]
    if limit is None:
        limit = 0
    remaining = max(0, limit - spent) if limit else 0
    safe_per_day = int(remaining // days_left) if days_left > 0 and limit else 0
    next_display = format_date_display(end_iso)
    if limit > 0:
        if remaining > 0:
            return f"💰 До {next_display}: осталось {remaining:.0f} ₽ из {limit:.0f} (~{safe_per_day} ₽/день)."
        else:
            return f"💰 Бюджет выбрали на {abs(remaining):.0f} ₽. До {next_display} — экономим."
    return None
