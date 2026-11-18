"""Гибридный разбор свободных текстовых и расшифрованных голосовых сообщений.

LLM отвечает только за понимание и извлечение структурных кандидатов.
Временные вычисления, защита от ложных обязательств и fallback без сети
выполняются детерминированным кодом Python.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from llm_client import LLMClient, client as default_llm_client
from utils.time import format_date_display, tzinfo_from_string

VALID_KINDS = {"task", "reminder", "shopping", "diary", "idea", "done"}

WEEKDAY_STEMS = {
    "понедель": 0,
    "вторник": 1,
    "сред": 2,
    "четвер": 3,
    "пятниц": 4,
    "суббот": 5,
    "воскрес": 6,
}

SHORT_WEEKDAYS = {
    "пн": 0,
    "вт": 1,
    "ср": 2,
    "чт": 3,
    "пт": 4,
    "сб": 5,
    "вс": 6,
}

_IDEA_GUARDRAIL_RE = re.compile(
    r"(?iu)(?:\bкогда-?нибудь\b|\bхочу\s+когда-?нибудь\b|\bмечтаю\b|"
    r"\bпридумал[аи]?\s+иде\w*|\bиде[яюией]\b|\bзадумк\w*|\bбыло\s+бы\s+круто\b|"
    r"\bможно\s+было\s+бы\b|\bхотелось\s+бы\s+как-?нибудь\b)"
)

_DIARY_FEELING_RE = re.compile(
    r"(?iu)(?:\bустал[аи]?\b|\bвыжат[аы]?\b|\bвыгорел[аи]?\b|\bтревож\w*|\bтревог\w*|"
    r"\bне\s+могу\s+заснуть\b|\bбессонниц\w*|\bплохо\s+спал[аи]?\b|\bтяжело\b|"
    r"\bнет\s+сил\b|\bмало\s+сил\b|\bгрустн\w*|\bраздраж\w*|\bпережива\w*|"
    r"\bголова\s+болит\b|\bболит\b|\bнастроени\w*|\bчувствую\b)"
)

_EXPLICIT_OBLIGATION_RE = re.compile(
    r"(?iu)(?:\bнапомн\w*|\bнадо\b|\bнужно\b|\bне\s+забыть\b|\bзапланируй\b|\bдобавь\s+задач\w*)"
)

_REMINDER_TRIGGER_RE = re.compile(
    r"(?iu)(?:\bнапомн\w*|\bне\s+дать\s+забыть\b|\bпоставь\s+напоминан\w*|\bнапоминалк\w*)"
)

_SHOPPING_TRIGGER_RE = re.compile(
    r"(?iu)(?:\bзакончил(?:ся|ась|ись|ось)\b|\bкончил(?:ся|ась|ись|ось)\b|"
    r"\bнадо\s+купить\b|\bнужно\s+купить\b|\bдокупить\b|\bв\s+список\s+покупок\b|"
    r"\bдобавь\s+в\s+покупки\b|\bкупить\b)"
)

_TASK_TRIGGER_RE = re.compile(
    r"(?iu)(?:\bнадо\b|\bнужно\b|\bнеобходимо\b|\bне\s+забыть\b|\bпредстоит\b|"
    r"\bзаняться\b|\bдобавь\s+в\s+план\b|\bсделать\b)"
)

_DONE_TRIGGER_RE = re.compile(
    r"(?iu)(?:\bуже\s+(?:сделал|купил|оплатил|помыл|убрал|приготовил|позвонил|отправил)[аи]?\b|"
    r"^(?:сделал|выполнил|закрыл|доделал|помыл|убрал|оплатил|приготовил|вынес|позвонил|"
    r"отправил|сходил|проверил|купил|поел|поспал|встал)[аи]?\b)"
)

_VAGUE_WHEN_RE = re.compile(
    r"(?iu)(?:\bпотом\b|\bпозже\b|\bкак-?нибудь\b|\bкогда-?нибудь\b|\bскоро\b|"
    r"\bна\s+днях\b|\bна\s+следующей\s+неделе\b|\bна\s+этой\s+неделе\b|\bна\s+неделе\b|"
    r"\bв\s+следующ\w+\s+(?:понедель|вторник|сред|четвер|пятниц|суббот|воскрес)\w*\b)"
)

_RELATIVE_DATE_MARKER_RE = re.compile(
    r"(?iu)(?:\bсегодня\b|\bзавтра\b|\bпослезавтра\b|\bчерез\s+\d+\s*(?:дн|день|дня|дней|суток)\b|"
    r"\bпонедель|\bвторник|\bсред|\bчетвер|\bпятниц|\bсуббот|\bвоскрес|"
    r"\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\b|\b\d{4}-\d{2}-\d{2}\b)"
)

_SECONDARY_SPLIT_RE = re.compile(
    r",\s*(?=(?:а\s+ещё|и\s+ещё|ещё|дома|надо|нужно|напомн\w*|сегодня|вчера|кстати|"
    r"купить|докупить|закончил\w*|кончил\w*|придумал\w*)\b)",
    re.IGNORECASE,
)

_INBOX_SYSTEM_PROMPT = """Ты — модуль разбора входящих сообщений личной помощницы HiDL.
Раздели входное сообщение пользователя на 1 или более коротких структурированных элементов.

