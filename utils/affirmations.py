import json
import random
from pathlib import Path
from typing import Literal, Optional, List, Dict, Any

Tone = Literal["soft", "cheerful", "calm"]


_CACHE: List[Dict[str, Any]] = []


def _data_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "affirmations.json"


def load_affirmations() -> List[Dict[str, Any]]:
    """Загрузить аффирмации из файла, с простым кэшем в памяти."""
    global _CACHE
    if _CACHE:
        return _CACHE
    path = _data_path()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                _CACHE = [dict(item) for item in data]
            else:
                _CACHE = []
    except FileNotFoundError:
        _CACHE = []
    return _CACHE


def random_affirmation(category: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Вернуть случайную аффирмацию (опционально отфильтровав по категории)."""
    items = load_affirmations()
    if category:
        filtered = [a for a in items if a.get("category") == category]
        if filtered:
            items = filtered
    if not items:
        return None
    return random.choice(items)


def random_affirmation_text(category: Optional[str] = None) -> Optional[str]:
    """Короткий хелпер: вернуть только текст аффирмации или None."""
    aff = random_affirmation(category)
    if not aff:
        return None
    return str(aff.get("text", "")).strip() or None

