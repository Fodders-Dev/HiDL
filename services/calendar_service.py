"""Сервис внешнего календаря HiDL: .ics синхронизация, Google OAuth 2.0, пред-событийные алерты и защита встреч."""

from __future__ import annotations

import calendar as py_calendar
import datetime
import hashlib
import html
import ipaddress
import re
import secrets
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import aiohttp

from config import GoogleCalendarSettings, get_gcal_settings
from db import repositories as repo
from utils.rows import rows_to_dicts
from utils.sender import safe_delete_or_clear_markup
from utils.time import format_time_local, local_date_str, parse_hhmm, tzinfo_from_string

_MAX_ICS_BYTES = 5 * 1024 * 1024
_MAX_RRULE_OCCURRENCES = 500
_ICS_FETCHER = Callable[[str], Awaitable[str]]
_OAUTH_EXCHANGER = Callable[[Dict[str, str]], Awaitable[Dict[str, Any]]]
_GCAL_API_GETTER = Callable[[str, str, Dict[str, str]], Awaitable[Dict[str, Any]]]
_GCAL_API_POSTER = Callable[[str, str, Dict[str, Any]], Awaitable[Dict[str, Any]]]

_WEEKDAY_MAP = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}

_TZ_ALIASES = {
    "russian standard time": "Europe/Moscow",
    "moscow standard time": "Europe/Moscow",
    "utc+03": "Europe/Moscow",
    "gmt+03:00": "Europe/Moscow",
    "coordinated universal time": "UTC",
}


def _naive_utc(dt: Optional[datetime.datetime] = None) -> datetime.datetime:
    if dt is None:
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


def validate_ics_url(url: str) -> Tuple[bool, str]:
    """Проверить URL .ics календаря с защитой от SSRF (только публичный HTTPS)."""
    raw = (url or "").strip()
    if not raw:
        return False, "Ссылка пустая."
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https":
        return False, "Нужна безопасная ссылка, начинающаяся с https://."
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "В ссылке не указан домен."
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".arpa")):
        return False, "Локальные и внутренние адреса запрещены."
    try:
        ip_obj = ipaddress.ip_address(host)
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_reserved
            or ip_obj.is_multicast
            or ip_obj.is_unspecified
        ):
            return False, "Локальные и приватные IP-адреса запрещены."
    except ValueError:
        # Обычное доменное имя (не IP-литерал)
        if "." not in host:
            return False, "Укажи корректный публичный домен календаря."
    return True, ""


def _resolve_ics_tz(tzid: str, default_tz: str) -> datetime.tzinfo:
    clean = (tzid or "").strip().strip('"')
    if not clean:
        return tzinfo_from_string(default_tz)
    alias = _TZ_ALIASES.get(clean.lower())
    if alias:
        return tzinfo_from_string(alias)
    return tzinfo_from_string(clean)


def _unescape_ics_text(val: str) -> str:
    s = str(val or "")
    s = s.replace("\\n", "\n").replace("\\N", "\n")
    s = s.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    return s.strip()


def _parse_ics_datetime(
    value: str,
    params: Dict[str, str],
    target_tz_str: str,
) -> Tuple[Optional[datetime.datetime], Optional[datetime.date], bool]:
    """Разобрать значение DTSTART/DTEND/UNTIL/EXDATE в осознанный datetime или date."""
    raw = (value or "").strip()
    if not raw:
        return None, None, False
    val_type = params.get("VALUE", "").upper()
    if val_type == "DATE" or (len(raw) == 8 and raw.isdigit()):
        try:
            d = datetime.datetime.strptime(raw[:8], "%Y%m%d").date()
            return None, d, True
        except ValueError:
            return None, None, False

    target_tz = tzinfo_from_string(target_tz_str)
    is_utc = raw.endswith("Z")
    core = raw[:-1] if is_utc else raw
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            naive = datetime.datetime.strptime(core, fmt)
            if is_utc:
                return naive.replace(tzinfo=datetime.timezone.utc), None, False
            tzid = params.get("TZID", "")
            tz = _resolve_ics_tz(tzid, target_tz_str) if tzid else target_tz
            return naive.replace(tzinfo=tz), None, False
        except ValueError:
            continue
    return None, None, False


def _parse_ics_duration(raw: str) -> datetime.timedelta:
    m = re.match(
        r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$",
        (raw or "").strip().upper(),
    )
    if not m:
        return datetime.timedelta(hours=1)
    sign = -1 if m.group(1) == "-" else 1
    weeks = int(m.group(2) or 0)
    days = int(m.group(3) or 0)
    hours = int(m.group(4) or 0)
    mins = int(m.group(5) or 0)
    secs = int(m.group(6) or 0)
    return sign * datetime.timedelta(weeks=weeks, days=days, hours=hours, minutes=mins, seconds=secs)


def _unfold_ics_lines(ics_text: str) -> List[str]:
    normalized = (ics_text or "").replace("\r\n", "\n").replace("\r", "\n")
    unfolded = []
    for line in normalized.split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _parse_ics_line(line: str) -> Tuple[str, Dict[str, str], str]:
    if ":" not in line:
        return "", {}, ""
    left, value = line.split(":", 1)
    parts = left.split(";")
    name = parts[0].strip().upper()
    params: Dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().upper()] = v.strip().strip('"')
    return name, params, value.strip()


