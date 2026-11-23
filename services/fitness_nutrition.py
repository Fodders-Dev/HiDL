"""Расчёт КБЖУ, умный подбор блюд по остатку на день, умные весы и анти-лень фитнес HiDL."""

from __future__ import annotations

import base64
import datetime
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from db import repositories as repo
from llm_client import LLMClient, client as default_llm_client
from services import karma_service
from utils.time import local_date_str

EQUIPMENT_CATALOG: Tuple[Tuple[str, str, str], ...] = (
    ("smart_scales", "⚖", "Умные весы (% жира/мышц)"),
    ("dumbbells", "🏋️", "Гантели / гири"),
    ("pullup_bar", "🤸", "Турник / брусья"),
    ("bands", "〰️", "Резинки / эспандеры"),
    ("mat", "🧘", "Коврик"),
    ("cardio", "🏃", "Дорожка / вело / эллипс"),
    ("bodyweight", "💪", "Свой вес (без инвентаря)"),
)

_EQUIPMENT_MAP: Dict[str, Tuple[str, str]] = {
    key: (emoji, label) for key, emoji, label in EQUIPMENT_CATALOG
}

EQUIPMENT_SHORT_LABELS: Dict[str, str] = {
    "smart_scales": "Умные весы",
    "dumbbells": "Гантели",
    "pullup_bar": "Турник",
    "bands": "Резинки",
    "mat": "Коврик",
    "cardio": "Кардио",
    "bodyweight": "Свой вес",
}

GOAL_LABELS: Dict[str, str] = {
    "loss": "🔥 Сбросить вес и подтянуться",
    "recomp": "💪 Подтянуться (рекомпозиция)",
    "keep": "⚖ Поддерживать форму",
    "gain": "📈 Набрать массу",
}

ACTIVITY_MULTIPLIERS: Dict[str, Tuple[str, float]] = {
    "sedentary": ("🪑 Малоподвижный (дом/офис)", 1.2),
    "light": ("🚶 Лёгкая активность (прогулки)", 1.375),
    "moderate": ("🏃 Средняя (2–4 трен./нед)", 1.55),
    "active": ("⚡ Высокая (почти каждый день)", 1.725),
}

FOCUS_LABELS: Dict[str, str] = {
    "full_body": "Всё тело",
    "upper": "Верх и спина",
    "lower": "Ноги и кор",
    "core": "Пресс и осанка",
    "mobility": "Спина и подвижность",
    "cardio": "Лёгкое кардио",
}

