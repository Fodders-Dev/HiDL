import re
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple

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
    
    # Исключаем чистый формат времени (08:00, 23:30)
    if re.match(r"^\s*\d{1,2}:\d{2}\s*$", txt):
        return None
    
    # Ищем число, но не в формате времени
    # Исключаем паттерны типа "08:00" (время)
    m = re.search(r"(-|\+)?\s*(\d+[.,]?\d*)(?!\s*:)", txt)
    if not m:
        return None
    
    amount = float(m.group(2).replace(",", "."))
    sign = -1 if m.group(1) == "-" else 1
    amount *= sign
    
    # Категория: ищем слово после "на"
    # "потратила 500 на еду" → "еду"
    category = "другое"
    na_match = re.search(r"\bна\s+(\w+)", txt)
    if na_match:
        category = na_match.group(1)
    else:
        # Fallback: слово после числа (но не служебные слова)
        after = txt[m.end():].strip()
        parts = after.split()
        skip_words = {"рублей", "руб", "р", "на", "в", "и", "или", "за"}
        for part in parts:
            if part not in skip_words and len(part) > 1:
                category = part
                break
    
    return ParsedCommand(type="expense", payload={"amount": amount, "category": category})


def parse_reminder(text: str) -> Optional[ParsedCommand]:
    txt = text.lower().strip()
    # Support copy-paste from Telegram exports:
    # "Name, [17.12.2025 9:01]\nНапомни ..."
    export_prefix = r"^[^\n]*,\s*\[\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2}\]\s*\n?"
    txt = re.sub(export_prefix, "", txt).strip()
    title_src = re.sub(export_prefix, "", text).strip()

    # intent keywords
    if not any(k in txt for k in ("напом", "напиши", "напомин", "напоминалк")):
        return None

    explicit_repeat = bool(re.search(r"(кажд\w+|ежеднев|еженед|раз\s+в\s+нед|по\s+будням|по\s+выходн)", txt))

    def _extract_time(src: str) -> Tuple[Optional[str], Optional[int], Optional[int]]:
        """Return (hhmm, rel_hours, rel_minutes)."""
        m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", src)
        if m:
            return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}", None, None
        # "в 10", "в 10 часов"
        m = re.search(r"\bв\s*(\d{1,2})(?:\s*час\w*)?(?!\d)", src)
        if m:
            return f"{int(m.group(1)):02d}:00", None, None
        # "на 10" (часто пишут так же, как "в 10")
        m = re.search(r"\bна\s*(\d{1,2})(?:\s*час\w*)?(?!\d)", src)
        if m:
            return f"{int(m.group(1)):02d}:00", None, None
        m = re.search(r"через\s+(\d+)\s*(час|ч)", src)
        if m:
            return None, int(m.group(1)), None
        m = re.search(r"через\s+(\d+)\s*(мин|минут)", src)
        if m:
            return None, None, int(m.group(1))
        if "полчас" in src:
            return None, None, 30
        return None, None, None

    def _extract_day_offset(src: str) -> int:
        if "послезавтра" in src:
            return 2
        if "завтра" in src:
            return 1
        if "сегодня" in src:
            return 0
        m = re.search(r"через\s+(\d+)\s*(дн|день|дня)", src)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return 0
        return 0

    def _extract_weekday(src: str) -> Optional[int]:
        for wd_word, wd in WEEKDAY_MAP.items():
            if re.search(rf"\b{wd_word}\b", src):
                return wd
        return None

    def _extract_freq(src: str, target_wd: Optional[int]) -> Tuple[Optional[int], bool]:
        once = False
        if re.search(r"однократ|только\s+один\s+раз|разово", src):
            return 9999, True
        if re.search(r"ежеднев|кажд\w*\s+день", src):
            return 1, False
        if "через день" in src:
            return 2, False
        m = re.search(r"кажд\w*\s+(\d+)\s*дн", src)
        if m:
            try:
                return int(m.group(1)), False
            except Exception:
                pass
        if re.search(r"раз\s+в\s+нед", src) or "еженед" in src:
            return 7, False
        if re.search(r"по\s+будням", src):
            return 1, False
        if target_wd is not None and ("кажд" in src or "по " in src):
            return 7, False
        return None, once

    hhmm, rel_hours, rel_minutes = _extract_time(txt)
    day_offset = _extract_day_offset(txt)
    target_weekday = _extract_weekday(txt)
    freq_days, once_flag = _extract_freq(txt, target_weekday)
    if not explicit_repeat and freq_days is None:
        # safer default: if the user didn't say "every day/week/…", treat as one-time
        once_flag = True

    cleanup_patterns = [
        r"(?i)\bнапомни( мне)?\b",
        r"(?i)\bнапиши\b",
        r"(?i)\bсделай\s+напоминани\w*\b",
        r"(?i)\bсделай\s+напоминалк\w*\b",
        r"(?i)\bсоздай\s+напоминани\w*\b",
        r"(?i)\bсоздай\s+напоминалк\w*\b",
        r"(?i)\bпоставь\s+напоминание\b",
        r"(?i)\bзавтра\b",
        r"(?i)\bпослезавтра\b",
        r"(?i)\bсегодня\b",
        r"(?i)\bраз\s+в\s+неделю\b",
        r"(?i)\bкажд\w+\b",
        r"(?i)\bеженед\w*\b",
        r"(?i)\bежеднев\w*\b",
        r"(?i)\bчерез\s+\d+\s*(час\w*|ч|мин\w*|дн\w*)\b",
    ]
    if hhmm:
        try:
            hh, mm = hhmm.split(":")
            hh_i = int(hh)
            mm_i = int(mm)
        except Exception:
            hh_i, mm_i = None, None
        if hh_i is not None and mm_i is not None:
            cleanup_patterns.append(rf"(?i)\b(?:в|на)\s*{hh_i}\s*[:.]\s*{mm_i:02d}\b")
            if mm_i == 0:
                cleanup_patterns.append(rf"(?i)\b(?:в|на)\s*{hh_i}(?:\s*час\w*)?\b")
        cleanup_patterns.append(re.escape(hhmm))
    for pat in cleanup_patterns:
        title_src = re.sub(pat, " ", title_src)
    title = re.sub(r"\s+", " ", title_src).strip(" ,.;:-")

    return ParsedCommand(
        type="reminder",
        payload={
            "time": hhmm,
            "title": title or "Напоминание",
            "rel_hours": rel_hours,
            "rel_minutes": rel_minutes,
            "day_offset": day_offset,
            "target_weekday": target_weekday,
            "freq_days": freq_days,
            "one_time": once_flag,
        },
    )


def parse_home(text: str) -> Optional[ParsedCommand]:
    txt = text.lower()
    if "уборка сейчас" in txt or "убраться" in txt:
        return ParsedCommand(type="home", payload={"action": "clean_now"})
    if "план" in txt and "дом" in txt:
        return ParsedCommand(type="home", payload={"action": "home_plan"})
    if "перенеси" in txt or "отложи" in txt:
        m = re.search(r"(\d+)", txt)
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
    txt = (text or "").lower()
    reminder_first = any(k in txt for k in ("напом", "напомин", "напоминалк"))
    parsers = (parse_reminder, parse_expense, parse_home, parse_ask) if reminder_first else (parse_expense, parse_reminder, parse_home, parse_ask)
    for parser in parsers:
        res = parser(text)
        if res:
            return res
    return None
