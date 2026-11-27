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
from utils.profile import is_student_mode
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
    ("oat_yogurt", ("йогурт овсяный", "овсяный йогурт", "йогурт nemoloko", "растительный йогурт")),
    (
        "milk_oat",
        (
            "овсяное молоко",
            "молоко овсяное",
            "напиток овсяный",
            "овсяный напиток",
            "немолоко",
            "nemoloko",
            "растительное молоко",
            "соевое молоко",
            "миндальное молоко",
        ),
    ),
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

_ING_NOUN_ENDINGS = (
    "иями", "ями", "ами", "ого", "ему", "ыми", "ими",
    "ее", "ие", "ые", "ое", "ей", "ой", "ий", "ый",
    "ам", "ям", "ах", "ях", "ом", "ем", "ым", "им",
    "ую", "юю", "ая", "яя", "ою", "ею",
    "ов", "ев", "ия", "ья", "ье",
    "а", "я", "у", "ю", "е", "о", "ы", "и", "ь", "й",
)


def _ing_stem(word: str) -> str:
    """Стем ингредиента с безопасными окончаниями (не режет «-ли» у «фасоли», «брокколи» и т.п.)."""
    w = (word or "").strip().casefold().replace("ё", "е")
    if len(w) >= 4:
        for end in _ING_NOUN_ENDINGS:
            if w.endswith(end) and len(w) - len(end) >= 3:
                w = w[: -len(end)]
                break
    if len(w) >= 4 and w.endswith("ь"):
        w = w[:-1]
    return w


