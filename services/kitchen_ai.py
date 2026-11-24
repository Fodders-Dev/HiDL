"""Умный кулинарный движок HiDL: подбор блюд из холодильника, списание продуктов и меню на 3-4 дня."""

from __future__ import annotations

import asyncio
import base64
import datetime
from functools import lru_cache
import html as html_lib
import ipaddress
import json
import math
import re
import socket
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import aiohttp

from db import repositories as repo
from llm_client import LLMClient, client as default_llm_client
from services import fitness_nutrition, karma_service, pulse_service
from utils.pantry import format_quantity
from utils.rows import row_to_dict, rows_to_dicts
from utils.time import format_time_local, local_date_str

RECIPES_FILE = Path(__file__).resolve().parents[1] / "data" / "knowledge" / "recipes_core.json"

DIET_TAGS: Dict[str, Optional[Set[str]]] = {
    "omnivore": None,
    "vegetarian": {"vegetarian", "vegan"},
    "vegan": {"vegan"},
}

MEAL_SLOT_LABELS: Dict[str, str] = {
    "breakfast": "завтрак",
    "lunch": "обед",
    "dinner": "ужин",
}

ENERGY_MODES: Dict[str, str] = {
    "low": "⚡ До 15м",
    "normal": "🍳 20–30м",
    "shop": "🛒 +1–2",
}

STAPLE_INGREDIENTS: Set[str] = {
    "вода",
    "соль",
    "сахар",
    "перец",
    "черный перец",
    "чёрный перец",
    "перец черный",
    "специи",
    "приправа",
    "приправы",
    "корица",
    "карри",
    "паприка",
    "паприка копченая",
    "паприка копчёная",
    "куркума",
    "кипяток",
}

_CANONICAL_ALIASES: List[Tuple[str, Tuple[str, ...]]] = [
    ("peanut_butter", ("арахисовая паста", "арахисов")),
    ("tomato_paste", ("томатная паста", "пассата")),
    ("oil_butter", ("масло сливочное", "сливочное масло")),
    (
        "oil_veg",
        (
            "масло растительное",
            "растительное масло",
            "масло оливковое",
            "оливковое масло",
            "подсолнечное масло",
            "масло подсолнечное",
            "масло для жарки",
            "масло кунжутное",
            "кунжутное масло",
        ),
    ),
    ("milk_coconut", ("кокосовое молоко",)),
    ("milk_oat", ("овсяное молоко", "растительное молоко", "соевое молоко", "миндальное молоко")),
    ("oat_cream", ("овсяные сливки", "растительные сливки")),
    ("milk_dairy", ("молоко",)),
    ("green_beans", ("стручковая фасоль", "фасоль стручковая")),
    ("beans", ("фасоль",)),
    ("chickpea", ("нут",)),
    ("lentils", ("чечевица",)),
    ("oats", ("овсянка", "овсяные хлопья", "хлопья овсяные", "геркулес")),
    ("rice", ("рис",)),
    ("buckwheat", ("гречка", "гречневая", "греча")),
    ("pasta", ("макароны", "спагетти", "паста", "пенне", "фузилли")),
    ("tofu", ("тофу",)),
    ("tomato", ("помидор", "помидоры", "томат", "томаты")),
    ("cucumber", ("огурец", "огурцы")),
    ("potato", ("картофель", "картошка")),
    ("zucchini", ("цукини", "кабачок", "кабачки")),
    ("onion", ("лук",)),
    ("garlic", ("чеснок",)),
    ("carrot", ("морковь", "морковка")),
    ("cauliflower", ("цветная капуста",)),
    ("cabbage", ("капуста",)),
    ("mushrooms", ("шампиньоны", "грибы")),
    ("herbs", ("зелень", "укроп", "петрушка", "кинза")),
    ("bread", ("хлеб", "батон", "чиабатта", "тост")),
    ("flatbread", ("лаваш", "тортилья", "тортильи", "пита")),
    ("veggies", ("овощная смесь", "овощи")),
    ("lemon", ("лимон", "лимонный сок")),
    ("egg", ("яйцо", "яйца")),
    ("pepper_bell", ("болгарский перец", "перец сладкий")),
    ("soy_sauce", ("соевый соус",)),
    ("soy_meat", ("соевое мясо", "соевый фарш", "соевый гуляш", "соевый текстурат")),
    ("spinach", ("шпинат",)),
    ("sweet_chili", ("сладкий чили",)),
    ("sweet_sour", ("кисло-сладкий соус", "кисло сладкий соус")),
    ("teriyaki", ("соус терияки", "терияки")),
    ("ketchup", ("кетчуп",)),
    ("vegan_patty", ("веганские котлеты", "веган котлеты", "растительные котлеты")),
    ("broccoli", ("брокколи",)),
    ("banana", ("банан",)),
    ("berries", ("ягоды",)),
    ("avocado", ("авокадо",)),
    ("hummus", ("хумус",)),
    ("cheese", ("сыр",)),
]

_CANONICAL_ALIAS_RULES: Tuple[Tuple[str, Tuple[Tuple[str, Tuple[str, ...], str], ...]], ...] = tuple(
    (
        key,
        tuple(
            (
                pat.replace("ё", "е"),
                tuple(pat.replace("ё", "е").split()),
                repo._ru_stem(pat.replace("ё", "е")) if " " not in pat.replace("ё", "е") else "",
            )
            for pat in patterns
        ),
    )
    for key, patterns in _CANONICAL_ALIASES
)

_MODIFIER_WORDS = {
    "варёный",
    "вареный",
    "варёная",
    "вареная",
    "готовый",
    "готовая",
    "консервированный",
    "консервированная",
    "замороженные",
    "замороженная",
    "свежий",
    "свежая",
    "свежие",
    "молодые",
    "спелый",
    "красная",
    "белокочанная",
    "репчатый",
    "черри",
    "собственном",
    "соку",
    "колотый",
    "чёрная",
    "черная",
    "цельнозерновой",
    "любые",
    "греческий",
    "суповой",
    "набор",
    "филе",
    "фарш",
}