# Питательная ценность на 100 г / 100 мл: (kcal, protein_g, fat_g, carbs_g)
_NUTRITION_DB: Dict[str, Tuple[float, float, float, float]] = {
    # Нулевые / специи
    "вода": (0, 0, 0, 0),
    "кипяток": (0, 0, 0, 0),
    "соль": (0, 0, 0, 0),
    "перец черный": (0, 0, 0, 0),
    "чёрный перец": (0, 0, 0, 0),
    "специи": (0, 0, 0, 0),
    "паприка": (0, 0, 0, 0),
    "куркума": (0, 0, 0, 0),
    "корица": (0, 0, 0, 0),
    "карри": (0, 0, 0, 0),
    "уксус": (15, 0, 0, 0),
    "ванилин": (0, 0, 0, 0),
    "сода": (0, 0, 0, 0),
    "разрыхлитель": (0, 0, 0, 0),
    # Жиры и соусы
    "масло растительное": (884, 0, 99.9, 0),
    "масло оливковое": (884, 0, 99.9, 0),
    "подсолнечное масло": (884, 0, 99.9, 0),
    "кунжутное масло": (884, 0, 99.9, 0),
    "масло кокосовое": (862, 0, 99.0, 0),
    "масло сливочное": (717, 0.8, 81.0, 0.1),
    "арахисовая паста": (588, 25.0, 50.0, 20.0),
    "тахини": (595, 17.0, 54.0, 21.0),
    "майонез": (620, 1.5, 67.0, 3.0),
    "соевый соус": (53, 8.0, 0.1, 4.9),
    "томатная паста": (82, 4.3, 0.5, 18.0),
    "пассата": (32, 1.5, 0.2, 6.0),
    "песто": (420, 4.5, 42.0, 4.0),
    "хумус": (220, 7.9, 14.0, 14.3),
    "кокосовое молоко": (185, 2.0, 18.0, 3.5),
    "овсяные сливки": (140, 1.0, 13.0, 5.0),
    "растительные сливки": (145, 1.0, 13.0, 6.0),
    "овсяное молоко": (48, 1.0, 1.5, 7.5),
    "соевое молоко": (45, 3.3, 1.8, 3.5),
    "миндальное молоко": (28, 0.6, 1.5, 3.0),
    "растительное молоко": (45, 1.5, 1.5, 6.0),
    "молоко": (54, 3.0, 2.5, 4.8),
    "сметана": (190, 2.6, 18.0, 3.4),
    "йогурт": (66, 5.0, 2.0, 6.0),
    # Белок
    "тофу": (120, 13.0, 7.0, 2.0),
    "темпе": (193, 20.0, 11.0, 8.0),
    "сейтан": (145, 25.0, 2.0, 6.0),
    "соевое мясо": (310, 50.0, 1.5, 22.0),
    "чечевица": (310, 24.0, 1.2, 52.0),
    "нут": (335, 19.5, 6.0, 52.0),
    "фасоль стручковая": (31, 1.8, 0.2, 5.0),
    "стручковая фасоль": (31, 1.8, 0.2, 5.0),
    "фасоль": (127, 8.7, 0.5, 22.0),
    "горох": (305, 21.0, 1.5, 52.0),
    "эдамаме": (121, 11.9, 5.2, 8.9),
    "куриное филе": (115, 23.6, 1.9, 0.4),
    "курица": (165, 20.0, 9.0, 0.0),
    "индейка": (125, 24.0, 2.5, 0.2),
    "говядина": (187, 19.0, 12.0, 0.0),
    "рыба": (135, 19.0, 6.0, 0.0),
    "лосось": (208, 20.0, 13.4, 0.0),
    "тунец": (130, 28.0, 1.0, 0.0),
    "яйцо": (155, 12.6, 11.0, 1.1),
    "творог": (121, 17.0, 5.0, 2.0),
    "сыр": (350, 24.0, 27.0, 1.5),
    "протеин": (380, 78.0, 4.0, 7.0),
    # Крупы и мучное
    "гречка": (330, 12.6, 3.3, 62.0),
    "рис": (344, 7.0, 1.0, 76.0),
    "овсянка": (352, 12.3, 6.1, 61.0),
    "овсяные хлопья": (352, 12.3, 6.1, 61.0),
    "киноа": (368, 14.1, 6.1, 64.2),
    "булгур": (342, 12.3, 1.3, 63.0),
    "пшено": (348, 11.5, 3.3, 66.5),
    "перловка": (320, 9.3, 1.1, 67.0),
    "кускус": (356, 12.8, 0.6, 72.0),
    "макароны": (348, 11.0, 1.5, 71.0),
    "спагетти": (348, 11.0, 1.5, 71.0),
    "паста": (348, 11.0, 1.5, 71.0),
    "фунчоза": (320, 0.5, 0.2, 79.0),
    "лапша": (345, 10.5, 1.5, 70.0),
    "мука": (342, 10.5, 1.3, 70.0),
    "лаваш": (260, 8.0, 1.2, 53.0),
    "тортилья": (285, 7.5, 6.0, 49.0),
    "хлеб": (245, 8.2, 1.5, 48.0),
    "пита": (266, 9.1, 1.2, 55.0),
    # Овощи и грибы
    "картофель": (77, 2.0, 0.4, 16.5),
    "батат": (86, 1.6, 0.1, 20.1),
    "помидор": (20, 0.9, 0.2, 3.9),
    "томат": (20, 0.9, 0.2, 3.9),
    "огурец": (15, 0.7, 0.1, 2.8),
    "болгарский перец": (27, 1.0, 0.2, 5.5),
    "перец сладкий": (27, 1.0, 0.2, 5.5),
    "лук": (40, 1.4, 0.2, 8.2),
    "чеснок": (149, 6.4, 0.5, 30.0),
    "морковь": (35, 1.3, 0.1, 7.2),
    "капуста": (28, 1.8, 0.1, 4.7),
    "брокколи": (34, 2.8, 0.4, 6.0),
    "цветная капуста": (25, 2.0, 0.3, 4.5),
    "цукини": (20, 1.2, 0.3, 3.5),
    "кабачок": (20, 1.2, 0.3, 3.5),
    "баклажан": (24, 1.2, 0.1, 4.5),
    "шпинат": (23, 2.9, 0.4, 2.0),
    "зелень": (35, 2.5, 0.4, 5.0),
    "укроп": (38, 2.5, 0.5, 6.0),
    "петрушка": (45, 3.7, 0.4, 7.6),
    "кинза": (23, 2.1, 0.5, 3.7),
    "шампиньоны": (24, 3.1, 0.3, 3.3),
    "грибы": (28, 3.0, 0.5, 3.5),
    "свекла": (43, 1.6, 0.1, 9.6),
    "тыква": (26, 1.0, 0.1, 6.0),
    "авокадо": (160, 2.0, 14.7, 4.5),
    "овощная смесь": (42, 2.2, 0.3, 7.5),
    "кукуруза": (96, 3.3, 1.2, 18.0),
    "горошек": (73, 5.0, 0.2, 12.5),
    # Фрукты, ягоды, орехи, сладкое
    "банан": (89, 1.1, 0.3, 21.8),
    "яблоко": (52, 0.4, 0.2, 12.0),
    "груша": (57, 0.4, 0.2, 13.5),
    "ягоды": (45, 0.9, 0.4, 9.0),
    "клубника": (33, 0.7, 0.3, 7.5),
    "черника": (57, 0.7, 0.3, 13.0),
    "лимон": (29, 0.9, 0.1, 4.5),
    "апельсин": (43, 0.9, 0.2, 9.0),
    "финики": (277, 2.5, 0.5, 69.0),
    "изюм": (264, 2.9, 0.6, 66.0),
    "грецкий орех": (654, 15.2, 65.2, 7.0),
    "миндаль": (579, 21.2, 49.9, 13.0),
    "кешью": (553, 18.2, 43.9, 27.0),
    "арахис": (567, 25.8, 49.2, 10.0),
    "орехи": (590, 18.0, 52.0, 14.0),
    "семечки": (584, 20.8, 51.5, 11.4),
    "тыквенные семечки": (559, 30.2, 49.0, 10.7),
    "семена чиа": (486, 16.5, 30.7, 14.0),
    "кунжут": (573, 17.7, 49.7, 12.0),
    "сироп": (260, 0.1, 0.0, 65.0),
    "мед": (304, 0.3, 0.0, 81.0),
    "сахар": (398, 0.0, 0.0, 99.8),
    "какао": (289, 24.3, 15.0, 27.9),
    "шоколад": (546, 5.5, 31.0, 58.0),
}

_PIECE_WEIGHTS_G: Dict[str, float] = {
    "яйцо": 55.0,
    "банан": 120.0,
    "яблоко": 160.0,
    "груша": 160.0,
    "авокадо": 150.0,
    "помидор": 120.0,
    "томат": 120.0,
    "огурец": 120.0,
    "картофель": 130.0,
    "батат": 180.0,
    "лук": 100.0,
    "морковь": 100.0,
    "болгарский перец": 140.0,
    "цукини": 220.0,
    "кабачок": 220.0,
    "баклажан": 220.0,
    "лимон": 80.0,
    "апельсин": 160.0,
    "лаваш": 100.0,
    "тортилья": 60.0,
    "хлеб": 35.0,
    "финики": 8.0,
    "чеснок": 5.0,
}

_SORTED_NUTRITION_KEYS = sorted(_NUTRITION_DB.keys(), key=len, reverse=True)


def format_equipment_labels(items: Iterable[str]) -> str:
    """Человекочитаемая строка домашнего инвентаря."""
    labels: List[str] = []
    for raw in items:
        key = str(raw or "").strip()
        if not key:
            continue
        if key in _EQUIPMENT_MAP:
            emoji, label = _EQUIPMENT_MAP[key]
            labels.append(f"{emoji} {label}")
        else:
            labels.append(f"🔧 {key}")
    return ", ".join(labels) if labels else "💪 Свой вес (по умолчанию)"