_CANONICAL_ALIAS_RULES: Tuple[Tuple[str, Tuple[Tuple[str, Tuple[str, ...], str], ...]], ...] = tuple(
    (
        key,
        tuple(
            (
                pat.replace("ё", "е"),
                tuple(pat.replace("ё", "е").split()),
                _ing_stem(pat.replace("ё", "е")) if " " not in pat.replace("ё", "е") else "",
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
    "варёной",
    "вареной",
    "готовый",
    "готовая",
    "готовой",
    "консервированный",
    "консервированная",
    "консервированной",
    "консервированного",
    "замороженные",
    "замороженная",
    "замороженной",
    "свежий",
    "свежая",
    "свежая",
    "свежей",
    "свежих",
    "свежие",
    "молодые",
    "спелый",
    "спелая",
    "белый",
    "белая",
    "белой",
    "белого",
    "красный",
    "красная",
    "красной",
    "белокочанная",
    "репчатый",
    "черри",
    "собственном",
    "соку",
    "колотый",
    "чёрная",
    "черная",
    "чёрный",
    "черный",
    "цельнозерновой",
    "цельнозерновая",
    "любые",
    "греческий",
    "суповой",
    "набор",
    "филе",
    "фарш",
    "молотый",
    "молотая",
    "молотые",
    "жареный",
    "жареная",
    "жареной",
    "тушеный",
    "тушеная",
    "очищенный",
    "очищенная",
    "очищенной",
    "сушеный",
    "сушеная",
}

_MODIFIER_STEMS: Set[str] = {
    _ing_stem(w)
    for w in _MODIFIER_WORDS
    if _ing_stem(w) not in {"сыр", "сок", "фил", "фарш"}
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
    norm_stems = {_ing_stem(nt) for nt in norm_tokens}
    for key, rules in _CANONICAL_ALIAS_RULES:
        for pat_norm, pat_tokens, pat_stem in rules:
            if pat_norm == norm:
                return key
            if len(pat_tokens) > 1:
                if all(pt in norm or _ing_stem(pt) in norm_stems for pt in pat_tokens):
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
    all_words = re.findall(r"[a-zа-я0-9]+", norm)
    words = [
        w for w in all_words
        if w not in _MODIFIER_WORDS and _ing_stem(w) not in _MODIFIER_STEMS
    ]
    if not words:
        words = all_words
    return tuple(_ing_stem(w) or w for w in words if len(w) >= 2)


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


def is_dry_legume_item(name: str) -> bool:
    """Вернуть True, если продукт в холодильнике — сухие/неготовые бобовые (требуют замачивания и долгой варки)."""
    low = str(name or "").casefold().replace("ё", "е")
    if not low:
        return False
    if "сух" in low and any(k in low for k in ("фасол", "нут", "горох")):
        return True
    return "колот" in low and "горох" in low


def recipe_expects_ready_legume(recipe: Dict[str, Any], ing_name: str) -> bool:
    """Вернуть True, если рецепт ожидает готовую/консервированную фасоль, нут или горох, а не сухую."""
    low = str(ing_name or "").casefold().replace("ё", "е")
    if not any(k in low for k in ("фасол", "нут", "горох")):
        return False
    if "сух" in low or "стручк" in low or "колот" in low:
        return False
    if any(k in low for k in ("консерв", "варен", "в томате")):
        return True
    try:
        t_min = int(recipe.get("time_minutes") or 25)
    except (TypeError, ValueError):
        t_min = 25
    return t_min <= 25


def detect_recipe_prep_warning(recipe: Dict[str, Any]) -> Optional[str]:
    """Определить, нужна ли рецепту предварительная подготовка заранее (замачивание, на ночь, маринад, разморозка)."""
    steps = [str(s) for s in (recipe.get("steps") or []) if s]
    blob = " ".join([str(recipe.get("title") or ""), str(recipe.get("desc") or ""), *steps]).casefold().replace("ё", "е")
    if not blob:
        return None
    if "на ночь" in blob or "с вечера" in blob:
        return "Подготовка с вечера (на ночь)"
    if "замоч" in blob or "замачив" in blob:
        return "Нужно замочить заранее"
    if "маринов" in blob:
        return "Нужно замариновать заранее"
    if "размороз" in blob:
        return "Нужно разморозить заранее"
    return None


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
            # Если одна основа полностью входит в более подробное описание (с учётом уже проверенных групп)
            if set_a.issubset(set_b) or set_b.issubset(set_a):
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
    """Сопоставить ингредиенты рецепта с текущим холодильником с учётом количества."""
    base_servings = int(recipe.get("base_servings") or 1) or 1
    eff_servings = max(1, int(servings or base_servings))
    factor = eff_servings / base_servings

    have: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    staples: List[Dict[str, Any]] = []
    total_non_staple = 0
    matched_score = 0.0

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

        total_non_staple += 1
        expects_ready_leg = recipe_expects_ready_legume(recipe, ing_name)
        matching_rows = [
            p for p in pantry_items
            if ingredients_match(ing_name, str(p.get("name") or ""))
            and float(p.get("amount") or 0) > 1e-6
            and not (expects_ready_leg and is_dry_legume_item(str(p.get("name") or "")))
        ]
        matched_pantry = matching_rows[0] if matching_rows else None
        if not matched_pantry:
            missing.append({"name": ing_name, "qty": needed_qty, "unit": unit})
            continue

        p_unit = str(matched_pantry.get("unit") or "")
        need_base, kind_n = to_base(needed_qty, unit)
        total_have_base = 0.0
        compatible_found = False
        for mp in matching_rows:
            mp_amt = float(mp.get("amount") or 0)
            mp_unit = str(mp.get("unit") or "")
            hb, kh = to_base(mp_amt, mp_unit)
            if kind_n == kh and kind_n != "other":
                total_have_base += hb
                compatible_found = True

        if compatible_found and total_have_base + 1e-6 < need_base:
            short_qty = round(from_base(need_base - total_have_base, unit or "г"), 2)
            if total_have_base >= need_base * 0.2:
                matched_score += 0.5
                have.append(
                    {
                        "name": ing_name,
                        "qty": round(from_base(total_have_base, unit or "г"), 2),
                        "unit": unit,
                        "pantry_id": int(matched_pantry["id"]),
                        "pantry_name": str(matched_pantry.get("name") or ing_name),
                        "pantry_amount": float(matched_pantry.get("amount") or 0),
                        "pantry_unit": p_unit,
                    }
                )
            missing.append({"name": ing_name, "qty": max(0.01, short_qty), "unit": unit})
        else:
            matched_score += 1.0
            have.append(
                {
                    "name": ing_name,
                    "qty": needed_qty,
                    "unit": unit,
                    "pantry_id": int(matched_pantry["id"]),
                    "pantry_name": str(matched_pantry.get("name") or ing_name),
                    "pantry_amount": float(matched_pantry.get("amount") or 0),
                    "pantry_unit": p_unit,
                }
            )

    coverage = (matched_score / total_non_staple) if total_non_staple > 0 else 1.0
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
        elif k1 == k2 and k1 == "other" and (target["unit"] or "").strip().lower() == (unit or "").strip().lower():
            target["qty"] = round(float(target["qty"] or 0) + qty, 2)
    return merged


def _pantry_covers_item(pantry: List[Dict[str, Any]], name: str, qty: float, unit: str) -> bool:
    """Проверить, хватает ли продукта в холодильнике на требуемое количество."""
    matches = [p for p in pantry if ingredients_match(name, str(p.get("name") or "")) and float(p.get("amount") or 0) > 1e-6]
    if not matches:
        return False
    need_base, kind_n = to_base(qty, unit)
    total_have = 0.0
    any_compatible = False
    for p in matches:
        p_amt = float(p.get("amount") or 0)
        p_unit = str(p.get("unit") or "шт")
        hb, kh = to_base(p_amt, p_unit)
        if kind_n == kh and kind_n != "other":
            total_have += hb
            any_compatible = True
        elif kind_n == "other" and unit.strip().lower() == p_unit.strip().lower():
            total_have += p_amt
            any_compatible = True
    if any_compatible:
        if kind_n == "other":
            return total_have + 1e-6 >= float(qty or 1.0)
        return total_have + 1e-6 >= need_base
    # Разные несовместимые единицы (например, банка/бутылка в холодильнике vs граммы в рецепте) — считаем покрытым
    return True


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
        qty = float(item.get("qty") or 1.0)
        unit = str(item.get("unit") or "шт")
        in_pantry = _pantry_covers_item(pantry, name, qty, unit)
        in_shop = any(ingredients_match(name, str(s.get("item_name") or "")) for s in shop_rows)
        if in_pantry or in_shop:
            skipped.append(item)
            continue
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
    recipe_override: Optional[Dict[str, Any]] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Списать продукты за приготовленное блюдо, обновить меню и начислить карму/закрыть Пульс."""
    user_id = int(user["id"])
    recipe = dict(recipe_override) if isinstance(recipe_override, dict) else await get_recipe_for_user(db, user_id, recipe_id)
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
            if kind_n == "count":
                new_amt = max(0.0, cur_amt - max(1.0, n_qty))
            else:
                ck = _canonical_key(str(item.get("name") or ""))
                nu_low = n_unit.strip().lower()
                is_condiment = ck in {"oil_veg", "soy_sauce", "sweet_chili", "sweet_sour", "teriyaki", "ketchup"} or any(
                    tok in nu_low for tok in ("ч.л", "ст.л", "кап", "щеп")
                ) or (kind_n in {"weight", "volume"} and need_base <= 60.0)
                step_deduct = 0.15 if is_condiment else (0.5 if (kind_n in {"weight", "volume"} and need_base >= 400.0) else 0.35)
                new_amt = max(0.0, round(cur_amt - step_deduct, 2))
        else:
            # Разные единицы (например, ложки/банка vs граммы) — аккуратно списываем долю
            new_amt = max(0.0, round(cur_amt - 0.25, 2))

        balances[pid] = new_amt
        if new_amt <= 0.01:
            await repo.delete_pantry_item(db, user_id, pid)
        else:
            await repo.update_pantry_item(db, user_id, pid, amount=round(new_amt, 2))
        deducted.append({
            "pantry_id": pid,
            "name": item["pantry_name"],
            "before": round(cur_amt, 2),
            "remaining": round(new_amt, 2),
            "unit": p_unit,
        })

    # Отметить в сегодняшнем меню, если совпадает рецепт (или исходный id адаптированного) или слот
    target_rid = str(recipe.get("adapted_from_id") or recipe.get("id") or recipe_id)
    today_plan = await repo.get_meal_plan_for_date(db, user_id, local_date)
    marked_slot: Optional[str] = None
    for slot_row in today_plan:
        if str(slot_row.get("recipe_id") or "") == target_rid:
            await repo.mark_meal_plan_status(db, user_id, local_date, str(slot_row["meal_slot"]), "cooked")
            marked_slot = str(slot_row["meal_slot"])
            break
    if marked_slot is None and meal_slot in {"breakfast", "lunch", "dinner"}:
        for slot_row in today_plan:
            if str(slot_row.get("meal_slot") or "") == meal_slot and slot_row.get("status") == "planned":
                await repo.mark_meal_plan_status(db, user_id, local_date, meal_slot, "cooked")
                marked_slot = meal_slot
                break

    # Синергия с Пульсом еды (breakfast/lunch/dinner): если слот открыт, закрываем его без двойной кармы
    if meal_slot in {"breakfast", "lunch", "dinner"}:
        pulse_food_slot = meal_slot
    elif local_hour < 11:
        pulse_food_slot = "breakfast"
    elif local_hour < 16:
        pulse_food_slot = "lunch"
    else:
        pulse_food_slot = "dinner"
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


_COUNT_WORDS_MAP: Dict[str, float] = {
    "пол": 0.5,
    "половина": 0.5,
    "половину": 0.5,
    "половинка": 0.5,
    "половинку": 0.5,
    "один": 1.0,
    "одно": 1.0,
    "одна": 1.0,
    "одну": 1.0,
    "пару": 2.0,
    "пара": 2.0,
    "два": 2.0,
    "две": 2.0,
    "три": 3.0,
    "четыре": 4.0,
    "пять": 5.0,
    "шесть": 6.0,
}

_PIECE_WEIGHT_G: Dict[str, float] = {
    "яблок": 180.0,
    "банан": 120.0,
    "апельсин": 200.0,
    "груш": 170.0,
    "авокад": 180.0,
    "помидор": 120.0,
    "томат": 120.0,
    "огур": 120.0,
    "картоф": 130.0,
    "картош": 130.0,
    "лук": 100.0,
    "морков": 100.0,
    "яйц": 60.0,
    "перец": 150.0,
    "киви": 80.0,
    "мандарин": 80.0,
    "лимон": 100.0,
    "персик": 140.0,
    "блинчик": 70.0,
    "тост": 35.0,
    "хлеб": 35.0,
    "лаваш": 90.0,
}

_UNIT_ALIAS_RX = (
    r"(?:кг|kg|гр?\.?|грамм\w*|мл|ml|л|l|шт\.?|штук\w*|ст\.?\s*л\.?|ч\.?\s*л\.?|"
    r"столов\w*\s+лож\w*|чайн\w*\s+лож\w*|лож(?:ка|ки|ку|ек))"
)
_NUM_OR_WORD_RX = (
    r"(?:(?P<num>\d+(?:[.,]\d+)?)|"
    r"(?P<word>половинка|половинку|половина|половину|пол|один|одно|одна|одну|"
    r"пару|пара|два|две|три|четыре|пять|шесть))"
)
_QTY_EXTRACT_RE = re.compile(
    rf"(?iu)(?<![a-zа-я0-9]){_NUM_OR_WORD_RX}(?:\s+(?P<unit>{_UNIT_ALIAS_RX}))?(?![a-zа-я0-9])"
)


def _norm_deduct_unit(raw_unit: str) -> str:
    u = re.sub(r"\s+", " ", (raw_unit or "").strip().lower()).rstrip(".")
    if not u:
        return ""
    if u in {"кг", "kg"}:
        return "kg"
    if u in {"г", "гр", "g"} or u.startswith("грамм"):
        return "g"
    if u in {"мл", "ml"}:
        return "ml"
    if u in {"л", "l"}:
        return "l"
    if u in {"ч л", "ч.л", "ч. л"} or "чайн" in u:
        return "ч. л."
    if u in {"ст л", "ст.л", "ст. л"} or "столов" in u or u.startswith("лож"):
        return "ст. л."
    if u == "шт" or u.startswith("штук"):
        return "шт"
    return u


def parse_requested_deduct_qty(raw_text: str) -> Tuple[Optional[float], str]:
    """Извлечь явно запрошенное количество и единицу списания; (None, '') если не указано."""
    txt = (raw_text or "").strip().casefold().replace("ё", "е")
    if not txt:
        return None, ""
    best: Optional[Tuple[float, str]] = None
    for m in _QTY_EXTRACT_RE.finditer(txt):
        raw_num = m.group("num")
        raw_word = m.group("word")
        unit_raw = m.group("unit") or ""
        if raw_num:
            try:
                qty = float(raw_num.replace(",", "."))
            except ValueError:
                continue
        else:
            qty = _COUNT_WORDS_MAP.get((raw_word or "").lower(), 0.0)
        if qty <= 0:
            continue
        unit = _norm_deduct_unit(unit_raw)
        if unit or best is None:
            best = (qty, unit)
            if unit:
                return best
    return best or (None, "")


def _estimate_piece_weight_g(name: str) -> float:
    low = (name or "").strip().casefold().replace("ё", "е")
    for stem, grams in _PIECE_WEIGHT_G.items():
        if stem in low:
            return grams
    return 150.0


def compute_pantry_deduct_amount(
    pantry_item: Dict[str, Any],
    req_qty: Optional[float],
    req_unit: str,
    *,
    default_single: bool = False,
) -> Optional[float]:
    """Пересчитать запрошенное количество списания в единицы хранения в холодильнике.

    Возвращает None, если нужно удалить позицию целиком (когда количество не задано и default_single=False).
    """
    cur_amt = float(pantry_item.get("amount") or 0.0)
    p_unit = str(pantry_item.get("unit") or "шт").strip()
    p_name = str(pantry_item.get("name") or "")
    _, kind_p = to_base(cur_amt, p_unit)

    if req_qty is None:
        if not default_single:
            return None
        if kind_p == "count":
            deduct = 1.0
        elif kind_p == "weight":
            deduct = from_base(_estimate_piece_weight_g(p_name), p_unit)
        elif kind_p == "volume":
            deduct = from_base(200.0, p_unit)
        else:
            deduct = 0.2
    else:
        u = _norm_deduct_unit(req_unit)
        if u == "ст. л.":
            if kind_p == "weight":
                deduct = from_base(15.0 * req_qty, p_unit)
            elif kind_p == "volume":
                deduct = from_base(15.0 * req_qty, p_unit)
            else:
                deduct = min(0.5, round(0.08 * req_qty, 2))
        elif u == "ч. л.":
            if kind_p in {"weight", "volume"}:
                deduct = from_base(5.0 * req_qty, p_unit)
            else:
                deduct = min(0.3, round(0.04 * req_qty, 2))
        else:
            eff_u = u or ("шт" if kind_p in {"count", "weight"} else "")
            req_base, kind_r = to_base(req_qty, eff_u)
            if kind_r == kind_p and kind_p != "other":
                deduct = from_base(req_base, p_unit)
            elif kind_r == "count" and kind_p == "weight":
                deduct = from_base(_estimate_piece_weight_g(p_name) * req_qty, p_unit)
            elif kind_r == "count" and kind_p == "volume":
                deduct = from_base(200.0 * req_qty, p_unit)
            elif kind_p == "other":
                deduct = min(0.5, max(0.1, round(0.1 * req_qty, 2)))
            else:
                deduct = req_qty

    deduct = max(0.0, round(float(deduct), 2))
    if deduct <= 0:
        deduct = 0.1 if cur_amt >= 0.1 else cur_amt
    if cur_amt > 0:
        deduct = min(cur_amt, deduct)
    return round(deduct, 2)


def _strip_qty_words_for_match(segment: str) -> str:
    txt = _QTY_EXTRACT_RE.sub(" ", (segment or "").strip())
    txt = re.sub(r"[()]", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip(" ,.-")
    return txt


def find_pantry_matches_for_meal_text(
    pantry_items: List[Dict[str, Any]],
    meal_text: str,
    *,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Найти продукты из холодильника, упомянутые в съеденном блюде, для 1-тап списания."""
    raw = (meal_text or "").strip()
    if not raw or not pantry_items:
        return []

    segments = [
        s.strip()
        for s in re.split(r"\s*(?:,|;|\+|\n|\s+и\s+|\s+с\s+|\s+со\s+|\s+на\s+)\s*", raw, flags=re.IGNORECASE)
        if s and s.strip()
    ]
    if not segments:
        segments = [raw]

    matches: List[Dict[str, Any]] = []
    seen_pids: Set[int] = set()
    for seg in segments:
        req_qty, req_unit = parse_requested_deduct_qty(seg)
        seg_clean = _strip_qty_words_for_match(seg) or seg
        seg_low = seg_clean.casefold().replace("ё", "е")
        for p in pantry_items:
            pid = int(p.get("id") or 0)
            cur_amt = float(p.get("amount") or 0)
            p_name = str(p.get("name") or "").strip()
            if not pid or cur_amt <= 0 or not p_name or pid in seen_pids:
                continue
            if is_staple_ingredient(p_name):
                continue
            p_low = p_name.casefold().replace("ё", "е")
            if "сгущ" in seg_low and "сгущ" not in p_low:
                continue
            if "молок" in p_low and "кофе" in seg_low and "молок" not in seg_low:
                continue
            if not ingredients_match(seg_clean, p_name):
                continue
            deduct_qty = compute_pantry_deduct_amount(p, req_qty, req_unit, default_single=True)
            if deduct_qty is None or deduct_qty <= 0:
                continue
            p_unit = str(p.get("unit") or "шт").strip()
            matches.append(
                {
                    "pantry_id": pid,
                    "name": p_name,
                    "unit": p_unit,
                    "deduct_qty": deduct_qty,
                    "btn_label": f"❄️ Списать: {p_name[:20]} (−{format_quantity(deduct_qty, p_unit)})",
                }
            )
            seen_pids.add(pid)
            if len(matches) >= limit:
                return matches
    return matches


async def deduct_pantry_entry(
    db,
    user_id: int,
    pantry_id: int,
    deduct_qty: Optional[float],
) -> Dict[str, Any]:
    """Списать указанное количество продукта из холодильника (или удалить, если осталось <= 0.01)."""
    uid = int(user_id)
    pid = int(pantry_id)
    pantry = rows_to_dicts(await repo.list_pantry_items(db, uid))
    target = next((p for p in pantry if int(p.get("id") or 0) == pid), None)
    if not target:
        return {"ok": False, "error": "not_found"}

    cur_amt = float(target.get("amount") or 0.0)
    p_unit = str(target.get("unit") or "шт").strip()
    p_name = str(target.get("name") or "Продукт").strip()

    if deduct_qty is None or cur_amt - float(deduct_qty) <= 0.01:
        await repo.delete_pantry_item(db, uid, pid)
        return {
            "ok": True,
            "pantry_id": pid,
            "name": p_name,
            "unit": p_unit,
            "deducted": round(cur_amt, 2),
            "remaining": 0.0,
            "deleted": True,
            "prev_item": dict(target),
        }

    eff_deduct = round(max(0.0, float(deduct_qty)), 2)
    new_amt = round(max(0.0, cur_amt - eff_deduct), 2)
    await repo.update_pantry_item(db, uid, pid, amount=new_amt)
    return {
        "ok": True,
        "pantry_id": pid,
        "name": p_name,
        "unit": p_unit,
        "deducted": eff_deduct,
        "remaining": new_amt,
        "deleted": False,
        "prev_item": dict(target),
    }


_SLOT_MACRO_SHARES: Dict[str, float] = {
    "breakfast": 0.25,
    "lunch": 0.40,
    "dinner": 0.35,
}

# (key, keywords, default_label, kcal, protein_g, fat_g, carbs_g, vegan_ok, sweet_ok, savory_ok)
_PANTRY_BOOSTER_CATALOG: Tuple[Tuple[str, Tuple[str, ...], str, int, float, float, float, bool, bool, bool], ...] = (
    ("cutlets", ("котлет", "green meat", "hichicken", "биточ", "шницел"), "растительные котлеты", 250, 22.0, 10.0, 16.0, True, False, True),
    ("hiballs", ("фрикадел", "hiballs", "тефтел"), "растительные фрикадельки", 240, 20.0, 10.0, 15.0, True, False, True),
    ("nuggets", ("наггетс",), "растительные наггетсы", 260, 16.0, 12.0, 20.0, True, False, True),
    ("seitan", ("сейтан",), "сейтан 120 г", 210, 30.0, 2.0, 14.0, True, False, True),
    ("tofu", ("тофу",), "тофу 150 г", 180, 18.0, 9.0, 4.0, True, True, True),
    ("protein_powder", ("протеин", "изолят", "protein"), "протеиновый коктейль", 120, 24.0, 1.0, 3.0, True, True, False),
    ("lentils", ("чечевиц",), "чечевица", 260, 19.0, 1.0, 44.0, True, False, True),
    ("beans", ("нут", "фасол", "эдамаме"), "фасоль/нут", 185, 11.0, 2.0, 30.0, True, False, True),
    ("peanut_butter", ("арахисовая паста", "ореховая паста", "миндаль", "грецк"), "арахисовая паста 30 г", 180, 8.0, 15.0, 6.0, True, True, False),
    ("hummus", ("хумус",), "хумус", 160, 6.0, 10.0, 12.0, True, False, True),
    ("yogurt", ("йогурт",), "растительный йогурт", 110, 3.0, 3.0, 17.0, True, True, False),
    ("seeds", ("чиа", "кунжут", "тыквенные семеч"), "семена", 115, 4.0, 9.0, 4.0, True, True, True),
    # Невеганские опции (только при profile != 'vegan')
    ("cottage_cheese", ("творог",), "творог 150 г", 165, 25.0, 4.0, 5.0, False, True, True),
    ("eggs", ("яйц",), "2 варёных яйца", 155, 13.0, 11.0, 1.0, False, False, True),
    ("poultry", ("куриц", "индейк", "филе"), "курица/индейка 140 г", 200, 31.0, 6.0, 0.0, False, False, True),
    ("fish", ("рыб", "тунец", "лосос"), "рыба/тунец 140 г", 190, 29.0, 7.0, 0.0, False, False, True),
)


def _is_sweet_breakfast(recipe: Dict[str, Any]) -> bool:
    blob = " ".join(
        [
            str(recipe.get("title") or ""),
            " ".join(str(i.get("name") or "") for i in (recipe.get("ingredients") or []) if isinstance(i, dict)),
        ]
    ).casefold().replace("ё", "е")
    return any(k in blob for k in ("оладь", "блин", "овсян", "каша", "смузи", "банан", "ягод", "гранол", "сырник", "тост с арах"))


def _short_pantry_label(raw_name: str, fallback: str) -> str:
    clean = re.sub(r"\s+\d+(?:[.,]\d+)?\s*(?:г|кг|мл|л|шт)\b.*$", "", str(raw_name or "").strip(), flags=re.IGNORECASE).strip()
    if not clean:
        return fallback
    words = clean.split()
    if len(words) > 4:
        clean = " ".join(words[:4])
    return clean[:38]


def _match_booster_spec(name: str, *, meal_profile: str) -> Optional[Tuple[str, str, int, float, float, float, bool, bool, bool]]:
    low = str(name or "").casefold().replace("ё", "е")
    is_vegan = str(meal_profile or "omnivore").strip().lower() == "vegan"
    for spec in _PANTRY_BOOSTER_CATALOG:
        if is_vegan and not spec[7]:
            continue
        if any(kw in low for kw in spec[1]):
            return spec
    return None


def _pick_pantry_protein_booster(
    pantry: List[Dict[str, Any]],
    *,
    meal_slot: str,
    recipe: Dict[str, Any],
    meal_profile: str,
    used_boosters: Set[str],
    max_kcal: float,
) -> Optional[Dict[str, Any]]:
    is_sweet = meal_slot == "breakfast" and _is_sweet_breakfast(recipe)
    ing_names = [
        str(i.get("name") or "").casefold().replace("ё", "е")
        for i in (recipe.get("ingredients") or [])
        if isinstance(i, dict)
    ]
    title_low = str(recipe.get("title") or "").casefold().replace("ё", "е")

    candidates: List[Tuple[float, Dict[str, Any]]] = []
    for item in pantry:
        p_name = str(item.get("name") or "").strip()
        if not p_name:
            continue
        p_low = p_name.casefold().replace("ё", "е")
        if p_low in title_low or any(ingredients_match(p_name, ing) for ing in ing_names if ing):
            continue
        spec = _match_booster_spec(p_name, meal_profile=meal_profile)
        if spec is None:
            continue
        key, _, fallback_lbl, kcal, p_g, f_g, c_g, _v_ok, sweet_ok, savory_ok = spec
        if is_sweet and not sweet_ok:
            continue
        if not is_sweet and meal_slot in {"lunch", "dinner"} and not savory_ok:
            continue
        if max_kcal > 0 and kcal > max_kcal:
            continue
        score = float(p_g) * 3.0 - (35.0 if key in used_boosters else 0.0)
        if meal_slot in {"lunch", "dinner"} and key in {"cutlets", "hiballs", "tofu", "seitan", "lentils", "poultry", "fish"}:
            score += 12.0
        candidates.append(
            (
                score,
                {
                    "key": key,
                    "label": _short_pantry_label(p_name, fallback_lbl),
                    "pantry_name": p_name,
                    "pantry_unit": str(item.get("unit") or "шт"),
                    "kcal": int(kcal),
                    "protein_g": float(p_g),
                    "fat_g": float(f_g),
                    "carbs_g": float(c_g),
                },
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda pair: -pair[0])
    return candidates[0][1]


def _scaled_slot_ingredients(
    recipe: Dict[str, Any],
    portion_mult: float,
    booster: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Сформировать ингредиенты слота с учётом коэффициента порции и белкового дополнения."""
    mult = max(0.5, float(portion_mult or 1.0))
    scaled: List[Dict[str, Any]] = []
    for ing in recipe.get("ingredients") or []:
        if not isinstance(ing, dict) or not ing.get("name"):
            continue
        item = dict(ing)
        unit = str(item.get("unit") or "шт")
        if abs(mult - 1.0) > 1e-6:
            item["qty"] = scale_qty(float(item.get("qty") or 1.0), mult, unit)
        scaled.append(item)
    if booster and booster.get("pantry_name"):
        scaled.append(
            {
                "name": str(booster["pantry_name"]),
                "qty": 1.0,
                "unit": "порц.",
            }
        )
    return scaled


def _enrich_slot_for_targets(
    recipe: Dict[str, Any],
    meal_slot: str,
    *,
    base_nutrition: Dict[str, Any],
    pantry: List[Dict[str, Any]],
    targets: Optional[Dict[str, Any]],
    meal_profile: str,
    used_boosters: Set[str],
) -> Dict[str, Any]:
    base_title = str(recipe.get("title") or "Блюдо").strip()
    bk = float(base_nutrition.get("kcal") or 0)
    bp = float(base_nutrition.get("protein_g") or 0)
    bf = float(base_nutrition.get("fat_g") or 0)
    bc = float(base_nutrition.get("carbs_g") or 0)

    if not targets or targets.get("mindful_mode") or bk <= 0:
        return {
            "title": base_title,
            "portion_mult": 1.0,
            "booster": None,
            "ingredients": _scaled_slot_ingredients(recipe, 1.0, None),
            "est_kcal": int(round(bk)),
            "est_protein_g": round(bp, 1),
            "est_fat_g": round(bf, 1),
            "est_carbs_g": round(bc, 1),
        }

    share = _SLOT_MACRO_SHARES.get(meal_slot, 0.35)
    slot_kcal = float(targets.get("kcal") or 2000) * share
    slot_p = float(targets.get("protein_g") or 100) * share

    mult = 1.0
    if bk < slot_kcal * 0.68:
        cand_mult = 2.0 if bk < slot_kcal * 0.45 else 1.5
        if bk * cand_mult <= slot_kcal * 1.15:
            mult = cand_mult
        elif bk * 1.5 <= slot_kcal * 1.15:
            mult = 1.5

    cur_k = bk * mult
    cur_p = bp * mult
    cur_f = bf * mult
    cur_c = bc * mult

    booster: Optional[Dict[str, Any]] = None
    need_protein = (slot_p - cur_p) >= 9.0
    need_kcal = cur_k < slot_kcal * 0.75
    if need_protein or need_kcal:
        headroom = max(160.0, slot_kcal * 1.25 - cur_k)
        booster = _pick_pantry_protein_booster(
            pantry,
            meal_slot=meal_slot,
            recipe=recipe,
            meal_profile=meal_profile,
            used_boosters=used_boosters,
            max_kcal=headroom,
        )
        if booster:
            used_boosters.add(booster["key"])
            cur_k += booster["kcal"]
            cur_p += booster["protein_g"]
            cur_f += booster["fat_g"]
            cur_c += booster["carbs_g"]

    mult_tag = " (×2)" if mult >= 1.9 else (" (×1.5)" if mult >= 1.4 else "")
    boost_tag = f" + {booster['label']}" if booster else ""
    return {
        "title": f"{base_title}{mult_tag}{boost_tag}",
        "portion_mult": mult,
        "booster": booster,
        "ingredients": _scaled_slot_ingredients(recipe, mult, booster),
        "est_kcal": int(round(cur_k)),
        "est_protein_g": round(cur_p, 1),
        "est_fat_g": round(cur_f, 1),
        "est_carbs_g": round(cur_c, 1),
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
    missing_count_by_id: Optional[Dict[str, int]] = None,
    expiring_hits_by_id: Optional[Dict[str, int]] = None,
    nutrition_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
    slot_target_kcal: float = 0.0,
    slot_target_protein: float = 0.0,
    slot_target_fat: float = 0.0,
    goal: str = "loss",
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
            miss_cnt = int((missing_count_by_id or {}).get(rid, 0))
        else:
            m_res = match_recipe_to_pantry(recipe, pantry, servings=2)
            cov = float(m_res["coverage"])
            miss_cnt = len(m_res["missing"])
        exp_hits = int((expiring_hits_by_id or {}).get(rid, 0))

        score = 0.0
        if meal_slot in tags:
            score += 40.0
        elif fallback_tag in tags:
            score += 18.0
        else:
            score -= 20.0

        # В первый день максимально опираемся на то, что уже лежит в холодильнике
        score += cov * (46.0 if day_idx == 0 else 22.0)
        if day_idx == 0:
            if miss_cnt == 0:
                score += 16.0
            elif miss_cnt >= 2:
                score -= 14.0 * float(miss_cnt - 1)
            score += 10.0 * min(3, exp_hits)
        else:
            if miss_cnt >= 3:
                score -= 6.0 * float(miss_cnt - 2)
            score += 5.0 * min(2, exp_hits)

        # Баланс КБЖУ и сытности под реальные цели пользователя
        if nutrition_by_id and rid in nutrition_by_id and slot_target_kcal > 0:
            nutr = nutrition_by_id[rid]
            rk = float(nutr.get("kcal") or 0)
            rp = float(nutr.get("protein_g") or 0)
            rf = float(nutr.get("fat_g") or 0)
            if rp >= 22.0:
                score += 26.0
            elif rp >= 14.0:
                score += 15.0
            elif rp >= 9.0:
                score += 4.0
            elif meal_slot in {"lunch", "dinner"}:
                score -= 28.0
            elif meal_slot == "breakfast" and rk < 350:
                score -= 14.0

            if 0.45 * slot_target_kcal <= rk <= 1.25 * slot_target_kcal:
                score += 10.0
            elif rk < 220:
                score -= 12.0

            if goal in {"loss", "recomp"} and slot_target_fat > 0 and rf > slot_target_fat * 1.4 and rp < 12.0:
                score -= 12.0

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


async def _attach_plan_nutrition(
    db,
    user: Dict[str, Any],
    rows: List[Dict[str, Any]],
    profile: str,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Прикрепить КБЖУ к слотам сохранённого меню и собрать дневную сводку."""
    if is_student_mode(user):
        return [dict(r) for r in rows], None, {}

    user_id = int(user["id"])
    latest_w = await repo.get_latest_weight(db, user_id)
    targets = fitness_nutrition.calculate_daily_targets(user, latest_w)
    if targets.get("mindful_mode"):
        return [dict(r) for r in rows], targets, {}

    recipes = await load_recipes_for_user(db, user_id)
    by_id = {str(r.get("id") or ""): r for r in recipes}

    enriched_rows: List[Dict[str, Any]] = []
    daily_totals: Dict[str, Dict[str, float]] = {}

    for raw_row in rows:
        row = dict(raw_row)
        title_str = str(row.get("title") or "").strip()
        if "est_kcal" not in row:
            booster_spec = None
            base_part = title_str
            if " + " in title_str:
                head_t, tail_t = title_str.rsplit(" + ", 1)
                matched_spec = _match_booster_spec(tail_t, meal_profile=profile)
                if matched_spec is not None:
                    booster_spec = matched_spec
                    base_part = head_t.strip()

            mult = 1.0
            if base_part.endswith(" (×2)"):
                mult = 2.0
            elif base_part.endswith(" (×1.5)"):
                mult = 1.5

            rid = str(row.get("recipe_id") or "")
            rec = by_id.get(rid)
            if rec is None:
                rec = {"title": base_part, "ingredients": list(row.get("ingredients") or [])}
            base_n = fitness_nutrition.estimate_recipe_nutrition(rec)

            k_val = float(base_n.get("kcal") or 0) * mult
            p_val = float(base_n.get("protein_g") or 0) * mult
            f_val = float(base_n.get("fat_g") or 0) * mult
            c_val = float(base_n.get("carbs_g") or 0) * mult
            if booster_spec is not None:
                k_val += float(booster_spec[3])
                p_val += float(booster_spec[4])
                f_val += float(booster_spec[5])
                c_val += float(booster_spec[6])

            row["est_kcal"] = int(round(k_val))
            row["est_protein_g"] = round(p_val, 1)
            row["est_fat_g"] = round(f_val, 1)
            row["est_carbs_g"] = round(c_val, 1)
            row["portion_mult"] = mult

        pdate = str(row.get("plan_date") or "")
        bucket = daily_totals.setdefault(pdate, {"kcal": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0})
        bucket["kcal"] += float(row.get("est_kcal") or 0)
        bucket["protein_g"] += float(row.get("est_protein_g") or 0)
        bucket["fat_g"] += float(row.get("est_fat_g") or 0)
        bucket["carbs_g"] += float(row.get("est_carbs_g") or 0)
        enriched_rows.append(row)

    daily_summary: Dict[str, Dict[str, Any]] = {
        d_str: {
            "kcal": int(round(vals["kcal"])),
            "protein_g": int(round(vals["protein_g"])),
            "fat_g": int(round(vals["fat_g"])),
            "carbs_g": int(round(vals["carbs_g"])),
            "target_kcal": int(targets["kcal"]),
            "target_protein_g": int(targets["protein_g"]),
        }
        for d_str, vals in daily_totals.items()
    }
    return enriched_rows, targets, daily_summary


def _expiring_pantry_id_set(
    pantry: List[Dict[str, Any]],
    base_date: datetime.date,
    *,
    window_days: int = 3,
) -> Set[int]:
    ids: Set[int] = set()
    for p in pantry:
        exp_raw = str(p.get("expires_at") or "").strip()
        if not exp_raw:
            continue
        try:
            exp_dt = datetime.date.fromisoformat(exp_raw[:10])
        except ValueError:
            continue
        if (exp_dt - base_date).days <= window_days:
            ids.add(int(p.get("id") or 0))
    return ids


async def generate_meal_plan(
    db,
    user: Dict[str, Any],
    *,
    days: int = 3,
    start_date: Optional[str] = None,
    exclude_ids: Optional[Iterable[str]] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Сгенерировать и сохранить не повторяющееся меню на 3-4 дня с учётом диеты, холодильника и КБЖУ."""
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

    student = is_student_mode(user)
    targets: Optional[Dict[str, Any]] = None
    if not student:
        latest_w = await repo.get_latest_weight(db, user_id)
        targets = fitness_nutrition.calculate_daily_targets(user, latest_w)
    use_kbju = bool(targets and not targets.get("mindful_mode"))
    goal = str(user.get("weight_goal") or "loss").strip().lower()

    recent_since = (base_date - datetime.timedelta(days=4)).isoformat()
    recent_ids = set(await repo.get_recent_meal_recipe_ids(db, user_id, recent_since))
    excl_set = {str(x) for x in (exclude_ids or []) if x}

    await repo.clear_meal_plans_range(db, user_id, base_date_str, end_date_str)

    used_ids: Set[str] = set()
    planned_ingredients: Set[str] = set()
    saved_rows: List[Dict[str, Any]] = []
    virtual_pantry: List[Dict[str, Any]] = [dict(p) for p in pantry]
    exp_ids = _expiring_pantry_id_set(virtual_pantry, base_date)

    def _compute_pantry_maps() -> Tuple[Dict[str, float], Dict[str, int], Dict[str, int]]:
        cov_m: Dict[str, float] = {}
        miss_m: Dict[str, int] = {}
        exp_m: Dict[str, int] = {}
        for r in candidates:
            rid_k = str(r.get("id") or "")
            m_res = match_recipe_to_pantry(r, virtual_pantry, servings=2)
            cov_m[rid_k] = float(m_res["coverage"])
            miss_m[rid_k] = len(m_res["missing"])
            exp_m[rid_k] = sum(1 for h in m_res["have"] if int(h.get("pantry_id") or 0) in exp_ids)
        return cov_m, miss_m, exp_m

    coverage_by_id, missing_count_by_id, expiring_hits_by_id = _compute_pantry_maps()
    nutrition_by_id: Dict[str, Dict[str, Any]] = {
        str(r.get("id") or ""): fitness_nutrition.estimate_recipe_nutrition(r)
        for r in candidates
    }

    for d_idx in range(eff_days):
        plan_date = (base_date + datetime.timedelta(days=d_idx)).isoformat()
        day_used_boosters: Set[str] = set()
        for meal_slot in ("breakfast", "lunch", "dinner"):
            share = _SLOT_MACRO_SHARES.get(meal_slot, 0.35)
            slot_kcal = float(targets["kcal"]) * share if use_kbju else 0.0
            slot_p = float(targets["protein_g"]) * share if use_kbju else 0.0
            slot_f = float(targets["fat_g"]) * share if use_kbju else 0.0

            picked = _pick_recipe_for_plan_slot(
                candidates,
                meal_slot=meal_slot,
                day_idx=d_idx,
                plan_date=plan_date,
                pantry=virtual_pantry,
                used_ids=used_ids,
                exclude_ids=excl_set,
                recent_ids=recent_ids,
                planned_ingredients=planned_ingredients,
                coverage_by_id=coverage_by_id,
                missing_count_by_id=missing_count_by_id,
                expiring_hits_by_id=expiring_hits_by_id,
                nutrition_by_id=nutrition_by_id if use_kbju else None,
                slot_target_kcal=slot_kcal,
                slot_target_protein=slot_p,
                slot_target_fat=slot_f,
                goal=goal,
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

            enriched = _enrich_slot_for_targets(
                picked,
                meal_slot,
                base_nutrition=nutrition_by_id.get(rid) or fitness_nutrition.estimate_recipe_nutrition(picked),
                pantry=virtual_pantry,
                targets=targets if use_kbju else None,
                meal_profile=profile,
                used_boosters=day_used_boosters,
            )
            slot_ings = list(enriched.get("ingredients") or picked.get("ingredients") or [])
            _consume_virtual_pool(virtual_pantry, slot_ings)
            coverage_by_id, missing_count_by_id, expiring_hits_by_id = _compute_pantry_maps()
            row = await repo.upsert_meal_plan_slot(
                db,
                user_id,
                plan_date,
                meal_slot,
                title=enriched["title"],
                recipe_id=rid,
                time_minutes=int(picked.get("time_minutes") or 20),
                ingredients=slot_ings,
                status="planned",
            )
            if row:
                row_dict = dict(row)
                if use_kbju:
                    row_dict["est_kcal"] = enriched["est_kcal"]
                    row_dict["est_protein_g"] = enriched["est_protein_g"]
                    row_dict["est_fat_g"] = enriched["est_fat_g"]
                    row_dict["est_carbs_g"] = enriched["est_carbs_g"]
                    row_dict["portion_mult"] = enriched["portion_mult"]
                saved_rows.append(row_dict)

    enriched_rows, final_targets, daily_summary = await _attach_plan_nutrition(
        db, user, saved_rows, profile
    )
    missing = await compute_meal_plan_missing_shopping(db, user, enriched_rows)
    export_data = build_shopping_export(missing)
    return {
        "start_date": base_date_str,
        "end_date": end_date_str,
        "days": eff_days,
        "meal_profile": profile,
        "nutrition_targets": final_targets,
        "daily_summary": daily_summary,
        "items": enriched_rows,
        "missing_shopping": missing,
        "missing_shopping_grouped": export_data["groups"],
        "shopping_export_text": export_data["plain_text"],
    }


def _consume_virtual_pool(
    pool: List[Dict[str, Any]],
    ingredients: List[Dict[str, Any]],
    *,
    name_key: str = "name",
    qty_key: str = "amount",
) -> None:
    """Списать ингредиенты из виртуального остатка (для расчёта меню на несколько дней)."""
    for ing in ingredients:
        if not isinstance(ing, dict) or not ing.get("name"):
            continue
        ing_name = str(ing["name"])
        if is_staple_ingredient(ing_name):
            continue
        need_qty = float(ing.get("qty") or 1.0)
        need_unit = str(ing.get("unit") or "шт")
        need_base, kind_n = to_base(need_qty, need_unit)
        for p in pool:
            cur_amt = float(p.get(qty_key) or 0)
            if cur_amt <= 1e-6 or not ingredients_match(ing_name, str(p.get(name_key) or "")):
                continue
            p_unit = str(p.get("unit") or "шт")
            have_base, kind_h = to_base(cur_amt, p_unit)
            if kind_n == kind_h and kind_n != "other":
                take_base = min(have_base, need_base)
                p[qty_key] = max(0.0, round(from_base(have_base - take_base, p_unit), 3))
                need_base -= take_base
                if need_base <= 1e-6:
                    break
            elif kind_h == "count":
                ck = _canonical_key(ing_name)
                step = 0.15 if ck in {"oil_veg", "soy_sauce", "sweet_chili", "sweet_sour", "teriyaki", "ketchup"} else 0.35
                p[qty_key] = max(0.0, round(cur_amt - step, 2))
                break
            else:
                p[qty_key] = max(0.0, round(cur_amt - 0.25, 2))
                break


def _subtract_from_pool_and_get_shortfall(
    pool: List[Dict[str, Any]],
    name: str,
    needed_qty: float,
    needed_unit: str,
    *,
    name_key: str,
    qty_key: str,
) -> float:
    """Вычесть доступное количество из виртуального пула и вернуть оставшуюся нехватку."""
    rem = float(needed_qty or 0)
    if rem <= 1e-6:
        return 0.0
    need_base, kind_n = to_base(rem, needed_unit)

    for entry in pool:
        cur_amt = float(entry.get(qty_key) or 0)
        if cur_amt <= 1e-6 or not ingredients_match(name, str(entry.get(name_key) or "")):
            continue
        e_unit = str(entry.get("unit") or "шт")
        have_base, kind_h = to_base(cur_amt, e_unit)
        if kind_n == kind_h and kind_n != "other":
            take_base = min(have_base, need_base)
            entry[qty_key] = max(0.0, round(from_base(have_base - take_base, e_unit), 3))
            need_base -= take_base
            rem = max(0.0, from_base(need_base, needed_unit))
            if need_base <= 1e-6:
                return 0.0
        elif kind_n == "other" and (needed_unit or "").strip().lower() == e_unit.strip().lower():
            take = min(cur_amt, rem)
            entry[qty_key] = max(0.0, round(cur_amt - take, 3))
            rem = max(0.0, rem - take)
            if rem <= 1e-6:
                return 0.0
        else:
            # Бутылка/пачка в наличии покрывает ложки/порции этого же продукта
            return 0.0
    return rem


async def compute_meal_plan_missing_shopping(
    db,
    user: Dict[str, Any],
    plan_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Собрать недостающие продукты по плану питания за вычетом холодильника и активных покупок."""
    user_id = int(user["id"])
    pantry_pool = [dict(p) for p in rows_to_dicts(await repo.list_pantry_items(db, user_id))]
    shop_pool = [
        dict(r) for r in rows_to_dicts(await repo.list_shopping_items(db, user_id, scope="household"))
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
        rem = float(item["qty"] or 1.0)
        unit = str(item["unit"] or "шт")
        rem = _subtract_from_pool_and_get_shortfall(
            pantry_pool, name, rem, unit, name_key="name", qty_key="amount"
        )
        if rem <= 1e-6:
            continue
        rem = _subtract_from_pool_and_get_shortfall(
            shop_pool, name, rem, unit, name_key="item_name", qty_key="quantity"
        )
        if rem <= 1e-6:
            continue
        missing.append({"name": name, "qty": scale_qty(rem, 1.0, unit), "unit": unit})
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
    enriched_rows, final_targets, daily_summary = await _attach_plan_nutrition(
        db, user, [dict(r) for r in rows], profile
    )
    pantry = rows_to_dicts(await repo.list_pantry_items(db, user_id))
    recipes_all = await load_recipes_for_user(db, user_id)
    recipes_by_id = {str(r.get("id") or ""): r for r in recipes_all}

    distinct_dates = {str(r.get("plan_date") or "") for r in enriched_rows if r.get("plan_date")}
    is_incomplete = len(distinct_dates) < eff_days
    today_uncooked = [
        r for r in enriched_rows
        if str(r.get("plan_date") or "") == base_date_str and r.get("status") != "cooked"
    ]
    all_today_old = False
    if today_uncooked:
        old_flags: List[bool] = []
        for r in today_uncooked:
            c_raw = str(r.get("created_at") or "").strip().replace("Z", "")
            try:
                c_dt = datetime.datetime.fromisoformat(c_raw)
                old_flags.append((now - c_dt) > datetime.timedelta(hours=48))
            except Exception:
                old_flags.append(False)
        all_today_old = bool(old_flags) and all(old_flags)

    for r in enriched_rows:
        if r.get("status") != "cooked":
            rec = recipes_by_id.get(str(r.get("recipe_id") or ""))
            if rec:
                r["missing_count"] = len(match_recipe_to_pantry(rec, pantry, servings=1)["missing"])
                r["prep_warning"] = detect_recipe_prep_warning(rec)
            else:
                r["missing_count"] = 0
                r["prep_warning"] = None

    missing = await compute_meal_plan_missing_shopping(db, user, enriched_rows)
    export_data = build_shopping_export(missing)
    return {
        "start_date": base_date_str,
        "end_date": end_date_str,
        "days": eff_days,
        "meal_profile": profile,
        "nutrition_targets": final_targets,
        "daily_summary": daily_summary,
        "items": enriched_rows,
        "is_stale": bool(is_incomplete or all_today_old),
        "missing_shopping": missing,
        "missing_shopping_grouped": export_data["groups"],
        "shopping_export_text": export_data["plain_text"],
    }


_ADAPT_SUBSTITUTION_FAMILIES: Tuple[Tuple[str, Tuple[str, ...], bool, str], ...] = (
    ("herbs", ("петруш", "укроп", "кинз", "базилик", "руккол", "шпинат", "зелен", "салат"), False, "зелень в конце можно пропустить"),
    ("nuts_seeds", ("миндал", "кешью", "грецк", "фундук", "кедров", "семечк", "кунжут", "чиа", "орех"), True, "посыпка для хруста — можно спокойно пропустить"),
    ("tomatoes", ("вялен", "пассат", "рублен", "томат", "черри", "помидор"), True, "акцент для вкуса — можно пропустить"),
    ("plant_milk_cream", ("овсян", "кокосов", "соев", "миндальн", "растительн", "немолок", "nemoloko", "сливк", "молок"), False, ""),
    ("legumes", ("фасол", "нут", "чечевиц", "эдамаме", "горошек"), False, ""),
    ("grains_pasta", ("бурый рис", "киноа", "булгур", "кускус", "рис", "гречк", "спагетти", "макарон", "лапш"), False, ""),
    ("bread_wrap", ("пита", "тортиль", "лаваш", "чиабатт", "хлеб", "батон"), False, ""),
    ("minor_garnish", ("лимон", "лайм", "имбир", "дрожж", "сироп", "мед", "мёд", "каперс", "оливк"), True, "необязательная добавка — можно пропустить"),
)

_MEAT_FISH_WORDS = (
    "куриц", "индейк", "говяд", "свин", "рыб", "лосос", "тунец",
    "креветк", "ветчин", "бекон", "колбас", "фарш",
)
_DAIRY_EGG_WORDS = (
    "яйц", "творог", "сыр", "сметан", "сливочн", "сливк", "сгущен",
    "молок", "кефир", "ряженк", "йогурт", "teos",
)
_PLANT_ALT_MARKERS = (
    "овсян", "кокос", "соев", "миндал", "растител", "веган", "немолок", "nemoloko", "тофу",
)


def _pantry_item_allowed_for_adapt(p_name: str, meal_profile: str) -> bool:
    low = str(p_name or "").casefold().replace("ё", "е")
    if meal_profile in {"vegan", "vegetarian"}:
        if any(w in low for w in _MEAT_FISH_WORDS) and not any(m in low for m in ("растител", "веган", "соев", "тофу")):
            return False
        if meal_profile == "vegan":
            if any(w in low for w in _DAIRY_EGG_WORDS) and not any(m in low for m in _PLANT_ALT_MARKERS):
                return False
    return True


def _match_sub_family(name: str) -> Optional[Tuple[str, bool, str]]:
    low = str(name or "").casefold().replace("ё", "е")
    if not low:
        return None
    # Не путать арахисовую пасту / томатную пасту с орехами или лапшой
    if "паст" in low and ("арахис" in low or "орех" in low):
        return None
    for fam, kws, skippable, skip_reason in _ADAPT_SUBSTITUTION_FAMILIES:
        if fam == "plant_milk_cream":
            has_dairy_like = "сливк" in low or "молок" in low or "немолок" in low or "nemoloko" in low
            if not has_dairy_like or "масл" in low or "сыр" in low or "йогурт" in low or "хлоп" in low or "каш" in low:
                continue
            return fam, skippable, skip_reason
        if fam == "tomatoes":
            if not any(k in low for k in ("томат", "помидор", "черри", "пассат")):
                continue
            return fam, ("вялен" in low), skip_reason
        if fam == "nuts_seeds":
            if "молоко" in low or "паст" in low:
                continue
        if fam == "grains_pasta" and "паст" in low and ("арахис" in low or "томат" in low):
            continue
        if any(kw in low for kw in kws):
            return fam, skippable, skip_reason
    return None


def _is_small_garnish_qty(qty: float, unit: str) -> bool:
    u = str(unit or "").strip().lower()
    if any(tok in u for tok in ("щеп", "по вкусу", "кап")):
        return True
    base, kind = to_base(float(qty or 1.0), u)
    if kind in {"weight", "volume"}:
        return base <= 30.0
    if any(tok in u for tok in ("ч.л", "ст.л", "зуб")):
        return float(qty or 1.0) <= 2.0
    return False


def _offline_substitute_missing(
    missing_items: List[Dict[str, Any]],
    pantry: List[Dict[str, Any]],
    *,
    have_pantry_ids: Set[int],
    meal_profile: str,
    base_date: datetime.date,
) -> List[Dict[str, Any]]:
    """Детерминированный подбор замен из реального холодильника с приоритетом истекающих продуктов."""
    exp_ids = _expiring_pantry_id_set(pantry, base_date)

    def _pantry_sort_key(p: Dict[str, Any]) -> Tuple[int, int, str, int]:
        pid = int(p.get("id") or 0)
        not_used = 0 if pid not in have_pantry_ids else 1
        not_exp = 0 if pid in exp_ids else 1
        exp = str(p.get("expires_at") or "9999-12-31")
        return (not_exp, not_used, exp, pid)

    sorted_pantry = sorted(
        [p for p in pantry if float(p.get("amount") or 0) > 1e-6 and _pantry_item_allowed_for_adapt(str(p.get("name") or ""), meal_profile)],
        key=_pantry_sort_key,
    )

    subs: List[Dict[str, Any]] = []
    for miss in missing_items:
        m_name = str(miss.get("name") or "").strip()
        m_qty = float(miss.get("qty") or 1.0)
        m_unit = str(miss.get("unit") or "")
        fam_info = _match_sub_family(m_name)
        if not fam_info:
            subs.append({"missing_name": m_name, "action": "keep", "replacement": "", "reason": "нет прямой замены"})
            continue
        fam, skippable, skip_reason = fam_info

        cand_pantry = None
        for p in sorted_pantry:
            pid = int(p.get("id") or 0)
            p_name = str(p.get("name") or "")
            if pid in have_pantry_ids:
                continue
            p_fam = _match_sub_family(p_name)
            if p_fam and p_fam[0] == fam:
                cand_pantry = p
                break

        if cand_pantry is not None and not ("вялен" in m_name.casefold() and _is_small_garnish_qty(m_qty, m_unit)):
            p_name = str(cand_pantry["name"])
            is_exp = int(cand_pantry.get("id") or 0) in exp_ids
            reason = "истекает скоро — в дело!" if is_exp else "есть в холодильнике"
            subs.append({
                "missing_name": m_name,
                "action": "replace",
                "replacement": p_name,
                "pantry_id": int(cand_pantry["id"]),
                "reason": reason,
            })
        elif skippable and _is_small_garnish_qty(m_qty, m_unit):
            subs.append({
                "missing_name": m_name,
                "action": "skip",
                "replacement": "",
                "reason": skip_reason or "второстепенная добавка — можно пропустить",
            })
        elif cand_pantry is not None:
            subs.append({
                "missing_name": m_name,
                "action": "replace",
                "replacement": str(cand_pantry["name"]),
                "pantry_id": int(cand_pantry["id"]),
                "reason": "есть в холодильнике",
            })
        else:
            subs.append({"missing_name": m_name, "action": "keep", "replacement": "", "reason": "нечем заменить дома"})
    return subs


_ADAPT_SYSTEM_PROMPT = (
    "Ты — шеф-повар HiDL. Пользователь хочет приготовить рецепт, но 1-3 ингредиентов нет дома.\n"
    "Подбери замену или пропуск СТРОГО из доступных продуктов в списке «В холодильнике» (никаких выдуманных продуктов!).\n"
    "Ответь СТРОГО валидным JSON:\n"
    "{\"substitutions\": [{\"missing_name\": str, \"action\": \"replace\"|\"skip\"|\"keep\", "
    "\"replacement\": str, \"reason\": str}], \"note\": str}.\n"
    "Правила:\n"
    "1. Если action='replace', replacement ОБЯЗАН дословно совпадать с названием одного из продуктов в списке холодильника.\n"
    "2. Отдавай приоритет продуктам с пометкой [истекает].\n"
    "3. Посыпки/мелкие акценты (орехи 10-20 г, вяленые томаты, лимон/зелень для подачи) можно помечать action='skip' или заменять похожим.\n"
    "4. Строго соблюдай профиль питания пользователя (vegan -> без молочки, яиц и мяса)."
)


async def adapt_recipe_to_pantry(
    db,
    user: Dict[str, Any],
    recipe_id: str,
    *,
    servings: int = 2,
    llm_client_instance: Optional[LLMClient] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Подобрать замены или пропуск для 1-3 недостающих продуктов рецепта строго по реальному холодильнику."""
    user_id = int(user["id"])
    recipe = await get_recipe_for_user(db, user_id, recipe_id)
    if not recipe:
        return {"ok": False, "error": "not_found", "message": "Рецепт не найден."}

    now = now_utc or datetime.datetime.utcnow()
    tz = str(user.get("timezone") or "UTC")
    base_date = datetime.date.fromisoformat(local_date_str(now, tz))
    pantry = rows_to_dicts(await repo.list_pantry_items(db, user_id))
    profile = await get_user_meal_profile(db, user_id)

    match = match_recipe_to_pantry(recipe, pantry, servings=servings)
    missing = list(match["missing"])
    have = list(match["have"])
    if not missing:
        return {"ok": True, "already_complete": True, "orig_recipe": recipe, "adapted_recipe": recipe, "substitutions": []}

    if not pantry or (match["coverage"] < 0.50 and len(missing) > 3):
        return {
            "ok": False,
            "error": "too_many_missing",
            "missing": missing,
            "coverage": match["coverage"],
            "orig_recipe": recipe,
        }

    have_ids = {int(h["pantry_id"]) for h in have if h.get("pantry_id") is not None}
    offline_subs = _offline_substitute_missing(
        missing, pantry, have_pantry_ids=have_ids, meal_profile=profile, base_date=base_date
    )
    subs_by_missing: Dict[str, Dict[str, Any]] = {
        str(s["missing_name"]).casefold(): s for s in offline_subs
    }

    client = llm_client_instance or default_llm_client
    used_llm = False
    note = ""
    if client.is_configured() and len(missing) <= 4:
        exp_ids = _expiring_pantry_id_set(pantry, base_date)
        pantry_lines = []
        for p in pantry[:45]:
            if float(p.get("amount") or 0) <= 1e-6:
                continue
            if not _pantry_item_allowed_for_adapt(str(p.get("name") or ""), profile):
                continue
            tag = " [истекает]" if int(p.get("id") or 0) in exp_ids else ""
            pantry_lines.append(f"- {p['name']} ({format_quantity(p.get('amount'), p.get('unit'))}){tag}")
        miss_lines = [f"- {m['name']} ({m['qty']:g} {m['unit']})".strip() for m in missing]
        if pantry_lines:
            prompt = (
                f"Рецепт: {recipe.get('title')}\n"
                f"Профиль питания: {profile}\n\n"
                f"Не хватает:\n" + "\n".join(miss_lines) + "\n\n"
                f"В холодильнике:\n" + "\n".join(pantry_lines)
            )
            llm_res = await client.complete_json(_ADAPT_SYSTEM_PROMPT, prompt, temperature=0.1, max_tokens=500)
            if llm_res.ok and isinstance(llm_res.data, dict):
                raw_subs = llm_res.data.get("substitutions") or []
                note = str(llm_res.data.get("note") or "").strip()
                if isinstance(raw_subs, list):
                    used_llm = True
                    for raw_s in raw_subs:
                        if not isinstance(raw_s, dict):
                            continue
                        m_name = str(raw_s.get("missing_name") or "").strip()
                        act = str(raw_s.get("action") or "").strip().lower()
                        rep = str(raw_s.get("replacement") or "").strip()
                        reason = str(raw_s.get("reason") or "").strip()
                        key = m_name.casefold()
                        if key not in subs_by_missing:
                            # Поиск по частичному совпадению имени
                            key = next((k for k in subs_by_missing if k in key or key in k), "")
                        if not key:
                            continue
                        if act == "replace":
                            matched_p = next(
                                (
                                    p for p in pantry
                                    if float(p.get("amount") or 0) > 1e-6
                                    and _pantry_item_allowed_for_adapt(str(p.get("name") or ""), profile)
                                    and (
                                        str(p.get("name") or "").casefold() == rep.casefold()
                                        or ingredients_match(rep, str(p.get("name") or ""))
                                    )
                                ),
                                None,
                            )
                            if matched_p is not None:
                                subs_by_missing[key] = {
                                    "missing_name": subs_by_missing[key]["missing_name"],
                                    "action": "replace",
                                    "replacement": str(matched_p["name"]),
                                    "pantry_id": int(matched_p["id"]),
                                    "reason": reason or "есть в холодильнике",
                                }
                        elif act == "skip":
                            subs_by_missing[key] = {
                                "missing_name": subs_by_missing[key]["missing_name"],
                                "action": "skip",
                                "replacement": "",
                                "reason": reason or "можно пропустить",
                            }

    ordered_subs = [subs_by_missing[str(m["name"]).casefold()] for m in missing]
    # Пересобрать адаптированный список ингредиентов
    new_ingredients: List[Dict[str, Any]] = []
    for ing in recipe.get("ingredients") or []:
        if not isinstance(ing, dict):
            continue
        iname = str(ing.get("name") or "").strip()
        sub = subs_by_missing.get(iname.casefold())
        if sub is None:
            new_ingredients.append(dict(ing))
        elif sub["action"] == "replace" and sub.get("replacement"):
            upd = dict(ing)
            upd["name"] = sub["replacement"]
            new_ingredients.append(upd)
        elif sub["action"] == "skip":
            continue
        else:
            new_ingredients.append(dict(ing))

    orig_nutrition = fitness_nutrition.estimate_recipe_nutrition(recipe)
    adapted_recipe = dict(recipe)
    adapted_recipe["adapted_from_id"] = str(recipe.get("id") or recipe_id)
    adapted_recipe["title"] = f"{recipe.get('title', 'Рецепт')} (под холодильник)"
    adapted_recipe["ingredients"] = new_ingredients
    adapted_recipe["nutrition"] = None
    adapted_nutrition = fitness_nutrition.estimate_recipe_nutrition(adapted_recipe)
    adapted_recipe["nutrition"] = adapted_nutrition

    after_match = match_recipe_to_pantry(adapted_recipe, pantry, servings=servings)
    return {
        "ok": True,
        "already_complete": False,
        "orig_recipe": recipe,
        "adapted_recipe": adapted_recipe,
        "substitutions": ordered_subs,
        "orig_nutrition": orig_nutrition,
        "adapted_nutrition": adapted_nutrition,
        "remaining_missing": after_match["missing"],
        "used_llm": used_llm,
        "note": note,
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
        pid = await repo.upsert_pantry_item_merged(
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
        pid = await repo.upsert_pantry_item_merged(
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