def _expand_rrule_dates(
    start_val: Any,  # datetime.date или aware datetime.datetime
    is_all_day: bool,
    rrule_str: str,
    window_start: datetime.date,
    window_end: datetime.date,
    target_tz_str: str,
) -> List[Any]:
    parts: Dict[str, str] = {}
    for chunk in (rrule_str or "").split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip().upper()] = v.strip().upper()

    freq = parts.get("FREQ", "")
    if freq not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
        return [start_val]

    try:
        interval = max(1, int(parts.get("INTERVAL", "1")))
    except ValueError:
        interval = 1

    count: Optional[int] = None
    if "COUNT" in parts:
        try:
            count = max(1, min(1000, int(parts["COUNT"])))
        except ValueError:
            count = None

    until_dt: Optional[datetime.datetime] = None
    until_date: Optional[datetime.date] = None
    if "UNTIL" in parts:
        u_dt, u_d, _ = _parse_ics_datetime(parts["UNTIL"], {}, target_tz_str)
        if u_dt is not None:
            until_dt = u_dt
            until_date = u_dt.astimezone(tzinfo_from_string(target_tz_str)).date()
        elif u_d is not None:
            until_date = u_d

    byday_list: List[int] = []
    if "BYDAY" in parts:
        for tok in parts["BYDAY"].split(","):
            code = tok.strip()[-2:]
            if code in _WEEKDAY_MAP:
                byday_list.append(_WEEKDAY_MAP[code])

    out: List[Any] = []
    produced = 0

    def _within_until(cand: Any) -> bool:
        if is_all_day:
            c_date = cand
            if until_date is not None and c_date > until_date:
                return False
            return True
        c_dt = cand
        if until_dt is not None and c_dt > until_dt:
            return False
        if until_date is not None and c_dt.date() > until_date:
            return False
        return True

    def _cand_date(cand: Any) -> datetime.date:
        if is_all_day:
            return cand
        return cand.astimezone(tzinfo_from_string(target_tz_str)).date()

    # Защита от слишком старых рядов без COUNT: перематываем быстро только для DAILY/WEEKLY без BYDAY
    step_idx = 0
    if freq == "DAILY" and count is None:
        s_date = _cand_date(start_val)
        if s_date < window_start - datetime.timedelta(days=30):
            days_diff = (window_start - datetime.timedelta(days=7) - s_date).days
            step_idx = max(0, days_diff // interval)

    if freq == "WEEKLY":
        target_days = sorted(set(byday_list)) if byday_list else [start_val.weekday()]
        # Начало недели понедельника от start_val
        s_date = start_val if is_all_day else start_val.date()
        week_anchor = s_date - datetime.timedelta(days=s_date.weekday())
        week_idx = 0
        if count is None and s_date < window_start - datetime.timedelta(days=30):
            days_diff = (window_start - datetime.timedelta(days=14) - week_anchor).days
            week_idx = max(0, days_diff // (7 * interval))
        while week_idx < _MAX_RRULE_OCCURRENCES * 4:
            cur_week_mon = week_anchor + datetime.timedelta(weeks=week_idx * interval)
            if cur_week_mon > window_end + datetime.timedelta(days=7):
                break
            for wd in target_days:
                cand_d = cur_week_mon + datetime.timedelta(days=wd)
                cand = (
                    cand_d
                    if is_all_day
                    else datetime.datetime(
                        cand_d.year,
                        cand_d.month,
                        cand_d.day,
                        start_val.hour,
                        start_val.minute,
                        start_val.second,
                        tzinfo=start_val.tzinfo,
                    )
                )
                if cand < start_val:
                    continue
                if not _within_until(cand):
                    return out
                produced += 1
                if count is not None and produced > count:
                    return out
                if _cand_date(cand) >= window_start - datetime.timedelta(days=1):
                    out.append(cand)
                if len(out) >= _MAX_RRULE_OCCURRENCES:
                    return out
            week_idx += 1
        return out

    while step_idx < _MAX_RRULE_OCCURRENCES * 4:
        if freq == "DAILY":
            cand = start_val + datetime.timedelta(days=step_idx * interval)
        elif freq == "MONTHLY":
            s_d = start_val if is_all_day else start_val.date()
            total_months = (s_d.year * 12 + (s_d.month - 1)) + step_idx * interval
            y, m0 = divmod(total_months, 12)
            m = m0 + 1
            last_day = py_calendar.monthrange(y, m)[1]
            if s_d.day > last_day:
                step_idx += 1
                continue
            cand_d = datetime.date(y, m, s_d.day)
            cand = cand_d if is_all_day else datetime.datetime.combine(cand_d, start_val.timetz())
        else:  # YEARLY
            s_d = start_val if is_all_day else start_val.date()
            y = s_d.year + step_idx * interval
            try:
                cand_d = datetime.date(y, s_d.month, s_d.day)
            except ValueError:
                step_idx += 1
                continue
            cand = cand_d if is_all_day else datetime.datetime.combine(cand_d, start_val.timetz())

        if not _within_until(cand):
            break
        produced += 1
        if count is not None and produced > count:
            break
        cd = _cand_date(cand)
        if cd > window_end + datetime.timedelta(days=2):
            break
        if cd >= window_start - datetime.timedelta(days=1):
            out.append(cand)
        if len(out) >= _MAX_RRULE_OCCURRENCES:
            break
        step_idx += 1

    return out


def parse_ics_events(
    ics_text: str,
    tz_str: str,
    window_start: datetime.date,
    window_end: datetime.date,
) -> List[Dict[str, Any]]:
    """Разобрать события RFC 5545 .ics (включая TZID, all-day, RRULE и EXDATE) в окне дат."""
    lines = _unfold_ics_lines(ics_text)
    target_tz = tzinfo_from_string(tz_str or "UTC")

    raw_events: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    in_alarm = False

    for line in lines:
        up = line.strip().upper()
        if up == "BEGIN:VEVENT":
            cur = {"exdates": []}
            in_alarm = False
            continue
        if up == "END:VEVENT":
            if cur is not None:
                raw_events.append(cur)
            cur = None
            in_alarm = False
            continue
        if cur is None:
            continue
        if up == "BEGIN:VALARM":
            in_alarm = True
            continue
        if up == "END:VALARM":
            in_alarm = False
            continue
        if in_alarm:
            continue

        name, params, val = _parse_ics_line(line)
        if not name:
            continue
        if name == "EXDATE":
            for part in val.split(","):
                cur["exdates"].append((part.strip(), params))
        elif name in {"UID", "SUMMARY", "LOCATION", "DESCRIPTION", "STATUS", "TRANSP", "RRULE", "DURATION"}:
            cur[name] = val
        elif name in {"DTSTART", "DTEND"}:
            cur[name] = (val, params)

    normalized: List[Dict[str, Any]] = []
    for ev in raw_events:
        if str(ev.get("STATUS") or "").strip().upper() == "CANCELLED":
            continue
        if "DTSTART" not in ev:
            continue

        st_val, st_params = ev["DTSTART"]
        st_dt, st_date, is_all_day = _parse_ics_datetime(st_val, st_params, tz_str or "UTC")
        if st_dt is None and st_date is None:
            continue

        duration: Optional[datetime.timedelta] = None
        en_dt: Optional[datetime.datetime] = None
        en_date: Optional[datetime.date] = None
        if "DTEND" in ev:
            ev_en_val, ev_en_params = ev["DTEND"]
            en_dt, en_date, _ = _parse_ics_datetime(ev_en_val, ev_en_params, tz_str or "UTC")
        elif "DURATION" in ev:
            duration = _parse_ics_duration(str(ev["DURATION"]))

        if is_all_day:
            assert st_date is not None
            if en_date is not None:
                span_days = max(1, (en_date - st_date).days)
            elif duration is not None:
                span_days = max(1, duration.days or 1)
            else:
                span_days = 1
        else:
            assert st_dt is not None
            if en_dt is not None and en_dt > st_dt:
                duration = en_dt - st_dt
            elif duration is None or duration.total_seconds() <= 0:
                duration = datetime.timedelta(hours=1)

        title = _unescape_ics_text(str(ev.get("SUMMARY") or "")) or "Событие"
        location = _unescape_ics_text(str(ev.get("LOCATION") or ""))
        description = _unescape_ics_text(str(ev.get("DESCRIPTION") or ""))[:500]
        transp = str(ev.get("TRANSP") or "OPAQUE").strip().upper()
        is_busy = 0 if (is_all_day or transp == "TRANSPARENT") else 1
        uid = str(ev.get("UID") or "").strip()
        if not uid:
            uid = hashlib.sha1(f"{title}|{st_val}".encode("utf-8")).hexdigest()[:16]

        # Исключённые даты/времена EXDATE
        excl_dates: set[datetime.date] = set()
        excl_ts: set[int] = set()
        for ex_val, ex_params in ev.get("exdates") or []:
            ex_dt, ex_d, ex_all = _parse_ics_datetime(ex_val, ex_params, tz_str or "UTC")
            if ex_all and ex_d is not None:
                excl_dates.add(ex_d)
            elif ex_dt is not None:
                excl_ts.add(int(ex_dt.timestamp()))
                excl_dates.add(ex_dt.astimezone(target_tz).date())

        rrule = str(ev.get("RRULE") or "").strip()
        if rrule:
            occurrences = _expand_rrule_dates(
                st_date if is_all_day else st_dt,
                is_all_day,
                rrule,
                window_start,
                window_end,
                tz_str or "UTC",
            )
        else:
            occurrences = [st_date if is_all_day else st_dt]

        for occ in occurrences:
            if is_all_day:
                occ_date: datetime.date = occ
                if occ_date in excl_dates:
                    continue
                for offset in range(span_days):
                    d_cur = occ_date + datetime.timedelta(days=offset)
                    if window_start <= d_cur <= window_end:
                        normalized.append(
                            {
                                "external_uid": uid,
                                "event_date": d_cur.isoformat(),
                                "start_time": "00:00",
                                "end_time": "23:59",
                                "is_all_day": 1,
                                "title": title,
                                "location": location,
                                "description": description,
                                "is_busy": 0,
                            }
                        )
            else:
                occ_dt: datetime.datetime = occ
                occ_local_start = occ_dt.astimezone(target_tz)
                if int(occ_dt.timestamp()) in excl_ts:
                    continue
                if occ_local_start.date() in excl_dates and int(occ_dt.timestamp()) not in excl_ts and excl_ts:
                    pass
                elif occ_local_start.date() in excl_dates and not excl_ts:
                    continue
                occ_local_end = (occ_dt + duration).astimezone(target_tz)
                d_start = occ_local_start.date()
                d_end = occ_local_end.date()
                if d_start == d_end:
                    if window_start <= d_start <= window_end:
                        normalized.append(
                            {
                                "external_uid": uid,
                                "event_date": d_start.isoformat(),
                                "start_time": occ_local_start.strftime("%H:%M"),
                                "end_time": occ_local_end.strftime("%H:%M"),
                                "is_all_day": 0,
                                "title": title,
                                "location": location,
                                "description": description,
                                "is_busy": is_busy,
                            }
                        )
                else:
                    # Событие через полночь — разбиваем на дни окна
                    alert_end_utc = (
                        (occ_dt + duration)
                        .astimezone(datetime.timezone.utc)
                        .replace(tzinfo=None)
                        .isoformat()
                    )
                    if window_start <= d_start <= window_end:
                        normalized.append(
                            {
                                "external_uid": uid,
                                "event_date": d_start.isoformat(),
                                "start_time": occ_local_start.strftime("%H:%M"),
                                "end_time": "23:59",
                                "is_all_day": 0,
                                "title": title,
                                "location": location,
                                "description": description,
                                "is_busy": is_busy,
                                "alert_end_utc": alert_end_utc,
                            }
                        )
                    if window_start <= d_end <= window_end and occ_local_end.strftime("%H:%M") != "00:00":
                        normalized.append(
                            {
                                "external_uid": uid,
                                "event_date": d_end.isoformat(),
                                "start_time": "00:00",
                                "end_time": occ_local_end.strftime("%H:%M"),
                                "is_all_day": 0,
                                "title": title,
                                "location": location,
                                "description": description,
                                "is_busy": is_busy,
                            }
                        )

    return normalized


async def _default_fetch_ics(url: str) -> str:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers={"User-Agent": "HiDL-Calendar-Sync/1.0"}) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            raw = await resp.content.read(_MAX_ICS_BYTES + 1)
            if len(raw) > _MAX_ICS_BYTES:
                raise RuntimeError("Файл календаря больше 5 МБ")
            return raw.decode("utf-8", errors="replace")


async def _default_oauth_exchange(payload: Dict[str, str]) -> Dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post("https://oauth2.googleapis.com/token", data=payload) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400 or not isinstance(data, dict) or data.get("error"):
                err = data.get("error_description") or data.get("error") if isinstance(data, dict) else f"HTTP {resp.status}"
                raise RuntimeError(f"Google OAuth error: {err}")
            return data


async def _default_gcal_get(url: str, access_token: str, params: Dict[str, str]) -> Dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=15)
    headers = {"Authorization": f"Bearer {access_token}"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers, params=params) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400 or not isinstance(data, dict):
                raise RuntimeError(f"Google Calendar GET HTTP {resp.status}")
            return data


