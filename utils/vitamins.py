import json
from pathlib import Path
from typing import List, Dict, Any


_CACHE: List[Dict[str, Any]] = []


def _data_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "vitamins_info.json"


def load_vitamins() -> List[Dict[str, Any]]:
    """Загрузить справочник витаминов."""
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


def vitamin_names() -> List[str]:
    return [v.get("name", "") for v in load_vitamins() if v.get("name")]


def get_vitamin(name: str) -> Dict[str, Any] | None:
    name_lower = name.strip().lower()
    for v in load_vitamins():
        if v.get("name", "").lower() == name_lower:
            return v
    return None