def calculate_daily_targets(
    user: Dict[str, Any],
    latest_weight: Optional[Dict[str, Any]] = None,
    *,
    workout_kcal_today: int = 0,
) -> Dict[str, Any]:
    """Посчитать личную суточную норму калорий и БЖУ (Mifflin-St Jeor + цель + тренировки)."""
    w_raw = (latest_weight or {}).get("weight")
    try:
        weight = float(w_raw) if w_raw and float(w_raw) > 0 else 0.0
    except (TypeError, ValueError):
        weight = 0.0

    is_estimated = False
    if weight <= 0:
        wt_raw = user.get("weight_target")
        try:
            weight = float(wt_raw) if wt_raw and float(wt_raw) > 0 else 75.0
        except (TypeError, ValueError):
            weight = 75.0
        is_estimated = True

    try:
        h_val = float(user.get("height_cm") or 0)
    except (TypeError, ValueError):
        h_val = 0.0
    if not (100.0 <= h_val <= 250.0):
        h_val = 172.0
        is_estimated = True

    try:
        age_val = int(user.get("age_years") or 0)
    except (TypeError, ValueError):
        age_val = 0
    if not (14 <= age_val <= 100):
        age_val = 28
        is_estimated = True

    sex_raw = str(user.get("sex") or user.get("gender") or "").strip().lower()
    if sex_raw in {"male", "m", "муж", "он"}:
        sex_offset = 5.0
        sex_key = "male"
    elif sex_raw in {"female", "f", "жен", "она"}:
        sex_offset = -161.0
        sex_key = "female"
    else:
        sex_offset = -78.0
        sex_key = "neutral"

    act_raw = str(user.get("activity_level") or "light").strip().lower()
    act_label, mult = ACTIVITY_MULTIPLIERS.get(act_raw, ACTIVITY_MULTIPLIERS["light"])

    goal_raw = str(user.get("weight_goal") or "loss").strip().lower()
    if goal_raw not in GOAL_LABELS:
        goal_raw = "loss"

    bmr = 10.0 * weight + 6.25 * h_val - 5.0 * age_val + sex_offset
    tdee = bmr * mult

    if goal_raw == "loss":
        kcal = max(1350.0, tdee * 0.85)
        protein_g = weight * 1.6
    elif goal_raw == "recomp":
        kcal = max(1450.0, tdee * 0.93)
        protein_g = weight * 1.7
    elif goal_raw == "gain":
        kcal = tdee * 1.10
        protein_g = weight * 1.7
    else:
        kcal = tdee
        protein_g = weight * 1.3

    try:
        custom_kcal = int(user.get("custom_kcal_target") or 0)
    except (TypeError, ValueError):
        custom_kcal = 0
    if custom_kcal >= 1000:
        kcal = float(custom_kcal)

    workout_bonus_kcal = int(round(max(0, int(workout_kcal_today or 0)) * 0.5))
    workout_bonus_protein = 10.0 if workout_bonus_kcal > 0 else 0.0
    kcal += workout_bonus_kcal
    protein_g += workout_bonus_protein

    fat_g = max(40.0, weight * 0.85)
    carbs_g = max(80.0, (kcal - protein_g * 4.0 - fat_g * 9.0) / 4.0)

    return {
        "kcal": int(round(kcal)),
        "protein_g": int(round(protein_g)),
        "fat_g": int(round(fat_g)),
        "carbs_g": int(round(carbs_g)),
        "tdee": int(round(tdee)),
        "goal": goal_raw,
        "goal_label": GOAL_LABELS[goal_raw],
        "activity_level": act_raw if act_raw in ACTIVITY_MULTIPLIERS else "light",
        "activity_label": act_label,
        "sex": sex_key,
        "age_years": age_val,
        "height_cm": int(round(h_val)),
        "weight_kg": round(weight, 1),
        "is_estimated": is_estimated,
        "workout_bonus_kcal": workout_bonus_kcal,
    }


def _match_nutrition_key(name: str) -> Optional[str]:
    low = str(name or "").casefold().replace("ё", "е").strip()
    if not low:
        return None
    for key in _SORTED_NUTRITION_KEYS:
        if key.replace("ё", "е") in low:
            return key
    return None


def _ingredient_grams(name: str, qty: float, unit: str) -> float:
    q = max(0.0, float(qty or 0.0))
    u = str(unit or "").strip().lower()
    low = str(name or "").casefold().replace("ё", "е")

    if any(tok in u for tok in ("щеп", "по вкусу")):
        return 1.0
    if u in {"г", "гр", "g", "мл", "ml"}:
        return q
    if u in {"кг", "kg", "л", "l"}:
        return q * 1000.0
    if any(tok in u for tok in ("ч.л", "tsp", "чайн")):
        return q * 5.0
    if any(tok in u for tok in ("ст.л", "tbsp", "стол")):
        return q * 15.0
    if any(tok in u for tok in ("стакан", "cup", "круж")):
        return q * 200.0
    if any(tok in u for tok in ("зуб",)):
        return q * 5.0
    if any(tok in u for tok in ("пуч",)):
        return q * 30.0
    if u in {"шт", "piece", "pieces", "порц", ""}:
        for key, grams in _PIECE_WEIGHTS_G.items():
            if key in low:
                return q * grams
        return q * 80.0
    return q * 60.0


def _normalize_nutrition_dict(raw: Any, *, estimated: bool = False) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    try:
        kcal = float(raw.get("kcal") or 0)
        p = float(raw.get("protein_g") if raw.get("protein_g") is not None else raw.get("protein") or 0)
        f = float(raw.get("fat_g") if raw.get("fat_g") is not None else raw.get("fat") or 0)
        c = float(raw.get("carbs_g") if raw.get("carbs_g") is not None else raw.get("carbs") or 0)
    except (TypeError, ValueError):
        return None
    if kcal <= 0:
        return None
    return {
        "kcal": int(round(kcal)),
        "protein_g": round(max(0.0, p), 1),
        "fat_g": round(max(0.0, f), 1),
        "carbs_g": round(max(0.0, c), 1),
        "estimated": bool(raw.get("estimated", estimated)),
    }


def estimate_recipe_nutrition(recipe: Dict[str, Any]) -> Dict[str, Any]:
    """Оценить КБЖУ одной порции рецепта (из сохранённого поля или по ингредиентам)."""
    existing = _normalize_nutrition_dict(recipe.get("nutrition"), estimated=False)
    if existing is not None:
        return existing

    servings = max(1, int(recipe.get("base_servings") or 1))
    total_kcal = 0.0
    total_p = 0.0
    total_f = 0.0
    total_c = 0.0
    non_staple_count = 0

    for ing in recipe.get("ingredients") or []:
        if not isinstance(ing, dict):
            continue
        iname = str(ing.get("name") or "").strip()
        if not iname:
            continue
        grams = _ingredient_grams(iname, float(ing.get("qty") or 1.0), str(ing.get("unit") or ""))
        key = _match_nutrition_key(iname)
        if key is not None:
            k100, p100, f100, c100 = _NUTRITION_DB[key]
        else:
            k100, p100, f100, c100 = (110.0, 4.0, 3.0, 16.0)
        if k100 > 0:
            non_staple_count += 1
        factor = grams / 100.0
        total_kcal += k100 * factor
        total_p += p100 * factor
        total_f += f100 * factor
        total_c += c100 * factor

    if total_kcal <= 10 and non_staple_count > 0:
        total_kcal = 280.0 * servings
        total_p = 12.0 * servings
        total_f = 9.0 * servings
        total_c = 36.0 * servings

    return {
        "kcal": int(round(total_kcal / servings)),
        "protein_g": round(total_p / servings, 1),
        "fat_g": round(total_f / servings, 1),
        "carbs_g": round(total_c / servings, 1),
        "estimated": True,
    }


