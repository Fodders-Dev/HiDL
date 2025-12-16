"""
Knowledge Base Service.

Provides access to tips, recipes, affirmations, and other structured knowledge
stored in JSON files under data/knowledge/.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Dict, List, Optional, Any


# Default path to knowledge files (relative to project root)
KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "data" / "knowledge"


class KnowledgeService:
    """
    Service for querying the HiDL knowledge base.
    
    Loads JSON files on initialization and provides methods to:
    - Get a random tip from a specific topic
    - Search tips by keyword
    - Get all tips in a category
    """

    def __init__(self, knowledge_dir: Path = KNOWLEDGE_DIR) -> None:
        self._knowledge_dir = knowledge_dir
        self._data: Dict[str, Any] = {}
        self._load_all()

    def _load_all(self) -> None:
        """Load all JSON files from knowledge directory."""
        if not self._knowledge_dir.exists():
            return
        for file_path in self._knowledge_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    self._data[file_path.stem] = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                # Silently skip malformed files
                pass

    @staticmethod
    def _iter_items(data: Any) -> List[Any]:
        """Normalize knowledge payloads to a flat list of items.

        Поддерживаем два формата:
        - dict: {"tips": [...]} / {"affirmations": [...]} / {"recipes": [...]}
        - list: [...] (например, recipes_core.json)
        """
        if not data:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("tips") or data.get("affirmations") or data.get("recipes") or []
        return []

    def get_random_tip(self, topic: str = "cleaning_tips") -> Optional[str]:
        """
        Get a random tip from the specified topic.
        
        Args:
            topic: One of 'cleaning_tips', 'affirmations', 'self_care', 'recipes'
        
        Returns:
            Formatted tip string or None if topic not found.
        """
        data = self._data.get(topic)
        if not data:
            return None
        
        items = self._iter_items(data)
        if not items:
            return None
        
        item = random.choice(items)
        
        # Format based on item type
        if isinstance(item, str):
            return f"💭 {item}"
        if isinstance(item, dict):
            if "title" in item and "text" in item:
                return f"💡 **{item['title']}**\n\n{item['text']}"
            if "title" in item and "desc" in item:
                return f"💡 **{item['title']}**\n\n{item.get('desc','')}"
            if "text" in item:
                return f"💭 {item['text']}"
        return None

    def get_random_affirmation(self, category: Optional[str] = None, categories: Optional[List[str]] = None) -> Optional[str]:
        """
        Get a random affirmation, optionally filtered by category or categories.
        
        Supports both old format (affirmations.json) and new format (affirmations_full.json).
        """
        # Try new format first (affirmations_full.json)
        data = self._data.get("affirmations_full")
        if data and isinstance(data, dict):
            # New format: {"motivation": [...], "calm": [...], ...}
            all_affirmations = []
            
            if categories:
                # Multiple categories selected
                for cat in categories:
                    cat_items = data.get(cat, [])
                    all_affirmations.extend(cat_items)
            elif category:
                # Single category
                all_affirmations = data.get(category, [])
            else:
                # All categories
                for cat_items in data.values():
                    if isinstance(cat_items, list):
                        all_affirmations.extend(cat_items)
            
            if all_affirmations:
                item = random.choice(all_affirmations)
                return f"💭 {item}"
        
        # Fallback to old format (affirmations.json)
        data = self._data.get("affirmations")
        if not data:
            return None
        
        affirmations = data.get("affirmations", [])
        if category:
            affirmations = [a for a in affirmations if a.get("category") == category]
        
        if not affirmations:
            return None
        
        item = random.choice(affirmations)
        return f"💭 {item['text']}"

    def get_random_cleaning_tip(self, category: Optional[str] = None) -> Optional[str]:
        """Get a random cleaning tip, optionally filtered by category."""
        data = self._data.get("cleaning_tips")
        if not data:
            return None
        
        tips = data.get("tips", [])
        if category:
            tips = [t for t in tips if t.get("category") == category]
        
        if not tips:
            return None
        
        item = random.choice(tips)
        return f"🧹 **{item['title']}**\n\n{item['text']}"

    def get_random_recipe(self, category: Optional[str] = None) -> Optional[str]:
        """Get a random recipe, optionally filtered by category."""
        data = self._data.get("recipes")
        if not data:
            return None
        
        recipes = data.get("recipes", [])
        if category:
            recipes = [r for r in recipes if r.get("category") == category]
        
        if not recipes:
            return None
        
        item = random.choice(recipes)
        ingredients = ", ".join(item.get("ingredients", []))
        time_str = f"⏱ {item['time_minutes']} мин" if "time_minutes" in item else ""
        
        return f"🍳 **{item['title']}** {time_str}\n\n📝 {ingredients}\n\n{item['text']}"

    def search(self, query: str, topic: Optional[str] = None) -> List[str]:
        """
        Search all knowledge bases for items containing the query.
        
        Returns list of formatted results.
        """
        results: List[str] = []
        query_lower = query.lower()
        
        topics_to_search = [topic] if topic else list(self._data.keys())
        
        for t in topics_to_search:
            data = self._data.get(t)
            if not data:
                continue
            
            items = self._iter_items(data)
            
            for item in items:
                if isinstance(item, str):
                    if query_lower in item.lower():
                        results.append(f"💭 {item}")
                    continue
                if not isinstance(item, dict):
                    continue

                tags = item.get("tags") or []
                if isinstance(tags, list):
                    tags_str = " ".join(str(x) for x in tags)
                else:
                    tags_str = str(tags)

                ingredients = item.get("ingredients") or []
                if isinstance(ingredients, list):
                    ing_str = " ".join(
                        str(x.get("name") if isinstance(x, dict) else x) for x in ingredients
                    )
                else:
                    ing_str = str(ingredients)

                steps = item.get("steps") or []
                if isinstance(steps, list):
                    steps_str = " ".join(str(x) for x in steps)
                else:
                    steps_str = str(steps)

                searchable = " ".join(
                    [
                        str(item.get("title", "")),
                        str(item.get("text", "")),
                        str(item.get("desc", "")),
                        tags_str,
                        ing_str,
                        steps_str,
                    ]
                ).lower()
                if query_lower in searchable:
                    if item.get("title") and item.get("text"):
                        results.append(f"💡 **{item['title']}**: {item['text']}")
                    elif item.get("title") and item.get("desc"):
                        results.append(f"💡 **{item['title']}**: {item.get('desc','')}")
                    elif item.get("text"):
                        results.append(f"💭 {item['text']}")
                    elif item.get("title"):
                        results.append(f"💭 {item['title']}")
        
        return results

    def get_self_care_tip(self, category: Optional[str] = None) -> Optional[str]:
        """Get a random self-care tip."""
        data = self._data.get("self_care")
        if not data:
            return None
        
        tips = data.get("tips", [])
        if category:
            tips = [t for t in tips if t.get("category") == category]
        
        if not tips:
            return None
        
        item = random.choice(tips)
        return f"✨ **{item['title']}**\n\n{item['text']}"

    def list_categories(self, topic: str) -> List[str]:
        """List available categories for a topic."""
        data = self._data.get(topic)
        if not data or not isinstance(data, dict):
            return []
        return list(data.get("categories", {}).keys())


# Global instance for easy access
_service: Optional[KnowledgeService] = None


def get_knowledge_service() -> KnowledgeService:
    """Get or create the global KnowledgeService instance."""
    global _service
    if _service is None:
        _service = KnowledgeService()
    return _service