Допустимые типы (kind):
- "task": конкретное дело/задача, когда пользователь явно выразил намерение сделать это (например: "надо заняться поздравлением", "нужно записаться к врачу").
- "reminder": явное поручение напомнить или сделать что-то ко времени/дате (например: "напомни завтра в 12:00...").
- "shopping": что нужно купить или что закончилось дома (например: "дома закончилась гречка", "купить овсяное молоко").
- "diary": мысли, переживания, усталость, самочувствие, события дня без явного поручения.
- "idea": задумки, мечты, "хочу когда-нибудь...", проекты на будущее.
- "done": сообщение о том, что пользователь уже сделал.

Строгие правила:
1. Мысль, усталость или мечта ("хочу когда-нибудь", "сегодня устал") НИКОГДА не должна становиться "task" или "reminder".
2. Сохраняй "source_quote" дословным фрагментом исходного текста пользователя.
3. Если добавляешь краткий комментарий, пиши его только в "model_note", не смешивай с "source_quote" и не выдумывай факты о пользователе.
4. Для "shopping" пиши чистое название товара в "title" (например "Гречка", "Овсяное молоко"), при наличии укажи quantity (число) и unit ("шт", "пач", "банк", "кг", "г", "л", "уп").
5. Для "reminder" вынеси "date_hint" (если есть, напр. "завтра", "19.11.2026") и "time_hint" (если есть, напр. "12:00", "в 8", "утром"). Не вычисляй календарные даты сам, передай исходную подсказку.
6. Отвечай строго валидным JSON объектом формата:
{
  "items": [
    {
      "kind": "task|reminder|shopping|diary|idea|done",
      "title": "короткий заголовок по-русски",
      "source_quote": "дословный фрагмент",
      "model_note": "",
      "date_hint": "",
      "time_hint": "",
      "quantity": 1,
      "unit": "шт",
      "tags": []
    }
  ]
}"""


@dataclass
class ResolvedSchedule:
    resolved_date: Optional[str]
    resolved_time: Optional[str]
    is_ambiguous: bool
    ambiguity_reason: Optional[str] = None
    candidate_times: list[str] = field(default_factory=list)
    question: Optional[str] = None


@dataclass
class ParsedInboxItem:
    kind: str
    title: str
    source_quote: str
    model_note: str = ""
    tags: list[str] = field(default_factory=list)
    quantity: float = 1.0
    unit: str = "шт"
    schedule: Optional[ResolvedSchedule] = None


@dataclass
class InboxParseResult:
    items: list[ParsedInboxItem]
    used_llm: bool
    llm_provider: str = ""
    llm_model: str = ""
    llm_raw_json: str = ""
    llm_error: Optional[str] = None


def compute_one_time_last_sent(
    target_date: Optional[str], today_date: str, freq: int = 9999
) -> Optional[str]:
    """Сместить last_sent назад на freq-delta дней для будущего одноразового напоминания."""
    if not target_date or target_date <= today_date:
        return None
    try:
        due = datetime.date.fromisoformat(target_date)
    except ValueError:
        return None
    return (due - datetime.timedelta(days=freq)).isoformat()


def _to_local_now(now_utc: datetime.datetime, tz_str: str) -> datetime.datetime:
    if now_utc.tzinfo is None:
        utc_dt = now_utc.replace(tzinfo=datetime.timezone.utc)
    else:
        utc_dt = now_utc.astimezone(datetime.timezone.utc)
    return utc_dt.astimezone(tzinfo_from_string(tz_str))


def _extract_relative_delta(src: str) -> Optional[datetime.timedelta]:
    txt = src.lower()
    if "полчас" in txt:
        return datetime.timedelta(minutes=30)
    m = re.search(r"через\s+(\d+)\s*(мин\w*|м)\b", txt)
    if m:
        return datetime.timedelta(minutes=int(m.group(1)))
    m = re.search(r"через\s+(\d+)\s*(час\w*|ч)\b", txt)
    if m:
        return datetime.timedelta(hours=int(m.group(1)))
    if re.search(r"\bчерез\s+час\b", txt):
        return datetime.timedelta(hours=1)
    return None


def _extract_date_from_text(src: str, local_today: datetime.date) -> Optional[datetime.date]:
    txt = src.lower()
    if "послезавтра" in txt:
        return local_today + datetime.timedelta(days=2)
    if "завтра" in txt:
        return local_today + datetime.timedelta(days=1)
    if "сегодня" in txt:
        return local_today

    m_days = re.search(r"через\s+(\d+)\s*(?:день|дня|дней|дн|суток)\b", txt)
    if m_days:
        return local_today + datetime.timedelta(days=int(m_days.group(1)))

    m_iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", txt)
    if m_iso:
        try:
            return datetime.date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        except ValueError:
            pass

    m_dmy = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", txt)
    if m_dmy:
        try:
            return datetime.date(int(m_dmy.group(3)), int(m_dmy.group(2)), int(m_dmy.group(1)))
        except ValueError:
            pass

    # DD.MM without year (ignore HH.MM preceded by "в"/"на")
    for m_dm in re.finditer(r"(?<![вк]\s)\b(\d{1,2})\.(\d{1,2})\b(?!\.\d)", txt):
        dd, mm = int(m_dm.group(1)), int(m_dm.group(2))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            try:
                cand = datetime.date(local_today.year, mm, dd)
            except ValueError:
                continue
            if cand < local_today:
                try:
                    cand = datetime.date(local_today.year + 1, mm, dd)
                except ValueError:
                    continue
            return cand

    for stem, wd in WEEKDAY_STEMS.items():
        if re.search(rf"\b{stem}\w*\b", txt):
            delta = (wd - local_today.weekday()) % 7
            if delta == 0:
                delta = 7
            return local_today + datetime.timedelta(days=delta)

    for short_wd, wd in SHORT_WEEKDAYS.items():
        if re.search(rf"\b(?:в|во|на)\s+{short_wd}\b", txt):
            delta = (wd - local_today.weekday()) % 7
            if delta == 0:
                delta = 7
            return local_today + datetime.timedelta(days=delta)

    return None


def _extract_time_from_text(
    src: str,
) -> tuple[Optional[str], list[str], Optional[str]]:
    """Return (resolved_hhmm, ambiguous_candidates, part_of_day)."""
    txt = src.lower()

    # 1. Explicit HH:MM or "в/на/к HH.MM"
    m_hhmm = re.search(r"\b(\d{1,2}):(\d{2})\b", txt)
    if not m_hhmm:
        m_hhmm = re.search(r"\b(?:в|на|к)\s*(\d{1,2})\.(\d{2})\b", txt)
    if m_hhmm:
        hh, mm = int(m_hhmm.group(1)), int(m_hhmm.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}", [], None

    # 2. "12 часов 30" / "8 ч 15"
    m_hm = re.search(r"\b(\d{1,2})\s*(?:час\w*|ч)\s+(\d{1,2})(?:\s*мин\w*)?\b", txt)
    if m_hm:
        hh, mm = int(m_hm.group(1)), int(m_hm.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}", [], None

    # 3. "в 8 утра / в 8 вечера / в 3 дня / в 2 ночи"
    m_qual = re.search(
        r"(?:\b(?:в|на|к)\s*)?(\d{1,2})\s*(?:час\w*\s*)?(утра|дня|вечера|ночи)\b",
        txt,
    )
    if m_qual:
        hh = int(m_qual.group(1))
        qual = m_qual.group(2)
        if 0 <= hh <= 23:
            if qual == "дня" and 1 <= hh <= 5:
                hh += 12
            elif qual == "вечера" and 1 <= hh <= 11:
                hh += 12
            elif qual == "ночи" and hh == 12:
                hh = 0
            return f"{hh:02d}:00", [], None

    if "полдень" in txt:
        return "12:00", [], None
    if "полночь" in txt:
        return "00:00", [], None

    # 4. Bare "в 8" / "на 10" / "к 15" (excluding "в 2 раза" or "через 8")
    m_bare = re.search(
        r"(?<!через\s)\b(?:в|на|к)\s*(\d{1,2})(?:\s*час\w*)?(?!\s*(?:[:.]\d|\d|мин|дн|раз|шт|₽|руб))",
        txt,
    )
    if m_bare:
        hh = int(m_bare.group(1))
        if hh == 0 or 12 <= hh <= 23:
            return f"{hh:02d}:00", [], None
        if 1 <= hh <= 11:
            return None, [f"{hh:02d}:00", f"{hh + 12:02d}:00"], None

    # 5. Standalone hour e.g. when user answers "14:00" or "8" in clarification
    m_only = re.match(r"^\s*(\d{1,2})\s*$", txt)
    if m_only:
        hh = int(m_only.group(1))
        if hh == 0 or 12 <= hh <= 23:
            return f"{hh:02d}:00", [], None
        if 1 <= hh <= 11:
            return None, [f"{hh:02d}:00", f"{hh + 12:02d}:00"], None

    # 6. Part of day without explicit hour
    if re.search(r"\bутр(?:ом|а)\b", txt):
        return None, [], "morning"
    if re.search(r"\bдн(?:ём|ем)\b", txt):
        return None, [], "day"
    if re.search(r"\bвечер(?:ом|а)\b", txt):
        return None, [], "evening"
    if re.search(r"\bночь(?:ю)?\b", txt):
        return None, [], "night"

    return None, [], None


def resolve_temporal_expression(
    date_hint: Optional[str],
    time_hint: Optional[str],
    raw_fragment: str,
    now_utc: datetime.datetime,
    tz_str: str,
) -> ResolvedSchedule:
    """Вычислить дату и время напоминания строго по часовому поясу пользователя."""
    local_now = _to_local_now(now_utc, tz_str)
    local_today = local_now.date()

    raw_lower = (raw_fragment or "").lower()
    combined_for_rel = " ".join(x for x in (raw_fragment or "", date_hint or "", time_hint or "") if x)

    # 1. Relative offset ("через 2 часа", "через 30 минут")
    rel_delta = _extract_relative_delta(combined_for_rel)
    if rel_delta is not None:
        target_dt = local_now + rel_delta
        return ResolvedSchedule(
            resolved_date=target_dt.date().isoformat(),
            resolved_time=target_dt.strftime("%H:%M"),
            is_ambiguous=False,
        )

    # 2. Vague wording check on raw fragment
    has_vague = bool(_VAGUE_WHEN_RE.search(raw_lower))
    has_explicit_cal = bool(
        re.search(r"\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\b|\b\d{4}-\d{2}-\d{2}\b", raw_lower)
    )
    if has_vague and not has_explicit_cal:
        # Preserve time if user wrote "потом в 18:00" -> still vague date
        return ResolvedSchedule(
            resolved_date=None,
            resolved_time=None,
            is_ambiguous=True,
            ambiguity_reason="vague_when",
            candidate_times=[],
            question="Не уверена в дате и времени. Напиши точнее, например: завтра в 12:00.",
        )

    # 3. Date resolution: prefer raw_fragment if it contains any date expression
    date_obj: Optional[datetime.date] = None
    if _RELATIVE_DATE_MARKER_RE.search(raw_lower):
        date_obj = _extract_date_from_text(raw_lower, local_today)
    if date_obj is None and date_hint:
        date_obj = _extract_date_from_text(date_hint, local_today)
    if date_obj is None and raw_lower:
        date_obj = _extract_date_from_text(raw_lower, local_today)

    # 4. Time resolution: check raw_fragment first, then time_hint
    hhmm, ampm_candidates, part_of_day = _extract_time_from_text(raw_lower)
    if hhmm is None and not ampm_candidates and time_hint:
        hhmm, ampm_candidates, hint_part = _extract_time_from_text(time_hint)
        part_of_day = part_of_day or hint_part

    if ampm_candidates:
        resolved_date_str = date_obj.isoformat() if date_obj else None
        date_note = f" на {format_date_display(resolved_date_str)}" if resolved_date_str else ""
        return ResolvedSchedule(
            resolved_date=resolved_date_str,
            resolved_time=None,
            is_ambiguous=True,
            ambiguity_reason="ambiguous_ampm",
            candidate_times=ampm_candidates,
            question=f"Уточни время{date_note}: {ampm_candidates[0]} или {ampm_candidates[1]}?",
        )

    if date_obj is not None and hhmm is None:
        presets = {
            "morning": ["09:00", "11:00"],
            "day": ["13:00", "15:00"],
            "evening": ["19:00", "21:00"],
            "night": ["22:00", "23:00"],
        }.get(part_of_day or "", ["10:00", "14:00", "19:00"])
        date_iso = date_obj.isoformat()
        return ResolvedSchedule(
            resolved_date=date_iso,
            resolved_time=None,
            is_ambiguous=True,
            ambiguity_reason="missing_time",
            candidate_times=presets,
            question=f"Во сколько напомнить {format_date_display(date_iso)}?",
        )

    if date_obj is None and hhmm is not None:
        parsed_t = datetime.datetime.strptime(hhmm, "%H:%M").time()
        target_d = local_today if parsed_t > local_now.time() else local_today + datetime.timedelta(days=1)
        return ResolvedSchedule(
            resolved_date=target_d.isoformat(),
            resolved_time=hhmm,
            is_ambiguous=False,
        )

    if date_obj is None and hhmm is None:
        return ResolvedSchedule(
            resolved_date=None,
            resolved_time=None,
            is_ambiguous=True,
            ambiguity_reason="vague_when",
            candidate_times=[],
            question="На какую дату и время поставить напоминание?",
        )

    return ResolvedSchedule(
        resolved_date=date_obj.isoformat(),
        resolved_time=hhmm,
        is_ambiguous=False,
    )


def _capitalize_first(text: str) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip(" ,.;:-"))
    if not clean:
        return ""
    return clean[0].upper() + clean[1:]


def _clean_shopping_title(raw: str) -> str:
    txt = (raw or "").strip()
    patterns = [
        r"(?iu)^(?:у\s+нас\s+)?(?:дома\s+)?(?:совсем\s+)?(?:уже\s+)?(?:закончил(?:ся|ась|ись|ось)|кончил(?:ся|ась|ись|ось))\s+",
        r"(?iu)^(?:дома\s+)?(?:надо|нужно|пора)\s+(?:бы\s+)?(?:купить|докупить)\s+",
        r"(?iu)^(?:добавь\s+в\s+(?:список\s+)?покупки?\s+)",
        r"(?iu)^(?:купить|докупить)\s+",
        r"(?iu)\b(?:дома|в\s+холодильнике)\b",
    ]
    for pat in patterns:
        txt = re.sub(pat, " ", txt)
    # Strip quantity words at start or end like "2 пачки", "полпачки", "1 банка"
    txt = re.sub(
        r"(?iu)\b(?:\d+(?:[.,]\d+)?\s*(?:пач\w*|уп\w*|банк\w*|шт\w*|кг|г|гр|л|мл)\.?|"
        r"половин\w*\s+упаковк\w*|полпачки|пачк[ауи]|банк[ауи])\b",
        " ",
        txt,
    )
    cleaned = _capitalize_first(txt)
    return cleaned or _capitalize_first(raw)


def _extract_shopping_qty_unit(raw: str) -> tuple[float, str]:
    txt = (raw or "").lower()
    if re.search(r"полпачк|половин\w*\s+упаковк|пол\s+пачк", txt):
        return 0.5, "уп"
    if re.search(r"полбанк", txt):
        return 0.5, "банк"
    m = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(пач\w*|уп\w*|банк\w*|шт\w*|кг|мл|л|гр|г)\b",
        txt,
    )
    if m:
        qty = float(m.group(1).replace(",", "."))
        u_raw = m.group(2)
        if u_raw.startswith("пач"):
            unit = "пач"
        elif u_raw.startswith("уп"):
            unit = "уп"
        elif u_raw.startswith("банк"):
            unit = "банк"
        elif u_raw in {"г", "гр"}:
            unit = "г"
        elif u_raw in {"кг", "л", "мл"}:
            unit = u_raw
        else:
            unit = "шт"
        return qty, unit
    if re.search(r"\bпачк[ауи]\b", txt):
        return 1.0, "пач"
    if re.search(r"\bбанк[ауи]\b", txt):
        return 1.0, "банк"
    if re.search(r"\bупаковк[ауи]\b", txt):
        return 1.0, "уп"
    return 1.0, "шт"


def _clean_task_title(raw: str) -> str:
    txt = (raw or "").strip()
    patterns = [
        r"(?iu)^(?:а\s+)?(?:ещё\s+)?(?:сегодня\s+)?(?:надо|нужно|необходимо|не\s+забыть|добавь\s+в\s+план)\s+",
        r"(?iu)^(?:надо|нужно)\s+",
    ]
    for pat in patterns:
        txt = re.sub(pat, "", txt).strip()
    return _capitalize_first(txt) or _capitalize_first(raw)


def _clean_idea_title(raw: str) -> str:
    txt = (raw or "").strip()
    txt = re.sub(r"(?iu)^(?:а\s+)?(?:ещё\s+)?(?:придумал[аи]?\s+)", "", txt).strip()
    txt = re.sub(r"(?iu)^(?:хочу\s+когда-?нибудь\s+|когда-?нибудь\s+хочу\s+|когда-?нибудь\s+)", "", txt).strip()
    txt = re.sub(r"(?iu)^идею\b", "Идея", txt)
    return _capitalize_first(txt) or _capitalize_first(raw)


def _clean_reminder_title(raw: str, resolved_time: Optional[str]) -> str:
    txt = (raw or "").strip()
    patterns = [
        r"(?iu)^(?:пожалуйста\s+)?(?:напомни(?:\s+мне)?|поставь\s+напоминание|сделай\s+напоминание)\s*",
        r"(?iu)\b(?:сегодня|завтра|послезавтра|потом|позже|утром|днём|вечером|ночью)\b",
        r"(?iu)\bчерез\s+\d+\s*(?:мин\w*|час\w*|ч|дн\w*|день|дня|дней)\b",
        r"(?iu)\b(?:в|во|на)\s+(?:понедель\w*|вторник\w*|сред\w*|четвер\w*|пятниц\w*|суббот\w*|воскрес\w*)\b",
        r"(?iu)\b(?:в|на|к)\s*\d{1,2}\s*[:.]\s*\d{2}\b",
        r"(?iu)\b(?:в|на|к)\s*\d{1,2}(?:\s*час\w*)?(?:\s*(?:утра|дня|вечера|ночи))?\b",
        r"(?iu)\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\b",
    ]
    for pat in patterns:
        txt = re.sub(pat, " ", txt)
    if resolved_time:
        txt = txt.replace(resolved_time, " ")
    return _capitalize_first(txt) or "Напоминание"


def _split_clauses(raw_text: str) -> list[str]:
    text = (raw_text or "").strip()
    if not text:
        return []
    primary = re.split(r"[.!?\n;]+", text)
    clauses: list[str] = []
    for chunk in primary:
        piece = chunk.strip(" ,")
        if not piece:
            continue
        subparts = _SECONDARY_SPLIT_RE.split(piece)
        for sub in subparts:
            clean = sub.strip(" ,")
            if clean:
                clauses.append(clean)
    return clauses


def _apply_guardrails(
    item: ParsedInboxItem,
    *,
    now_utc: datetime.datetime,
    tz_str: str,
    date_hint: str = "",
    time_hint: str = "",
) -> ParsedInboxItem:
    quote = (item.source_quote or item.title or "").strip()
    kind = item.kind if item.kind in VALID_KINDS else "diary"

    # Guardrail 1: wishes / future dreams / ideas are NEVER tasks or reminders
    if _IDEA_GUARDRAIL_RE.search(quote) and not _REMINDER_TRIGGER_RE.search(quote):
        kind = "idea"
        if not item.title or item.title == quote:
            item.title = _clean_idea_title(quote)

    # Guardrail 2: emotional state / feeling without explicit task/reminder trigger -> diary
    elif (
        kind in {"task", "reminder"}
        and _DIARY_FEELING_RE.search(quote)
        and not _EXPLICIT_OBLIGATION_RE.search(quote)
    ):
        kind = "diary"

    # Guardrail 3: clean shopping title & units
    if kind == "shopping":
        qty, unit = _extract_shopping_qty_unit(quote)
        if item.quantity == 1.0 and qty != 1.0:
            item.quantity = qty
        if item.unit == "шт" and unit != "шт":
            item.unit = unit
        if any(w in (item.title or "").lower() for w in ("закончил", "кончил", "дома", "купить")):
            item.title = _clean_shopping_title(item.title or quote)
        else:
            item.title = _capitalize_first(item.title or _clean_shopping_title(quote))

    elif kind == "reminder":
        schedule = resolve_temporal_expression(
            date_hint=date_hint,
            time_hint=time_hint,
            raw_fragment=quote,
            now_utc=now_utc,
            tz_str=tz_str,
        )
        item.schedule = schedule
        if not item.title or "напомн" in item.title.lower():
            item.title = _clean_reminder_title(quote, schedule.resolved_time)
        else:
            item.title = _clean_reminder_title(item.title, schedule.resolved_time)
    else:
        item.title = _capitalize_first(item.title or quote)

    item.kind = kind
    item.source_quote = quote
    return item


def _dedupe_items(items: list[ParsedInboxItem]) -> list[ParsedInboxItem]:
    seen: set[tuple[str, str]] = set()
    out: list[ParsedInboxItem] = []
    for it in items:
        key = (it.kind, re.sub(r"\s+", " ", (it.title or "").strip().lower()))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def parse_inbox_fallback(
    raw_text: str,
    now_utc: datetime.datetime,
    tz_str: str,
) -> list[ParsedInboxItem]:
    """Детерминированный разбор смешанного сообщения без обращения к LLM."""
    clauses = _split_clauses(raw_text)
    if not clauses:
        return []

    items: list[ParsedInboxItem] = []
    for clause in clauses:
        if _REMINDER_TRIGGER_RE.search(clause):
            schedule = resolve_temporal_expression(
                date_hint="",
                time_hint="",
                raw_fragment=clause,
                now_utc=now_utc,
                tz_str=tz_str,
            )
            item = ParsedInboxItem(
                kind="reminder",
                title=_clean_reminder_title(clause, schedule.resolved_time),
                source_quote=clause,
                schedule=schedule,
            )
        elif _IDEA_GUARDRAIL_RE.search(clause):
            item = ParsedInboxItem(
                kind="idea",
                title=_clean_idea_title(clause),
                source_quote=clause,
            )
        elif _SHOPPING_TRIGGER_RE.search(clause):
            qty, unit = _extract_shopping_qty_unit(clause)
            item = ParsedInboxItem(
                kind="shopping",
                title=_clean_shopping_title(clause),
                source_quote=clause,
                quantity=qty,
                unit=unit,
            )
        elif _DONE_TRIGGER_RE.search(clause) and not _DIARY_FEELING_RE.search(clause):
            item = ParsedInboxItem(
                kind="done",
                title=_capitalize_first(clause),
                source_quote=clause,
            )
        elif _DIARY_FEELING_RE.search(clause) and not _EXPLICIT_OBLIGATION_RE.search(clause):
            item = ParsedInboxItem(
                kind="diary",
                title=_capitalize_first(clause),
                source_quote=clause,
            )
        elif _TASK_TRIGGER_RE.search(clause):
            item = ParsedInboxItem(
                kind="task",
                title=_clean_task_title(clause),
                source_quote=clause,
            )
        else:
            item = ParsedInboxItem(
                kind="diary",
                title=_capitalize_first(clause),
                source_quote=clause,
            )
        items.append(
            _apply_guardrails(item, now_utc=now_utc, tz_str=tz_str)
        )

    return _dedupe_items(items)


_KIND_ALIASES = {
    "задача": "task",
    "дело": "task",
    "напоминание": "reminder",
    "покупка": "shopping",
    "покупки": "shopping",
    "список покупок": "shopping",
    "дневник": "diary",
    "мысль": "diary",
    "заметка": "diary",
    "идея": "idea",
    "сделано": "done",
    "выполнено": "done",
}


def validate_and_normalize_llm_items(
    raw_items: Any,
    *,
    raw_text: str,
    now_utc: datetime.datetime,
    tz_str: str,
) -> list[ParsedInboxItem]:
    if not isinstance(raw_items, list):
        return []
    out: list[ParsedInboxItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "diary").strip().lower()
        kind = _KIND_ALIASES.get(kind, kind)
        if kind not in VALID_KINDS:
            kind = "diary"
        source_quote = str(entry.get("source_quote") or entry.get("title") or raw_text).strip()
        title = str(entry.get("title") or source_quote).strip()
        model_note = str(entry.get("model_note") or "").strip()
        date_hint = str(entry.get("date_hint") or "").strip()
        time_hint = str(entry.get("time_hint") or "").strip()

        raw_tags = entry.get("tags")
        tags = (
            [str(t).strip() for t in raw_tags if str(t).strip()][:6]
            if isinstance(raw_tags, list)
            else []
        )

        try:
            qty = float(entry.get("quantity") or 1.0)
            if qty <= 0:
                qty = 1.0
        except (TypeError, ValueError):
            qty = 1.0
        unit = str(entry.get("unit") or "шт").strip() or "шт"

        item = ParsedInboxItem(
            kind=kind,
            title=title,
            source_quote=source_quote,
            model_note=model_note,
            tags=tags,
            quantity=qty,
            unit=unit,
        )
        out.append(
            _apply_guardrails(
                item,
                now_utc=now_utc,
                tz_str=tz_str,
                date_hint=date_hint,
                time_hint=time_hint,
            )
        )
    return _dedupe_items(out)


async def analyze_inbox_text(
    raw_text: str,
    now_utc: datetime.datetime,
    tz_str: str,
    llm_client_instance: Optional[LLMClient] = None,
) -> InboxParseResult:
    """Разобрать сообщение через настроенную LLM с отказоустойчивым fallback."""
    clean_text = (raw_text or "").strip()
    if not clean_text:
        return InboxParseResult(items=[], used_llm=False)

    client = llm_client_instance or default_llm_client
    if client.is_configured():
        local_now = _to_local_now(now_utc, tz_str)
        user_prompt = (
            f"Локальное время пользователя: {local_now.strftime('%d.%m.%Y %H:%M')} ({tz_str}).\n"
            f"Сообщение:\n{clean_text}"
        )
        res = await client.complete_json(_INBOX_SYSTEM_PROMPT, user_prompt, temperature=0.1)
        if res.ok and isinstance(res.data, dict):
            items = validate_and_normalize_llm_items(
                res.data.get("items"),
                raw_text=clean_text,
                now_utc=now_utc,
                tz_str=tz_str,
            )
            if items:
                return InboxParseResult(
                    items=items,
                    used_llm=True,
                    llm_provider=res.provider,
                    llm_model=res.model,
                    llm_raw_json=res.text,
                    llm_error=None,
                )
            llm_error = "empty_items"
        else:
            llm_error = res.error or "llm_failed"

        fallback_items = parse_inbox_fallback(clean_text, now_utc, tz_str)
        return InboxParseResult(
            items=fallback_items,
            used_llm=False,
            llm_provider=res.provider,
            llm_model=res.model,
            llm_raw_json=res.text,
            llm_error=llm_error,
        )

    fallback_items = parse_inbox_fallback(clean_text, now_utc, tz_str)
    return InboxParseResult(items=fallback_items, used_llm=False)
