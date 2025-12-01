import re
from dataclasses import dataclass
from typing import Optional, Dict, Any

WEEKDAY_MAP = {
    "понедельник": 0,
    "пн": 0,
    "вторник": 1,
    "вт": 1,
    "сред": 2,
    "ср": 2,
    "четвер": 3,
    "чт": 3,
    "пятн": 4,
    "пт": 4,
    "суб": 5,
    "сб": 5,
    "воскр": 6,
    "вс": 6,
}


@dataclass
class ParsedCommand:
    type: str  # "expense" | "reminder" | "home" | "ask"
    payload: Dict[str, Any]


def parse_expense(text: str) -> Optional[ParsedCommand]:
    txt = text.lower().strip()
    m = re.search(r"(-|\+)?\\s*(\\d+[\\.,]?\\d*)", txt)
    if not m:
        return None
    amount = float(m.group(2).replace(",", "."))
    sign = -1 if m.group(1) == "-" else 1
    amount *= sign
    # категория — слово после числа или последнее слово
    after = txt[m.end():].strip()
    parts = after.split()
    category = parts[0] if parts else txt.split()[-1]
    return ParsedCommand(type="expense", payload={"amount": amount, "category": category})


def parse_reminder(text: str) -> Optional[ParsedCommand]:
    txt = text.lower().strip()
    if "напом" not in txt and "напиши" not in txt:
        return None
    # время HH:MM
    time_m = re.search(r"(\\d{1,2}:\\d{2})", txt)
    hhmm = time_m.group(1) if time_m else None
    # относительное время: через N часов/минут
    rel_hours = None
    rel_minutes = None
    rel_m = re.search(r"через\\s+(\\d+)\\s*(час|ч)", txt)
    if rel_m:
        rel_hours = int(rel_m.group(1))
    rel_m = re.search(r"через\\s+(\\d+)\\s*(мин|минут)", txt)
    if rel_m:
        rel_minutes = int(rel_m.group(1))
    # дата: завтра/сегодня/день недели
    day_offset = 0
    target_weekday = None
    if "завтра" in txt:
        day_offset = 1
    elif "послезавтра" in txt:
        day_offset = 2
    for wd_word, wd in WEEKDAY_MAP.items():
        if wd_word in txt:
            target_weekday = wd
            break
    # частота — если есть "каждый"
    freq_days = None
    if "каждый" in txt or "каждые" in txt:
        if target_weekday is not None:
            freq_days = 7
        else:
            # если нет явного дня недели — раз в сутки
            freq_days = 1
    # текст напоминания — всё после времени или слово после "напомни"
    title = ""
    if hhmm:
        title = txt.split(hhmm, 1)[1].strip()
    elif "напомни" in txt:
        title = txt.split("напомни", 1)[1].strip()
    elif "напиши" in txt:
        title = txt.split("напиши", 1)[1].strip()
    return ParsedCommand(
        type="reminder",
        payload={
            "time": hhmm,
            "title": title,
            "rel_hours": rel_hours,
            "rel_minutes": rel_minutes,
            "day_offset": day_offset,
            "target_weekday": target_weekday,
            "freq_days": freq_days,
        },
    )


def parse_home(text: str) -> Optional[ParsedCommand]:
    txt = text.lower()
    if "уборка сейчас" in txt or "убраться" in txt:
        return ParsedCommand(type="home", payload={"action": "clean_now"})
    if "план" in txt and "дом" in txt:
        return ParsedCommand(type="home", payload={"action": "home_plan"})
    if "перенеси" in txt or "отложи" in txt:
        m = re.search(r"(\\d+)", txt)
        days = int(m.group(1)) if m else 1
        return ParsedCommand(type="home", payload={"action": "postpone", "days": days})
    if "дом" in txt or "что по дому" in txt:
        return ParsedCommand(type="home", payload={"action": "home_menu"})
    return None


def parse_ask(text: str) -> Optional[ParsedCommand]:
    txt = text.lower()
    if any(k in txt for k in ["стирать", "стирка", "джинсы", "прач"]):
        cat = "stirka"
    elif any(k in txt for k in ["воняет", "запах", "раковин", "холодил"]):
        cat = "zapahi"
    elif any(k in txt for k in ["переезд", "квартира", "снять"]):
        cat = "kv"
    elif any(k in txt for k in ["готов", "что приготовить", "рецепт", "еда"]):
        cat = "kuhnya"
    else:
        cat = "generic"
    return ParsedCommand(type="ask", payload={"category": cat, "question": text})


def parse_command(text: str) -> Optional[ParsedCommand]:
    for parser in (parse_expense, parse_reminder, parse_home, parse_ask):
        res = parser(text)
        if res:
            return res
    return None
