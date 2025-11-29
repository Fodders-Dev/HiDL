import json
import random
from pathlib import Path
from typing import Dict, List, Optional

_CACHE: Optional[Dict[str, List[dict]]] = None


def load_tips() -> Dict[str, List[dict]]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = Path(__file__).resolve().parent.parent / "data" / "mom_tips.json"
    if not path.exists():
        _CACHE = {}
        return _CACHE
    with path.open("r", encoding="utf-8") as f:
        _CACHE = json.load(f)
    return _CACHE or {}


def pick_tip(category: str) -> Optional[dict]:
    tips = load_tips().get(category, [])
    if not tips:
        return None
    return random.choice(tips)


def find_tip_by_tag(query: str) -> Optional[dict]:
    """Simple keyword search across titles and tags."""
    q = query.lower()
    tips = load_tips()
    for cat, arr in tips.items():
        for tip in arr:
            hay = " ".join([tip.get("title", ""), " ".join(tip.get("tags", []))]).lower()
            if q in hay:
                return tip
    return None