async def get_daily_nutrition_advice(
    db,
    user: Dict[str, Any],
    *,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Рассчитать текущий баланс КБЖУ на день и подсказку: что добрать, а что ограничить."""
    now = now_utc or datetime.datetime.utcnow()
    tz = str(user.get("timezone") or "UTC")
    local_date = local_date_str(now, tz)
    user_id = int(user["id"])

    latest_weight = await repo.get_latest_weight(db, user_id)
    week_workouts = await repo.workout_stats_week(db, user_id, local_date)
    wellness = await repo.get_wellness(db, user_id) or {}
    meal_profile = str(wellness.get("meal_profile") or "").strip().lower()
    targets = calculate_daily_targets(
        user,
        latest_weight,
        workout_kcal_today=int(week_workouts.get("today_kcal_burned") or 0),
    )
    eaten = await repo.daily_nutrition_totals(db, user_id, local_date)
    meals = await repo.list_meal_logs(db, user_id, local_date)

    remaining = {
        "kcal": max(0, int(round(targets["kcal"] - eaten["kcal"]))),
        "protein_g": max(0, int(round(targets["protein_g"] - eaten["protein_g"]))),
        "fat_g": max(0, int(round(targets["fat_g"] - eaten["fat_g"]))),
        "carbs_g": max(0, int(round(targets["carbs_g"] - eaten["carbs_g"]))),
    }
    over = {
        "kcal": max(0, int(round(eaten["kcal"] - targets["kcal"]))),
        "protein_g": max(0, int(round(eaten["protein_g"] - targets["protein_g"]))),
        "fat_g": max(0, int(round(eaten["fat_g"] - targets["fat_g"]))),
        "carbs_g": max(0, int(round(eaten["carbs_g"] - targets["carbs_g"]))),
    }

    eat_more: List[str] = []
    go_easy_on: List[str] = []

    if remaining["protein_g"] >= 25:
        if meal_profile == "vegan":
            eat_more.append("растительный белок (тофу, чечевица, нут, фасоль, эдамаме или протеин)")
        elif meal_profile == "vegetarian":
            eat_more.append("белок (яйца, творог, греческий йогурт, сыр, тофу или чечевицу)")
        else:
            eat_more.append("белок (курица, рыба, яйца, творог, греческий йогурт или тофу)")
    if remaining["kcal"] >= 350 and remaining["carbs_g"] >= 35:
        eat_more.append("сложные углеводы и объём (гречка, киноа, овощи, зелень)")
    if remaining["kcal"] > 0 and not eat_more:
        eat_more.append("лёгкое сбалансированное блюдо с овощами и зеленью")

    if over["kcal"] > 0 or remaining["kcal"] <= 250:
        go_easy_on.append("калорийные перекусы, десерты и плотные соусы")
    if over["fat_g"] > 0 or remaining["fat_g"] <= 15:
        go_easy_on.append("лишнее масло, орехи, авокадо и жареное (жиры почти выбраны)")
    if over["carbs_g"] > 0 or remaining["carbs_g"] <= 30:
        go_easy_on.append("сладкое, выпечка и быстрые углеводы")

    summary_line = (
        f"Осталось ~{remaining['kcal']} ккал · "
        f"Б +{remaining['protein_g']} г · Ж ≤{remaining['fat_g']} г · У +{remaining['carbs_g']} г"
    )
    chef_parts = [
        f"Цель пользователя: {targets['goal_label']}.",
        (
            f"На день осталось: {remaining['kcal']} из {targets['kcal']} ккал, "
            f"нужно добрать белка {remaining['protein_g']} г, лимит жиров ~{remaining['fat_g']} г, "
            f"углеводов ~{remaining['carbs_g']} г."
        ),
    ]
    if eat_more:
        chef_parts.append(f"Сделать акцент на: {'; '.join(eat_more)}.")
    if go_easy_on:
        chef_parts.append(f"Воздержаться / минимизировать: {'; '.join(go_easy_on)}.")

    return {
        "local_date": local_date,
        "targets": targets,
        "eaten": eaten,
        "remaining": remaining,
        "over": over,
        "eat_more": eat_more,
        "go_easy_on": go_easy_on,
        "summary_line": summary_line,
        "chef_hint": " ".join(chef_parts),
        "meals": meals,
    }


def score_recipe_for_macros(
    recipe_nutrition: Dict[str, Any], advice: Dict[str, Any]
) -> float:
    """Оценка соответствия блюда текущему остатку КБЖУ (прибавка к рейтингу подбора)."""
    remaining = advice.get("remaining") or {}
    over = advice.get("over") or {}
    eaten = advice.get("eaten") or {}
    rem_kcal = float(remaining.get("kcal") or 0)
    rem_p = float(remaining.get("protein_g") or 0)
    rem_f = float(remaining.get("fat_g") or 0)

    r_kcal = float(recipe_nutrition.get("kcal") or 0)
    r_p = float(recipe_nutrition.get("protein_g") or 0)
    r_f = float(recipe_nutrition.get("fat_g") or 0)

    delta = 0.0
    if rem_kcal > 0 and r_kcal > 0:
        if r_kcal <= rem_kcal + 60:
            delta += 5.0
        elif r_kcal > rem_kcal + 180 and int(eaten.get("meals_count") or 0) > 0:
            delta -= 7.0
    elif int(over.get("kcal") or 0) > 0 and r_kcal > 450:
        delta -= 8.0

    if rem_p >= 20 and r_p >= 14:
        delta += min(10.0, r_p / 2.5)
    if (rem_f <= 15 or int(over.get("fat_g") or 0) > 0) and r_f >= 22:
        delta -= 5.0

    return round(max(-12.0, min(16.0, delta)), 2)


_MEAL_PARSE_PROMPT = (
    "Ты — нутриционист HiDL. По описанию или фото еды оцени блюдо и его КБЖУ за указанную порцию. "
    "Ответь строго JSON-объектом вида: "
    "{\"title\": str, \"meal_slot\": \"breakfast\"|\"lunch\"|\"dinner\"|\"snack\", "
    "\"kcal\": float, \"protein_g\": float, \"fat_g\": float, \"carbs_g\": float}. "
    "Если на фото несколько продуктов, суммируй в одну общую запись с понятным названием."
)


def _parse_meal_offline(raw_text: str) -> Optional[Dict[str, Any]]:
    text = re.sub(r"\s+", " ", str(raw_text or "").strip())
    if not text:
        return None
    low = text.casefold().replace("ё", "е")

    # Явно указанные калории: "овсянка 350 ккал б12 ж8 у55"
    kcal_m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ккал|kcal|калл)\b", low)
    if kcal_m:
        kcal = float(kcal_m.group(1).replace(",", "."))
        p_m = re.search(r"(?:б|белк[а-я]*|p)\s*[:=]?\s*(\d+(?:[.,]\d+)?)", low)
        f_m = re.search(r"(?:ж|жир[а-я]*|f)\s*[:=]?\s*(\d+(?:[.,]\d+)?)", low)
        c_m = re.search(r"(?:у|углев[а-я]*|c)\s*[:=]?\s*(\d+(?:[.,]\d+)?)", low)
        p = float(p_m.group(1).replace(",", ".")) if p_m else round(kcal * 0.20 / 4.0, 1)
        f = float(f_m.group(1).replace(",", ".")) if f_m else round(kcal * 0.28 / 9.0, 1)
        c = float(c_m.group(1).replace(",", ".")) if c_m else round(kcal * 0.52 / 4.0, 1)
        clean_title = re.sub(r"\d+.*$", "", text).strip(" -:,.") or text[:50]
        return {
            "title": clean_title,
            "meal_slot": "snack",
            "kcal": int(round(kcal)),
            "protein_g": round(p, 1),
            "fat_g": round(f, 1),
            "carbs_g": round(c, 1),
        }

    parts = [p.strip() for p in re.split(r"[,;\n]+|\s+\+\s+|\s+и\s+", text) if p.strip()]
    total_k, total_p, total_f, total_c = 0.0, 0.0, 0.0, 0.0
    matched_any = False
    for part in parts:
        key = _match_nutrition_key(part)
        qty_m = re.search(r"(\d+(?:[.,]\d+)?)\s*(кг|kg|г|гр|g|мл|ml|л|l|шт)?", part.lower())
        qty = float(qty_m.group(1).replace(",", ".")) if qty_m and qty_m.group(1) else 1.0
        unit = (qty_m.group(2) or "шт") if qty_m else "шт"
        if qty_m and qty >= 15 and not qty_m.group(2):
            unit = "г"
        grams = _ingredient_grams(part, qty, unit)
        if key is not None:
            k100, p100, f100, c100 = _NUTRITION_DB[key]
            matched_any = True
        else:
            k100, p100, f100, c100 = (150.0, 5.0, 5.0, 20.0)
            grams = max(grams, 100.0)
        factor = grams / 100.0
        total_k += k100 * factor
        total_p += p100 * factor
        total_f += f100 * factor
        total_c += c100 * factor

    if not matched_any and total_k <= 0:
        return None
    return {
        "title": text[:70],
        "meal_slot": "snack",
        "kcal": int(round(max(10.0, total_k))),
        "protein_g": round(total_p, 1),
        "fat_g": round(total_f, 1),
        "carbs_g": round(total_c, 1),
    }


async def parse_meal_input(
    *,
    raw_text: str = "",
    image_bytes: Optional[bytes] = None,
    image_mime: str = "image/jpeg",
    llm_client_instance: Optional[LLMClient] = None,
) -> Dict[str, Any]:
    """Распознать приём пищи (КБЖУ) с фото тарелки/этикетки или из текста."""
    client = llm_client_instance or default_llm_client
    clean_text = str(raw_text or "").strip()

    if image_bytes:
        if not client.is_configured():
            return {"ok": False, "error": "llm_not_configured"}
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{image_mime};base64,{b64}"
        user_msg = "Оцени это блюдо и его КБЖУ." + (f" Комментарий: {clean_text}" if clean_text else "")
        res = await client.complete_json(
            _MEAL_PARSE_PROMPT, user_msg, temperature=0.1, max_tokens=500, image_data_urls=[data_url]
        )
        if not res.ok or not isinstance(res.data, dict):
            return {"ok": False, "error": res.error or "vision_parse_failed"}
        norm = _normalize_nutrition_dict(res.data, estimated=True)
        if not norm:
            return {"ok": False, "error": "no_meal_found"}
        slot = str(res.data.get("meal_slot") or "snack").strip().lower()
        return {
            "ok": True,
            "used_llm": True,
            "title": str(res.data.get("title") or clean_text or "Блюдо по фото").strip(),
            "meal_slot": slot if slot in {"breakfast", "lunch", "dinner", "snack"} else "snack",
            **norm,
        }

    if not clean_text:
        return {"ok": False, "error": "empty_input"}

    if client.is_configured():
        res = await client.complete_json(
            _MEAL_PARSE_PROMPT, f"Оцени КБЖУ съеденного:\n{clean_text}", temperature=0.1, max_tokens=500
        )
        if res.ok and isinstance(res.data, dict):
            norm = _normalize_nutrition_dict(res.data, estimated=True)
            if norm:
                slot = str(res.data.get("meal_slot") or "snack").strip().lower()
                return {
                    "ok": True,
                    "used_llm": True,
                    "title": str(res.data.get("title") or clean_text[:60]).strip(),
                    "meal_slot": slot if slot in {"breakfast", "lunch", "dinner", "snack"} else "snack",
                    **norm,
                }

    offline = _parse_meal_offline(clean_text)
    if not offline:
        return {"ok": False, "error": "no_meal_found"}
    return {"ok": True, "used_llm": False, **offline}


_SCALE_PARSE_PROMPT = (
    "Ты — парсер умных весов HiDL (Mi Fit, Zepp, Picooc, Huawei Health, Apple Health и др.). "
    "Извлеки замеры с фото или из текста и верни строго JSON: "
    "{\"weight\": float|null, \"body_fat_pct\": float|null, \"muscle_mass_kg\": float|null, "
    "\"water_pct\": float|null, \"visceral_fat\": float|null, \"bmi\": float|null}."
)


def _validate_scale_payload(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    def _num(key: str, lo: float, hi: float) -> Optional[float]:
        val = raw.get(key)
        if val is None or val == "":
            return None
        try:
            f = float(str(val).replace(",", "."))
        except (TypeError, ValueError):
            return None
        if lo <= f <= hi:
            return round(f, 2)
        return None

    weight = _num("weight", 25.0, 300.0)
    if weight is None:
        return None
    fat = _num("body_fat_pct", 3.0, 70.0)
    muscle = _num("muscle_mass_kg", 10.0, 120.0)
    water = _num("water_pct", 25.0, 80.0)
    visc = _num("visceral_fat", 1.0, 50.0)
    bmi = _num("bmi", 10.0, 70.0)
    return {
        "weight": round(weight, 1),
        "body_fat_pct": fat,
        "muscle_mass_kg": muscle,
        "water_pct": water,
        "visceral_fat": visc,
        "bmi": bmi,
    }


def _parse_scale_offline(raw_text: str) -> Optional[Dict[str, Any]]:
    text = str(raw_text or "").strip().replace(",", ".")
    if not text:
        return None
    low = text.casefold().replace("ё", "е")

    def _find(pattern: str) -> Optional[float]:
        m = re.search(pattern, low)
        if not m:
            return None
        try:
            return float(m.group(1))
        except Exception:
            return None

    w = _find(r"(?:вес|weight)\s*[:=]?\s*(\d+(?:\.\d+)?)")
    if w is None:
        m0 = re.match(r"^\s*(\d{2,3}(?:\.\d+)?)\b", low)
        if m0:
            w = float(m0.group(1))

    payload = {
        "weight": w,
        "body_fat_pct": _find(r"(?:жир[а-я]*|fat)\s*[:=]?\s*(\d+(?:\.\d+)?)"),
        "muscle_mass_kg": _find(r"(?:мышц[а-я]*|muscle)\s*[:=]?\s*(\d+(?:\.\d+)?)"),
        "water_pct": _find(r"(?:вод[а-я]*|water)\s*[:=]?\s*(\d+(?:\.\d+)?)"),
        "visceral_fat": _find(r"(?:висц[а-я]*|visceral)\s*[:=]?\s*(\d+(?:\.\d+)?)"),
        "bmi": _find(r"(?:имт|bmi)\s*[:=]?\s*(\d+(?:\.\d+)?)"),
    }
    return _validate_scale_payload(payload)


async def parse_smart_scale_input(
    *,
    raw_text: str = "",
    image_bytes: Optional[bytes] = None,
    image_mime: str = "image/jpeg",
    llm_client_instance: Optional[LLMClient] = None,
) -> Dict[str, Any]:
    """Распознать данные умных весов (вес, % жира, мышцы, вода) по скрину или тексту."""
    client = llm_client_instance or default_llm_client
    clean_text = str(raw_text or "").strip()

    if image_bytes:
        if not client.is_configured():
            return {"ok": False, "error": "llm_not_configured"}
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{image_mime};base64,{b64}"
        res = await client.complete_json(
            _SCALE_PARSE_PROMPT,
            "Извлеки все показатели с экрана умных весов.",
            temperature=0.0,
            max_tokens=400,
            image_data_urls=[data_url],
        )
        if not res.ok or not isinstance(res.data, dict):
            return {"ok": False, "error": res.error or "vision_parse_failed"}
        valid = _validate_scale_payload(res.data)
        if not valid:
            return {"ok": False, "error": "no_scale_metrics"}
        return {"ok": True, "used_llm": True, "source": "scale_photo", **valid}

    if not clean_text:
        return {"ok": False, "error": "empty_input"}

    offline = _parse_scale_offline(clean_text)
    if offline:
        has_extra = any(offline.get(k) is not None for k in ("body_fat_pct", "muscle_mass_kg", "water_pct"))
        return {"ok": True, "used_llm": False, "source": "scale_text" if has_extra else "manual", **offline}

    if client.is_configured():
        res = await client.complete_json(
            _SCALE_PARSE_PROMPT,
            f"Извлеки замеры весов из текста:\n{clean_text}",
            temperature=0.0,
            max_tokens=400,
        )
        if res.ok and isinstance(res.data, dict):
            valid = _validate_scale_payload(res.data)
            if valid:
                return {"ok": True, "used_llm": True, "source": "scale_text", **valid}

    return {"ok": False, "error": "no_scale_metrics"}


def format_body_trend_feedback(trend: Dict[str, Any], goal: str = "loss") -> str:
    """Поддерживающий комментарий по тренду веса и состава тела без стыда."""
    w_d = trend.get("weight_delta")
    f_d = trend.get("fat_delta")
    m_d = trend.get("muscle_delta")
    count = int(trend.get("count") or 0)
    if count < 2 or w_d is None:
        return "Первые замеры задают точку отсчёта. Сравнивать будем спокойный тренд за 2–4 недели, а не разовые цифры."

    if f_d is not None and f_d <= -0.3 and w_d >= -0.4:
        muscle_note = f", а мышцы +{m_d:.1f} кг" if m_d is not None and m_d >= 0.2 else ""
        return (
            f"🔥 Отличная рекомпозиция: вес почти ровный ({w_d:+.1f} кг), "
            f"но жир уже ушёл на {f_d:.1f}%{muscle_note}! Именно так тело подтягивается."
        )
    if goal in {"loss", "recomp"} and w_d <= -0.4:
        return f"📉 За {trend.get('days', 30)} дн. вес плавно снижается ({w_d:+.1f} кг) — здоровый темп без срывов."
    if goal == "loss" and 0.1 < w_d <= 1.0:
        return "💧 Небольшой плюс за короткий отрезок чаще всего даёт вода, соль или поздний ужин — смотрим на длинный тренд."
    return f"📊 Тренд за {trend.get('days', 30)} дн.: {w_d:+.1f} кг. Держи ровный ритм без жёстких рамок."


_LAZY_WORKOUTS_BY_GEAR: Dict[str, List[Dict[str, Any]]] = {
    "dumbbells": [
        {
            "title": "Микро-старт 4 мин с гантелями",
            "focus_area": "full_body",
            "duration_min": 4,
            "equipment_used": "гантели",
            "kcal_burned": 28,
            "steps": [
                "Возьми комфортные гантели и включи 1 любимый трек.",
                "Присед-кубок (гантель у груди) — 10 спокойных повторов.",
                "Тяга гантели к поясу в наклоне — по 10 каждой рукой.",
                "Жим вверх сидя или стоя — 10 раз в удобном темпе.",
            ],
        },
        {
            "title": "Разгон верха с гантелями за 4 мин",
            "focus_area": "upper",
            "duration_min": 4,
            "equipment_used": "гантели",
            "kcal_burned": 25,
            "steps": [
                "Круги плечами 20 секунд (без веса).",
                "Подъём гантелей на бицепс — 12 раз без рывков.",
                "Разведение рук с лёгким весом или полуприсед + мах — 12 раз.",
            ],
        },
    ],
    "pullup_bar": [
        {
            "title": "Турник-лайт: спина и плечи за 3 мин",
            "focus_area": "upper",
            "duration_min": 3,
            "equipment_used": "турник / брусья",
            "kcal_burned": 22,
            "steps": [
                "Спокойный пассивный вис на турнике — 2 подхода по 20–30 сек (расправляем спину).",
                "Австралийские подтягивания или тяга резинки/негативное опускание — 4–6 раз.",
                "Встряхни руки и потянись вверх — уже молодец.",
            ],
        },
    ],
    "bands": [
        {
            "title": "Резинка + осанка за 4 мин",
            "focus_area": "upper",
            "duration_min": 4,
            "equipment_used": "резинки",
            "kcal_burned": 22,
            "steps": [
                "Растягивание резинки перед грудью — 15 спокойных раз.",
                "Присед с резинкой над коленями (или без) — 12 раз.",
                "Потягивания вверх и выдох — 20 секунд.",
            ],
        },
    ],
    "mat": [
        {
            "title": "На коврике без напряжения — 4 мин",
            "focus_area": "core",
            "duration_min": 4,
            "equipment_used": "коврик",
            "kcal_burned": 20,
            "steps": [
                "Кошка-корова для спины — 8 медленных движений.",
                "Ягодичный мостик на полу — 12 раз.",
                "Планка на локтях (или с колен) — всего 25 секунд.",
            ],
        },
    ],
    "cardio": [
        {
            "title": "5 минут лёгкого шага / педалей под подкаст",
            "focus_area": "cardio",
            "duration_min": 5,
            "equipment_used": "дорожка / велотренажёр",
            "kcal_burned": 35,
            "steps": [
                "Включи любимый ролик или трек.",
                "Пройдись или покрути педали 5 минут в совсем лёгком темпе (не запыхиваясь).",
            ],
        },
    ],
    "bodyweight": [
        {
            "title": "Анти-лень 3 минуты около дивана",
            "focus_area": "full_body",
            "duration_min": 3,
            "equipment_used": "свой вес",
            "kcal_burned": 20,
            "steps": [
                "Потянись вверх и сделай 5 кругов плечами.",
                "10 спокойных приседов до комфортной глубины.",
                "8–10 отжиманий от стола или стены (даже 5 — уже победа).",
            ],
        },
    ],
}


def build_lazy_start_workout(
    equipment: List[str],
    *,
    recent_focus_areas: Optional[List[str]] = None,
    variation: int = 0,
) -> Dict[str, Any]:
    """Собрать 3–5 минутный микро-старт с нулевым психологическим барьером под инвентарь."""
    pool: List[Dict[str, Any]] = []
    for key in equipment or []:
        pool.extend(_LAZY_WORKOUTS_BY_GEAR.get(key, []))
    if not pool:
        pool.extend(_LAZY_WORKOUTS_BY_GEAR["bodyweight"])
    elif len(pool) == 1:
        pool.extend(_LAZY_WORKOUTS_BY_GEAR["bodyweight"])

    recent = set(recent_focus_areas or [])
    fresh = [w for w in pool if w["focus_area"] not in recent]
    ordered = fresh + [w for w in pool if w["focus_area"] in recent] if fresh else pool
    picked = dict(ordered[int(variation) % len(ordered)])
    picked["intensity"] = "lazy"
    picked["deal_text"] = (
        "Честный договор: сделай только эти несколько минут. "
        "Не зайдёт — сразу жмёшь «Готово» и забираешь карму; захочется ещё — добавишь 10 мин."
    )
    return picked


def _build_offline_workout(
    equipment: List[str], *, duration_min: int = 15, focus_area: str = "full_body"
) -> Dict[str, Any]:
    eq_set = set(equipment or [])
    dur = max(5, min(60, int(duration_min or 15)))
    rounds = 2 if dur <= 12 else (3 if dur <= 22 else 4)
    has_db = "dumbbells" in eq_set
    has_bar = "pullup_bar" in eq_set
    has_bands = "bands" in eq_set
    has_cardio = "cardio" in eq_set
    has_mat = "mat" in eq_set

    eq_used_parts = [
        _EQUIPMENT_MAP[k][1].split(" (")[0]
        for k in equipment
        if k in _EQUIPMENT_MAP and k != "smart_scales"
    ]
    eq_used = ", ".join(eq_used_parts[:4]) if eq_used_parts else "свой вес"

    steps = ["Разминка 2–3 мин: шея, плечи, таз, колени, мягкое дыхание."]
    if focus_area == "upper":
        steps.append(f"Сделай {rounds} круга (отдых 45–60 сек между упражнениями):")
        if has_bar:
            steps.append("Подтягивания (или австралийские / с резинкой) — 4–8 раз.")
        elif has_db:
            steps.append("Тяга гантелей в наклоне к поясу — 12 раз.")
        elif has_bands:
            steps.append("Тяга резинки к груди/поясу — 15 раз.")
        else:
            steps.append("Отжимания от стола или пола — 8–12 раз.")
        if has_db:
            steps.append("Жим гантелей вверх стоя/сидя — 10–12 раз.")
            steps.append("Отжимания (от пола или колен) — 8–12 раз.")
        else:
            steps.append("Отжимания от пола/опоры — 8–12 раз.")
        if has_mat or True:
            steps.append("Планка на локтях — 30 сек.")
    elif focus_area == "lower":
        steps.append(f"Сделай {rounds} круга без спешки:")
        steps.append(
            "Приседания с гантелями у груди — 12 раз." if has_db else "Приседания — 15 раз."
        )
        steps.append(
            "Румынская тяга с гантелями (ровная спина) — 12 раз."
            if has_db
            else "Ягодичный мостик на полу — 15 раз."
        )
        steps.append(
            "Боковые шаги или махи с резинкой — по 12 на ногу."
            if has_bands
            else "Обратные выпады (или зашагивания) — 10–12 суммарно."
        )
        steps.append("Велосипед или скручивания на коврике — 30 сек.")
    elif focus_area == "core":
        steps.append(f"Сделай {rounds} круга на кор и осанку:")
        steps.append("Мёртвый жук на коврике (медленно) — 10–12 раз.")
        steps.append("Ягодичный мостик с паузой 2 сек наверху — 12–15 раз.")
        steps.append("Боковая планка — по 20–25 сек на сторону.")
        steps.append("Лодочка на животе (спина) — 12 раз.")
    elif focus_area == "cardio" and has_cardio:
        steps = [
            "3 минуты плавного разогрева на тренажёре.",
            f"Основная часть {max(5, dur - 5)} минут: чередуй 2 мин спокойного темпа и 1 мин чуть быстрее.",
            "2 минуты заминки и стакан воды.",
        ]
    else:
        steps.append(f"Сделай {rounds} круга (отдых между кругами ~90 сек):")
        steps.append(
            "Гоблет-присед с гантелью — 12 раз." if has_db else "Приседания — 12–15 раз."
        )
        steps.append("Отжимания (от стола или пола) — 8–12 раз.")
        if has_bar:
            steps.append("Подтягивания или вис со сведением лопаток — 5–8 раз.")
        elif has_db:
            steps.append("Тяга гантелей в наклоне — 12 раз.")
        elif has_bands:
            steps.append("Тяга резинки на спину — 15 раз.")
        steps.append("Планка — 30–40 сек.")
        if has_cardio and dur >= 20:
            steps.append("В финале — 5–7 минут спокойного кардио на тренажёре.")
    steps.append("Заминка 2 мин: потяни плечи, спину и ноги, выпей воды.")

    kcal_burned = int(round(dur * (6.2 if (has_db or has_bar or has_cardio) else 5.2)))
    label = FOCUS_LABELS.get(focus_area, "Всё тело")
    return {
        "title": f"{label} · {dur} мин",
        "focus_area": focus_area,
        "duration_min": dur,
        "equipment_used": eq_used,
        "intensity": "moderate" if dur >= 15 else "easy",
        "kcal_burned": kcal_burned,
        "steps": steps,
    }


async def generate_personal_workout(
    db,
    user: Dict[str, Any],
    *,
    duration_min: int = 15,
    focus_area: str = "auto",
    llm_client_instance: Optional[LLMClient] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Собрать персональную домашнюю тренировку под инвентарь, цель и недавнюю нагрузку."""
    now = now_utc or datetime.datetime.utcnow()
    tz = str(user.get("timezone") or "UTC")
    local_date = local_date_str(now, tz)
    user_id = int(user["id"])

    equipment = repo.get_user_equipment(user) or ["bodyweight"]
    week_stats = await repo.workout_stats_week(db, user_id, local_date)
    recent_areas = list(week_stats.get("recent_focus_areas") or [])
    goal = str(user.get("weight_goal") or "loss")

    eff_focus = focus_area
    if eff_focus not in FOCUS_LABELS:
        candidates = ["full_body", "upper", "lower", "core"]
        if "cardio" in equipment:
            candidates.append("cardio")
        eff_focus = next((c for c in candidates if c not in recent_areas[:2]), "full_body")

    fallback = _build_offline_workout(equipment, duration_min=duration_min, focus_area=eff_focus)

    client = llm_client_instance or default_llm_client
    if not client.is_configured():
        return {"ok": True, "used_llm": False, **fallback}

    latest_weight = await repo.get_latest_weight(db, user_id)
    weight_txt = f"{latest_weight['weight']:.1f} кг" if latest_weight else "не указан"
    eq_human = format_equipment_labels(equipment)
    sys_prompt = (
        "Ты — бережный персональный тренер HiDL. Составь простую, безопасную домашнюю тренировку БЕЗ резкого хардкора. "
        "Ответь строго JSON-объектом вида: "
        "{\"title\": str, \"kcal_burned\": int, \"steps\": [str]}. "
        "В steps 5–7 коротких понятных шагов с разминкой, подходами и заминкой."
    )
    user_prompt = (
        f"Длительность: {duration_min} мин. Акцент: {FOCUS_LABELS.get(eff_focus, eff_focus)}.\n"
        f"Инвентарь дома: {eq_human}.\n"
        f"Цель: {GOAL_LABELS.get(goal, goal)}. Вес: {weight_txt}.\n"
        f"Недавние акценты за неделю: {', '.join(recent_areas) or 'давно не тренировался'}."
    )
    res = await client.complete_json(sys_prompt, user_prompt, temperature=0.35, max_tokens=800)
    if res.ok and isinstance(res.data, dict):
        steps = [str(s).strip() for s in (res.data.get("steps") or []) if str(s).strip()]
        if steps:
            try:
                kcal_b = max(15, min(600, int(res.data.get("kcal_burned") or fallback["kcal_burned"])))
            except (TypeError, ValueError):
                kcal_b = fallback["kcal_burned"]
            return {
                "ok": True,
                "used_llm": True,
                "title": str(res.data.get("title") or fallback["title"]).strip(),
                "focus_area": eff_focus,
                "duration_min": int(duration_min),
                "equipment_used": fallback["equipment_used"],
                "intensity": fallback["intensity"],
                "kcal_burned": kcal_b,
                "steps": steps[:9],
            }
    return {"ok": True, "used_llm": False, **fallback}


async def complete_workout(
    db,
    user: Dict[str, Any],
    workout: Dict[str, Any],
    *,
    is_lazy_start: bool = False,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Зафиксировать выполненную тренировку, начислить карму и пересчитать недельный/КБЖУ баланс."""
    now = now_utc or datetime.datetime.utcnow()
    user_id = int(user["id"])
    tz = str(user.get("timezone") or "UTC")
    local_date = local_date_str(now, tz)

    title = str(workout.get("title") or "Домашняя тренировка").strip()
    duration_min = max(1, int(workout.get("duration_min") or (3 if is_lazy_start else 15)))
    focus_area = str(workout.get("focus_area") or "full_body")
    equipment_used = str(workout.get("equipment_used") or "")
    intensity = "lazy" if is_lazy_start else str(workout.get("intensity") or "moderate")
    kcal_burned = max(10, int(workout.get("kcal_burned") or (25 if is_lazy_start else 80)))

    log_id = await repo.add_workout_log(
        db,
        user_id,
        local_date,
        title,
        focus_area=focus_area,
        duration_min=duration_min,
        equipment_used=equipment_used,
        intensity=intensity,
        kcal_burned=kcal_burned,
        is_lazy_start=is_lazy_start,
    )

    pts = 3 if is_lazy_start else 5
    await repo.add_points(db, user_id, pts, local_date=local_date)
    karma_res = await karma_service.record_karma_event(
        db,
        user,
        domain="tasks",
        event_type="workout_done",
        delta=pts,
        reason=f"Движение: {title}",
        now_utc=now,
    )
    week_stats = await repo.workout_stats_week(db, user_id, local_date)
    advice = await get_daily_nutrition_advice(db, user, now_utc=now)
    return {
        "ok": True,
        "log_id": log_id,
        "title": title,
        "duration_min": duration_min,
        "kcal_burned": kcal_burned,
        "karma_delta": int(karma_res.get("delta") or pts),
        "score_after": karma_res.get("score_after"),
        "week_stats": week_stats,
        "daily_advice": advice,
    }