def load_recipes() -> List[Dict[str, Any]]:
    """Загрузить каталог рецептов из JSON."""
    if not RECIPES_FILE.exists():
        return []
    try:
        data = json.loads(RECIPES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def get_recipe(recipe_id: str) -> Optional[Dict[str, Any]]:
    """Найти рецепт по ID в базовом каталоге."""
    rid = str(recipe_id or "").strip()
    if not rid:
        return None
    for item in load_recipes():
        if str(item.get("id") or "") == rid:
            return item
    return None


async def load_recipes_for_user(db, user_id: int) -> List[Dict[str, Any]]:
    """Загрузить личные рецепты пользователя и базовый каталог."""
    personal = await repo.list_user_recipes(db, int(user_id))
    return list(personal) + load_recipes()


async def get_recipe_for_user(db, user_id: int, recipe_id: str) -> Optional[Dict[str, Any]]:
    """Найти рецепт по ID сначала среди личных рецептов пользователя, затем в базовом каталоге."""
    rid = str(recipe_id or "").strip()
    if not rid:
        return None
    personal = await repo.get_user_recipe_by_key(db, int(user_id), rid)
    if personal is not None:
        return personal
    return get_recipe(rid)


def recipe_allowed_for_profile(recipe: Dict[str, Any], profile: str) -> bool:
    """Проверить, подходит ли рецепт под диету пользователя."""
    tags = set(recipe.get("tags") or [])
    allowed = DIET_TAGS.get(profile)
    if allowed is None:
        return True
    return bool(tags.intersection(allowed))


async def get_user_meal_profile(db, user_id: int) -> str:
    """Получить профиль питания пользователя ('omnivore'/'vegetarian'/'vegan')."""
    wellness = await repo.get_wellness(db, user_id)
    profile = (dict(wellness) if wellness else {}).get("meal_profile", "omnivore")
    return profile if profile in {"omnivore", "vegetarian", "vegan"} else "omnivore"


def infer_meal_slot(local_hour: int) -> str:
    """Определить текущий слот приёма пищи по локальному часу."""
    if 5 <= local_hour < 11:
        return "breakfast"
    if 11 <= local_hour < 16:
        return "lunch"
    return "dinner"


def scale_qty(qty: float, factor: float, unit: str) -> float:
    """Масштабировать количество ингредиента на нужное число порций."""
    val = float(qty or 0) * float(factor)
    unit_l = (unit or "").strip().lower()
    if unit_l in {"г", "g", "гр", "мл", "ml"}:
        return float(int(round(val)))
    if unit_l in {"кг", "kg", "л", "l"}:
        return round(val, 2)
    if unit_l in {"шт", "piece", "pieces"}:
        return float(int(math.ceil(val - 1e-9)))
    return round(val, 2)


def unit_kind(unit: str) -> str:
    u = (unit or "").strip().lower()
    if u in {"г", "гр", "g", "kg", "кг"}:
        return "weight"
    if u in {"мл", "ml", "л", "l"}:
        return "volume"
    if u in {"шт", "piece", "pieces"}:
        return "count"
    return "other"


def to_base(amount: float, unit: str) -> Tuple[float, str]:
    kind = unit_kind(unit)
    u = (unit or "").strip().lower()
    val = float(amount or 0)
    if kind == "weight":
        return (val * 1000.0 if u in {"kg", "кг"} else val), kind
    if kind == "volume":
        return (val * 1000.0 if u in {"л", "l"} else val), kind
    return val, kind


def from_base(amount_base: float, unit: str) -> float:
    kind = unit_kind(unit)
    u = (unit or "").strip().lower()
    val = float(amount_base or 0)
    if kind == "weight" and u in {"kg", "кг"}:
        return val / 1000.0
    if kind == "volume" and u in {"л", "l"}:
        return val / 1000.0
    return val


@lru_cache(maxsize=4096)
def _clean_ing_text(text: str) -> str:
    s = (text or "").strip().casefold().replace("ё", "е")
    s = re.sub(r"[()]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


@lru_cache(maxsize=4096)
def _split_alternatives(name: str) -> Tuple[str, ...]:
    cleaned = _clean_ing_text(name)
    if not cleaned:
        return ()
    parts = re.split(r"\s*/\s*|\s+или\s+", cleaned)
    return tuple(p.strip() for p in parts if p.strip())


@lru_cache(maxsize=4096)
def _canonical_key(single_name: str) -> Optional[str]:
    norm = _clean_ing_text(single_name)
    if not norm:
        return None
    norm_tokens = norm.split()
    norm_token_set = set(norm_tokens)
    norm_stems = {repo._ru_stem(nt) for nt in norm_tokens}
    for key, rules in _CANONICAL_ALIAS_RULES:
        for pat_norm, pat_tokens, pat_stem in rules:
            if pat_norm == norm:
                return key
            if len(pat_tokens) > 1:
                if all(pt in norm for pt in pat_tokens):
                    return key
            elif pat_norm in norm_token_set or pat_stem in norm_stems:
                # Не даём одиночному «молоко» перехватить растительное/кокосовое молоко,
                # а «паста» — арахисовую или томатную пасту.
                if key == "milk_dairy" and any(q in norm for q in ("кокос", "овсян", "растительн", "соев", "миндал")):
                    continue
                if key == "pasta" and any(q in norm for q in ("арахис", "томат")):
                    continue
                if key == "tomato" and "паст" in norm:
                    continue
                return key
    return None


@lru_cache(maxsize=4096)
def _meaningful_stems(single_name: str) -> Tuple[str, ...]:
    norm = _clean_ing_text(single_name)
    words = [w for w in re.findall(r"[a-zа-я0-9]+", norm) if w not in _MODIFIER_WORDS]
    if not words:
        words = re.findall(r"[a-zа-я0-9]+", norm)
    return tuple(repo._ru_stem(w) or w for w in words if len(w) >= 2)


@lru_cache(maxsize=4096)
def is_staple_ingredient(name: str) -> bool:
    """Проверить, относится ли ингредиент к базовым специям/воде, которые не нужно докупать."""
    alts = _split_alternatives(name)
    if not alts:
        return True
    # Если среди альтернатив есть только базовые продукты (например «вода», «соль»), считаем базовым;
    # если «вода или растительное молоко», достаточно считать, что вода всегда есть дома.
    for alt in alts:
        if alt in STAPLE_INGREDIENTS:
            return True
        stems = _meaningful_stems(alt)
        if stems and all(st in {"вод", "сол", "сахар", "перец", "кориц", "карр", "паприк", "куркум"} for st in stems):
            return True
    return False


@lru_cache(maxsize=16384)
def ingredients_match(name_a: str, name_b: str) -> bool:
    """Сопоставить два названия ингредиентов по синонимам и русским основам."""
    alts_a = _split_alternatives(name_a)
    alts_b = _split_alternatives(name_b)
    if not alts_a or not alts_b:
        return False

    for a in alts_a:
        for b in alts_b:
            if a == b:
                return True
            ca = _canonical_key(a)
            cb = _canonical_key(b)
            if ca and cb:
                if ca == cb:
                    return True
                continue
            # Если только одна сторона попала в жёсткую группу типа butter vs oil_veg, не путаем
            if (ca in {"oil_butter", "oil_veg", "peanut_butter", "tomato_paste", "milk_coconut", "milk_oat"} or
                cb in {"oil_butter", "oil_veg", "peanut_butter", "tomato_paste", "milk_coconut", "milk_oat"}):
                if ca != cb:
                    continue
            sa = _meaningful_stems(a)
            sb = _meaningful_stems(b)
            if not sa or not sb:
                continue
            set_a, set_b = set(sa), set(sb)
            if set_a == set_b:
                return True
            # Если короткое общее название вроде «сыр» или «тофу» целиком входит в более подробное
            if (len(set_a) == 1 and set_a.issubset(set_b)) or (len(set_b) == 1 and set_b.issubset(set_a)):
                return True
            if a in b or b in a:
                return True
    return False


def match_recipe_to_pantry(
    recipe: Dict[str, Any],
    pantry_items: List[Dict[str, Any]],
    *,
    servings: Optional[int] = None,
) -> Dict[str, Any]:
    """Сопоставить ингредиенты рецепта с текущим холодильником."""
    base_servings = int(recipe.get("base_servings") or 1) or 1
    eff_servings = max(1, int(servings or base_servings))
    factor = eff_servings / base_servings

    have: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    staples: List[Dict[str, Any]] = []

    for ing in recipe.get("ingredients") or []:
        if not isinstance(ing, dict):
            continue
        ing_name = str(ing.get("name") or "").strip()
        if not ing_name:
            continue
        unit = str(ing.get("unit") or "").strip()
        needed_qty = scale_qty(float(ing.get("qty") or 0), factor, unit)
        if needed_qty <= 0:
            needed_qty = 1.0

        if is_staple_ingredient(ing_name):
            staples.append({"name": ing_name, "qty": needed_qty, "unit": unit})
            continue

        matched_pantry = next(
            (p for p in pantry_items if ingredients_match(ing_name, str(p.get("name") or ""))),
            None,
        )
        if matched_pantry:
            have.append(
                {
                    "name": ing_name,
                    "qty": needed_qty,
                    "unit": unit,
                    "pantry_id": int(matched_pantry["id"]),
                    "pantry_name": str(matched_pantry.get("name") or ing_name),
                    "pantry_amount": float(matched_pantry.get("amount") or 0),
                    "pantry_unit": str(matched_pantry.get("unit") or ""),
                }
            )
        else:
            missing.append({"name": ing_name, "qty": needed_qty, "unit": unit})

    total_non_staple = len(have) + len(missing)
    coverage = (len(have) / total_non_staple) if total_non_staple > 0 else 1.0
    return {
        "have": have,
        "missing": missing,
        "staples": staples,
        "coverage": coverage,
        "servings": eff_servings,
    }


def _recipe_matches_excluded_terms(recipe: Dict[str, Any], excluded_terms: Iterable[str]) -> bool:
    terms = [t.strip().lower() for t in excluded_terms if t and t.strip()]
    if not terms:
        return False
    parts = [str(recipe.get("title") or ""), str(recipe.get("desc") or "")]
    for ing in recipe.get("ingredients") or []:
        if isinstance(ing, dict):
            parts.append(str(ing.get("name") or ""))
    haystack = " ".join(parts).casefold().replace("ё", "е")
    return any(t in haystack for t in terms)


async def suggest_meals_now(
    db,
    user: Dict[str, Any],
    *,
    energy: str = "normal",
    meal_slot: Optional[str] = None,
    exclude_ids: Optional[Iterable[str]] = None,
    excluded_terms: Optional[Iterable[str]] = None,
    limit: int = 3,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Подобрать лучшие блюда прямо сейчас с учётом холодильника, времени, энергии и диеты."""
    now = now_utc or datetime.datetime.utcnow()
    user_id = int(user["id"])
    tz = user.get("timezone") or "UTC"
    local_hhmm = format_time_local(now, tz)
    try:
        local_hour = int(local_hhmm.split(":")[0])
    except Exception:
        local_hour = 13

    eff_slot = meal_slot if meal_slot in {"breakfast", "lunch", "dinner"} else infer_meal_slot(local_hour)
    eff_energy = energy if energy in {"low", "normal", "shop"} else "normal"
    profile = await get_user_meal_profile(db, user_id)
    pantry = rows_to_dicts(await repo.list_pantry_items(db, user_id))

    local_today = local_date_str(now, tz)
    recent_since = (datetime.date.fromisoformat(local_today) - datetime.timedelta(days=4)).isoformat()
    recent_ids = set(await repo.get_recent_meal_recipe_ids(db, user_id, recent_since))
    advice = await fitness_nutrition.get_daily_nutrition_advice(db, user, now_utc=now)
    excl_id_set = {str(x) for x in (exclude_ids or []) if x}
    excl_terms = list(excluded_terms or [])

    all_user_recipes = await load_recipes_for_user(db, user_id)
    allowed = [
        r for r in all_user_recipes
        if recipe_allowed_for_profile(r, profile)
        and str(r.get("id") or "") not in excl_id_set
        and not _recipe_matches_excluded_terms(r, excl_terms)
    ]

    scored: List[Dict[str, Any]] = []
    for recipe in allowed:
        rid = str(recipe.get("id") or "")
        tags = set(recipe.get("tags") or [])
        t_min = int(recipe.get("time_minutes") or 25)
        match = match_recipe_to_pantry(recipe, pantry, servings=2)
        cov = float(match["coverage"])
        miss_cnt = len(match["missing"])

        score = cov * 45.0
        if eff_slot in tags:
            score += 20.0
        elif eff_slot == "breakfast" and "snack" in tags:
            score += 8.0
        elif eff_slot in {"lunch", "dinner"} and tags.intersection({"lunch", "dinner"}):
            score += 10.0

        if eff_energy == "low":
            if t_min <= 15:
                score += 26.0
            elif t_min <= 20:
                score += 10.0
            else:
                score -= 22.0
        elif eff_energy == "normal":
            if t_min <= 30:
                score += 12.0
            elif t_min > 40:
                score -= 10.0
        elif eff_energy == "shop":
            if 1 <= miss_cnt <= 2 and t_min <= 45:
                score += 18.0
            elif miss_cnt == 0:
                score += 8.0

        if miss_cnt == 0 and pantry:
            score += 15.0
        if rid in recent_ids:
            score -= 18.0

        nutrition = fitness_nutrition.estimate_recipe_nutrition(recipe)
        score += fitness_nutrition.score_recipe_for_macros(nutrition, advice)

        scored.append(
            {
                "recipe": recipe,
                "have": match["have"],
                "missing": match["missing"],
                "coverage": cov,
                "nutrition": nutrition,
                "score": round(score, 2),
            }
        )

    scored.sort(
        key=lambda item: (
            -item["score"],
            len(item["missing"]),
            int(item["recipe"].get("time_minutes") or 999),
        )
    )

    return {
        "meal_slot": eff_slot,
        "meal_slot_label": MEAL_SLOT_LABELS.get(eff_slot, "приём пищи"),
        "energy": eff_energy,
        "meal_profile": profile,
        "pantry_count": len(pantry),
        "daily_advice": advice,
        "suggestions": scored[:limit],
    }


async def suggest_almost_ready_recipes(
    db,
    user: Dict[str, Any],
    *,
    max_missing: int = 2,
    meal_slot: Optional[str] = None,
    exclude_ids: Optional[Iterable[str]] = None,
    limit: int = 30,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Подобрать рецепты, к которым не хватает строго от 1 до `max_missing` продуктов."""
    now = now_utc or datetime.datetime.utcnow()
    user_id = int(user["id"])
    tz = user.get("timezone") or "UTC"
    local_hhmm = format_time_local(now, tz)
    try:
        local_hour = int(local_hhmm.split(":")[0])
    except Exception:
        local_hour = 13

    eff_slot = meal_slot if meal_slot in {"breakfast", "lunch", "dinner"} else infer_meal_slot(local_hour)
    eff_max = max(1, min(3, int(max_missing or 2)))
    profile = await get_user_meal_profile(db, user_id)
    pantry = rows_to_dicts(await repo.list_pantry_items(db, user_id))

    local_today = local_date_str(now, tz)
    soon_rows, _ = await repo.pantry_expiring(db, user_id, local_today, window_days=3)
    expiring_names = [str(r.get("name") or "") for r in rows_to_dicts(soon_rows) if r.get("name")]

    recent_since = (datetime.date.fromisoformat(local_today) - datetime.timedelta(days=4)).isoformat()
    recent_ids = set(await repo.get_recent_meal_recipe_ids(db, user_id, recent_since))
    advice = await fitness_nutrition.get_daily_nutrition_advice(db, user, now_utc=now)
    excl_id_set = {str(x) for x in (exclude_ids or []) if x}

    all_user_recipes = await load_recipes_for_user(db, user_id)
    allowed = [
        r for r in all_user_recipes
        if recipe_allowed_for_profile(r, profile) and str(r.get("id") or "") not in excl_id_set
    ]

    with_have: List[Dict[str, Any]] = []
    any_missing: List[Dict[str, Any]] = []
    for recipe in allowed:
        rid = str(recipe.get("id") or "")
        tags = set(recipe.get("tags") or [])
        t_min = int(recipe.get("time_minutes") or 25)
        match = match_recipe_to_pantry(recipe, pantry, servings=2)
        miss_cnt = len(match["missing"])
        if not (1 <= miss_cnt <= eff_max):
            continue

        cov = float(match["coverage"])
        used_expiring: List[str] = []
        if expiring_names and match["have"]:
            for exp_name in expiring_names:
                if any(ingredients_match(exp_name, str(h.get("name") or "")) for h in match["have"]):
                    used_expiring.append(exp_name)

        score = cov * 50.0 + (14.0 if miss_cnt == 1 else 6.0)
        if used_expiring:
            score += 12.0
        if eff_slot in tags:
            score += 10.0
        if t_min <= 25:
            score += 6.0
        if rid in recent_ids:
            score -= 16.0

        nutrition = fitness_nutrition.estimate_recipe_nutrition(recipe)
        score += fitness_nutrition.score_recipe_for_macros(nutrition, advice)

        entry = {
            "recipe": recipe,
            "have": match["have"],
            "missing": match["missing"],
            "coverage": cov,
            "nutrition": nutrition,
            "uses_expiring": used_expiring,
            "score": round(score, 2),
        }
        any_missing.append(entry)
        if match["have"]:
            with_have.append(entry)

    candidates = with_have if (pantry and with_have) else any_missing
    candidates.sort(
        key=lambda item: (
            -item["score"],
            len(item["missing"]),
            int(item["recipe"].get("time_minutes") or 999),
        )
    )
    eff_limit = max(1, int(limit or 30))
    return {
        "meal_slot": eff_slot,
        "meal_slot_label": MEAL_SLOT_LABELS.get(eff_slot, "приём пищи"),
        "meal_profile": profile,
        "pantry_count": len(pantry),
        "max_missing": eff_max,
        "total_matches": len(candidates),
        "daily_advice": advice,
        "suggestions": candidates[:eff_limit],
    }


def _merge_missing_items(
    items: Iterable[Dict[str, Any]],
    *,
    preserve_staples: bool = False,
) -> List[Dict[str, Any]]:
    """Объединить повторяющиеся недостающие ингредиенты."""
    merged: List[Dict[str, Any]] = []
    for raw in items:
        name = str(raw.get("name") or "").strip()
        if not name or (not preserve_staples and is_staple_ingredient(name)):
            continue
        qty = float(raw.get("qty") or 1.0)
        unit = str(raw.get("unit") or "шт").strip() or "шт"

        target = next((m for m in merged if ingredients_match(m["name"], name)), None)
        if target is None:
            merged.append({"name": name, "qty": qty, "unit": unit})
            continue

        k1 = unit_kind(target["unit"])
        k2 = unit_kind(unit)
        if k1 == k2 and k1 != "other":
            b1, _ = to_base(float(target["qty"]), target["unit"])
            b2, _ = to_base(qty, unit)
            target["qty"] = round(from_base(b1 + b2, target["unit"]), 2)
    return merged


async def add_missing_ingredients_to_shopping(
    db,
    user_id: int,
    missing_items: Iterable[Dict[str, Any]],
    *,
    scope: str = "household",
) -> Dict[str, List[Dict[str, Any]]]:
    """Добавить только недостающие продукты в список покупок без дублей."""
    pantry = rows_to_dicts(await repo.list_pantry_items(db, user_id))
    shop_rows = [
        r for r in rows_to_dicts(await repo.list_shopping_items(db, user_id, scope=scope))
        if not r.get("is_bought")
    ]

    added: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for item in _merge_missing_items(missing_items):
        name = item["name"]
        in_pantry = any(ingredients_match(name, str(p.get("name") or "")) for p in pantry)
        in_shop = any(ingredients_match(name, str(s.get("item_name") or "")) for s in shop_rows)
        if in_pantry or in_shop:
            skipped.append(item)
            continue
        qty = float(item.get("qty") or 1.0)
        unit = str(item.get("unit") or "шт")
        new_id = await repo.create_shopping_item(
            db, user_id, name, qty=qty, unit=unit, category="food", scope=scope
        )
        added_entry = {"id": new_id, "name": name, "qty": qty, "unit": unit}
        added.append(added_entry)
        shop_rows.append({"item_name": name, "is_bought": 0})
    return {"added": added, "skipped": skipped}


async def commit_cooked_recipe(
    db,
    user: Dict[str, Any],
    recipe_id: str,
    *,
    servings: int = 2,
    meal_slot: Optional[str] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Списать продукты за приготовленное блюдо, обновить меню и начислить карму/закрыть Пульс."""
    user_id = int(user["id"])
    recipe = await get_recipe_for_user(db, user_id, recipe_id)
    if not recipe:
        return {"ok": False, "error": "Рецепт не найден."}

    now = now_utc or datetime.datetime.utcnow()
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    local_hhmm = format_time_local(now, tz)
    try:
        local_hour = int(local_hhmm.split(":")[0])
    except Exception:
        local_hour = 13

    pantry = rows_to_dicts(await repo.list_pantry_items(db, user_id))
    match = match_recipe_to_pantry(recipe, pantry, servings=servings)

    balances: Dict[int, float] = {
        int(p["id"]): float(p.get("amount") or 0) for p in pantry
    }
    pantry_units: Dict[int, str] = {
        int(p["id"]): str(p.get("unit") or "шт") for p in pantry
    }

    deducted: List[Dict[str, Any]] = []
    for item in match["have"]:
        pid = int(item["pantry_id"])
        cur_amt = balances.get(pid, 0.0)
        if cur_amt <= 0:
            continue
        p_unit = pantry_units.get(pid, "шт")
        n_unit = str(item.get("unit") or "")
        n_qty = float(item.get("qty") or 0)

        need_base, kind_n = to_base(n_qty, n_unit)
        have_base, kind_h = to_base(cur_amt, p_unit)

        if kind_n == kind_h and kind_n != "other":
            need_in_pantry = from_base(need_base, p_unit)
            new_amt = max(0.0, cur_amt - need_in_pantry)
        elif kind_h == "count":
            new_amt = max(0.0, cur_amt - 1.0)
        else:
            # Разные единицы (например, в холодильнике 1 пачка/шт, а в рецепте граммы) — осторожно оставляем без краша
            new_amt = max(0.0, cur_amt - 0.5)

        balances[pid] = new_amt
        if new_amt <= 0.01:
            await repo.delete_pantry_item(db, user_id, pid)
        else:
            await repo.update_pantry_item(db, user_id, pid, amount=round(new_amt, 2))
        deducted.append({"name": item["pantry_name"], "remaining": round(new_amt, 2), "unit": p_unit})

    # Отметить в сегодняшнем меню, если совпадает рецепт или слот
    today_plan = await repo.get_meal_plan_for_date(db, user_id, local_date)
    marked_slot: Optional[str] = None
    for slot_row in today_plan:
        if str(slot_row.get("recipe_id") or "") == str(recipe["id"]):
            await repo.mark_meal_plan_status(db, user_id, local_date, str(slot_row["meal_slot"]), "cooked")
            marked_slot = str(slot_row["meal_slot"])
            break
    if marked_slot is None and meal_slot in {"breakfast", "lunch", "dinner"}:
        for slot_row in today_plan:
            if str(slot_row.get("meal_slot") or "") == meal_slot and slot_row.get("status") == "planned":
                await repo.mark_meal_plan_status(db, user_id, local_date, meal_slot, "cooked")
                marked_slot = meal_slot
                break

    # Синергия с Пульсом еды (lunch/dinner): если слот открыт, закрываем его без двойной кармы
    pulse_food_slot = meal_slot if meal_slot in {"lunch", "dinner"} else ("lunch" if local_hour < 16 else "dinner")
    chk = await repo.get_pulse_checkin(db, user_id, local_date, pulse_food_slot)
    pulse_closed = ""
    if chk and chk.get("status") in {"pending", "snoozed"}:
        pulse_res = await pulse_service.complete_checkin(db, user, int(chk["id"]), now_utc=now)
        karma_info = pulse_res.get("karma") or {}
        karma_delta = int(karma_info.get("delta") or 0)
        score_after = karma_info.get("score_after")
        pulse_closed = pulse_food_slot
    else:
        karma_res = await karma_service.record_karma_event(
            db,
            user,
            domain="food",
            event_type="cooked_meal",
            delta=3,
            reason=f"Приготовил: {recipe['title']}",
            now_utc=now,
        )
        karma_delta = int(karma_res.get("delta") or 0)
        score_after = karma_res.get("score_after")

    return {
        "ok": True,
        "recipe": recipe,
        "nutrition_per_serving": fitness_nutrition.estimate_recipe_nutrition(recipe),
        "deducted": deducted,
        "missing": match["missing"],
        "marked_slot": marked_slot,
        "pulse_closed": pulse_closed,
        "karma_delta": karma_delta,
        "score_after": score_after,
    }


def _pick_recipe_for_plan_slot(
    candidates: List[Dict[str, Any]],
    *,
    meal_slot: str,
    day_idx: int,
    plan_date: str,
    pantry: List[Dict[str, Any]],
    used_ids: Set[str],
    exclude_ids: Set[str],
    recent_ids: Set[str],
    planned_ingredients: Iterable[str],
    coverage_by_id: Optional[Dict[str, float]] = None,
) -> Optional[Dict[str, Any]]:
    pool = [r for r in candidates if str(r.get("id") or "") not in used_ids]
    if not pool:
        pool = list(candidates)
    if not pool:
        return None

    is_weekday = False
    try:
        is_weekday = datetime.date.fromisoformat(plan_date).weekday() < 5
    except Exception:
        pass

    fallback_tag = "snack" if meal_slot == "breakfast" else ("dinner" if meal_slot == "lunch" else "lunch")
    ranked: List[Tuple[float, Dict[str, Any]]] = []
    for idx, recipe in enumerate(pool):
        rid = str(recipe.get("id") or "")
        tags = set(recipe.get("tags") or [])
        t_min = int(recipe.get("time_minutes") or 25)
        if coverage_by_id is not None and rid in coverage_by_id:
            cov = coverage_by_id[rid]
        else:
            cov = float(match_recipe_to_pantry(recipe, pantry, servings=2)["coverage"])

        score = 0.0
        if meal_slot in tags:
            score += 40.0
        elif fallback_tag in tags:
            score += 18.0
        else:
            score -= 20.0

        # В первый день максимально опираемся на то, что уже лежит в холодильнике
        score += cov * (50.0 if day_idx == 0 else 22.0)

        # Переиспользуем продукты из уже выбранных блюд меню, чтобы не покупать 30 разных позиций
        if planned_ingredients:
            reused = sum(
                1
                for ing in recipe.get("ingredients") or []
                if isinstance(ing, dict)
                and not is_staple_ingredient(str(ing.get("name") or ""))
                and any(ingredients_match(str(ing.get("name") or ""), prev) for prev in planned_ingredients)
            )
            score += min(15.0, reused * 4.0)

        if is_weekday and meal_slot == "dinner":
            if t_min <= 30:
                score += 12.0
            elif t_min > 40:
                score -= 15.0
        elif meal_slot == "breakfast" and t_min <= 15:
            score += 8.0

        if rid in exclude_ids:
            score -= 45.0
        elif rid in recent_ids:
            score -= 20.0

        # Мягкий сдвиг для разнообразия порядка при равных очках
        score += ((idx % 5) * 0.01)
        ranked.append((score, recipe))

    ranked.sort(key=lambda pair: -pair[0])
    return ranked[0][1] if ranked else None


async def generate_meal_plan(
    db,
    user: Dict[str, Any],
    *,
    days: int = 3,
    start_date: Optional[str] = None,
    exclude_ids: Optional[Iterable[str]] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Сгенерировать и сохранить не повторяющееся меню на 3-4 дня с учётом диеты и холодильника."""
    now = now_utc or datetime.datetime.utcnow()
    user_id = int(user["id"])
    tz = user.get("timezone") or "UTC"
    eff_days = max(1, min(7, int(days or 3)))
    base_date_str = start_date or local_date_str(now, tz)
    base_date = datetime.date.fromisoformat(base_date_str)
    end_date_str = (base_date + datetime.timedelta(days=eff_days - 1)).isoformat()

    profile = await get_user_meal_profile(db, user_id)
    pantry = rows_to_dicts(await repo.list_pantry_items(db, user_id))
    candidates = [r for r in await load_recipes_for_user(db, user_id) if recipe_allowed_for_profile(r, profile)]

    recent_since = (base_date - datetime.timedelta(days=4)).isoformat()
    recent_ids = set(await repo.get_recent_meal_recipe_ids(db, user_id, recent_since))
    excl_set = {str(x) for x in (exclude_ids or []) if x}

    await repo.clear_meal_plans_range(db, user_id, base_date_str, end_date_str)

    used_ids: Set[str] = set()
    planned_ingredients: Set[str] = set()
    saved_rows: List[Dict[str, Any]] = []
    coverage_by_id: Dict[str, float] = {
        str(r.get("id") or ""): float(match_recipe_to_pantry(r, pantry, servings=2)["coverage"])
        for r in candidates
    }

    for d_idx in range(eff_days):
        plan_date = (base_date + datetime.timedelta(days=d_idx)).isoformat()
        for meal_slot in ("breakfast", "lunch", "dinner"):
            picked = _pick_recipe_for_plan_slot(
                candidates,
                meal_slot=meal_slot,
                day_idx=d_idx,
                plan_date=plan_date,
                pantry=pantry,
                used_ids=used_ids,
                exclude_ids=excl_set,
                recent_ids=recent_ids,
                planned_ingredients=planned_ingredients,
                coverage_by_id=coverage_by_id,
            )
            if not picked:
                continue
            rid = str(picked.get("id") or "")
            used_ids.add(rid)
            for ing in picked.get("ingredients") or []:
                if isinstance(ing, dict) and ing.get("name") and not is_staple_ingredient(str(ing["name"])):
                    cleaned_ing = _clean_ing_text(str(ing["name"]))
                    if cleaned_ing:
                        planned_ingredients.add(cleaned_ing)
            row = await repo.upsert_meal_plan_slot(
                db,
                user_id,
                plan_date,
                meal_slot,
                title=str(picked.get("title") or "Блюдо"),
                recipe_id=rid,
                time_minutes=int(picked.get("time_minutes") or 20),
                ingredients=list(picked.get("ingredients") or []),
                status="planned",
            )
            if row:
                saved_rows.append(row)

    missing = await compute_meal_plan_missing_shopping(db, user, saved_rows)
    export_data = build_shopping_export(missing)
    return {
        "start_date": base_date_str,
        "end_date": end_date_str,
        "days": eff_days,
        "meal_profile": profile,
        "items": saved_rows,
        "missing_shopping": missing,
        "missing_shopping_grouped": export_data["groups"],
        "shopping_export_text": export_data["plain_text"],
    }


async def compute_meal_plan_missing_shopping(
    db,
    user: Dict[str, Any],
    plan_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Собрать недостающие продукты по плану питания за вычетом холодильника и активных покупок."""
    user_id = int(user["id"])
    pantry = rows_to_dicts(await repo.list_pantry_items(db, user_id))
    shop_rows = [
        r for r in rows_to_dicts(await repo.list_shopping_items(db, user_id, scope="household"))
        if not r.get("is_bought")
    ]

    raw_needed: List[Dict[str, Any]] = []
    for row in plan_rows:
        if row.get("status") == "cooked":
            continue
        ings = row.get("ingredients") or []
        if not ings and row.get("recipe_id"):
            rec = await get_recipe_for_user(db, user_id, str(row["recipe_id"]))
            ings = list(rec.get("ingredients") or []) if rec else []
        for ing in ings:
            if not isinstance(ing, dict) or not ing.get("name"):
                continue
            raw_needed.append(
                {
                    "name": str(ing["name"]),
                    "qty": float(ing.get("qty") or 1.0),
                    "unit": str(ing.get("unit") or "шт"),
                }
            )

    merged = _merge_missing_items(raw_needed)
    missing: List[Dict[str, Any]] = []
    for item in merged:
        name = item["name"]
        if any(ingredients_match(name, str(p.get("name") or "")) for p in pantry):
            continue
        if any(ingredients_match(name, str(s.get("item_name") or "")) for s in shop_rows):
            continue
        missing.append(item)
    return missing


async def get_meal_plan_view(
    db,
    user: Dict[str, Any],
    *,
    days: int = 3,
    start_date: Optional[str] = None,
    auto_generate: bool = True,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Получить текущее меню на 3-4 дня (при пустоте — сгенерировать автоматически)."""
    now = now_utc or datetime.datetime.utcnow()
    user_id = int(user["id"])
    tz = user.get("timezone") or "UTC"
    eff_days = max(1, min(7, int(days or 3)))
    base_date_str = start_date or local_date_str(now, tz)
    base_date = datetime.date.fromisoformat(base_date_str)
    end_date_str = (base_date + datetime.timedelta(days=eff_days - 1)).isoformat()

    rows = await repo.list_meal_plans_range(db, user_id, base_date_str, end_date_str)
    if not rows and auto_generate:
        return await generate_meal_plan(
            db, user, days=eff_days, start_date=base_date_str, now_utc=now
        )

    profile = await get_user_meal_profile(db, user_id)
    missing = await compute_meal_plan_missing_shopping(db, user, rows)
    export_data = build_shopping_export(missing)
    return {
        "start_date": base_date_str,
        "end_date": end_date_str,
        "days": eff_days,
        "meal_profile": profile,
        "items": rows,
        "missing_shopping": missing,
        "missing_shopping_grouped": export_data["groups"],
        "shopping_export_text": export_data["plain_text"],
    }


def _guess_pantry_category(name: str) -> str:
    low = (name or "").casefold().replace("ё", "е")
    if any(k in low for k in ("огур", "помидор", "томат", "морков", "лук", "чеснок", "капуст", "броккол", "перец", "кабач", "цукин", "карто", "зелен", "шпинат", "салат")):
        return "овощи"
    if any(k in low for k in ("яблок", "банан", "лимон", "апельсин", "груш", "ягод", "авокадо")):
        return "фрукты"
    if any(k in low for k in ("греч", "рис", "овсян", "хлоп", "макарон", "спагет", "пшен", "киноа", "кускус", "мука", "хлеб", "лаваш")):
        return "крупы"
    if any(k in low for k in ("тоф", "нут", "чечевиц", "фасол", "горох", "хумус", "соев")):
        return "белок"
    if any(k in low for k in ("молок", "кефир", "йогурт", "творог", "сыр", "сливк", "сметан")):
        return "молочка"
    if any(k in low for k in ("масл", "соус", "паста", "тахини")):
        return "соусы/масла"
    return "продукты"


SHOPPING_EXPORT_CATEGORIES: List[Tuple[str, str]] = [
    ("овощи", "🥬 Овощи и зелень"),
    ("фрукты", "🍎 Фрукты и ягоды"),
    ("белок", "🫘 Белок и бобовые"),
    ("крупы", "🌾 Крупы, паста и хлеб"),
    ("молочка", "🥛 Молочка / растительное"),
    ("соусы/масла", "🫙 Соусы и масла"),
    ("продукты", "🧺 Прочее"),
]


def build_shopping_export(
    items: Iterable[Dict[str, Any]],
    *,
    header: str = "",
    preserve_explicit: bool = False,
) -> Dict[str, Any]:
    """Сгруппировать продукты по отделам и сформировать текст для копирования/доставки."""
    merged = _merge_missing_items(items, preserve_staples=preserve_explicit)
    buckets: Dict[str, List[Dict[str, Any]]] = {key: [] for key, _ in SHOPPING_EXPORT_CATEGORIES}
    for item in merged:
        cat = _guess_pantry_category(item["name"])
        buckets.setdefault(cat, []).append(
            {
                "name": item["name"],
                "qty": item["qty"],
                "unit": item["unit"],
                "quantity_text": format_quantity(item["qty"], item["unit"]),
            }
        )

    groups: List[Dict[str, Any]] = []
    text_blocks: List[str] = []
    clean_header = (header or "").strip()
    if clean_header:
        text_blocks.append(clean_header)

    for cat_key, label in SHOPPING_EXPORT_CATEGORIES:
        cat_items = buckets.get(cat_key) or []
        if not cat_items:
            continue
        cat_items.sort(key=lambda it: it["name"].casefold())
        groups.append({"category": cat_key, "label": label, "items": cat_items})
        section_lines = [label]
        for it in cat_items:
            section_lines.append(f"- {it['name']} — {it['quantity_text']}")
        text_blocks.append("\n".join(section_lines))

    return {
        "total_items": len(merged),
        "groups": groups,
        "plain_text": "\n\n".join(text_blocks).strip(),
    }


async def build_meal_plan_shopping_export(
    db,
    user: Dict[str, Any],
    plan_rows: List[Dict[str, Any]],
    *,
    include_active_shopping: bool = False,
) -> Dict[str, Any]:
    """Сформировать категоризированный список продуктов для меню на 3–4 дня."""
    user_id = int(user["id"])
    missing = await compute_meal_plan_missing_shopping(db, user, plan_rows)
    combined: List[Dict[str, Any]] = list(missing)
    if include_active_shopping:
        shop_rows = [
            r for r in rows_to_dicts(await repo.list_shopping_items(db, user_id, scope="household"))
            if not r.get("is_bought")
        ]
        for row in shop_rows:
            name = str(row.get("item_name") or "").strip()
            if not name:
                continue
            combined.append(
                {
                    "name": name,
                    "qty": float(row.get("quantity") or 1.0),
                    "unit": str(row.get("unit") or "шт"),
                }
            )
    header = (
        "Список покупок (меню + общий список)"
        if include_active_shopping
        else "Список покупок под меню на 3–4 дня"
    )
    export = build_shopping_export(
        combined,
        header=header,
        preserve_explicit=include_active_shopping,
    )
    return {
        **export,
        "missing_count": len(missing),
        "include_active_shopping": include_active_shopping,
    }


def _normalize_unit(raw_unit: str) -> str:
    """Нормализовать единицу измерения продукта."""
    u = (raw_unit or "").strip().lower().rstrip(".")
    if u in {"кг", "kg", "килограмм", "килограмма"}:
        return "kg"
    if u in {"г", "гр", "g", "грамм", "грамма", "граммов"}:
        return "g"
    if u in {"мл", "ml"}:
        return "ml"
    if u in {"л", "l", "liter", "liters"} or re.fullmatch(r"литр\w*", u):
        return "l"
    if not u or u.startswith(("шт", "уп", "пач", "бан", "бутыл", "piece")):
        return "шт"
    return u[:12]


def _parse_single_pantry_token(raw_token: str) -> Optional[Dict[str, Any]]:
    token = re.sub(r"\s+", " ", (raw_token or "").strip(" •-—\t"))
    if not token:
        return None

    unit_pat = r"(?:кг|kg|г|гр|мл|ml|л|литр(?:а|ов)?|шт|штук(?:и)?|уп|пач(?:ка|ки|ек)?|бан(?:ка|ки|ок)?)"
    qty = 1.0
    unit = "шт"
    name = token

    m_tail = re.match(
        rf"^(.+?)\s+(\d+(?:[.,]\d+)?)\s*({unit_pat})\.?$",
        token,
        flags=re.IGNORECASE,
    )
    m_num_tail = re.match(r"^(.+?)\s+(\d+(?:[.,]\d+)?)$", token)
    m_head = re.match(
        rf"^(\d+(?:[.,]\d+)?)\s*({unit_pat})\.?\s+(.+)$",
        token,
        flags=re.IGNORECASE,
    )

    unit_raw = ""
    if m_tail:
        name = m_tail.group(1).strip(" :-—")
        qty = float(m_tail.group(2).replace(",", "."))
        unit_raw = m_tail.group(3).lower()
    elif m_head:
        qty = float(m_head.group(1).replace(",", "."))
        unit_raw = m_head.group(2).lower()
        name = m_head.group(3).strip(" :-—")
    elif m_num_tail:
        name = m_num_tail.group(1).strip(" :-—")
        qty = float(m_num_tail.group(2).replace(",", "."))
        unit = "шт"

    if unit_raw:
        unit = _normalize_unit(unit_raw)

    if not name:
        return None
    name_clean = name[:1].upper() + name[1:]
    return {
        "name": name_clean,
        "amount": qty,
        "unit": unit,
        "category": _guess_pantry_category(name_clean),
    }


def parse_bulk_pantry_text(text: str) -> List[Dict[str, Any]]:
    """Разобрать списочный ввод продуктов вроде 'Тофу 300 г, гречка 1 кг, огурцы'."""
    parts = re.split(r"[,;\n]+", text or "")
    items: List[Dict[str, Any]] = []
    for part in parts:
        parsed = _parse_single_pantry_token(part)
        if parsed:
            items.append(parsed)
    return items


async def bulk_add_pantry_items(
    db,
    user_id: int,
    text: str,
) -> Dict[str, Any]:
    """Добавить список продуктов в холодильник в 1 сообщение и вычеркнуть их из покупок."""
    parsed = parse_bulk_pantry_text(text)
    added: List[Dict[str, Any]] = []
    synced_shopping: List[str] = []

    for item in parsed:
        pid = await repo.create_pantry_item(
            db,
            user_id,
            item["name"],
            item["amount"],
            item["unit"],
            None,
            item["category"],
        )
        synced = await repo.find_and_delete_shopping_item_by_name(
            db, user_id, item["name"], scope="household"
        )
        if synced and synced.get("item_name"):
            synced_shopping.append(str(synced["item_name"]))
        added.append({**item, "id": pid})

    return {"added": added, "synced_shopping": synced_shopping}


def _normalize_cart_item(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    name = re.sub(r"\s+", " ", str(raw.get("name") or "").strip())
    if not name:
        return None
    name_clean = name[:1].upper() + name[1:]
    raw_amt = raw.get("amount") if raw.get("amount") is not None else raw.get("quantity")
    try:
        amount = float(raw_amt) if raw_amt is not None else 1.0
    except (TypeError, ValueError):
        amount = 1.0
    if amount <= 0:
        amount = 1.0
    unit = _normalize_unit(str(raw.get("unit") or "шт"))
    category = str(raw.get("category") or "").strip() or _guess_pantry_category(name_clean)
    expires_at = str(raw.get("expires_at") or "").strip() or None
    return {
        "name": name_clean,
        "amount": round(amount, 2),
        "unit": unit,
        "category": category,
        "expires_at": expires_at,
    }


def _extract_price_number(text: str) -> Optional[float]:
    m = re.search(r"(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)\s*(?:₽|руб\.?|р\.?)\b", text, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)", text)
    if not m:
        return None
    num_str = re.sub(r"\s+", "", m.group(1)).replace(",", ".")
    try:
        val = float(num_str)
        return round(val, 2) if val > 0 else None
    except ValueError:
        return None


def _parse_cart_text_offline(raw_text: str) -> Dict[str, Any]:
    raw = (raw_text or "").strip()
    if not raw:
        return {"items": [], "total_amount": None, "store": ""}

    total_amount: Optional[float] = None
    store = "Перекрёсток" if "перекр" in raw.casefold() else ""
    lines = [re.sub(r"^\s*\d+[.)]\s*", "", ln.strip()) for ln in raw.splitlines() if ln.strip()]
    if len(lines) == 1 and ("," in lines[0] or ";" in lines[0]):
        lines = [part.strip() for part in re.split(r"[,;]+", lines[0]) if part.strip()]

    items: List[Dict[str, Any]] = []
    for ln in lines:
        low = ln.casefold()
        if re.match(r"^(?:итого|всего|к\s+оплате|сумма\s+заказа|оплата)\b", low):
            extracted = _extract_price_number(ln)
            if extracted is not None:
                total_amount = extracted
            continue
        if re.match(r"^(?:доставка|скидка|промокод|списано|бонус|пакет|перекр[её]сток)\b", low):
            continue

        cleaned = re.sub(
            r"(?:\s*[—–-]\s*)?\d[\d\s]*(?:[.,]\d{1,2})?\s*(?:₽|руб\.?|р\.?)\.?\s*$",
            "",
            ln,
            flags=re.IGNORECASE,
        ).strip()
        mult = 1
        m_mult = re.search(r"\s+(?:×|x|х|\*)\s*(\d+)\s*(?:шт\.?)?$", cleaned, flags=re.IGNORECASE)
        if m_mult:
            mult = max(1, min(50, int(m_mult.group(1))))
            cleaned = cleaned[: m_mult.start()].strip()

        parsed = _parse_single_pantry_token(cleaned)
        if parsed:
            if mult > 1:
                parsed["amount"] = round(float(parsed["amount"]) * mult, 2)
            norm = _normalize_cart_item(parsed)
            if norm:
                items.append(norm)

    return {"items": items, "total_amount": total_amount, "store": store}


_CART_PARSE_SYSTEM_PROMPT = (
    "Ты извлекаешь список купленных продуктов из скриншота корзины/чека магазина или текстового списка. "
    "Ответь строго JSON-объектом вида: "
    "{\"store\": str, \"total_amount\": number|null, "
    "\"items\": [{\"name\": str, \"amount\": number, \"unit\": \"g\"|\"kg\"|\"ml\"|\"l\"|\"шт\", \"category\": str}]}. "
    "Игнорируй плату за доставку, пакеты, скидки и служебные подписи; объединяй множитель (например 300 г x 2 -> 600 g)."
)


async def parse_cart_input(
    raw_text: str = "",
    *,
    image_bytes: Optional[bytes] = None,
    image_mime: str = "image/jpeg",
    llm_client_instance: Optional[LLMClient] = None,
) -> Dict[str, Any]:
    """Разобрать корзину/чек из текста или фото через LLM (с оффлайн-fallback для текста)."""
    clean_text = (raw_text or "").strip()
    client = llm_client_instance or default_llm_client

    if image_bytes:
        if not client.is_configured():
            return {
                "ok": False,
                "items": [],
                "total_amount": None,
                "store": "",
                "used_llm": False,
                "error": "llm_not_configured",
            }
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{image_mime};base64,{b64}"
        prompt = "Разбери продукты и итоговую сумму с этого скриншота корзины/чека."
        if clean_text:
            prompt += f"\nПодпись пользователя: {clean_text}"
        res = await client.complete_json(
            _CART_PARSE_SYSTEM_PROMPT,
            prompt,
            temperature=0.1,
            max_tokens=1600,
            image_data_urls=[data_url],
        )
        if not res.ok or not isinstance(res.data, dict):
            return {
                "ok": False,
                "items": [],
                "total_amount": None,
                "store": "",
                "used_llm": True,
                "error": res.error or "vision_parse_failed",
            }
        items = [x for x in (_normalize_cart_item(r) for r in (res.data.get("items") or [])) if x]
        total_raw = res.data.get("total_amount")
        try:
            total_amt = round(float(total_raw), 2) if total_raw is not None and float(total_raw) > 0 else None
        except (TypeError, ValueError):
            total_amt = None
        return {
            "ok": bool(items),
            "items": items,
            "total_amount": total_amt,
            "store": str(res.data.get("store") or "").strip(),
            "used_llm": True,
            "error": None if items else "no_items_found",
        }

    if not clean_text:
        return {"ok": False, "items": [], "total_amount": None, "store": "", "used_llm": False, "error": "empty_input"}

    if client.is_configured():
        res = await client.complete_json(
            _CART_PARSE_SYSTEM_PROMPT,
            f"Разбери корзину/чек/список продуктов:\n{clean_text}",
            temperature=0.1,
            max_tokens=1600,
        )
        if res.ok and isinstance(res.data, dict):
            items = [x for x in (_normalize_cart_item(r) for r in (res.data.get("items") or [])) if x]
            if items:
                total_raw = res.data.get("total_amount")
                try:
                    total_amt = round(float(total_raw), 2) if total_raw is not None and float(total_raw) > 0 else None
                except (TypeError, ValueError):
                    total_amt = None
                return {
                    "ok": True,
                    "items": items,
                    "total_amount": total_amt,
                    "store": str(res.data.get("store") or "").strip(),
                    "used_llm": True,
                    "error": None,
                }

    offline = _parse_cart_text_offline(clean_text)
    return {
        "ok": bool(offline["items"]),
        "items": offline["items"],
        "total_amount": offline["total_amount"],
        "store": offline["store"],
        "used_llm": False,
        "error": None if offline["items"] else "no_items_found",
    }


async def import_cart_items(
    db,
    user: Dict[str, Any],
    items: List[Dict[str, Any]],
    *,
    total_amount: Optional[float] = None,
    expense_category: str = "продукты",
    expense_scope: str = "personal",
    sync_shopping: bool = True,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Пакетно добавить корзину продуктов в холодильник, вычеркнуть покупки и опционально записать трату."""
    _ = now_utc
    user_id = int(user["id"])
    added: List[Dict[str, Any]] = []
    synced_shopping: List[str] = []
    shop_rows = (
        rows_to_dicts(await repo.list_shopping_items(db, user_id, scope="household"))
        if sync_shopping
        else []
    )
    removed_shop_ids: Set[int] = set()

    for raw in items or []:
        norm = _normalize_cart_item(raw)
        if not norm:
            continue
        pid = await repo.create_pantry_item(
            db,
            user_id,
            norm["name"],
            norm["amount"],
            norm["unit"],
            norm["expires_at"],
            norm["category"],
        )
        added.append({**norm, "id": pid})
        if sync_shopping:
            synced = await repo.find_and_delete_shopping_item_by_name(
                db, user_id, norm["name"], scope="household"
            )
            if synced and synced.get("item_name"):
                synced_shopping.append(str(synced["item_name"]))
                if synced.get("id") is not None:
                    removed_shop_ids.add(int(synced["id"]))
            else:
                matched_shop = next(
                    (
                        s
                        for s in shop_rows
                        if int(s["id"]) not in removed_shop_ids
                        and not s.get("is_bought")
                        and ingredients_match(norm["name"], str(s.get("item_name") or ""))
                    ),
                    None,
                )
                if matched_shop:
                    await repo.delete_shopping_item(db, user_id, int(matched_shop["id"]), scope="household")
                    removed_shop_ids.add(int(matched_shop["id"]))
                    synced_shopping.append(str(matched_shop["item_name"]))

    expense_id: Optional[int] = None
    recorded_total: Optional[float] = None
    if total_amount is not None:
        try:
            amt = float(total_amount)
        except (TypeError, ValueError):
            amt = 0.0
        if amt > 0:
            recorded_total = round(amt, 2)
            cat = str(expense_category or "продукты").strip() or "продукты"
            eff_scope = "household" if expense_scope == "household" else "personal"
            expense_id = await repo.add_expense(db, user_id, recorded_total, cat, scope=eff_scope)

    return {
        "ok": True,
        "added_count": len(added),
        "added": added,
        "synced_shopping": synced_shopping,
        "expense_id": expense_id,
        "total_amount": recorded_total,
    }


_AI_CHEF_SYSTEM_PROMPT = (
    "Ты — практичный домашний ИИ-шеф HiDL. Придумывай понятные, вкусные рецепты из того, что есть дома, "
    "и подстраивай их под КБЖУ-остаток пользователя на сегодня. Ответь строго JSON-объектом вида: "
    "{\"recipes\": [{\"title\": str, \"desc\": str, \"time_minutes\": int, \"base_servings\": int, "
    "\"tags\": [str], \"nutrition\": {\"kcal\": int, \"protein_g\": float, \"fat_g\": float, \"carbs_g\": float}, "
    "\"ingredients\": [{\"name\": str, \"qty\": float, \"unit\": str}], \"steps\": [str]}]}. "
    "Строго соблюдай диету пользователя (vegan = 0 животных продуктов: без мяса, рыбы, молочки, сыра, яиц, мёда; "
    "vegetarian = без мяса и рыбы). Поле nutrition указывай примерно за 1 порцию. В первую очередь используй скоропортящиеся продукты."
)


def _normalize_ai_recipe(
    raw: Any,
    *,
    profile: str,
    eff_slot: str,
    source: str = "ai_chef",
) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    title = re.sub(r"\s+", " ", str(raw.get("title") or "").strip())
    if not title:
        return None
    desc = str(raw.get("desc") or "").strip()
    try:
        t_min = max(5, min(180, int(raw.get("time_minutes") or 20)))
    except (TypeError, ValueError):
        t_min = 20
    try:
        servings = max(1, min(8, int(raw.get("base_servings") or 2)))
    except (TypeError, ValueError):
        servings = 2

    raw_tags = [str(t).strip().lower() for t in (raw.get("tags") or []) if str(t).strip()]
    tags = list(dict.fromkeys(raw_tags))
    if profile == "vegan":
        if "vegan" not in tags:
            tags.append("vegan")
        if "vegetarian" not in tags:
            tags.append("vegetarian")
    elif profile == "vegetarian":
        if "vegetarian" not in tags:
            tags.append("vegetarian")
    elif "omnivore" not in tags:
        tags.append("omnivore")
    if not any(slot in tags for slot in ("breakfast", "lunch", "dinner", "snack")):
        tags.append(eff_slot)
    if t_min <= 15 and "fast" not in tags:
        tags.append("fast")

    ingredients: List[Dict[str, Any]] = []
    for ing in raw.get("ingredients") or []:
        if not isinstance(ing, dict):
            continue
        iname = str(ing.get("name") or "").strip()
        if not iname:
            continue
        try:
            iqty = float(ing.get("qty") or 1.0)
        except (TypeError, ValueError):
            iqty = 1.0
        iunit = _normalize_unit(str(ing.get("unit") or "шт"))
        ingredients.append({"name": iname, "qty": round(max(0.1, iqty), 2), "unit": iunit})

    steps = [str(s).strip() for s in (raw.get("steps") or []) if str(s).strip()]
    if not ingredients or not steps:
        return None

    recipe_obj: Dict[str, Any] = {
        "title": title,
        "desc": desc,
        "time_minutes": t_min,
        "base_servings": servings,
        "tags": tags,
        "nutrition": raw.get("nutrition") if isinstance(raw.get("nutrition"), dict) else None,
        "ingredients": ingredients,
        "steps": steps,
        "source": str(source or "ai_chef"),
    }
    recipe_obj["nutrition"] = fitness_nutrition.estimate_recipe_nutrition(recipe_obj)
    return recipe_obj


async def generate_ai_recipes_from_pantry(
    db,
    user: Dict[str, Any],
    *,
    meal_slot: Optional[str] = None,
    max_minutes: int = 30,
    count: int = 3,
    llm_client_instance: Optional[LLMClient] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Сгенерировать 2–3 новых рецепта через LLM по реальным остаткам холодильника и КБЖУ."""
    now = now_utc or datetime.datetime.utcnow()
    user_id = int(user["id"])
    tz = user.get("timezone") or "UTC"
    local_hhmm = format_time_local(now, tz)
    try:
        local_hour = int(local_hhmm.split(":")[0])
    except Exception:
        local_hour = 13

    eff_slot = meal_slot if meal_slot in {"breakfast", "lunch", "dinner"} else infer_meal_slot(local_hour)
    slot_label = MEAL_SLOT_LABELS.get(eff_slot, "приём пищи")
    profile = await get_user_meal_profile(db, user_id)
    pantry = rows_to_dicts(await repo.list_pantry_items(db, user_id))
    local_today = local_date_str(now, tz)
    soon, _ = await repo.pantry_expiring(db, user_id, local_today, window_days=3)
    soon_rows = rows_to_dicts(soon)
    expiring_names = [str(r.get("name") or "") for r in soon_rows if r.get("name")]
    advice = await fitness_nutrition.get_daily_nutrition_advice(db, user, now_utc=now)

    client = llm_client_instance or default_llm_client
    if not client.is_configured():
        return {
            "ok": False,
            "error": "llm_not_configured",
            "recipes": [],
            "pantry_count": len(pantry),
            "expiring_names": expiring_names,
            "meal_profile": profile,
            "meal_slot": eff_slot,
            "meal_slot_label": slot_label,
            "daily_advice": advice,
        }

    eff_count = max(1, min(5, int(count or 3)))
    pantry_desc = (
        ", ".join(f"{p['name']} ({p.get('amount', 1)} {p.get('unit', 'шт')})" for p in pantry[:40])
        if pantry
        else "холодильник пуст — предложи простые базовые домашние блюда"
    )
    expiring_desc = ", ".join(expiring_names) if expiring_names else "нет срочных"
    prompt = (
        f"Придумай ровно {eff_count} разных рецепта на {slot_label} до {max_minutes} мин.\n"
        f"Профиль питания: {profile}.\n"
        f"Ориентир по КБЖУ: {advice['chef_hint']}\n"
        f"Продукты в холодильнике: {pantry_desc}.\n"
        f"Скоропортящиеся (используй в первую очередь): {expiring_desc}.\n"
        "Опирайся на имеющиеся продукты; допустимо добавить не больше 1–2 базовых мелочей."
    )
    res = await client.complete_json(
        _AI_CHEF_SYSTEM_PROMPT,
        prompt,
        temperature=0.4,
        max_tokens=1800,
    )
    if not res.ok or not isinstance(res.data, dict):
        return {
            "ok": False,
            "error": res.error or "ai_chef_failed",
            "recipes": [],
            "pantry_count": len(pantry),
            "expiring_names": expiring_names,
            "meal_profile": profile,
            "meal_slot": eff_slot,
            "meal_slot_label": slot_label,
            "daily_advice": advice,
        }

    recipes = [
        rec
        for rec in (_normalize_ai_recipe(r, profile=profile, eff_slot=eff_slot) for r in (res.data.get("recipes") or []))
        if rec is not None
    ][:eff_count]

    return {
        "ok": bool(recipes),
        "error": None if recipes else "no_valid_recipes",
        "recipes": recipes,
        "pantry_count": len(pantry),
        "expiring_names": expiring_names,
        "meal_profile": profile,
        "meal_slot": eff_slot,
        "meal_slot_label": slot_label,
        "daily_advice": advice,
    }


_RECIPE_IMPORT_SYSTEM_PROMPT = (
    "Ты извлекаешь один структурированный кулинарный рецепт из фото страницы книги/экрана, веб-страницы или текста. "
    "Ответь строго JSON-объектом вида: "
    "{\"title\": str, \"desc\": str, \"time_minutes\": int, \"base_servings\": int, "
    "\"tags\": [str], \"nutrition\": {\"kcal\": int, \"protein_g\": float, \"fat_g\": float, \"carbs_g\": float}|null, "
    "\"ingredients\": [{\"name\": str, \"qty\": float, \"unit\": str}], \"steps\": [str]}. "
    "Пиши на русском языке; теги выбирай из breakfast, lunch, dinner, snack, salad, fast, healthy, budget, "
    "и при отсутствии животных продуктов добавляй vegetarian и/или vegan."
)

_CGNAT_NET = ipaddress.IPv4Network("100.64.0.0/10")


def _is_public_ip(ip_str: str) -> bool:
    clean = str(ip_str or "").strip().split("%", 1)[0]
    if not clean:
        return False
    try:
        ip = ipaddress.ip_address(clean)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
        return False
    return bool(
        ip.is_global
        and not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    )


async def _validate_public_url(url: str, *, resolve_fn=None) -> str:
    """Проверить URL на безопасность (разрешены только http/https на публичные IP)."""
    clean = (url or "").strip()
    if not clean:
        raise ValueError("unsafe_url")
    parsed = urllib.parse.urlparse(clean)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError("unsafe_url")
    if not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ValueError("unsafe_url")
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".arpa")):
        raise ValueError("unsafe_url")

    try:
        ipaddress.ip_address(host)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False

    if is_literal_ip:
        if not _is_public_ip(host):
            raise ValueError("unsafe_url")
        return parsed._replace(fragment="").geturl()

    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        if resolve_fn is not None:
            resolved = resolve_fn(host)
            if asyncio.iscoroutine(resolved):
                resolved = await resolved
        else:
            resolved = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except Exception as exc:
        raise ValueError("unsafe_url") from exc

    ips: List[str] = []
    for entry in resolved or []:
        if isinstance(entry, str):
            ips.append(entry)
        elif isinstance(entry, (tuple, list)) and len(entry) >= 5 and isinstance(entry[4], (tuple, list)):
            ips.append(str(entry[4][0]))
    if not ips or not all(_is_public_ip(ip_s) for ip_s in ips):
        raise ValueError("unsafe_url")
    return parsed._replace(fragment="").geturl()


async def _fetch_recipe_url_safe(
    url: str,
    *,
    timeout_sec: float = 10.0,
    max_bytes: int = 512 * 1024,
    resolve_fn=None,
) -> Tuple[str, str]:
    """Безопасно скачать текстовую/HTML-страницу с проверкой каждого редиректа от SSRF."""
    cur_url = await _validate_public_url(url, resolve_fn=resolve_fn)
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    headers = {
        "User-Agent": "HiDL-RecipeBot/1.0 (+https://hidl.fodders.ru)",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,text/plain;q=0.8",
        "Accept-Language": "ru,en;q=0.8",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for _ in range(4):
            cur_url = await _validate_public_url(cur_url, resolve_fn=resolve_fn)
            async with session.get(cur_url, allow_redirects=False) as resp:
                if resp.status in {301, 302, 303, 307, 308}:
                    loc = resp.headers.get("Location") or ""
                    if not loc:
                        raise RuntimeError("redirect_missing_location")
                    cur_url = urllib.parse.urljoin(cur_url, loc)
                    continue
                if resp.status != 200:
                    raise RuntimeError(f"http_{resp.status}")
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if ctype and not any(tok in ctype for tok in ("text/", "html", "json", "xml")):
                    raise RuntimeError("unsupported_content_type")
                raw_bytes = await resp.content.read(max_bytes)
                return raw_bytes.decode("utf-8", errors="replace"), cur_url
    raise RuntimeError("too_many_redirects")


def _parse_iso_duration_minutes(val: Any) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        v = int(round(float(val)))
        return v if v > 0 else None
    s = str(val).strip()
    if not s:
        return None
    if s.isdigit():
        v = int(s)
        return v if v > 0 else None
    m = re.match(r"^P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?", s, flags=re.IGNORECASE)
    if not m:
        return None
    hours = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    total = hours * 60 + mins
    return total if total > 0 else None


def _parse_fraction_number(raw_num: str) -> Optional[float]:
    s = (raw_num or "").strip().replace(",", ".")
    if not s:
        return None
    if "-" in s and not s.startswith("-"):
        parts = [p for p in s.split("-") if p.strip()]
        if len(parts) == 2:
            s = parts[0]
    if "/" in s:
        parts = s.split("/", 1)
        try:
            den = float(parts[1])
            return round(float(parts[0]) / den, 2) if den > 0 else None
        except ValueError:
            return None
    try:
        val = float(s)
        return round(val, 2) if val > 0 else None
    except ValueError:
        return None


_KITCHEN_UNIT_PAT = (
    r"ст\.?\s*л\.?|столов(?:ая|ые|ых)\s+ложк(?:а|и|ек)|tbsp|"
    r"ч\.?\s*л\.?|чайн(?:ая|ые|ых)\s+ложк(?:а|и|ек)|tsp|"
    r"стакан(?:а|ов)?|cup(?:s)?|"
    r"зубчик(?:а|ов)?|зуб\.?|"
    r"пучок|пучка|пучков|пуч\.?|"
    r"щепот(?:ка|ки|ок)|"
    r"кг|kg|г|гр|g|мл|ml|литр(?:а|ов)?|л|шт|штук(?:а|и)?|уп|пач(?:ка|ки|ек)?|бан(?:ка|ки|ок)?"
)


def _canonical_kitchen_unit(raw_unit: str) -> str:
    u = re.sub(r"\s+", " ", (raw_unit or "").strip().lower()).rstrip(".")
    if not u:
        return "шт"
    if u in {"ст. л", "ст.л", "ст л", "tbsp"} or u.startswith("столов"):
        return "ст. л."
    if u in {"ч. л", "ч.л", "ч л", "tsp"} or u.startswith("чайн"):
        return "ч. л."
    if u.startswith("стакан") or u.startswith("cup"):
        return "стакан"
    if u.startswith("зуб"):
        return "зубч."
    if u.startswith("пуч"):
        return "пучок"
    if u.startswith("щепот"):
        return "щепотка"
    return _normalize_unit(u)


def _parse_recipe_ingredient_line(line: str) -> Optional[Dict[str, Any]]:
    raw = html_lib.unescape(str(line or ""))
    for ch, rep in (("½", "0.5"), ("¼", "0.25"), ("¾", "0.75"), ("⅓", "0.33"), ("⅔", "0.67")):
        raw = raw.replace(ch, rep)
    raw = re.sub(r"^\s*(?:[•\-\*—]+|\d+[.)])\s*", "", raw.strip())
    raw = re.sub(r"\s+", " ", raw).strip(" \t,;")
    if not raw:
        return None

    m_taste = re.match(r"^(.+?)\s*(?:[—–:-]|\s)\s*по\s+вкусу\.?$", raw, flags=re.IGNORECASE)
    if m_taste:
        name = m_taste.group(1).strip(" :-—")
        if name:
            return {"name": name[:1].upper() + name[1:], "qty": 1.0, "unit": "по вкусу"}

    num_re = r"\d+(?:[.,]\d+|/\d+)?(?:\s*[-–]\s*\d+(?:[.,]\d+)?)?"
    m_sep = re.match(
        rf"^(.+?)\s*(?:[—–:-]|\s)\s*({num_re})\s*({_KITCHEN_UNIT_PAT})\.?$",
        raw,
        flags=re.IGNORECASE,
    )
    if m_sep:
        name = m_sep.group(1).strip(" :-—")
        qty = _parse_fraction_number(m_sep.group(2)) or 1.0
        unit = _canonical_kitchen_unit(m_sep.group(3))
        if name:
            return {"name": name[:1].upper() + name[1:], "qty": qty, "unit": unit}

    m_head = re.match(
        rf"^({num_re})\s*({_KITCHEN_UNIT_PAT})\.?\s+(.+)$",
        raw,
        flags=re.IGNORECASE,
    )
    if m_head:
        qty = _parse_fraction_number(m_head.group(1)) or 1.0
        unit = _canonical_kitchen_unit(m_head.group(2))
        name = m_head.group(3).strip(" :-—")
        if name:
            return {"name": name[:1].upper() + name[1:], "qty": qty, "unit": unit}

    norm_for_pantry = re.sub(r"\s+[—–:]\s+", " ", raw)
    pantry_parsed = _parse_single_pantry_token(norm_for_pantry)
    if pantry_parsed:
        return {
            "name": pantry_parsed["name"],
            "qty": round(float(pantry_parsed["amount"]), 2),
            "unit": pantry_parsed["unit"],
        }

    return {"name": raw[:1].upper() + raw[1:], "qty": 1.0, "unit": "шт"}


def _iter_jsonld_nodes(obj: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for node in graph:
                yield from _iter_jsonld_nodes(node)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_jsonld_nodes(item)


def _extract_jsonld_steps(raw_instructions: Any) -> List[str]:
    steps: List[str] = []
    if isinstance(raw_instructions, str):
        for part in re.split(r"[\r\n]+", html_lib.unescape(raw_instructions)):
            clean = re.sub(r"^\s*\d+[.)]\s*", "", part.strip())
            if clean:
                steps.append(clean)
        return steps
    if isinstance(raw_instructions, dict):
        if raw_instructions.get("itemListElement"):
            return _extract_jsonld_steps(raw_instructions["itemListElement"])
        txt = html_lib.unescape(str(raw_instructions.get("text") or raw_instructions.get("name") or "").strip())
        if txt:
            steps.append(re.sub(r"\s+", " ", txt))
        return steps
    if isinstance(raw_instructions, list):
        for item in raw_instructions:
            steps.extend(_extract_jsonld_steps(item))
    return steps


def _extract_float_prefix(val: Any) -> Optional[float]:
    if val is None:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", str(val))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _extract_jsonld_recipe(html_text: str) -> Optional[Dict[str, Any]]:
    """Извлечь Schema.org JSON-LD Recipe из HTML страницы."""
    scripts = re.findall(
        r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html_text or "",
        flags=re.DOTALL | re.IGNORECASE,
    )
    for raw_json in scripts:
        raw_clean = raw_json.strip()
        if not raw_clean:
            continue
        try:
            payload = json.loads(raw_clean)
        except Exception:
            continue
        for node in _iter_jsonld_nodes(payload):
            raw_type = node.get("@type")
            types = [raw_type] if isinstance(raw_type, str) else (raw_type if isinstance(raw_type, list) else [])
            if not any(str(t).split("/")[-1].lower() == "recipe" for t in types):
                continue

            title = html_lib.unescape(str(node.get("name") or "").strip())
            desc = html_lib.unescape(str(node.get("description") or "").strip())
            raw_ings = node.get("recipeIngredient") or node.get("ingredients") or []
            if isinstance(raw_ings, str):
                raw_ings = [ln.strip() for ln in raw_ings.splitlines() if ln.strip()]
            ingredients = [
                parsed
                for parsed in (_parse_recipe_ingredient_line(str(i)) for i in raw_ings)
                if parsed is not None
            ]
            steps = _extract_jsonld_steps(node.get("recipeInstructions"))
            if not title or not ingredients or not steps:
                continue

            t_min = _parse_iso_duration_minutes(node.get("totalTime"))
            if t_min is None:
                prep_m = _parse_iso_duration_minutes(node.get("prepTime")) or 0
                cook_m = _parse_iso_duration_minutes(node.get("cookTime")) or 0
                t_min = (prep_m + cook_m) or 25

            yield_val = node.get("recipeYield")
            if isinstance(yield_val, list) and yield_val:
                yield_val = yield_val[0]
            serv_num = _extract_float_prefix(yield_val)
            servings = max(1, min(8, int(round(serv_num)))) if serv_num else 2

            nutr_raw = node.get("nutrition") if isinstance(node.get("nutrition"), dict) else None
            nutrition: Optional[Dict[str, Any]] = None
            if nutr_raw:
                kcal = _extract_float_prefix(nutr_raw.get("calories"))
                prot = _extract_float_prefix(nutr_raw.get("proteinContent"))
                fat = _extract_float_prefix(nutr_raw.get("fatContent"))
                carbs = _extract_float_prefix(nutr_raw.get("carbohydrateContent"))
                if kcal is not None and kcal > 0:
                    nutrition = {
                        "kcal": int(round(kcal)),
                        "protein_g": round(prot or 0.0, 1),
                        "fat_g": round(fat or 0.0, 1),
                        "carbs_g": round(carbs or 0.0, 1),
                    }

            tags: List[str] = []
            for key in ("recipeCategory", "keywords", "suitableForDiet"):
                val = node.get(key)
                if isinstance(val, str):
                    tags.extend(p.strip().lower() for p in re.split(r"[,;]+", val) if p.strip())
                elif isinstance(val, list):
                    tags.extend(str(p).strip().lower() for p in val if str(p).strip())

            return {
                "title": title,
                "desc": desc[:240],
                "time_minutes": t_min,
                "base_servings": servings,
                "tags": tags[:10],
                "nutrition": nutrition,
                "ingredients": ingredients,
                "steps": steps,
            }
    return None


def _html_to_plain_text(html_text: str, max_chars: int = 8000) -> str:
    cleaned = re.sub(
        r"<(?:script|style|noscript|svg)[^>]*>.*?</(?:script|style|noscript|svg)>",
        " ",
        html_text or "",
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = re.sub(r"<(?:br|p|div|li|tr|h[1-6]|section|article)[^>]*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html_lib.unescape(cleaned)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in cleaned.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return text[:max_chars]


def _parse_recipe_text_offline(raw_text: str) -> Optional[Dict[str, Any]]:
    """Разобрать структурированный или полуструктурированный текст рецепта без LLM."""
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in (raw_text or "").splitlines() if ln.strip()]
    if len(lines) < 3:
        return None

    title = ""
    time_minutes = 20
    servings = 2
    ingredients_raw: List[str] = []
    steps_raw: List[str] = []

    section = "head"
    for ln in lines:
        low = ln.casefold().rstrip(":").strip()
        if re.match(r"^(?:ингредиент\w*|понадобится|состав|продукты|ingredients?)\b", low):
            section = "ingredients"
            tail = ln.split(":", 1)[1].strip() if ":" in ln else ""
            if tail:
                ingredients_raw.extend(p.strip() for p in re.split(r"[,;]+", tail) if p.strip())
            continue
        if re.match(r"^(?:приготовлен\w*|шаг[иа]?|инструкц\w*|способ(?:\s+приготовления)?|steps?|directions?)\b", low):
            section = "steps"
            tail = ln.split(":", 1)[1].strip() if ":" in ln else ""
            if tail:
                steps_raw.append(tail)
            continue

        if section == "head":
            if not title and not re.match(r"^(?:время|порц|кбжу|калор)", low):
                title = re.sub(r"^(?:рецепт|название)\s*:\s*", "", ln, flags=re.IGNORECASE).strip(" «»\"")
                continue
            m_time = re.search(r"(\d+)\s*(?:мин|мин\.|м\b|minutes?|min\b)", low)
            if m_time:
                time_minutes = max(5, min(180, int(m_time.group(1))))
            m_serv = re.search(r"(\d+)\s*(?:порц|servings?)", low)
            if m_serv:
                servings = max(1, min(8, int(m_serv.group(1))))
            if re.match(r"^[•\-\*]", ln):
                section = "ingredients"
                ingredients_raw.append(ln)
            elif re.match(r"^\d+[.)]\s+", ln):
                section = "steps"
                steps_raw.append(ln)
        elif section == "ingredients":
            if re.match(r"^\d+[.)]\s+", ln) and len(ln) > 25:
                section = "steps"
                steps_raw.append(re.sub(r"^\d+[.)]\s+", "", ln).strip())
            else:
                ingredients_raw.append(ln)
        elif section == "steps":
            clean_step = re.sub(r"^(?:\d+[.)]|[•\-\*])\s*", "", ln).strip()
            if clean_step:
                steps_raw.append(clean_step)

    if not title or not ingredients_raw or not steps_raw:
        return None

    ingredients = [
        ing for ing in (_parse_recipe_ingredient_line(item) for item in ingredients_raw) if ing is not None
    ]
    if not ingredients:
        return None
    return {
        "title": title,
        "desc": "",
        "time_minutes": time_minutes,
        "base_servings": servings,
        "tags": [],
        "ingredients": ingredients,
        "steps": steps_raw,
    }


async def parse_recipe_from_input(
    raw_text: str = "",
    *,
    image_bytes: Optional[bytes] = None,
    image_mime: str = "image/jpeg",
    url: str = "",
    profile: str = "omnivore",
    llm_client_instance: Optional[LLMClient] = None,
    fetch_url_fn=None,
) -> Dict[str, Any]:
    """Импортировать рецепт из фото (Vision LLM), ссылки (JSON-LD/HTML) или текста."""
    clean_text = (raw_text or "").strip()
    client = llm_client_instance or default_llm_client

    if image_bytes:
        if not client.is_configured():
            return {
                "ok": False,
                "recipe": None,
                "source": "photo_import",
                "source_label": "Импорт с фото",
                "used_llm": False,
                "error": "llm_not_configured",
            }
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{image_mime};base64,{b64}"
        prompt = "Извлеки рецепт с этого фото (ингредиенты с граммовками и пошаговое приготовление)."
        if clean_text:
            prompt += f"\nПодпись пользователя: {clean_text}"
        res = await client.complete_json(
            _RECIPE_IMPORT_SYSTEM_PROMPT,
            prompt,
            temperature=0.1,
            max_tokens=1800,
            image_data_urls=[data_url],
        )
        recipe = (
            _normalize_ai_recipe(res.data, profile=profile, eff_slot="lunch", source="photo_import")
            if res.ok and isinstance(res.data, dict)
            else None
        )
        return {
            "ok": recipe is not None,
            "recipe": recipe,
            "source": "photo_import",
            "source_label": "Импорт с фото",
            "used_llm": True,
            "error": None if recipe else (res.error or "vision_parse_failed"),
        }

    target_url = (url or "").strip()
    if not target_url and clean_text:
        m_url = re.search(r"(?:https?|ftp|file)://[^\s<>\"']+", clean_text, flags=re.IGNORECASE)
        if m_url:
            target_url = m_url.group(0).rstrip(".,);")

    if target_url:
        parsed_u = urllib.parse.urlparse(target_url)
        host_lbl = (parsed_u.hostname or "").strip()
        source_label = f"Сайт: {host_lbl}" if host_lbl else "Импорт по ссылке"
        try:
            if fetch_url_fn is not None:
                if (parsed_u.scheme or "").lower() not in {"http", "https"}:
                    raise ValueError("unsafe_url")
                fetched = fetch_url_fn(target_url)
                if asyncio.iscoroutine(fetched):
                    fetched = await fetched
                if isinstance(fetched, tuple):
                    page_html, final_url = str(fetched[0] or ""), str(fetched[1] or target_url)
                else:
                    page_html, final_url = str(fetched or ""), target_url
            else:
                page_html, final_url = await _fetch_recipe_url_safe(target_url)
        except ValueError:
            return {
                "ok": False,
                "recipe": None,
                "source": "url_import",
                "source_label": source_label,
                "used_llm": False,
                "error": "unsafe_url",
            }
        except Exception:
            return {
                "ok": False,
                "recipe": None,
                "source": "url_import",
                "source_label": source_label,
                "used_llm": False,
                "error": "fetch_failed",
            }

        final_host = (urllib.parse.urlparse(final_url).hostname or "").strip()
        if final_host:
            source_label = f"Сайт: {final_host}"

        jsonld_raw = _extract_jsonld_recipe(page_html)
        if jsonld_raw:
            norm = _normalize_ai_recipe(jsonld_raw, profile=profile, eff_slot="lunch", source="url_import")
            if norm:
                return {
                    "ok": True,
                    "recipe": norm,
                    "source": "url_import",
                    "source_label": source_label,
                    "used_llm": False,
                    "error": None,
                }

        plain = _html_to_plain_text(page_html)
        if plain and client.is_configured():
            res = await client.complete_json(
                _RECIPE_IMPORT_SYSTEM_PROMPT,
                f"Извлеки один рецепт с веб-страницы ({final_url}):\n\n{plain}",
                temperature=0.1,
                max_tokens=1800,
            )
            if res.ok and isinstance(res.data, dict):
                norm = _normalize_ai_recipe(res.data, profile=profile, eff_slot="lunch", source="url_import")
                if norm:
                    return {
                        "ok": True,
                        "recipe": norm,
                        "source": "url_import",
                        "source_label": source_label,
                        "used_llm": True,
                        "error": None,
                    }

        offline_raw = _parse_recipe_text_offline(plain or page_html)
        norm_off = (
            _normalize_ai_recipe(offline_raw, profile=profile, eff_slot="lunch", source="url_import")
            if offline_raw
            else None
        )
        return {
            "ok": norm_off is not None,
            "recipe": norm_off,
            "source": "url_import",
            "source_label": source_label,
            "used_llm": False,
            "error": None if norm_off else "recipe_not_found",
        }

    if not clean_text:
        return {
            "ok": False,
            "recipe": None,
            "source": "text_import",
            "source_label": "Импорт текстом",
            "used_llm": False,
            "error": "empty_input",
        }

    if client.is_configured():
        res = await client.complete_json(
            _RECIPE_IMPORT_SYSTEM_PROMPT,
            f"Извлеки структурированный рецепт из этого текста:\n\n{clean_text}",
            temperature=0.1,
            max_tokens=1800,
        )
        if res.ok and isinstance(res.data, dict):
            norm = _normalize_ai_recipe(res.data, profile=profile, eff_slot="lunch", source="text_import")
            if norm:
                return {
                    "ok": True,
                    "recipe": norm,
                    "source": "text_import",
                    "source_label": "Импорт текстом",
                    "used_llm": True,
                    "error": None,
                }

    offline_raw = _parse_recipe_text_offline(clean_text)
    norm_off = (
        _normalize_ai_recipe(offline_raw, profile=profile, eff_slot="lunch", source="text_import")
        if offline_raw
        else None
    )
    return {
        "ok": norm_off is not None,
        "recipe": norm_off,
        "source": "text_import",
        "source_label": "Импорт текстом",
        "used_llm": False,
        "error": None if norm_off else "recipe_not_found",
    }
