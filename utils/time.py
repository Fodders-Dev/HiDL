import datetime
import re
from typing import Optional
from zoneinfo import ZoneInfo


_UTC_OFFSET_RE = re.compile(r"^UTC\s*([+-])\s*(\d{1,2})(?::(\d{2}))?$", re.IGNORECASE)


def parse_hhmm(value: str) -> Optional[datetime.time]:
    """Parse time string HH:MM."""
    try:
        return datetime.datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return None


def _parse_utc_offset_delta(tz: str) -> Optional[datetime.timedelta]:
    raw = (tz or "").strip()
    if raw.upper() == "UTC":
        return datetime.timedelta(0)
    m = _UTC_OFFSET_RE.match(raw)
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    hours = int(m.group(2))
    minutes = int(m.group(3) or 0)
    if hours > 14 or minutes >= 60 or (hours == 14 and minutes > 0):
        return None
    return datetime.timedelta(hours=sign * hours, minutes=sign * minutes)


def tzinfo_from_string(tz: str) -> datetime.tzinfo:
    """Return tzinfo from IANA name or UTC offset like UTC+3 or UTC+3:30."""
    raw = (tz or "").strip()
    if not raw:
        return datetime.timezone.utc
    delta = _parse_utc_offset_delta(raw)
    if delta is not None:
        return datetime.timezone.utc if delta == datetime.timedelta(0) else datetime.timezone(delta)
    try:
        return ZoneInfo(raw)
    except Exception:
        return datetime.timezone.utc


def is_valid_timezone(tz: str) -> bool:
    """Validate timezone string."""
    raw = (tz or "").strip()
    if not raw:
        return False
    if raw.upper().startswith("UTC"):
        return _parse_utc_offset_delta(raw) is not None
    try:
        ZoneInfo(raw)
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


def is_in_time_window(
    now_utc: datetime.datetime,
    tz_str: str,
    start_hhmm: str,
    end_hhmm: str,
) -> bool:
    """Return True if local time falls inside [start_hhmm, end_hhmm) (supports wrap past midnight)."""
    start_t = parse_hhmm(start_hhmm or "")
    end_t = parse_hhmm(end_hhmm or "")
    if not start_t or not end_t:
        return False
    tzinfo = tzinfo_from_string(tz_str or "UTC")
    local_dt = now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
    local_min = local_dt.hour * 60 + local_dt.minute
    start_min = start_t.hour * 60 + start_t.minute
    end_min = end_t.hour * 60 + end_t.minute
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= local_min < end_min
    return local_min >= start_min or local_min < end_min
