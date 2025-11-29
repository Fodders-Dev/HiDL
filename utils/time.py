import datetime
from typing import Optional
from zoneinfo import ZoneInfo


def parse_hhmm(value: str) -> Optional[datetime.time]:
    """Parse time string HH:MM."""
    try:
        return datetime.datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return None


def tzinfo_from_string(tz: str) -> datetime.tzinfo:
    """Return tzinfo from IANA name or UTC offset like UTC+3."""
    if tz.upper() == "UTC":
        return datetime.timezone.utc
    if tz.upper().startswith("UTC"):
        sign = 1 if "+" in tz else -1
        try:
            hours = int(tz.split("+")[-1]) if "+" in tz else int(tz.split("-")[-1])
            return datetime.timezone(datetime.timedelta(hours=sign * hours))
        except Exception:
            return datetime.timezone.utc
    try:
        return ZoneInfo(tz)
    except Exception:
        return datetime.timezone.utc


def is_valid_timezone(tz: str) -> bool:
    """Validate timezone string."""
    if tz.upper() == "UTC":
        return True
    if tz.upper().startswith("UTC"):
        try:
            int(tz.split("+")[-1]) if "+" in tz else int(tz.split("-")[-1])
            return True
        except Exception:
            return False
    try:
        ZoneInfo(tz)
        return True
    except Exception:
        return False


def local_date_str(now_utc: datetime.datetime, tz_str: str) -> str:
    """Return YYYY-MM-DD string in user's timezone (для расчётов и БД)."""
    tzinfo = tzinfo_from_string(tz_str)
    local = now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
    return local.date().isoformat()


def should_trigger(
    now_utc: datetime.datetime, tz_str: str, target_hhmm: str, window_minutes: int = 2
) -> bool:
    """Check if local time reached target HH:MM within forward window."""
    target_time = parse_hhmm(target_hhmm)
    if not target_time:
        return False

    tzinfo = tzinfo_from_string(tz_str)
    local = now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
    target = datetime.datetime.combine(local.date(), target_time, tzinfo)
    delta = (local - target).total_seconds()
    return 0 <= delta <= window_minutes * 60


def format_time_local(now_utc: datetime.datetime, tz_str: str) -> str:
    """Return HH:MM string in user's timezone."""
    tzinfo = tzinfo_from_string(tz_str)
    local = now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
    return local.strftime("%H:%M")


def local_date_plus_days(now_utc: datetime.datetime, tz_str: str, days: int) -> str:
    """Return local date string offset by given days (YYYY-MM-DD)."""
    tzinfo = tzinfo_from_string(tz_str)
    local = now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
    target = local.date() + datetime.timedelta(days=days)
    return target.isoformat()


def format_date_display(date_str: str) -> str:
    """Format YYYY-MM-DD -> DD.MM.YYYY for отображения."""
    try:
        d = datetime.date.fromisoformat(date_str)
        return f"{d.day:02d}.{d.month:02d}.{d.year}"
    except Exception:
        return date_str
