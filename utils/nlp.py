import re
import datetime
from typing import Optional, Tuple


def match_simple_intent(text: str):
    """Return ('done'|'skip'|'later'|None) for quick acknowledgements."""
    t = text.lower().strip()
    if t in {"сделал", "сделал(а)", "сделала", "готово", "ок", "ok", "done", "✅", "👍"}:
        return "done"
    if t in {"пропусти", "пропустить", "не сегодня", "skip", "⏭"}:
        return "skip"
    if t in {"позже", "напомни позже", "later", "↪️"}:
        return "later"
    return None


def parse_when(text: str) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """
    Грубый парсер времени: возвращает (absolute_hhmm, plus_hours, weekday)
    - absolute_hhmm: 'HH:MM' если нашли явное время
    - plus_hours: int если нашли "через N часов"
    - weekday: 0-6 если "в понедельник" и т.п.
    """
    t = text.lower()
    # через N часов
    m = re.search(r"через\s+(\d{1,2})\s*час", t)
    if m:
        return None, int(m.group(1)), None
    # время HH:MM
    m = re.search(r"(\d{1,2})[:.](\d{2})", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        return f"{hh:02d}:{mm:02d}", None, None
    # завтра
    if "завтра" in t:
        return None, None, -1
    # дни недели
    days = {
        "пн": 0, "понедель": 0,
        "вт": 1, "вторни": 1,
        "ср": 2, "сред": 2,
        "чт": 3, "четверг": 3,
        "пт": 4, "пятниц": 4,
        "сб": 5, "суббот": 5,
        "вс": 6, "воскрес": 6,
    }
    for key, idx in days.items():
        if key in t:
            return None, None, idx
    return None, None, None
