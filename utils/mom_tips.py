import json
import random
from pathlib import Path
from typing import Dict, List, Optional

_CACHE: Optional[List[dict]] = None


def load_tips() -> List[dict]:
    """Load mom tips as flat list."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = Path(__file__).resolve().parent.parent / "data" / "mom_tips.json"
    if not path.exists():
        _CACHE = []
        return _CACHE
    with path.open("r", encoding="utf-8") as f:
        _CACHE = json.load(f)
    return _CACHE or []


def pick_tip(category: str) -> Optional[dict]:
    tips = [t for t in load_tips() if t.get("category") == category]
    if not tips:
        return None
    return random.choice(tips)


def find_tip_by_tag(query: str) -> Optional[dict]:
    """Simple keyword search across title/question_example/tags."""
    q = query.lower()
    for tip in load_tips():
        hay_parts = [
            tip.get("title", ""),
            tip.get("question_example", ""),
            " ".join(tip.get("tags", [])),
        ]
        hay = " ".join(hay_parts).lower()
        if any(token in hay for token in q.split()):
            return tip
    return None


def get_tip(tip_id: str) -> Optional[dict]:
    for tip in load_tips():
        if tip.get("id") == tip_id:
            return tip
    return None