async def _default_gcal_post(url: str, access_token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=15)
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=body) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400 or not isinstance(data, dict):
                raise RuntimeError(f"Google Calendar POST HTTP {resp.status}")
            return data


async def _default_gcal_delete(url: str, access_token: str) -> None:
    timeout = aiohttp.ClientTimeout(total=15)
    headers = {"Authorization": f"Bearer {access_token}"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.delete(url, headers=headers) as resp:
            if resp.status in {200, 204, 404, 410}:
                return
            raise RuntimeError(f"Google Calendar DELETE HTTP {resp.status}")


def _map_google_api_items(
    items: List[Dict[str, Any]],
    tz_str: str,
    window_start: datetime.date,
    window_end: datetime.date,
) -> List[Dict[str, Any]]:
    target_tz = tzinfo_from_string(tz_str or "UTC")
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("status") == "cancelled":
            continue
        uid = str(item.get("iCalUID") or item.get("id") or "").strip()
        if not uid:
            continue
        title = str(item.get("summary") or "Событие").strip() or "Событие"
        location = str(item.get("location") or "").strip()
        description = str(item.get("description") or "").strip()[:500]
        start_obj = item.get("start") or {}
        end_obj = item.get("end") or {}
        transp = str(item.get("transparency") or "opaque").lower()

        if start_obj.get("date") and not start_obj.get("dateTime"):
            try:
                s_d = datetime.date.fromisoformat(str(start_obj["date"])[:10])
                e_d = datetime.date.fromisoformat(str(end_obj.get("date") or start_obj["date"])[:10])
            except ValueError:
                continue
            span = max(1, (e_d - s_d).days)
            for off in range(span):
                d_cur = s_d + datetime.timedelta(days=off)
                if window_start <= d_cur <= window_end:
                    out.append(
                        {
                            "external_uid": uid,
                            "event_date": d_cur.isoformat(),
                            "start_time": "00:00",
                            "end_time": "23:59",
                            "is_all_day": 1,
                            "title": title,
                            "location": location,
                            "description": description,
                            "is_busy": 0,
                        }
                    )
            continue

        raw_st = str(start_obj.get("dateTime") or "").strip()
        raw_en = str(end_obj.get("dateTime") or "").strip()
        if not raw_st:
            continue
        try:
            st_dt = datetime.datetime.fromisoformat(raw_st.replace("Z", "+00:00"))
            if st_dt.tzinfo is None:
                st_dt = st_dt.replace(tzinfo=target_tz)
            en_dt = (
                datetime.datetime.fromisoformat(raw_en.replace("Z", "+00:00"))
                if raw_en
                else st_dt + datetime.timedelta(hours=1)
            )
            if en_dt.tzinfo is None:
                en_dt = en_dt.replace(tzinfo=target_tz)
        except ValueError:
            continue

        loc_st = st_dt.astimezone(target_tz)
        loc_en = en_dt.astimezone(target_tz)
        ev_date = loc_st.date()
        if window_start <= ev_date <= window_end:
            out.append(
                {
                    "external_uid": uid,
                    "event_date": ev_date.isoformat(),
                    "start_time": loc_st.strftime("%H:%M"),
                    "end_time": loc_en.strftime("%H:%M") if loc_en.date() == ev_date else "23:59",
                    "is_all_day": 0,
                    "title": title,
                    "location": location,
                    "description": description,
                    "is_busy": 0 if transp == "transparent" else 1,
                    **(
                        {
                            "alert_end_utc": en_dt.astimezone(datetime.timezone.utc)
                            .replace(tzinfo=None)
                            .isoformat()
                        }
                        if loc_en.date() != ev_date
                        else {}
                    ),
                }
            )
    return out


async def _ensure_valid_google_token(
    db,
    conn_row: Dict[str, Any],
    *,
    settings: Optional[GoogleCalendarSettings] = None,
    token_exchanger: Optional[_OAUTH_EXCHANGER] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> str:
    now = _naive_utc(now_utc)
    access_token = str(conn_row.get("access_token") or "").strip()
    refresh_token = str(conn_row.get("refresh_token") or "").strip()
    expires_raw = str(conn_row.get("token_expires_at") or "").strip()

    still_valid = False
    if access_token and expires_raw:
        try:
            exp_dt = _naive_utc(datetime.datetime.fromisoformat(expires_raw))
            still_valid = (exp_dt - now).total_seconds() > 120
        except ValueError:
            still_valid = False
    elif access_token and not refresh_token:
        still_valid = True

    if still_valid:
        return access_token

    if not refresh_token:
        return access_token

    cfg = settings or get_gcal_settings()
    if not cfg.oauth_configured and token_exchanger is None:
        return access_token

    exchanger = token_exchanger or _default_oauth_exchange
    tok_data = await exchanger(
        {
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    )
    new_access = str(tok_data.get("access_token") or "").strip()
    if not new_access:
        raise RuntimeError("Не удалось обновить access_token Google")
    expires_in = int(tok_data.get("expires_in") or 3600)
    new_exp = (now + datetime.timedelta(seconds=expires_in)).isoformat()
    await repo.update_calendar_connection_sync(
        db,
        int(conn_row["id"]),
        access_token=new_access,
        token_expires_at=new_exp,
    )
    return new_access


async def sync_ics_connection(
    db,
    user: Dict[str, Any],
    conn_row: Dict[str, Any],
    *,
    fetcher: Optional[_ICS_FETCHER] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Синхронизировать одно .ics-подключение в окно [-1..+14] дней."""
    now = _naive_utc(now_utc)
    tz = str(user.get("timezone") or "UTC")
    today_local = datetime.date.fromisoformat(local_date_str(now, tz))
    w_start = today_local - datetime.timedelta(days=1)
    w_end = today_local + datetime.timedelta(days=14)

    url = str(conn_row.get("ics_url") or "").strip()
    ok_url, err_msg = validate_ics_url(url)
    if not ok_url:
        await repo.update_calendar_connection_sync(db, int(conn_row["id"]), last_error=err_msg)
        return {"ok": False, "provider": "ics", "count": 0, "error": err_msg}

    fn = fetcher or _default_fetch_ics
    try:
        ics_text = await fn(url)
        events = parse_ics_events(ics_text, tz, w_start, w_end)
        count = await repo.replace_calendar_events_window(
            db,
            int(user["id"]),
            "ics",
            w_start.isoformat(),
            w_end.isoformat(),
            events,
        )
        await repo.update_calendar_connection_sync(
            db, int(conn_row["id"]), last_sync_at=now.isoformat(), last_error=""
        )
        return {"ok": True, "provider": "ics", "count": count, "error": ""}
    except Exception as exc:  # noqa: BLE001
        err_text = f"Ошибка .ics синхронизации ({exc.__class__.__name__})"
        await repo.update_calendar_connection_sync(db, int(conn_row["id"]), last_error=err_text)
        return {"ok": False, "provider": "ics", "count": 0, "error": err_text}


async def sync_google_oauth_connection(
    db,
    user: Dict[str, Any],
    conn_row: Dict[str, Any],
    *,
    settings: Optional[GoogleCalendarSettings] = None,
    token_exchanger: Optional[_OAUTH_EXCHANGER] = None,
    getter: Optional[_GCAL_API_GETTER] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Синхронизировать Google Calendar API v3 по OAuth 2.0 токену."""
    now = _naive_utc(now_utc)
    tz = str(user.get("timezone") or "UTC")
    today_local = datetime.date.fromisoformat(local_date_str(now, tz))
    w_start = today_local - datetime.timedelta(days=1)
    w_end = today_local + datetime.timedelta(days=14)

    cal_id = str(conn_row.get("calendar_id") or "primary").strip() or "primary"
    try:
        access_token = await _ensure_valid_google_token(
            db, conn_row, settings=settings, token_exchanger=token_exchanger, now_utc=now
        )
        if not access_token:
            raise RuntimeError("Отсутствует access_token")
        tzinfo = tzinfo_from_string(tz)
        t_min = datetime.datetime.combine(w_start, datetime.time(0, 0), tzinfo).astimezone(datetime.timezone.utc)
        t_max = datetime.datetime.combine(w_end + datetime.timedelta(days=1), datetime.time(0, 0), tzinfo).astimezone(datetime.timezone.utc)
        api_get = getter or _default_gcal_get
        data = await api_get(
            f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events",
            access_token,
            {
                "singleEvents": "true",
                "orderBy": "startTime",
                "timeMin": t_min.isoformat().replace("+00:00", "Z"),
                "timeMax": t_max.isoformat().replace("+00:00", "Z"),
                "maxResults": "250",
            },
        )
        items = data.get("items") if isinstance(data.get("items"), list) else []
        events = _map_google_api_items(items, tz, w_start, w_end)
        count = await repo.replace_calendar_events_window(
            db,
            int(user["id"]),
            "google_oauth",
            w_start.isoformat(),
            w_end.isoformat(),
            events,
        )
        await repo.update_calendar_connection_sync(
            db, int(conn_row["id"]), last_sync_at=now.isoformat(), last_error=""
        )
        return {"ok": True, "provider": "google_oauth", "count": count, "error": ""}
    except Exception as exc:  # noqa: BLE001
        err_text = f"Ошибка Google Calendar sync ({exc.__class__.__name__})"
        await repo.update_calendar_connection_sync(db, int(conn_row["id"]), last_error=err_text)
        return {"ok": False, "provider": "google_oauth", "count": 0, "error": err_text}


async def sync_user_calendars(
    db,
    user: Dict[str, Any],
    *,
    ics_fetcher: Optional[_ICS_FETCHER] = None,
    gcal_getter: Optional[_GCAL_API_GETTER] = None,
    token_exchanger: Optional[_OAUTH_EXCHANGER] = None,
    settings: Optional[GoogleCalendarSettings] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Синхронизировать все активные подключения календаря для пользователя."""
    now = _naive_utc(now_utc)
    conns = await repo.list_user_calendar_connections(db, int(user["id"]), enabled_only=True)
    results: List[Dict[str, Any]] = []
    total_count = 0
    for c in conns:
        prov = str(c.get("provider") or "")
        if prov == "ics":
            res = await sync_ics_connection(db, user, c, fetcher=ics_fetcher, now_utc=now)
        elif prov == "google_oauth":
            res = await sync_google_oauth_connection(
                db,
                user,
                c,
                settings=settings,
                token_exchanger=token_exchanger,
                getter=gcal_getter,
                now_utc=now,
            )
        else:
            continue
        results.append(res)
        if res.get("ok"):
            total_count += int(res.get("count") or 0)
    return {"connections": len(conns), "synced_events": total_count, "results": results}


async def build_gcal_oauth_start_url(
    db,
    user_id: int,
    *,
    settings: Optional[GoogleCalendarSettings] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Optional[str]:
    """Сгенерировать одноразовую ссылку Google OAuth 2.0 для пользователя."""
    cfg = settings or get_gcal_settings()
    if not cfg.oauth_configured:
        return None
    now = _naive_utc(now_utc)
    state_token = secrets.token_urlsafe(24)
    expires_at = (now + datetime.timedelta(minutes=15)).isoformat()
    await repo.create_gcal_oauth_state(db, user_id, state_token, expires_at)
    query = urlencode(
        {
            "client_id": cfg.client_id,
            "redirect_uri": cfg.redirect_uri,
            "response_type": "code",
            "scope": cfg.scopes,
            "access_type": "offline",
            "prompt": "consent",
            "state": state_token,
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


async def complete_gcal_oauth_callback(
    db,
    *,
    code: str,
    state: str,
    settings: Optional[GoogleCalendarSettings] = None,
    token_exchanger: Optional[_OAUTH_EXCHANGER] = None,
    gcal_getter: Optional[_GCAL_API_GETTER] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Tuple[bool, str]:
    """Обработать OAuth-колбэк Google, сохранить токены и выполнить первичную синхронизацию."""
    now = _naive_utc(now_utc)
    clean_code = (code or "").strip()
    clean_state = (state or "").strip()
    if not clean_code or not clean_state:
        return False, "Не хватает параметров code или state."

    user_id = await repo.consume_gcal_oauth_state(db, clean_state, now.isoformat())
    if user_id is None:
        return False, "Ссылка авторизации устарела или уже использована."

    cfg = settings or get_gcal_settings()
    if not cfg.oauth_configured and token_exchanger is None:
        return False, "OAuth для Google Calendar не настроен на сервере."

    exchanger = token_exchanger or _default_oauth_exchange
    try:
        tok = await exchanger(
            {
                "code": clean_code,
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "redirect_uri": cfg.redirect_uri,
                "grant_type": "authorization_code",
            }
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"Не удалось обменять код на токен ({exc.__class__.__name__})."

    access_token = str(tok.get("access_token") or "").strip()
    if not access_token:
        return False, "Google не вернул access_token."
    refresh_token = str(tok.get("refresh_token") or "").strip()
    expires_in = int(tok.get("expires_in") or 3600)
    exp_iso = (now + datetime.timedelta(seconds=expires_in)).isoformat()

    conn_row = await repo.upsert_calendar_connection(
        db,
        user_id,
        "google_oauth",
        label="Google Calendar (OAuth)",
        calendar_id="primary",
        access_token=access_token,
        refresh_token=refresh_token,
        token_expires_at=exp_iso,
        enabled=True,
    )
    user_row = await repo.get_user(db, user_id)
    if user_row:
        await sync_google_oauth_connection(
            db,
            dict(user_row),
            conn_row,
            settings=cfg,
            token_exchanger=exchanger,
            getter=gcal_getter,
            now_utc=now,
        )
    return True, "Google Calendar подключён к HiDL! Можно закрыть страницу и вернуться в Telegram."


async def create_unified_event(
    db,
    user: Dict[str, Any],
    *,
    title: str,
    event_date: str,
    start_time: str,
    end_time: str,
    location: str = "",
    description: str = "",
    category: str = "manual",
    push_to_google: bool = True,
    poster: Optional[_GCAL_API_POSTER] = None,
    token_exchanger: Optional[_OAUTH_EXCHANGER] = None,
    settings: Optional[GoogleCalendarSettings] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Создать событие (в Google Calendar по OAuth, либо локально в schedule_events)."""
    now = _naive_utc(now_utc)
    user_id = int(user["id"])
    tz = str(user.get("timezone") or "UTC")
    clean_title = (title or "").strip() or "Событие"

    conns = await repo.list_user_calendar_connections(db, user_id, enabled_only=True)
    oauth_conn = next((c for c in conns if c.get("provider") == "google_oauth"), None)
    cfg = settings or get_gcal_settings()

    google_error = ""
    if push_to_google and oauth_conn and (poster is not None or cfg.oauth_configured or oauth_conn.get("access_token")):
        try:
            access_token = await _ensure_valid_google_token(
                db, oauth_conn, settings=cfg, token_exchanger=token_exchanger, now_utc=now
            )
            if not access_token:
                raise RuntimeError("Нет access_token Google")
            st_t = parse_hhmm(start_time) or datetime.time(9, 0)
            en_t = parse_hhmm(end_time) or datetime.time(10, 0)
            d_obj = datetime.date.fromisoformat(event_date)
            tzinfo = tzinfo_from_string(tz)
            st_iso = datetime.datetime.combine(d_obj, st_t, tzinfo).isoformat()
            en_iso = datetime.datetime.combine(d_obj, en_t, tzinfo).isoformat()
            cal_id = str(oauth_conn.get("calendar_id") or "primary").strip() or "primary"
            api_post = poster or _default_gcal_post
            body: Dict[str, Any] = {
                "summary": clean_title,
                "start": {"dateTime": st_iso},
                "end": {"dateTime": en_iso},
            }
            if location:
                body["location"] = location.strip()
            if description:
                body["description"] = description.strip()
            if "/" in tz:
                body["start"]["timeZone"] = tz
                body["end"]["timeZone"] = tz

            created = await api_post(
                f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events",
                access_token,
                body,
            )
            google_event_id = str(created.get("id") or "").strip()
            ext_uid = str(created.get("iCalUID") or google_event_id or f"gcal_{secrets.token_hex(6)}")
            cached = await repo.upsert_single_calendar_event(
                db,
                user_id,
                "google_oauth",
                {
                    "external_uid": ext_uid,
                    "event_date": event_date,
                    "start_time": st_t.strftime("%H:%M"),
                    "end_time": en_t.strftime("%H:%M"),
                    "is_all_day": 0,
                    "title": clean_title,
                    "location": location.strip(),
                    "description": description.strip(),
                    "is_busy": 1,
                },
            )
            return {
                "ok": True,
                "storage": "google_oauth",
                "google_synced": True,
                "google_event_id": google_event_id,
                "google_error": "",
                "event": cached,
            }
        except Exception as exc:  # noqa: BLE001
            google_error = f"Google Calendar недоступен ({exc.__class__.__name__}), сохранила локально."

    ev_id = await repo.create_schedule_event(
        db,
        user_id=user_id,
        event_date=event_date,
        start_time=start_time,
        end_time=end_time,
        title=clean_title,
        category=category,
        source="manual",
    )
    return {
        "ok": True,
        "storage": "local",
        "google_synced": False,
        "google_event_id": "",
        "google_error": google_error,
        "event": {
            "id": ev_id,
            "source": "manual",
            "event_date": event_date,
            "start_time": start_time,
            "end_time": end_time,
            "is_all_day": 0,
            "title": clean_title,
            "location": location.strip(),
            "description": description.strip(),
            "is_busy": 1,
        },
    }


async def delete_unified_event(
    db,
    user: Dict[str, Any],
    *,
    storage: str,
    event_id: Optional[int] = None,
    google_event_id: str = "",
    external_uid: str = "",
    event_date: str = "",
    start_time: str = "",
    deleter: Optional[Callable[[str, str], Awaitable[None]]] = None,
    token_exchanger: Optional[_OAUTH_EXCHANGER] = None,
    settings: Optional[GoogleCalendarSettings] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> bool:
    """Удалить событие локально (schedule_events) и при необходимости из Google Calendar (для Undo)."""
    user_id = int(user["id"])
    now = _naive_utc(now_utc)
    if storage == "google_oauth":
        if event_id:
            await db.execute(
                "DELETE FROM calendar_events WHERE id = ? AND user_id = ? AND source = 'google_oauth'",
                (int(event_id), user_id),
            )
        elif external_uid and event_date and start_time:
            await db.execute(
                """
                DELETE FROM calendar_events
                WHERE user_id = ? AND source = 'google_oauth'
                  AND external_uid = ? AND event_date = ? AND start_time = ?
                """,
                (user_id, external_uid, event_date, start_time),
            )
        await db.commit()

        if google_event_id:
            conns = await repo.list_user_calendar_connections(db, user_id, enabled_only=True)
            oauth_conn = next((c for c in conns if c.get("provider") == "google_oauth"), None)
            if oauth_conn:
                try:
                    cfg = settings or get_gcal_settings()
                    access_token = await _ensure_valid_google_token(
                        db, oauth_conn, settings=cfg, token_exchanger=token_exchanger, now_utc=now
                    )
                    if access_token:
                        cal_id = str(oauth_conn.get("calendar_id") or "primary").strip() or "primary"
                        api_del = deleter or _default_gcal_delete
                        await api_del(
                            f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events/{google_event_id}",
                            access_token,
                        )
                except Exception:
                    pass
        return True

    if event_id:
        await repo.delete_schedule_event(db, user_id, int(event_id))
        return True
    return False


def _merge_unified_events(
    ext_rows: List[Dict[str, Any]],
    local_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in ext_rows:
        d = str(row.get("event_date") or "")
        st = str(row.get("start_time") or "00:00")
        t_norm = str(row.get("title") or "").strip().casefold()
        seen.add((d, st, t_norm))
        merged.append({**row, "is_external": True})

    for row in local_rows:
        d = str(row.get("event_date") or "")
        st = str(row.get("start_time") or "00:00")
        t_norm = str(row.get("title") or "").strip().casefold()
        if (d, st, t_norm) in seen:
            continue
        seen.add((d, st, t_norm))
        merged.append(
            {
                "id": row.get("id"),
                "user_id": row.get("user_id"),
                "source": row.get("source") or "manual",
                "external_uid": f"local:{row.get('id')}",
                "event_date": d,
                "start_time": st,
                "end_time": str(row.get("end_time") or st),
                "is_all_day": 0,
                "title": str(row.get("title") or "Событие"),
                "location": "",
                "description": "",
                "is_busy": 1,
                "pre_alert_sent": 0,
                "is_external": False,
            }
        )

    merged.sort(
        key=lambda e: (
            str(e.get("event_date") or ""),
            0 if e.get("is_all_day") else 1,
            str(e.get("start_time") or "00:00"),
        )
    )
    return merged


async def get_unified_events_range(
    db,
    user_id: int,
    start_date: str,
    end_date: str,
) -> List[Dict[str, Any]]:
    """Вернуть объединённый список событий (внешний календарь + локальные) в диапазоне дат."""
    ext = await repo.list_calendar_events_range(db, user_id, start_date, end_date)
    loc = await repo.list_schedule_events_range(db, user_id, start_date, end_date)
    return _merge_unified_events(ext, loc)


async def get_unified_events_for_date(
    db,
    user_id: int,
    local_date: str,
) -> List[Dict[str, Any]]:
    """Вернуть объединённый список событий на конкретную дату."""
    return await get_unified_events_range(db, user_id, local_date, local_date)


def _event_utc_bounds(
    event: Dict[str, Any],
    tz_str: str,
) -> Optional[Tuple[datetime.datetime, datetime.datetime]]:
    if event.get("is_all_day"):
        return None
    d_str = str(event.get("event_date") or "")
    st_t = parse_hhmm(str(event.get("start_time") or ""))
    en_t = parse_hhmm(str(event.get("end_time") or ""))
    if not d_str or st_t is None or en_t is None:
        return None
    try:
        d_obj = datetime.date.fromisoformat(d_str)
    except ValueError:
        return None
    tzinfo = tzinfo_from_string(tz_str or "UTC")
    st_loc = datetime.datetime.combine(d_obj, st_t, tzinfo)
    en_loc = datetime.datetime.combine(d_obj, en_t, tzinfo)
    if en_loc <= st_loc:
        return None
    st_utc = st_loc.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    en_utc = en_loc.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return st_utc, en_utc


async def get_active_busy_event(
    db,
    user: Dict[str, Any],
    now_utc: Optional[datetime.datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Проверить, идёт ли у пользователя прямо сейчас занятое событие в календаре."""
    now = _naive_utc(now_utc)
    tz = str(user.get("timezone") or "UTC")
    local_date = local_date_str(now, tz)
    events = await get_unified_events_for_date(db, int(user["id"]), local_date)
    for ev in events:
        if not ev.get("is_busy", 1) or ev.get("is_all_day"):
            continue
        bounds = _event_utc_bounds(ev, tz)
        if not bounds:
            continue
        st_utc, en_utc = bounds
        if st_utc <= now < en_utc:
            return {**ev, "start_utc": st_utc, "end_utc": en_utc}
    return None


def _effective_user_schedule(user: Dict[str, Any], local_date: str) -> Tuple[str, str]:
    weekday = datetime.date.fromisoformat(local_date).weekday()
    is_weekend = weekday >= 5
    wake = str(user.get("wake_up_time") or "08:00")
    sleep = str(user.get("sleep_time") or "23:00")
    if is_weekend:
        w_end = str(user.get("weekend_wake_up_time") or "").strip()
        s_end = str(user.get("weekend_sleep_time") or "").strip()
        if w_end:
            wake = w_end
        if s_end:
            sleep = s_end
    return wake, sleep


def _hhmm_min(hhmm: str, default_min: int) -> int:
    t = parse_hhmm(hhmm)
    return (t.hour * 60 + t.minute) if t else default_min


def _min_hhmm(minutes: int) -> str:
    m = int(minutes) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


async def compute_early_wake_shift(
    db,
    user: Dict[str, Any],
    now_utc: Optional[datetime.datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Определить, требуется ли лечь спать раньше из-за утреннего события завтра."""
    now = _naive_utc(now_utc)
    tz = str(user.get("timezone") or "UTC")
    local_today = local_date_str(now, tz)
    tomorrow_date = (datetime.date.fromisoformat(local_today) + datetime.timedelta(days=1)).isoformat()

    events = await get_unified_events_for_date(db, int(user["id"]), tomorrow_date)
    morning_timed: List[Tuple[int, Dict[str, Any]]] = []
    for ev in events:
        if ev.get("is_all_day"):
            continue
        st_m = _hhmm_min(str(ev.get("start_time") or ""), -1)
        if 5 * 60 <= st_m <= 11 * 60:
            morning_timed.append((st_m, ev))
    if not morning_timed:
        return None

    morning_timed.sort(key=lambda pair: pair[0])
    earliest_start_min, earliest_ev = morning_timed[0]

    tom_wake_hhmm, _ = _effective_user_schedule(user, tomorrow_date)
    cur_wake_hhmm, cur_sleep_hhmm = _effective_user_schedule(user, local_today)
    tom_wake_min = _hhmm_min(tom_wake_hhmm, 8 * 60)
    cur_wake_min = _hhmm_min(cur_wake_hhmm, 8 * 60)
    cur_sleep_min = _hhmm_min(cur_sleep_hhmm, 23 * 60)

    if earliest_start_min - 60 > tom_wake_min - 30:
        return None

    rec_wake_min = max(4 * 60, earliest_start_min - 60)
    rec_sleep_min = (rec_wake_min - 8 * 60) % (24 * 60)

    cur_sleep_abs = cur_sleep_min + (1440 if cur_sleep_min <= cur_wake_min else 0)
    rec_sleep_abs = rec_sleep_min + (1440 if rec_sleep_min <= 240 else 0)
    if rec_sleep_abs >= cur_sleep_abs - 15:
        return None

    return {
        "event": earliest_ev,
        "tomorrow_date": tomorrow_date,
        "recommended_wake": _min_hhmm(rec_wake_min),
        "recommended_sleep": _min_hhmm(rec_sleep_min),
        "current_sleep": cur_sleep_hhmm,
    }


async def process_calendar_sync_tick(
    db,
    now_utc: Optional[datetime.datetime] = None,
    *,
    ics_fetcher: Optional[_ICS_FETCHER] = None,
    gcal_getter: Optional[_GCAL_API_GETTER] = None,
) -> int:
    """15-минутная фоновая синхронизация всех активных календарей."""
    now = _naive_utc(now_utc)
    conns = await repo.list_all_enabled_calendar_connections(db)
    if not conns:
        return 0
    user_cache: Dict[int, Dict[str, Any]] = {}
    synced_ok = 0
    for c in conns:
        uid = int(c["user_id"])
        user = user_cache.get(uid)
        if user is None:
            row = await repo.get_user(db, uid)
            if not row:
                continue
            user = dict(row)
            user_cache[uid] = user
        prov = str(c.get("provider") or "")
        if prov == "ics":
            res = await sync_ics_connection(db, user, c, fetcher=ics_fetcher, now_utc=now)
        elif prov == "google_oauth":
            res = await sync_google_oauth_connection(
                db, user, c, getter=gcal_getter, now_utc=now
            )
        else:
            continue
        if res.get("ok"):
            synced_ok += 1
    return synced_ok


async def process_calendar_alerts_tick(
    db,
    bot,
    now_utc: Optional[datetime.datetime] = None,
) -> int:
    """60-секундный тик пред-событийных предупреждений и вечернего предложения сдвинуть отбой."""
    now = _naive_utc(now_utc)
    users = rows_to_dicts(await repo.list_users(db))
    users_by_id = {int(user["id"]): user for user in users if user.get("id") is not None}
    sent_count = 0

    # Remove expired alert cards before quiet/pause filtering: cleanup never sends
    # a notification, and must also see events from earlier local dates.
    active_alerts = await repo.list_calendar_events_with_active_alert(db)
    for raw_event in active_alerts:
        ev = dict(raw_event)
        try:
            event_id = int(ev["id"])
            uid = int(ev["user_id"])
        except (KeyError, TypeError, ValueError):
            continue
        user = users_by_id.get(uid)
        if user is None:
            row = await repo.get_user(db, uid)
            if not row:
                continue
            user = dict(row)
            users_by_id[uid] = user
        bounds = _event_utc_bounds(ev, str(user.get("timezone") or "UTC"))
        alert_end_utc = bounds[1] if bounds is not None else None
        if bounds is not None and ev.get("alert_end_utc"):
            try:
                alert_end_utc = _naive_utc(
                    datetime.datetime.fromisoformat(str(ev["alert_end_utc"]))
                )
            except (TypeError, ValueError):
                pass
        if bounds is not None and alert_end_utc is not None and now < alert_end_utc:
            continue
        try:
            await safe_delete_or_clear_markup(
                bot,
                ev.get("alert_chat_id"),
                ev.get("alert_message_id"),
            )
        finally:
            # Telegram's 48-hour deletion limit and already-deleted cards should
            # not cause this alert to be retried on every scheduler tick.
            await repo.clear_calendar_event_alert_message(db, event_id)

    for user in users:
        uid = int(user["id"])
        tg_id = user.get("telegram_id")
        if not tg_id:
            continue
        tz = str(user.get("timezone") or "UTC")
        local_date = local_date_str(now, tz)
        if user.get("quiet_mode"):
            continue
        if user.get("pause_until") and local_date <= str(user["pause_until"] or ""):
            continue

        # 1. Пред-событийные предупреждения (15-30м, с локацией 30-45м)
        ext_today = await repo.list_calendar_events_range(db, uid, local_date, local_date)
        seen_event_keys = {
            (
                str(ev.get("event_date") or ""),
                str(ev.get("start_time") or ""),
                str(ev.get("title") or "").strip().casefold(),
            )
            for ev in ext_today
        }
        for ev in ext_today:
            if ev.get("is_all_day") or ev.get("pre_alert_sent"):
                continue
            bounds = _event_utc_bounds(ev, tz)
            if not bounds:
                continue
            st_utc, _ = bounds
            mins_left = (st_utc - now).total_seconds() / 60.0
            has_loc = bool(str(ev.get("location") or "").strip())
            low_w, high_w = (30.0, 45.0) if has_loc else (15.0, 30.0)
            if low_w <= mins_left <= high_w:
                mins_int = max(1, int(round(mins_left)))
                lines = [
                    f"🗓 <b>Через {mins_int} мин: {html.escape(str(ev.get('title') or 'Событие'))}</b> "
                    f"({html.escape(str(ev.get('start_time')))}–{html.escape(str(ev.get('end_time')))})"
                ]
                if has_loc:
                    lines.append(f"📍 {html.escape(str(ev['location']))} (с запасом на дорогу)")
                try:
                    sent_msg = await bot.send_message(chat_id=tg_id, text="\n".join(lines), parse_mode="HTML")
                    await repo.mark_calendar_event_alert_sent(
                        db,
                        int(ev["id"]),
                        chat_id=getattr(getattr(sent_msg, "chat", None), "id", None),
                        message_id=getattr(sent_msg, "message_id", None),
                    )
                    sent_count += 1
                except Exception:
                    pass

        # 1b. Пред-событийные предупреждения для локальных событий расписания (schedule_events)
        loc_today = rows_to_dicts(await repo.list_schedule_events(db, uid, local_date))
        for lev in loc_today:
            if lev.get("pre_alert_sent"):
                continue
            key = (
                str(lev.get("event_date") or local_date),
                str(lev.get("start_time") or ""),
                str(lev.get("title") or "").strip().casefold(),
            )
            if key in seen_event_keys:
                continue
            bounds = _event_utc_bounds(
                {
                    "event_date": lev.get("event_date") or local_date,
                    "start_time": lev.get("start_time"),
                    "end_time": lev.get("end_time"),
                    "is_all_day": 0,
                },
                tz,
            )
            if not bounds:
                continue
            st_utc, _ = bounds
            mins_left = (st_utc - now).total_seconds() / 60.0
            if 15.0 <= mins_left <= 30.0:
                mins_int = max(1, int(round(mins_left)))
                text_loc = (
                    f"🗓 <b>Через {mins_int} мин: {html.escape(str(lev.get('title') or 'Событие'))}</b> "
                    f"({html.escape(str(lev.get('start_time') or ''))}–{html.escape(str(lev.get('end_time') or ''))})"
                )
                try:
                    await bot.send_message(chat_id=tg_id, text=text_loc, parse_mode="HTML")
                    await repo.mark_schedule_event_alert_sent(db, int(lev["id"]))
                    sent_count += 1
                except Exception:
                    pass

        # 2. Вечернее предупреждение о раннем событии завтра (1 раз в день)
        if str(user.get("early_wake_alert_date") or "") == local_date:
            continue
        wake_hhmm, sleep_hhmm = _effective_user_schedule(user, local_date)
        wake_m = _hhmm_min(wake_hhmm, 8 * 60)
        sleep_m = _hhmm_min(sleep_hhmm, 23 * 60)
        sleep_abs = sleep_m + (1440 if sleep_m <= wake_m else 0)
        now_hhmm = format_time_local(now, tz)
        now_m = _hhmm_min(now_hhmm, 12 * 60)
        now_abs = now_m + (1440 if now_m < wake_m and sleep_m <= wake_m else 0)

        if (sleep_abs - 150) <= now_abs <= (sleep_abs - 75):
            shift = await compute_early_wake_shift(db, user, now_utc=now)
            if shift:
                ev = shift["event"]
                rec_sleep = shift["recommended_sleep"]
                rec_wake = shift["recommended_wake"]
                ev_title = html.escape(str(ev.get("title") or "дело"))
                ev_start = html.escape(str(ev.get("start_time") or ""))
                text = (
                    f"🌙 <b>Завтра раннее событие: {ev_title} в {ev_start}.</b>\n"
                    f"Чтобы успеть выспаться и не срываться с утра (подъём в <b>{rec_wake}</b>), "
                    f"предлагаю сегодня лечь в <b>{rec_sleep}</b> вместо {html.escape(str(shift['current_sleep']))}."
                )
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=f"🌙 Сдвинуть отбой на {rec_sleep}",
                                callback_data=f"cal:shift_bed:{rec_sleep}",
                            )
                        ],
                        [InlineKeyboardButton(text="👍 Оставлю как есть", callback_data="cal:dismiss")],
                    ]
                )
                try:
                    await bot.send_message(chat_id=tg_id, text=text, reply_markup=kb, parse_mode="HTML")
                    await repo.update_user_early_wake_alert_date(db, uid, local_date)
                    sent_count += 1
                except Exception:
                    pass

    return sent_count
