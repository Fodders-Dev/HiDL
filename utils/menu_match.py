"""Точное сопоставление пунктов меню и фильтрация коротких фраз без ложных срабатываний."""

from __future__ import annotations

import re
from typing import Optional

_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_MULTI_SPACE_RE = re.compile(r"\s+")

_MIXED_INTENT_MARKERS_RE = re.compile(
    r"(?iu)(?:\bустал[аи]?\b|\bвыгорел[аи]?\b|\bтревож\w*|\bпридумал\w*\b|\bиде[яюи]\b|"
    r"\bзакончил(?:ся|ась|ись|ось)\b|\bкончил(?:ся|ась|ись|ось)\b|"
    r",\s*(?:а\s+ещё|и\s+ещё|ещё|дома|сегодня|вчера|надо|нужно)\b)"
)

_VAGUE_REMINDER_RE = re.compile(
    r"(?iu)(?:\bпотом\b|\bпозже\b|\bкак-?нибудь\b|\bкогда-?нибудь\b|\bскоро\b|"
    r"\bна\s+днях\b|\bна\s+следующей\s+неделе\b|\bна\s+неделе\b|"
    r"\bв\s+следующ\w+\s+(?:понедель|вторник|сред|четвер|пятниц|суббот|воскрес)\w*\b)"
)

_AMBIGUOUS_HOUR_RE = re.compile(
    r"(?iu)(?<!через\s)\b(?:в|на|к)\s*([1-9]|1[01])(?:\s*час\w*)?(?!\s*(?:[:.]\d|\d|утра|дня|вечера|ночи|мин|дн))"
)


def normalize_menu_text(text: Optional[str]) -> str:
    """Убрать эмодзи, пунктуацию и лишние пробелы для точного сравнения с кнопками."""
    if not text:
        return ""
    cleaned = _NON_WORD_RE.sub(" ", text.lower()).replace("_", " ")
    return _MULTI_SPACE_RE.sub(" ", cleaned).strip()


def match_menu_label(text: Optional[str], labels: set[str]) -> bool:
    """Вернуть True только при точном совпадении нормализованной строки с кнопкой."""
    norm = normalize_menu_text(text)
    return bool(norm and norm in labels)


def looks_like_expense(text: Optional[str]) -> bool:
    """Проверить, действительно ли сообщение похоже на быструю запись траты."""
    if not text:
        return False
    raw = text.strip()
    lower = raw.lower()
    if not any(ch.isdigit() for ch in lower):
        return False
    if "напом" in lower:
        return False
    # Многочастные фразы ("Надо..., дома..., сегодня было 3 урока") разбираем в inbox.
    if "\n" in raw or len([p for p in re.split(r"[.!?;]+", raw) if p.strip()]) > 1:
        return False
    if _MIXED_INTENT_MARKERS_RE.search(lower):
        return False

    if any(k in lower for k in ("потрат", "запиши трату", "запиши расход", "стоил")):
        return True
    if re.match(r"^[+-]\s*\d+(?:[.,]\d+)?(?:\s+|$)", lower):
        return True

    words = lower.split()
    if len(words) <= 4 and re.match(
        r"^\d+(?:[.,]\d+)?\s+(?!"
        r"час\w*\b|мин\w*\b|сек\w*\b|дн\w*\b|недел\w*\b|месяц\w*\b|год\w*\b|лет\b|"
        r"урок\w*\b|раз\w*\b|подход\w*\b|повтор\w*\b|шаг\w*\b|"
        r"л\b|кг\b|г\b|гр\b|мл\b|мг\b|шт\b|км\b|метр\w*\b|м\b|"
        r"ккал\w*\b|kcal\b|калл\w*\b|калор\w*\b|"
        r"куск\w*\b|кусок\b|яйц\w*\b|яиц\b|пачк\w*\b|порц\w*\b|ложк\w*\b|стакан\w*\b|дольк\w*\b|"
        r"таблет\w*\b|капсул\w*\b|отжиман\w*\b|приседан\w*\b|подтягиван\w*\b|"
        r"январ\w*\b|феврал\w*\b|март\w*\b|апрел\w*\b|ма[йя]\b|июн\w*\b|июл\w*\b|август\w*\b|"
        r"сентябр\w*\b|октябр\w*\b|ноябр\w*\b|декабр\w*\b"
        r")[а-яёa-z]",
        lower,
    ):
        return True
    if len(words) <= 4 and re.search(r"\b\d+(?:[.,]\d+)?\s*(?:руб\w*|₽|р)\b", lower):
        return True
    return False


def is_single_clause_reminder(text: Optional[str]) -> bool:
    """Проверить, является ли сообщение простой односоставной командой напоминания."""
    if not text:
        return False
    raw = text.strip()
    lower = raw.lower()
    if "напом" not in lower:
        return False
    if raw.endswith("?"):
        return False
    if "\n" in raw:
        return False
    if re.search(
        r"(?iu)\b(?:удал\w*|отмен\w*|убер\w*|выключ\w*|перенес\w*|измен\w*|поменя\w*|какие|сколько|список|покажи)\b",
        lower,
    ):
        return False
    sentences = [p.strip() for p in re.split(r"[.!?;]+", raw) if p.strip()]
    if len(sentences) > 1:
        return False
    if _MIXED_INTENT_MARKERS_RE.search(lower):
        return False
    if _VAGUE_REMINDER_RE.search(lower):
        return False
    if _AMBIGUOUS_HOUR_RE.search(lower):
        return False
    return True
