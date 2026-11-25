"""Расчёт КБЖУ, умный подбор блюд по остатку на день, умные весы и анти-лень фитнес HiDL."""

from __future__ import annotations

import base64
import datetime
import html as _html_mod
import io
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from db import repositories as repo
from llm_client import LLMClient, client as default_llm_client
from services import coach_knowledge, karma_service, nutrition_web
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
    "mindful": "🧘 Без подсчёта КБЖУ",
}

COACH_MODES: Dict[str, Tuple[str, str, str]] = {
    "gentle": (
        "🌱",
        "Мягкий анти-лень",
        "Ноль вины и давления: поддерживай за любой шаг, хвали за 3-минутный старт и мягко выравнивай следующий приём пищи.",
    ),
    "balanced": (
        "🎯",
        "Осознанный баланс",
        "Короткие цифры, честный разбор плюсов и минусов блюда и практичная спокойная альтернатива.",
    ),
    "strict": (
        "🔥",
        "Тренер / фокус",
        "Прямолинейно тормози перед лишним сахаром/жиром и держи фокус на белке и тренировках (без грубости и оскорблений).",
    ),
}


def get_coach_directive(user: Dict[str, Any], *, student: bool = False) -> str:
    """Сформировать короткую директиву цели и стиля коуча для промптов ИИ."""
    goal_raw = str(user.get("weight_goal") or "loss").strip().lower()
    if goal_raw == "maintain":
        goal_raw = "keep"
    mode_raw = str(user.get("coach_mode") or "balanced").strip().lower()
    if student or goal_raw == "mindful":
        return (
            "Цель и тон: бережная забота о самочувствии и режиме БЕЗ жёсткого контроля калорий, "
            "стыда или диетических ограничений."
        )
    emoji, label, hint = COACH_MODES.get(mode_raw, COACH_MODES["balanced"])
    goal_label = GOAL_LABELS.get(goal_raw, GOAL_LABELS["loss"])
    return f"Цель: {goal_label}. Стиль коуча: {emoji} {label} — {hint}"

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
    "кисло-сладкий соус": (140, 0.5, 0.2, 34.0),
    "сладкий чили соус": (175, 0.5, 0.3, 42.0),
    "соус терияки": (110, 3.0, 0.2, 24.0),
    "кетчуп": (105, 1.5, 0.2, 24.0),
    "горчица": (140, 6.0, 6.0, 11.0),
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
    "соевый фарш": (315, 50.0, 1.5, 22.0),
    "соевый гуляш": (315, 50.0, 1.5, 22.0),
    "веганские котлеты": (195, 14.0, 9.0, 14.0),
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
    "нутовая мука": (356, 20.0, 6.0, 55.0),
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
    "руккола": (25, 2.6, 0.7, 3.7),
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
    "оливки": (145, 1.0, 15.0, 3.5),
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
    # Готовые блюда и напитки (средние ориентиры на 100 г / 100 мл)
    "суп": (58, 2.6, 2.0, 6.8),
    "борщ": (62, 2.6, 2.4, 7.2),
    "салат": (98, 3.0, 6.5, 6.8),
    "пицца": (255, 10.5, 10.0, 30.0),
    "бургер": (260, 12.0, 13.0, 24.0),
    "шаурма": (210, 9.5, 9.5, 21.5),
    "шаверма": (210, 9.5, 9.5, 21.5),
    "торт": (360, 4.5, 18.0, 45.0),
    "мороженое": (210, 3.5, 11.0, 24.0),
    "булочка": (310, 7.5, 10.0, 48.0),
    "круассан": (406, 8.2, 21.0, 45.8),
    "блин": (220, 6.5, 8.5, 29.0),
    "сырник": (195, 11.0, 7.5, 20.0),
    "ролл": (165, 6.5, 4.5, 24.0),
    "латте": (48, 2.2, 2.0, 5.2),
    "капучино": (42, 2.0, 1.8, 4.5),
    "раф": (95, 2.0, 5.5, 9.5),
    "кофе": (15, 0.5, 0.4, 2.0),
    "смузи": (55, 1.0, 0.5, 12.0),
    "батончик": (365, 18.0, 12.0, 42.0),
    "йогурт": (72, 4.5, 2.5, 7.8),
    "кефир": (51, 3.0, 2.5, 4.0),
    "чай": (1, 0.0, 0.0, 0.2),
    "кола": (42, 0.0, 0.0, 10.6),
    "лимонад": (40, 0.0, 0.0, 10.0),
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
    "суп": 320.0,
    "борщ": 320.0,
    "салат": 220.0,
    "пицца": 250.0,
    "бургер": 220.0,
    "шаурма": 320.0,
    "шаверма": 320.0,
    "торт": 130.0,
    "мороженое": 100.0,
    "булочка": 90.0,
    "круассан": 80.0,
    "блин": 160.0,
    "сырник": 75.0,
    "ролл": 200.0,
    "латте": 300.0,
    "капучино": 250.0,
    "раф": 300.0,
    "кофе": 250.0,
    "смузи": 300.0,
    "батончик": 55.0,
    "йогурт": 150.0,
    "кефир": 250.0,
    "вода": 250.0,
    "чай": 250.0,
    "кола": 250.0,
    "лимонад": 250.0,
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
    if goal_raw == "maintain":
        goal_raw = "keep"
    if goal_raw not in GOAL_LABELS:
        goal_raw = "loss"

    bf_raw = (latest_weight or {}).get("body_fat_pct")
    lean_mass_kg: Optional[float] = None
    if weight > 0 and bf_raw is not None:
        try:
            bf_val = float(bf_raw)
            if 5.0 <= bf_val <= 60.0:
                lean_mass_kg = round(weight * (1.0 - bf_val / 100.0), 1)
        except (TypeError, ValueError):
            lean_mass_kg = None

    if lean_mass_kg is not None and lean_mass_kg > 20.0:
        bmr = 370.0 + 21.6 * lean_mass_kg
        bmr_formula = "katch_mcardle"
    else:
        lean_mass_kg = None
        bmr = 10.0 * weight + 6.25 * h_val - 5.0 * age_val + sex_offset
        bmr_formula = "mifflin_st_jeor"
    tdee = bmr * mult

    if lean_mass_kg is not None:
        if goal_raw == "loss":
            kcal = max(1350.0, tdee * 0.85)
            protein_g = lean_mass_kg * 1.9
            fat_g = max(45.0, lean_mass_kg * 0.95)
        elif goal_raw == "recomp":
            kcal = max(1450.0, tdee * 0.90)
            protein_g = lean_mass_kg * 2.1
            fat_g = max(45.0, lean_mass_kg * 0.95)
        elif goal_raw == "gain":
            kcal = tdee * 1.10
            protein_g = lean_mass_kg * 2.0
            fat_g = max(50.0, lean_mass_kg * 1.0)
        else:
            kcal = tdee
            protein_g = lean_mass_kg * 1.6
            fat_g = max(45.0, lean_mass_kg * 0.95)
    else:
        if goal_raw == "loss":
            kcal = max(1350.0, tdee * 0.85)
            protein_g = weight * 1.6
        elif goal_raw == "recomp":
            kcal = max(1450.0, tdee * 0.90)
            protein_g = weight * 1.8
        elif goal_raw == "gain":
            kcal = tdee * 1.10
            protein_g = weight * 1.7
        else:
            kcal = tdee
            protein_g = weight * 1.3
        fat_g = max(40.0, weight * 0.85)

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

    carbs_g = max(80.0, (kcal - protein_g * 4.0 - fat_g * 9.0) / 4.0)

    return {
        "kcal": int(round(kcal)),
        "protein_g": int(round(protein_g)),
        "fat_g": int(round(fat_g)),
        "carbs_g": int(round(carbs_g)),
        "tdee": int(round(tdee)),
        "bmr": int(round(bmr)),
        "bmr_formula": bmr_formula,
        "lean_mass_kg": lean_mass_kg,
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
        "mindful_mode": goal_raw == "mindful",
    }


_NEGATED_MODIFIER_RE = re.compile(
    r"(?iu)\bбез\s+(?:добавленн\w+\s+|добавления\s+)?[а-яёa-z]+(?:\s+и\s+[а-яёa-z]+)?\b"
)


def _strip_negated_modifiers(text: str) -> str:
    """Убрать отрицательные уточнения вида 'без сахара', 'без масла и соли' до поиска ингредиентов."""
    low = str(text or "").casefold().replace("ё", "е")
    stripped = _NEGATED_MODIFIER_RE.sub(" ", low)
    return re.sub(r"\s+", " ", stripped).strip()


def _food_stem(key: str) -> str:
    k = str(key or "").casefold().replace("ё", "е").strip()
    if len(k) >= 5 and k.endswith(("а", "я", "о", "е", "ь", "ы", "и")):
        return k[:-1]
    return k


def _match_nutrition_key(name: str) -> Optional[str]:
    low = _strip_negated_modifiers(name)
    if not low:
        return None
    for key in _SORTED_NUTRITION_KEYS:
        if key.replace("ё", "е") in low:
            return key
    for key in _SORTED_NUTRITION_KEYS:
        stem = _food_stem(key)
        if len(stem) >= 4 and stem in low:
            return key
    if "яиц" in low or "яич" in low or "яйц" in low:
        return "яйцо"
    if re.search(r"\bкол[аеуыо]й?\b", low):
        return "кола"
    if re.search(r"\bча[йюяе]\b", low):
        return "чай"
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
            if key in low or (len(_food_stem(key)) >= 4 and _food_stem(key) in low):
                return q * grams
        if "яиц" in low or "яйц" in low:
            return q * 55.0
        return q * 80.0
    return q * 60.0


def _normalize_nutrition_dict(
    raw: Any, *, estimated: bool = False, allow_zero: bool = False
) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    if allow_zero and ("kcal" not in raw or raw.get("kcal") is None):
        return None
    try:
        kcal = float(raw.get("kcal") if raw.get("kcal") is not None else 0)
        p = float(raw.get("protein_g") if raw.get("protein_g") is not None else raw.get("protein") or 0)
        f = float(raw.get("fat_g") if raw.get("fat_g") is not None else raw.get("fat") or 0)
        c = float(raw.get("carbs_g") if raw.get("carbs_g") is not None else raw.get("carbs") or 0)
    except (TypeError, ValueError):
        return None
    if allow_zero:
        if not (0.0 <= kcal <= 6000.0):
            return None
        if kcal == 0 and not str(raw.get("title") or "").strip():
            return None
    elif kcal <= 0:
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
    "Ты — нутриционист HiDL. По описанию, фото тарелки или фото этикетки/состава оцени КБЖУ за съеденную порцию.\n"
    "Ответь СТРОГО валидным JSON-объектом вида:\n"
    "{\"title\": str, \"meal_slot\": \"breakfast\"|\"lunch\"|\"dinner\"|\"snack\", "
    "\"kcal\": float, \"protein_g\": float, \"fat_g\": float, \"carbs_g\": float, "
    "\"is_nutrition_label\": bool, \"per_100g\": {\"kcal\": float, \"protein_g\": float, \"fat_g\": float, \"carbs_g\": float}|null, "
    "\"serving_g\": float, \"brand_or_place\": str, "
    "\"confidence\": \"high\"|\"medium\"|\"low\", \"needs_clarification\": bool, \"clarification_question\": str}.\n"
    "Правила:\n"
    "1. Если на фото таблица пищевой ценности (КБЖУ на 100 г/мл): поставь is_nutrition_label=true, заполни точные per_100g, "
    "вытащи название продукта и в kcal/protein_g/fat_g/carbs_g посчитай порцию (из комментария пользователя или стандартной упаковки serving_g).\n"
    "2. Напитки без калорий и сахара (кола зеро / без сахара / без калорий, вода, чай или чёрный кофе без сахара и молока): "
    "ставь kcal=0, protein_g=0, fat_g=0, carbs_g=0, needs_clarification=false.\n"
    "3. Учитывай профиль питания пользователя из контекста: если профиль vegan/vegetarian, считай блюда растительными "
    "(например, 'фарш', 'котлета', 'молоко', 'сыр' — растительные/веганские, типа Hi!) и не спрашивай, какое мясо использовалось.\n"
    "4. Если пользователь ссылается на вчерашний или недавний приём пищи ('то, что ел вчера', 'как вчера', 'доел вчерашнее') "
    "и он есть в списке недавних приёмов пищи в контексте — используй и при необходимости отмасштабируй его КБЖУ, не задавай лишних вопросов.\n"
    "5. Если описание достаточно понятное (\"банан\", \"овсянка 80 г\", \"латте 300 мл\", \"гречка с тофу\", \"блин с ветчиной и сыром в теремке\"): "
    "needs_clarification=false, clarification_question=\"\". Не мучай лишними вопросами.\n"
    "6. Ставь needs_clarification=true и короткий вопрос (1 фраза по-русски, например \"Какие блины и сколько штук?\") "
    "ТОЛЬКО если совсем непонятен размер/начинка/состав (\"поел суп\", \"блины в теремке\", \"кофе с булочкой\", \"съел торт\", \"пообедал в кафе\"), "
    "но при этом всё равно заполни лучшую примерную оценку kcal/БЖУ."
)

_UNIT_WEIGHT_VOL = r"(?:грамм[а-я]*|гр|г|g|миллилитр[а-я]*|мл|ml)"

_PER_100_MARKER_RE = re.compile(
    rf"(?iu)(?:на\s*100\s*{_UNIT_WEIGHT_VOL}\b\.?|/\s*100\s*{_UNIT_WEIGHT_VOL}\b\.?|100\s*{_UNIT_WEIGHT_VOL}\b\.?\s*[-:])"
)

_LAZY_ACCEPT_EXACT: set[str] = {
    "хз",
    "похуй",
    "пох",
    "на похуй",
    "пофиг",
    "пофигу",
    "по барабану",
    "забей",
    "на глаз",
    "примерно",
    "прикинь",
    "сам прикинь",
    "сама прикинь",
    "как хочешь",
    "не знаю",
    "не помню",
    "без понятия",
    "норм",
    "пойдет",
    "пойдёт",
    "сойдет",
    "сойдёт",
    "ок",
    "да ок",
    "ладно",
    "лень",
    "так запиши",
    "давай так",
    "давай примерно",
    "запиши примерно",
    "хз примерно",
    "да похуй",
    "да пофиг",
}

_MEAL_CANCEL_EXACT = {
    "нет",
    "не надо",
    "не записывай",
    "отмена",
    "отменить",
    "пропусти",
    "пропустить",
    "не ел",
    "не ела",
    "передумал",
    "передумала",
    "отстань",
    "не сохраняй",
}

_NON_CLARIFICATION_RE = re.compile(
    r"(?iu)(?:^\s*/|\bнапомн|\bзадач|\bв\s+план|\bкупи\b|\bкупить\b|\bзакончил(?:ся|ась|ось|ись)\b|"
    r"\bпотратил|\bв\s+календар|\bперенеси|\bвзвесил|\bустал|\bтрево|\bне\s+могу\s+уснуть|"
    r"\bчто\s+на\s+сегодня|\bкакая\s+карма|\bчто\s+в\s+холодильник|\bдай\s+совет|"
    r"\bпривет\b|\bку\b|\bхай\b|\bздравствуй\w*\b|\bспасибо\b|\bпока\b|"
    r"\bпроснул\w*\b|\bвстал\w*\b|\bложусь\b|\bсплю\b|\bпочистил\w*\b|\bвыпил\s+вод\w*\b|"
    r"\bболею\b|\bвыходной\b|\bудал\w*\b|\bотмен\w*\b|\bподъ[её]м\w*\b)"
)

_VAGUE_MEAL_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?iu)\b(?:блин[аы]?|сырник[аи]?)\b"),
        "Сколько штук и какая начинка/добавка? (или ответь «хз» — запишу примерно)",
    ),
    (
        re.compile(r"(?iu)\b(?:кофе|капучино|латте|раф|чай)\b.*\b(?:булоч\w*|десерт\w*|торт\w*|пирож\w*|печень\w*)\b"),
        "Какой объём напитка (S/M/L) и какая именно выпечка? (или напиши «хз»)",
    ),
    (
        re.compile(r"(?iu)\b(?:суп|борщ|солянк\w*|ух[ау]|щи|рамен)\b"),
        "Примерно сколько мл/половников и что в нём? (или ответь «примерно»)",
    ),
    (
        re.compile(r"(?iu)\bсалат\b"),
        "Какой примерно салат (лёгкий с овощами или с майонезом/маслом) и какая порция? (или «хз»)",
    ),
    (
        re.compile(r"(?iu)\b(?:пицц\w*|ролл\w*|торт\w*|пирог\w*|шаурм\w*|шаверм\w*|бургер\w*)\b"),
        "Какая примерно начинка или сколько кусков/грамм? (или напиши «хз» — оценю примерно)",
    ),
    (
        re.compile(r"(?iu)\b(?:в\s+кафе|в\s+ресторане|пообедал|поужинал|позавтракал|перекусил|доставк\w*)\b"),
        "Что именно было в тарелке и большая ли порция? (или ответь «примерно»)",
    ),
)


def is_lazy_accept_reply(text: str) -> bool:
    """Проверить, отвечает ли пользователь в духе 'хз / похуй / запиши примерно'."""
    raw = str(text or "").strip().casefold().replace("ё", "е")
    if not raw or len(raw) > 70:
        return False
    norm = re.sub(r"[?!.,;:\"'«»()]+", " ", raw)
    norm = re.sub(r"\s+", " ", norm).strip()
    if norm in _LAZY_ACCEPT_EXACT:
        return True
    if any(
        tok in norm
        for tok in ("похуй", "пофиг", "на глаз", "примерно", "сам прикинь", "сама прикинь", "не знаю", "забей")
    ) and not _NON_CLARIFICATION_RE.search(norm):
        return True
    return False


def is_meal_cancel_reply(text: str) -> bool:
    """Проверить, просит ли пользователь НЕ записывать ожидающий приём пищи."""
    raw = str(text or "").strip().casefold().replace("ё", "е")
    if not raw or len(raw) > 70:
        return False
    norm = re.sub(r"[?!.,;:\"'«»()]+", " ", raw)
    norm = re.sub(r"\s+", " ", norm).strip()
    return norm in _MEAL_CANCEL_EXACT


def looks_like_meal_clarification(text: str) -> bool:
    """Отличить уточнение по прошлой еде (граммы, начинка, этикетка) от новой отдельной команды."""
    raw = str(text or "").strip()
    if not raw or len(raw) > 180:
        return False
    if is_meal_cancel_reply(raw):
        return False
    if _NON_CLARIFICATION_RE.search(raw):
        return False
    if raw.endswith("?"):
        return False
    low = raw.casefold().replace("ё", "е")
    if is_lazy_accept_reply(low):
        return True
    if re.search(
        r"(?iu)^на\s+(?:десерт|завтрак|обед|ужин|перекус|полдник)\b",
        low,
    ):
        return False
    if re.search(
        r"(?iu)\b(?:закусил[аи]?|перекусил[аи]?|позавтракал[аи]?|пообедал[аи]?|поужинал[аи]?)\b",
        low,
    ):
        return False
    if _PER_100_MARKER_RE.search(low) or re.search(
        rf"\b\d+(?:[.,]\d+)?\s*(?:{_UNIT_WEIGHT_VOL}|ккал|kcal|калл|килокалор[а-я]*|шт|куск|порц|половник|ложк)\b",
        low,
    ):
        return True
    if re.search(
        r"(?iu)^(?:с\s+|без\s+|"
        r"на\s+(?:вод[еу]|молок[ео]|овсян\w*|кокосов\w*|миндальн\w*|соев\w*|масл[ео]|пар[уе]|грил[ея]|сковород\w*|100\b|глаз\b)|"
        r"два|две|три|пол|полов|маленьк|средн|больш|обычн|двойн|порц|кусок|куск)",
        low,
    ):
        return True
    return len(low.split()) <= 5 and not re.search(
        r"(?iu)\b(?:позвонить|написать|оплатить|убрать|помыть|сделать|придумал|идея|кстати)\b",
        low,
    )


def _detect_vague_meal(clean_text: str) -> Optional[str]:
    """Вернуть 1 короткий вопрос ТОЛЬКО если описание еды реально размытое."""
    raw = str(clean_text or "").strip()
    low = raw.casefold().replace("ё", "е")
    if not low:
        return None
    if _PER_100_MARKER_RE.search(low) or re.search(r"\b\d+\s*(?:ккал|kcal|калл|г|гр|g|мл|ml|кг|л)\b", low):
        return None
    if is_lazy_accept_reply(low) or any(w in low for w in ("примерно", "на глаз", "около ")):
        return None
    # Если уже есть уточнение начинки ("с ветчиной и сыром", "со сметаной", "с курицей") и размер/число или бренд с конкретным блюдом
    has_filling = bool(re.search(r"(?iu)\bс(?:о)?\s+[а-яёa-z]{3,}", low)) and not re.search(
        r"(?iu)\bкофе\s+с\s+булоч", low
    )
    has_count = bool(re.search(r"(?iu)\b(?:\d+\s*(?:шт|куск|блин|ролл|сырник)|два|две|три|пол)\b", low))
    for pat, question in _VAGUE_MEAL_PATTERNS:
        if pat.search(low):
            if pat.pattern.find("блин") >= 0 and (has_filling and (has_count or not re.search(r"(?iu)\b(?:блин[аы]|сырник[аи])\b", low))):
                return None
            if pat.pattern.find("салат") >= 0 and (has_filling or any(w in low for w in ("цезарь", "греческ", "оливье", "винегрет", "овощн"))):
                return None
            if pat.pattern.find("пицц") >= 0 and (has_filling and (has_count or "цел" not in low)):
                return None
            if pat.pattern.find("в\\s+кафе") >= 0 and (has_filling or re.search(r"(?iu)(?:съел|выпил|блин|бургер|суп|паст|овсян|греч)", low)):
                continue
            return question
    return None


def _clean_label_title(raw_title: str) -> str:
    cleaned = re.sub(
        r"(?iu)^(?:этикетка|упаковка|съел[аи]?|доел[аи]?|выпил[аи]?|пью|ем)\s*[:,-]?\s*",
        "",
        str(raw_title or "").strip(),
    )
    for _ in range(2):
        cleaned = re.sub(
            r"(?iu),?\s*(?:пищевая\s+ценность|энергетическая\s+ценность|кбжу|калорийность)\s*$",
            "",
            cleaned,
        )
        cleaned = re.sub(
            rf"(?iu),?\s*(?:примерно|около|где-то)?\s*(?:{_UNIT_WEIGHT_VOL}\s*\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s*{_UNIT_WEIGHT_VOL})\s*(?:пью|ем|выпил[аи]?|съел[аи]?)?\s*$",
            "",
            cleaned,
        )
        cleaned = cleaned.strip(" -:,.")
    return cleaned


def _parse_label_math_offline(raw_text: str, title_hint: str = "") -> Optional[Dict[str, Any]]:
    """Распознать текст формата 'на 100 г 240 ккал б12 ж8 у30, съел 150 г' и посчитать порцию."""
    text = re.sub(r"\s+", " ", str(raw_text or "").strip())
    low = text.casefold().replace("ё", "е")
    m_100 = _PER_100_MARKER_RE.search(low)
    if not m_100:
        return None
    kcal_m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ккал|kcal|калл|килокалор[а-я]*|калор[а-я]*)\b", low)
    if not kcal_m:
        return None
    k100 = float(kcal_m.group(1).replace(",", "."))
    if not (5.0 <= k100 <= 950.0):
        return None
    p_m = re.search(r"\b(?:бел(?:ок|к[а-я]*)|б|p)\s*[:=\-]?\s*(\d+(?:[.,]\d+)?)", low)
    f_m = re.search(r"\b(?:жир[а-я]*|ж|f)\s*[:=\-]?\s*(\d+(?:[.,]\d+)?)", low)
    c_m = re.search(r"\b(?:углев[а-я]*|у|c)\s*[:=\-]?\s*(\d+(?:[.,]\d+)?)", low)
    p100 = float(p_m.group(1).replace(",", ".")) if p_m else round(k100 * 0.20 / 4.0, 1)
    f100 = float(f_m.group(1).replace(",", ".")) if f_m else round(k100 * 0.28 / 9.0, 1)
    c100 = float(c_m.group(1).replace(",", ".")) if c_m else round(k100 * 0.52 / 4.0, 1)

    without_100 = low[: m_100.start()] + " " + low[m_100.end() :]
    macro_tail_re = re.compile(r"\b(?:б|ж|у|бел(?:ок|к[а-я]*)|жир[а-я]*|углев[а-я]*)\s*[:=\-]?\s*$")

    consumed_matches: list[tuple[int, float]] = []
    verb_before_qty_re = re.compile(
        rf"(?iu)\b(?:съел[аи]?|доел[аи]?|выпил[аи]?|пью|порц[а-я]*|взял[аи]?|вышло)\s+"
        rf"(?:(?:примерно|около|где-то|на)\s+)?"
        rf"(?:{_UNIT_WEIGHT_VOL}\s*(\d+(?:[.,]\d+)?)\b|(\d+(?:[.,]\d+)?)\s*{_UNIT_WEIGHT_VOL}\b)"
    )
    qty_before_verb_re = re.compile(
        rf"(?iu)(?:{_UNIT_WEIGHT_VOL}\s*(\d+(?:[.,]\d+)?)\b|(\d+(?:[.,]\d+)?)\s*{_UNIT_WEIGHT_VOL}\b)\s+"
        rf"(?:(?:сейчас|уже|тут|сегодня|только\s+что)\s+)*"
        rf"(?:съел[аи]?|доел[аи]?|выпил[аи]?|пью|поел[аи]?|взял[аи]?)\b"
    )
    for m in verb_before_qty_re.finditer(without_100):
        val = (m.group(1) or m.group(2) or "").replace(",", ".")
        if val:
            consumed_matches.append((m.start(), float(val)))
    for m in qty_before_verb_re.finditer(without_100):
        if macro_tail_re.search(without_100[: m.start()]):
            continue
        val = (m.group(1) or m.group(2) or "").replace(",", ".")
        if val:
            consumed_matches.append((m.start(), float(val)))

    if consumed_matches:
        consumed_matches.sort(key=lambda pair: pair[0])
        grams = consumed_matches[-1][1]
    else:
        container_re = re.compile(
            rf"(?iu)\b(?:упаковк[аиу]?|пачк[аиу]?|батончик[а]?|бутылк[аиу]?|банк[аиу]?|стакан[а]?)\s+"
            rf"(?:на\s+)?(?:{_UNIT_WEIGHT_VOL}\s*(\d+(?:[.,]\d+)?)\b|(\d+(?:[.,]\d+)?)\s*{_UNIT_WEIGHT_VOL}\b)"
        )
        m_pack = container_re.search(without_100)
        if m_pack:
            val = (m_pack.group(1) or m_pack.group(2) or "").replace(",", ".")
            grams = float(val) if val else 100.0
        else:
            candidates = [
                float((m.group(1) or m.group(2)).replace(",", "."))
                for m in re.finditer(
                    rf"(?iu)(?:(\d+(?:[.,]\d+)?)\s*{_UNIT_WEIGHT_VOL}\b|{_UNIT_WEIGHT_VOL}\s*(\d+(?:[.,]\d+)?)\b)",
                    without_100,
                )
                if not macro_tail_re.search(without_100[: m.start()])
            ]
            grams = candidates[-1] if candidates else 100.0
    grams = max(5.0, min(2000.0, grams))
    factor = grams / 100.0

    cleaned_hint = _clean_label_title(title_hint)
    cleaned_prefix = _clean_label_title(text[: m_100.start()])
    if len(cleaned_hint) >= 3:
        title = cleaned_hint[:65]
    elif len(cleaned_prefix) >= 3:
        title = cleaned_prefix[:65]
    else:
        title = f"Продукт ({int(round(grams))} г)"
    return {
        "title": title,
        "brand": "",
        "meal_slot": "snack",
        "kcal": int(round(k100 * factor)),
        "protein_g": round(p100 * factor, 1),
        "fat_g": round(f100 * factor, 1),
        "carbs_g": round(c100 * factor, 1),
        "from_label": True,
        "is_nutrition_label": True,
        "serving_g": round(grams, 1),
        "per_100g": {
            "kcal": round(k100, 1),
            "protein_g": round(p100, 1),
            "fat_g": round(f100, 1),
            "carbs_g": round(c100, 1),
        },
        "needs_clarification": False,
        "clarification_question": "",
    }


_ZERO_CAL_EXPLICIT_RE = re.compile(
    r"(?iu)(?:\bбез\s+калор\w*|\bноль\s+калор\w*|\b0\s*(?:ккал|kcal|калор\w*)|\bzero\s*kcal\b)"
)
_ZERO_SUGAR_QUALIFIER_RE = re.compile(
    r"(?iu)(?:\bбез\s+(?:добавленн\w+\s+)?сахар\w*|\bзеро\b|\bzero\b|\bлайт\b|\blight\b|"
    r"\bsugar[\s-]*free\b|\bno[\s-]*sugar\b|\bпепси\s+макс\b|\bpepsi\s+max\b)"
)
_ZERO_CAL_DRINK_RE = re.compile(
    r"(?iu)\b(?:кол[аеуыо]й?|кок[аеу]-?кол\w*|coca[\s-]*cola|пепси|pepsi|спрайт|sprite|"
    r"фанта|fanta|газировк\w*|лимонад\w*|энергетик\w*|напит\w*|ча[йюяе]|кофе|вод[аыу]|"
    r"минералк\w*|американо|эспрессо|тоник\w*|айс[\s-]*ти|icetea)\b"
)
_PURE_ZERO_DRINK_RE = re.compile(
    r"(?iu)\b(?:вод[аыу]|минералк\w*|минеральн\w+\s+вод\w*|боржоми|ессентуки|"
    r"американо|эспрессо|ч[её]рн\w+\s+кофе)\b"
)
_CALORIC_DRINK_ADDIN_RE = re.compile(
    r"(?iu)(?:\bсо?\s+(?:молок\w*|сливк\w*|сгущ\w*|сироп\w*|м[её]д\w*|сахар\w*|пенк\w*|морожен\w*|пломбир\w*)|"
    r"\b(?:латте|капучино|раф|смузи|коктейл\w*|горяч\w+\s+шоколад\w*|какао|матча)\b)"
)
_ZERO_DRINK_KEY_ALLOWLIST = {None, "вода", "кипяток", "чай", "кофе", "кола", "лимонад", "лимон"}


def _clean_drink_title(raw_text: str, title_hint: str = "") -> str:
    hint = str(title_hint or "").strip()
    if len(hint) >= 3:
        return hint[:70]
    cleaned = re.sub(
        r"(?iu)^(?:я\s+|только\s+что\s+|ещ[её]\s+)*"
        r"(?:запивал[аи]?\s+(?:это|всё|все)?|выпил[аи]?|попил[аи]?|пью|съел[аи]?)\s*",
        "",
        str(raw_text or "").strip(),
    ).strip(" .,-:")
    return (cleaned or str(raw_text or "").strip())[:70]


def _parse_zero_calorie_drink(raw_text: str, title_hint: str = "") -> Optional[Dict[str, Any]]:
    """Распознать напиток без калорий/сахара (кола зеро, вода, чай/американо без сахара и т.п.)."""
    text = re.sub(r"\s+", " ", str(raw_text or "").strip())
    if not text:
        return None
    low = text.casefold().replace("ё", "е")
    stripped = _strip_negated_modifiers(low)
    if _CALORIC_DRINK_ADDIN_RE.search(stripped):
        return None

    parts = [p.strip() for p in re.split(r"[,;\n]+|\s+\+\s+|\s+и\s+", stripped) if p.strip()]
    for part in parts or [stripped]:
        if _match_nutrition_key(part) not in _ZERO_DRINK_KEY_ALLOWLIST:
            return None

    has_explicit_zero = bool(_ZERO_CAL_EXPLICIT_RE.search(low))
    has_drink_word = bool(_ZERO_CAL_DRINK_RE.search(low))
    has_zero_sugar = bool(_ZERO_SUGAR_QUALIFIER_RE.search(low))
    has_pure_zero_drink = bool(_PURE_ZERO_DRINK_RE.search(low))

    if not (
        (has_explicit_zero and (has_drink_word or bool(parts)))
        or (has_drink_word and has_zero_sugar)
        or has_pure_zero_drink
    ):
        return None

    return {
        "title": _clean_drink_title(text, title_hint=title_hint),
        "meal_slot": "snack",
        "kcal": 0,
        "protein_g": 0.0,
        "fat_g": 0.0,
        "carbs_g": 0.0,
        "estimated": False,
        "needs_clarification": False,
        "clarification_question": "",
    }


def _parse_meal_offline(raw_text: str, title_hint: str = "") -> Optional[Dict[str, Any]]:
    text = re.sub(r"\s+", " ", str(raw_text or "").strip())
    if not text:
        return None
    label_res = _parse_label_math_offline(text, title_hint=title_hint)
    if label_res is not None:
        return label_res
    zero_res = _parse_zero_calorie_drink(text, title_hint=title_hint)
    if zero_res is not None:
        return zero_res

    low = text.casefold().replace("ё", "е")

    # Явно указанные калории: "овсянка 350 ккал б12 ж8 у55"
    kcal_m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ккал|kcal|калл|килокалор[а-я]*|калор[а-я]*)\b", low)
    if kcal_m:
        kcal = float(kcal_m.group(1).replace(",", "."))
        p_m = re.search(r"(?:б|белк[а-я]*|p)\s*[:=]?\s*(\d+(?:[.,]\d+)?)", low)
        f_m = re.search(r"(?:ж|жир[а-я]*|f)\s*[:=]?\s*(\d+(?:[.,]\d+)?)", low)
        c_m = re.search(r"(?:у|углев[а-я]*|c)\s*[:=]?\s*(\d+(?:[.,]\d+)?)", low)
        p = float(p_m.group(1).replace(",", ".")) if p_m else round(kcal * 0.20 / 4.0, 1)
        f = float(f_m.group(1).replace(",", ".")) if f_m else round(kcal * 0.28 / 9.0, 1)
        c = float(c_m.group(1).replace(",", ".")) if c_m else round(kcal * 0.52 / 4.0, 1)
        clean_title = (
            title_hint.strip()[:70]
            if len(str(title_hint or "").strip()) >= 3
            else (re.sub(r"\d+.*$", "", text).strip(" -:,.") or text[:50])
        )
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
        if key is None:
            continue
        qty_m = re.search(
            r"(\d+(?:[.,]\d+)?)\s*(кг|kg|г|гр|g|мл|ml|л|l|шт|стакан\w*|круж\w*|порц\w*)?",
            part.lower(),
        )
        qty = float(qty_m.group(1).replace(",", ".")) if qty_m and qty_m.group(1) else 1.0
        unit = (qty_m.group(2) or "шт") if qty_m else "шт"
        if qty_m and qty >= 15 and not qty_m.group(2):
            unit = "г"
        grams = _ingredient_grams(part, qty, unit)
        k100, p100, f100, c100 = _NUTRITION_DB[key]
        matched_any = True
        factor = grams / 100.0
        total_k += k100 * factor
        total_p += p100 * factor
        total_f += f100 * factor
        total_c += c100 * factor

    if not matched_any:
        # Одно консервативное оценочное значение, а не 150 * число запятых
        total_k, total_p, total_f, total_c = 150.0, 5.0, 5.0, 20.0
    clean_hint = str(title_hint or "").strip()
    out_title = clean_hint[:70] if len(clean_hint) >= 3 else text[:70]
    return {
        "title": out_title,
        "meal_slot": "snack",
        "kcal": int(round(max(0.0, total_k) if matched_any else max(10.0, total_k))),
        "protein_g": round(total_p, 1),
        "fat_g": round(total_f, 1),
        "carbs_g": round(total_c, 1),
    }


async def _maybe_save_label_product(
    db,
    user_id: Optional[int],
    title: str,
    per_100g: Optional[Dict[str, Any]],
    *,
    brand: str = "",
    serving_g: float = 100.0,
    source: str = "label",
) -> None:
    if db is None or user_id is None or not isinstance(per_100g, dict):
        return
    try:
        k100 = float(per_100g.get("kcal") or 0.0)
    except (TypeError, ValueError):
        k100 = 0.0
    if k100 <= 0 or not title or title.lower().startswith("продукт ("):
        return
    clean_title = nutrition_web.normalize_food_query(title)
    if len(clean_title) < 3:
        return
    await repo.upsert_user_food_product(
        db,
        int(user_id),
        clean_title,
        title.strip(),
        brand=brand,
        per_100g={
            "kcal": round(k100, 1),
            "protein_g": round(float(per_100g.get("protein_g") or per_100g.get("protein") or 0.0), 1),
            "fat_g": round(float(per_100g.get("fat_g") or per_100g.get("fat") or 0.0), 1),
            "carbs_g": round(float(per_100g.get("carbs_g") or per_100g.get("carbs") or 0.0), 1),
        },
        default_serving_g=float(serving_g or 100.0),
        source=source,
    )


async def parse_meal_input(
    *,
    raw_text: str = "",
    image_bytes: Optional[bytes] = None,
    image_mime: str = "image/jpeg",
    llm_client_instance: Optional[LLMClient] = None,
    db=None,
    user_id: Optional[int] = None,
    allow_clarification: bool = True,
    allow_web_lookup: bool = True,
    web_lookup_fn: Optional[nutrition_web.SearchFn] = None,
    title_hint: str = "",
) -> Dict[str, Any]:
    """Распознать приём пищи (КБЖУ) по фото тарелки/этикетки, тексту, этикетке или меню в сети."""
    client = llm_client_instance or default_llm_client
    clean_text = str(raw_text or "").strip()

    if image_bytes:
        if not client.is_configured():
            return {"ok": False, "error": "llm_not_configured"}
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{image_mime};base64,{b64}"
        user_msg = (
            "Оцени это фото: если это блюдо — посчитай КБЖУ тарелки; если это этикетка/состав — вытащи per_100g и посчитай порцию."
            + (f" Комментарий: {clean_text}" if clean_text else "")
        )
        res = await client.complete_json(
            _MEAL_PARSE_PROMPT, user_msg, temperature=0.1, max_tokens=600, image_data_urls=[data_url]
        )
        if not res.ok or not isinstance(res.data, dict):
            return {"ok": False, "error": res.error or "vision_parse_failed"}
        norm = _normalize_nutrition_dict(
            res.data, estimated=not bool(res.data.get("is_nutrition_label")), allow_zero=True
        )
        if not norm:
            return {"ok": False, "error": "no_meal_found"}
        slot = str(res.data.get("meal_slot") or "snack").strip().lower()
        title = str(res.data.get("title") or clean_text or "Блюдо по фото").strip()
        is_label = bool(res.data.get("is_nutrition_label"))
        per_100 = res.data.get("per_100g") if isinstance(res.data.get("per_100g"), dict) else None
        srv_g = float(res.data.get("serving_g") or 100.0) if res.data.get("serving_g") else 100.0

        # Если пользователь в подписи указал точные граммы ("150 г") к фото этикетки — пересчитаем строго!
        if is_label and per_100 and clean_text:
            exp_g, _ = nutrition_web.extract_requested_grams_and_mult(clean_text, default_serving_g=srv_g)
            if exp_g is not None and float(per_100.get("kcal") or 0) > 0:
                fac = exp_g / 100.0
                norm = {
                    "kcal": int(round(float(per_100.get("kcal") or 0) * fac)),
                    "protein_g": round(float(per_100.get("protein_g") or 0) * fac, 1),
                    "fat_g": round(float(per_100.get("fat_g") or 0) * fac, 1),
                    "carbs_g": round(float(per_100.get("carbs_g") or 0) * fac, 1),
                    "estimated": False,
                }
                srv_g = exp_g

        if is_label and per_100:
            await _maybe_save_label_product(
                db,
                user_id,
                title,
                per_100,
                brand=str(res.data.get("brand_or_place") or ""),
                serving_g=srv_g,
                source="vision_label",
            )
        clar_q = str(res.data.get("clarification_question") or "").strip() if allow_clarification else ""
        needs_clar = bool(res.data.get("needs_clarification")) and bool(clar_q) and allow_clarification
        return {
            "ok": True,
            "used_llm": True,
            "title": title,
            "meal_slot": slot if slot in {"breakfast", "lunch", "dinner", "snack"} else "snack",
            "is_nutrition_label": is_label,
            "per_100g": per_100 or {},
            "serving_g": srv_g,
            "needs_clarification": needs_clar,
            "clarification_question": clar_q,
            **norm,
        }

    if not clean_text:
        return {"ok": False, "error": "empty_input"}

    # 1. Прямая математика этикетки на 100 г в тексте ("на 100г 240 ккал ..., съел 150 г")
    label_hit = _parse_label_math_offline(clean_text, title_hint=title_hint)
    if label_hit is not None:
        await _maybe_save_label_product(
            db,
            user_id,
            label_hit["title"],
            label_hit.get("per_100g"),
            serving_g=float(label_hit.get("serving_g") or 100.0),
            source="text_label",
        )
        return {"ok": True, "used_llm": False, "estimated": False, **label_hit}

    # 1b. Напитки без калорий (кола зеро / без сахара, вода, чай/американо без сахара)
    zero_hit = _parse_zero_calorie_drink(clean_text, title_hint=title_hint)
    if zero_hit is not None:
        return {"ok": True, "used_llm": False, **zero_hit}

    # 2. Многоуровневый справочник: личные этикетки -> SQLite кэш -> OpenFoodFacts / Web Search
    allow_net_now = bool(
        allow_web_lookup
        and (web_lookup_fn is not None or nutrition_web.needs_web_nutrition_lookup(clean_text))
    )
    lookup_rec = await nutrition_web.lookup_food_nutrition(
        db,
        user_id,
        clean_text,
        allow_network=allow_net_now,
        llm_client_instance=client,
        web_fn=web_lookup_fn,
    )
    vague_q = _detect_vague_meal(clean_text) if allow_clarification else None
    if lookup_rec is not None:
        scaled = nutrition_web.scale_lookup_record(lookup_rec, clean_text)
        if scaled is not None:
            return {
                "ok": True,
                "used_llm": scaled["lookup_source"] == "web_search",
                "title": scaled["title"],
                "meal_slot": "snack",
                "kcal": scaled["kcal"],
                "protein_g": scaled["protein_g"],
                "fat_g": scaled["fat_g"],
                "carbs_g": scaled["carbs_g"],
                "estimated": scaled["lookup_source"] not in {"my_label", "openfoodfacts"},
                "lookup_source": scaled["lookup_source"],
                "lookup_url": scaled["lookup_url"],
                "needs_clarification": bool(vague_q),
                "clarification_question": vague_q or "",
            }

    # 3. Умный LLM-разбор (если настроен)
    if client.is_configured():
        llm_prompt = f"Оцени КБЖУ съеденного:\n{clean_text}"
        if db is not None and user_id is not None:
            ctx_lines: List[str] = []
            try:
                wellness = await repo.get_wellness(db, int(user_id)) or {}
                meal_profile = str(wellness.get("meal_profile") or "").strip().lower()
                if meal_profile and meal_profile != "omnivore":
                    ctx_lines.append(f"Профиль питания пользователя: {meal_profile}")
                if re.search(
                    r"(?iu)\b(?:вчера|позавчера|вчерашн\w*|прошл\w*|то\s*,?\s*что\s+ел|"
                    r"как\s+вчера|то\s+же|такой\s+же|такую\s+же|доел\w*|осталось)\b",
                    clean_text,
                ):
                    cur = await db.execute(
                        """
                        SELECT local_date, meal_slot, title, kcal, protein_g, fat_g, carbs_g
                        FROM meal_logs
                        WHERE user_id = ?
                        ORDER BY id DESC
                        LIMIT 6
                        """,
                        (int(user_id),),
                    )
                    recent_rows = await cur.fetchall()
                    if recent_rows:
                        ctx_lines.append("Недавние приёмы пищи пользователя:")
                        for mr in recent_rows:
                            r = dict(mr)
                            ctx_lines.append(
                                f"- {r.get('local_date')} ({r.get('meal_slot')}): {r.get('title')} — "
                                f"{int(round(float(r.get('kcal') or 0)))} ккал "
                                f"(Б{round(float(r.get('protein_g') or 0), 1)}/"
                                f"Ж{round(float(r.get('fat_g') or 0), 1)}/"
                                f"У{round(float(r.get('carbs_g') or 0), 1)})"
                            )
            except Exception:
                pass
            if ctx_lines:
                llm_prompt += "\n\nКонтекст:\n" + "\n".join(ctx_lines)

        res = await client.complete_json(
            _MEAL_PARSE_PROMPT, llm_prompt, temperature=0.1, max_tokens=550
        )
        if res.ok and isinstance(res.data, dict):
            norm = _normalize_nutrition_dict(res.data, estimated=True, allow_zero=True)
            if norm:
                slot = str(res.data.get("meal_slot") or "snack").strip().lower()
                clar_q = str(res.data.get("clarification_question") or "").strip() if allow_clarification else ""
                needs_clar = bool(res.data.get("needs_clarification")) and bool(clar_q) and allow_clarification
                return {
                    "ok": True,
                    "used_llm": True,
                    "title": str(res.data.get("title") or clean_text[:60]).strip(),
                    "meal_slot": slot if slot in {"breakfast", "lunch", "dinner", "snack"} else "snack",
                    "needs_clarification": needs_clar,
                    "clarification_question": clar_q,
                    **norm,
                }

    # 4. Надёжный офлайн-fallback + мягкая проверка на расплывчатость
    offline = _parse_meal_offline(clean_text, title_hint=title_hint)
    if not offline:
        return {"ok": False, "error": "no_meal_found"}
    return {
        "ok": True,
        "used_llm": False,
        "needs_clarification": bool(vague_q),
        "clarification_question": vague_q or "",
        **offline,
    }


def _mins_until_sleep_for_user(user: Dict[str, Any], now: datetime.datetime) -> int:
    try:
        from utils.time import format_time_local, parse_hhmm
        local_hhmm = format_time_local(now, str(user.get("timezone") or "UTC"))
        now_t = parse_hhmm(local_hhmm) or datetime.time(12, 0)
        sleep_t = parse_hhmm(str(user.get("sleep_time") or "23:00")) or datetime.time(23, 0)
        now_m = now_t.hour * 60 + now_t.minute
        sleep_m = sleep_t.hour * 60 + sleep_t.minute
        if sleep_m < now_m and now_m - sleep_m > 360:
            sleep_m += 24 * 60
        return sleep_m - now_m
    except Exception:
        return 180


async def evaluate_meal_check(
    db,
    user: Dict[str, Any],
    meal_text: str,
    *,
    now_utc: Optional[datetime.datetime] = None,
    llm_client_instance: Optional[LLMClient] = None,
    web_lookup_fn: Optional[nutrition_web.SearchFn] = None,
) -> Dict[str, Any]:
    """Примерить планируемое блюдо к остатку КБЖУ, времени до отбоя и цели/стилю коуча."""
    now = now_utc or datetime.datetime.utcnow()
    clean_meal = str(meal_text or "").strip()
    if not clean_meal:
        return {"ok": False, "error": "empty_meal"}

    dish = await parse_meal_input(
        raw_text=clean_meal,
        llm_client_instance=llm_client_instance,
        db=db,
        user_id=int(user["id"]),
        allow_clarification=False,
        allow_web_lookup=True,
        web_lookup_fn=web_lookup_fn,
    )
    if not dish.get("ok"):
        return {"ok": False, "error": "dish_parse_failed"}

    advice = await get_daily_nutrition_advice(db, user, now_utc=now)
    targets = advice["targets"]
    rem = advice["remaining"]
    mindful = bool(targets.get("mindful_mode"))
    coach_mode = str(user.get("coach_mode") or "balanced").strip().lower()
    if coach_mode not in COACH_MODES:
        coach_mode = "balanced"
    mins_left = _mins_until_sleep_for_user(user, now)

    d_kcal = int(round(float(dish.get("kcal") or 0)))
    d_p = float(dish.get("protein_g") or 0)
    d_f = float(dish.get("fat_g") or 0)
    d_c = float(dish.get("carbs_g") or 0)

    rem_after_kcal = int(rem["kcal"]) - d_kcal
    rem_after_fat = round(float(rem["fat_g"]) - d_f, 1)

    if mindful:
        verdict = "fits"
    elif rem_after_kcal >= -60 and rem_after_fat >= -5:
        verdict = "fits"
    elif rem_after_kcal >= -260:
        verdict = "tight"
    else:
        verdict = "over"

    # Лучшие быстрые альтернативы из холодильника
    pantry_rows = await repo.list_pantry_items(db, int(user["id"]))
    pantry_names = [str(dict(r).get("name") or "") for r in pantry_rows if dict(r).get("name")][:10]

    src_note = ""
    if dish.get("lookup_source") == "openfoodfacts":
        src_note = " · Open Food Facts"
    elif dish.get("lookup_source") == "web_search":
        src_note = " · из меню в сети"
    elif dish.get("lookup_source") == "my_label":
        src_note = " · по твоей этикетке"

    v_badge = (
        "🧘 <b>Осознанный выбор</b>"
        if mindful
        else "✅ <b>Отлично вписывается</b>"
        if verdict == "fits"
        else "⚖ <b>На грани лимита</b>"
        if verdict == "tight"
        else "⚠️ <b>Будет перебор на сегодня</b>"
    )
    half_kcal = max(10, int(round(d_kcal * 0.5)))

    client = llm_client_instance or default_llm_client
    advice_body = ""
    if client.is_configured():
        directive = get_coach_directive(user)
        pantry_line = ", ".join(pantry_names) if pantry_names else "холодильник не заполнен"
        latest_w = await repo.get_latest_weight(db, int(user["id"]))
        wellness_row = await repo.get_wellness(db, int(user["id"]))
        meal_prof = str((dict(wellness_row) if wellness_row else {}).get("meal_profile") or "")
        cards = coach_knowledge.select_coach_principles(
            {
                "latest": latest_w or {},
                "nutrition_advice": {"targets": targets, "eaten": advice.get("eaten") or {}},
                "meal_profile": meal_prof,
                "weight_goal": str(user.get("weight_goal") or targets.get("goal") or "loss"),
            },
            f"{dish.get('title', '')} {'перебор' if verdict == 'over' else ''}",
            limit=1,
        )
        coach_hint = coach_knowledge.format_coach_knowledge_for_llm(cards)
        sys_p = (
            "Ты — коуч по питанию HiDL. Пользователь ещё НЕ съел блюдо, а примеряет его к своему дню. "
            "Ответь коротко (2–4 предложения, без ** и ## Markdown), опирайся ТОЛЬКО на точные цифры ниже и соблюдай стиль коуча."
        )
        usr_p = (
            f"{directive}\n"
            + (f"{coach_hint}\n" if coach_hint else "")
            + f"Кандидат: {dish['title']} — ~{d_kcal} ккал (Б {d_p:g}г / Ж {d_f:g}г / У {d_c:g}г).\n"
            f"Остаток дня ДО этого блюда: {rem['kcal']} ккал, белка добрать {rem['protein_g']} г, лимит жиров {rem['fat_g']} г.\n"
            f"До отбоя: {mins_left} мин.\n"
            f"Дома в холодильнике: {pantry_line}.\n"
            "Скажи, стоит ли есть целиком, взять полпорции или чем заменить/дополнить."
        )
        res = await client.complete_text(sys_p, usr_p, temperature=0.3, max_tokens=260)
        if res.ok and res.text.strip():
            from services.advice_service import _llm_text_to_safe_html
            advice_body = _llm_text_to_safe_html(res.text)

    if not advice_body:
        late_note = " Уже поздно (до отбоя меньше часа) — тяжёлое перед сном хуже ложится." if mins_left <= 65 else ""
        alt_note = f" Дома есть: {', '.join(pantry_names[:4])}." if pantry_names else ""
        if mindful:
            advice_body = (
                f"Порция потянет примерно на {d_kcal} ккал (Б {d_p:g}г).{late_note} "
                "Если хочется именно этого — съешь спокойно и без чувства вины, прислушиваясь к сытости."
            )
        elif verdict == "fits":
            p_plus = " Отлично помогает добрать белок." if rem["protein_g"] >= 20 and d_p >= 14 else ""
            advice_body = (
                f"Спокойно влезает в норму: останется ещё ~{max(0, rem_after_kcal)} ккал на сегодня.{p_plus}{late_note}"
            )
        elif verdict == "tight":
            if coach_mode == "gentle":
                advice_body = (
                    f"Вплотную к дневному лимиту ({rem_after_kcal:+} ккал). Если очень хочется — бери смело половину (~{half_kcal} ккал) или просто сделай более лёгкий приём позже.{late_note}"
                )
            elif coach_mode == "strict":
                advice_body = (
                    f"На грани ({rem_after_kcal:+} ккал{', по жирам перекос' if rem_after_fat < 0 else ''}). Лучше взять полпорции (~{half_kcal} ккал) и добрать белок.{alt_note}{late_note}"
                )
            else:
                advice_body = (
                    f"Почти впритык ({rem_after_kcal:+} ккал от остатка). Удобный компромисс — полпорции (~{half_kcal} ккал) или вариант с белком.{alt_note}{late_note}"
                )
        else:
            over_k = abs(rem_after_kcal)
            if coach_mode == "gentle":
                advice_body = (
                    f"Целиком даст +{over_k} ккал сверх нормы. Ничего страшного — можно взять полпорции (~{half_kcal} ккал) с овощами/водой или выбрать что-то полегче.{alt_note}{late_note}"
                )
            elif coach_mode == "strict":
                advice_body = (
                    f"Сейчас будет ощутимый перебор (+{over_k} ккал"
                    + (f", жиры +{abs(rem_after_fat):g} г" if rem_after_fat < -2 else "")
                    + f"). Тормози перед полной порцией — максимум полпорции (~{half_kcal} ккал) или белковая альтернатива.{alt_note}{late_note}"
                )
            else:
                advice_body = (
                    f"Целиком выйдет за дневной коридор (+{over_k} ккал). Если хочется — бери полпорции (~{half_kcal} ккал) или замени на вариант с высоким белком.{alt_note}{late_note}"
                )

    from html import escape as _esc
    header = (
        f"{v_badge}\n"
        f"🍽 <b>{_esc(dish['title'])}</b>{src_note}\n"
        f"🔥 ~<b>{d_kcal} ккал</b> (Б {d_p:g}г / Ж {d_f:g}г / У {d_c:g}г)\n"
    )
    if not mindful:
        header += f"📊 Сейчас осталось: <b>{rem['kcal']} ккал</b> · белок +{rem['protein_g']} г\n"

    return {
        "ok": True,
        "dish": dish,
        "verdict": verdict,
        "coach_mode": coach_mode,
        "remaining_before": rem,
        "advice_html": f"{header}\n{advice_body}".strip(),
    }


_SCALE_PARSE_PROMPT = (
    "Ты — парсер умных весов HiDL (Xiaomi Mi Fitness, Zepp Life, Mi Fit, Picooc, Huawei Health, Apple Health). "
    "Изображение может быть на русском ИЛИ английском языке, может быть длинным вертикальным отчётом, "
    "нарезанным на несколько пересекающихся фрагментов (тогда объедини показатели со всех фрагментов), "
    "или второй половиной экрана отчёта без цифры веса. "
    "Верни строго JSON:\n"
    "{\"is_scale_screen\": bool, \"weight\": float|null, \"body_fat_pct\": float|null, "
    "\"muscle_mass_kg\": float|null, \"water_pct\": float|null, \"visceral_fat\": float|null, \"bmi\": float|null}.\n"
    "Правила:\n"
    "- is_scale_screen: true ТОЛЬКО если на фото экран/отчёт приложения умных весов или дисплей весов; "
    "false для еды, чеков, этикеток, мемов и обычных фото.\n"
    "- weight: текущий вес в кг (НЕ бери 'Стандартный вес'/'Контроль веса'/'Standard weight'/'Fat-free body weight'). "
    "Если веса нет на экране, верни null.\n"
    "- body_fat_pct: процент жира (%, 'Body fat percentage'/'Процент жировой массы тела'; НЕ путай с 'Fat mass' в кг и висцеральным жиром).\n"
    "- muscle_mass_kg: мышцы строго в кг (предпочитай 'Muscle mass'/'Мышечная масса' в кг; если есть только 'Skeletal muscle mass'/'Масса скелетных мышц' в кг — бери её; "
    "если есть только %, и виден weight — посчитай weight*pct/100; НЕ бери 'Fat-free body weight'/'Безжировая масса' и 'Protein mass').\n"
    "- water_pct: % воды в организме ('Body water'/'Вода в организме' %; если указана только 'Body water mass' в кг и виден weight — посчитай water_kg/weight*100).\n"
    "- visceral_fat: висцеральный жир ('Visceral fat rating'/'Уровень висцерального жира').\n"
    "- bmi: ИМТ / BMI."
)


def _inspect_and_tile_scale_image(
    image_bytes: bytes,
    image_mime: str = "image/jpeg",
) -> Tuple[List[str], Dict[str, Any]]:
    """Нарезать длинные вертикальные отчёты (например, Xiaomi 'Поделиться') с перекрытием и сохранить детали."""
    b64_orig = base64.b64encode(image_bytes).decode("ascii")
    fallback_urls = [f"data:{image_mime};base64,{b64_orig}"]
    fallback_meta: Dict[str, Any] = {
        "tiled": False,
        "tile_count": 1,
        "width": 0,
        "height": 0,
        "is_tall": False,
        "is_low_res_tall": False,
    }
    try:
        from PIL import Image
    except Exception:
        return fallback_urls, fallback_meta

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            w, h = img.size
            if w <= 0 or h <= 0:
                return fallback_urls, fallback_meta
            aspect = h / float(w)
            is_tall = aspect >= 2.1
            is_low_res_tall = bool(is_tall and w < 350)
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)

            if is_tall:
                rgb = img.convert("RGB")
                n_tiles = min(4, max(2, int(round(aspect / 1.5))))
                base_step = h / float(n_tiles)
                overlap = int(round(base_step * 0.14))
                urls: List[str] = []
                for idx in range(n_tiles):
                    y0 = max(0, int(round(idx * base_step)) - (overlap if idx > 0 else 0))
                    y1 = min(h, int(round((idx + 1) * base_step)) + (overlap if idx < n_tiles - 1 else 0))
                    if y1 <= y0:
                        continue
                    crop = rgb.crop((0, y0, w, y1))
                    tw, th = crop.size
                    if w < 320:
                        crop = crop.resize((tw * 2, th * 2), resample=resample)
                    elif max(tw, th) > 1600:
                        scale = 1600.0 / float(max(tw, th))
                        crop = crop.resize(
                            (max(1, int(round(tw * scale))), max(1, int(round(th * scale)))),
                            resample=resample,
                        )
                    out_buf = io.BytesIO()
                    if crop.width < 450:
                        crop.save(out_buf, format="PNG", optimize=True)
                        tile_mime = "image/png"
                    else:
                        crop.save(out_buf, format="JPEG", quality=92)
                        tile_mime = "image/jpeg"
                    urls.append(
                        f"data:{tile_mime};base64,{base64.b64encode(out_buf.getvalue()).decode('ascii')}"
                    )
                if urls:
                    return urls, {
                        "tiled": len(urls) > 1,
                        "tile_count": len(urls),
                        "width": w,
                        "height": h,
                        "is_tall": True,
                        "is_low_res_tall": is_low_res_tall,
                    }

            if max(w, h) > 2000:
                rgb = img.convert("RGB")
                scale = 2000.0 / float(max(w, h))
                resized = rgb.resize(
                    (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                    resample=resample,
                )
                out_buf = io.BytesIO()
                resized.save(out_buf, format="JPEG", quality=92)
                return [
                    f"data:image/jpeg;base64,{base64.b64encode(out_buf.getvalue()).decode('ascii')}"
                ], {
                    "tiled": False,
                    "tile_count": 1,
                    "width": w,
                    "height": h,
                    "is_tall": False,
                    "is_low_res_tall": False,
                }

            return fallback_urls, {
                "tiled": False,
                "tile_count": 1,
                "width": w,
                "height": h,
                "is_tall": False,
                "is_low_res_tall": False,
            }
    except Exception:
        return fallback_urls, fallback_meta


def is_tall_scale_like_image(image_bytes: bytes) -> bool:
    """Вернуть True, если картинка похожа на длинный вертикальный экспорт приложения весов."""
    if not image_bytes:
        return False
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            w, h = img.size
            return w > 0 and (h / float(w)) >= 2.1
    except Exception:
        return False


def _validate_scale_payload(
    raw: Dict[str, Any],
    *,
    allow_partial: bool = False,
) -> Optional[Dict[str, Any]]:
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
    fat = _num("body_fat_pct", 3.0, 70.0)
    muscle = _num("muscle_mass_kg", 10.0, 120.0)
    water = _num("water_pct", 25.0, 80.0)
    visc = _num("visceral_fat", 1.0, 50.0)
    bmi = _num("bmi", 10.0, 70.0)
    if weight is None:
        if not allow_partial:
            return None
        if all(v is None for v in (fat, muscle, water, visc, bmi)):
            return None
    return {
        "weight": round(weight, 1) if weight is not None else None,
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

    w = _find(r"(?:вес(?:\s+(?:сейчас|сегодня|стал))?|weight|взвесил(?:ся|ась|ись)?|на\s+весах)\s*[:=-]?\s*(\d{2,3}(?:\.\d+)?)")
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
    require_scale_screen: bool = False,
    allow_partial: bool = False,
) -> Dict[str, Any]:
    """Распознать данные умных весов (вес, % жира, мышцы, вода, висц. жир, ИМТ) по фото/файлу или тексту."""
    client = llm_client_instance or default_llm_client
    clean_text = str(raw_text or "").strip()

    if image_bytes:
        if not client.is_configured():
            return {"ok": False, "error": "llm_not_configured"}
        data_urls, img_meta = _inspect_and_tile_scale_image(image_bytes, image_mime)
        prompt_parts = ["Извлеки все доступные показатели состава тела с отчёта умных весов."]
        if len(data_urls) > 1:
            prompt_parts.append(
                f"Это {len(data_urls)} перекрывающихся вертикальных фрагмента одного длинного отчёта (сверху вниз). Объедини все поля в один JSON."
            )
        if clean_text:
            prompt_parts.append(f"Подпись пользователя: {clean_text}")
        res = await client.complete_json(
            _SCALE_PARSE_PROMPT,
            " ".join(prompt_parts),
            temperature=0.0,
            max_tokens=500,
            image_data_urls=data_urls,
        )
        if not res.ok or not isinstance(res.data, dict):
            return {"ok": False, "error": res.error or "vision_parse_failed"}
        if require_scale_screen and res.data.get("is_scale_screen") is False:
            return {"ok": False, "error": "not_scale_screen"}
        valid = _validate_scale_payload(res.data, allow_partial=allow_partial)
        if not valid:
            return {"ok": False, "error": "no_scale_metrics"}
        extra_count = sum(
            1
            for k in ("body_fat_pct", "muscle_mass_kg", "water_pct", "visceral_fat", "bmi")
            if valid.get(k) is not None
        )
        low_res_warning = bool(img_meta.get("is_low_res_tall") and extra_count < 2)
        return {
            "ok": True,
            "used_llm": True,
            "source": "scale_photo",
            "partial": valid["weight"] is None,
            "tiled": bool(img_meta.get("tiled")),
            "tile_count": int(img_meta.get("tile_count") or 1),
            "low_res_warning": low_res_warning,
            **valid,
        }

    if not clean_text:
        return {"ok": False, "error": "empty_input"}

    offline = _parse_scale_offline(clean_text)
    if offline:
        has_extra = any(offline.get(k) is not None for k in ("body_fat_pct", "muscle_mass_kg", "water_pct", "visceral_fat", "bmi"))
        return {
            "ok": True,
            "used_llm": False,
            "source": "scale_text" if has_extra else "manual",
            "partial": False,
            "tiled": False,
            "tile_count": 1,
            "low_res_warning": False,
            **offline,
        }

    if client.is_configured():
        res = await client.complete_json(
            _SCALE_PARSE_PROMPT,
            f"Извлеки замеры весов из текста:\n{clean_text}",
            temperature=0.0,
            max_tokens=450,
        )
        if res.ok and isinstance(res.data, dict):
            valid = _validate_scale_payload(res.data, allow_partial=allow_partial)
            if valid:
                return {
                    "ok": True,
                    "used_llm": True,
                    "source": "scale_text",
                    "partial": valid["weight"] is None,
                    "tiled": False,
                    "tile_count": 1,
                    "low_res_warning": False,
                    **valid,
                }

    return {"ok": False, "error": "no_scale_metrics"}


def format_body_trend_feedback(trend: Dict[str, Any], goal: str = "loss") -> str:
    """Поддерживающий комментарий по тренду веса, воды, висц. жира и рекомпозиции."""
    w_d = trend.get("weight_delta")
    f_d = trend.get("fat_delta")
    m_d = trend.get("muscle_delta")
    water_d = trend.get("water_delta")
    visc_d = trend.get("visceral_delta")
    latest = trend.get("latest") if isinstance(trend.get("latest"), dict) else {}
    count = int(trend.get("count") or 0)
    visc_note = (
        f" Висцеральный жир уже снизился на {visc_d:g}!"
        if visc_d is not None and float(visc_d) <= -1.0
        else ""
    )

    if count < 2 or w_d is None:
        visc_now = latest.get("visceral_fat")
        if visc_now is not None and float(visc_now) >= 13.0:
            return (
                f"Точка отсчёта задана (висц. жир {float(visc_now):g} — отличный приоритет для снижения: "
                "он уходит быстрее всего на шагах и умеренном дефиците)."
            )
        return "Первые замеры задают точку отсчёта. Сравнивать будем спокойный тренд за 2–4 недели, а не разовые цифры."

    if f_d is not None and f_d <= -0.3 and w_d >= -0.4:
        muscle_note = f", а мышцы +{m_d:.1f} кг" if m_d is not None and m_d >= 0.2 else ""
        return (
            f"🔥 Отличная рекомпозиция: вес почти ровный ({w_d:+.1f} кг), "
            f"но жир уже ушёл на {f_d:.1f}%{muscle_note}! Именно так тело подтягивается.{visc_note}"
        )
    if w_d > 0.1 and ((water_d is not None and float(water_d) >= 0.3) or (f_d is not None and f_d <= 0.1)):
        return (
            f"💧 Плюс {w_d:+.1f} кг — это вода/гликоген или соль, а не жир (жир не вырос). "
            "Не режь калории с паники, просто держи воду и режим сна."
        )
    if goal in {"loss", "recomp"} and w_d <= -0.4:
        return f"📉 За {trend.get('days', 30)} дн. вес плавно снижается ({w_d:+.1f} кг) — здоровый темп без срывов.{visc_note}"
    if goal == "loss" and 0.1 < w_d <= 1.0:
        return "💧 Небольшой плюс за короткий отрезок чаще всего даёт вода, соль или поздний ужин — смотрим на длинный тренд."
    return f"📊 Тренд за {trend.get('days', 30)} дн.: {w_d:+.1f} кг. Держи ровный ритм без жёстких рамок.{visc_note}"


def format_scale_saved_summary(
    row: Dict[str, Any],
    trend: Dict[str, Any],
    targets: Dict[str, Any],
    goal: str = "loss",
    *,
    merged: bool = False,
    low_res_warning: bool = False,
    karma_note: str = "",
    action_plan: Optional[Dict[str, Any]] = None,
) -> str:
    """Собрать полный HTML-ответ после сохранения или дополнения замера весов."""
    bits = [f"вес <b>{float(row['weight']):.1f} кг</b>"]
    if row.get("body_fat_pct") is not None:
        bits.append(f"жир <b>{float(row['body_fat_pct']):.1f}%</b>")
    if row.get("muscle_mass_kg") is not None:
        bits.append(f"мышцы <b>{float(row['muscle_mass_kg']):.1f} кг</b>")
    if row.get("water_pct") is not None:
        bits.append(f"вода {float(row['water_pct']):.1f}%")
    if row.get("visceral_fat") is not None:
        bits.append(f"висц. жир <b>{float(row['visceral_fat']):g}</b>")
    if row.get("bmi") is not None:
        bits.append(f"ИМТ {float(row['bmi']):.1f}")

    head = "✅ <b>Дополнила свежий замер:</b>" if merged else "✅ <b>Записала замер:</b>"
    lines = [f"{head} {', '.join(bits)}."]
    if karma_note:
        lines.append(_html_mod.escape(karma_note))
    lines.append(_html_mod.escape(format_body_trend_feedback(trend, goal)))

    lean_note = ""
    if targets.get("lean_mass_kg") is not None and targets.get("bmr"):
        lean_note = f"\n🧬 Сухая масса: <b>{targets['lean_mass_kg']} кг</b> · базовый обмен ~<b>{targets['bmr']} ккал</b>"

    if not targets.get("mindful_mode"):
        lines.append(
            f"{lean_note}\n🔥 Ориентир КБЖУ: <b>{targets['kcal']} ккал</b> "
            f"(Б {targets['protein_g']}г / Ж {targets['fat_g']}г / У {targets['carbs_g']}г)."
        )

    if isinstance(action_plan, dict) and (action_plan.get("diagnosis") or action_plan.get("action_today")):
        lines.append("")
        lines.append(f"🎯 <b>Тренер HiDL:</b> {_html_mod.escape(str(action_plan.get('diagnosis') or ''))}")
        if action_plan.get("action_today"):
            action_label = str(action_plan.get("action_label") or "Что делать сейчас")
            lines.append(f"👉 <b>{_html_mod.escape(action_label)}:</b> {_html_mod.escape(str(action_plan['action_today']))}")

    if low_res_warning:
        lines.append(
            "\n💡 <i>Telegram сильно сжал длинный отчёт — мелкие цифры снизу могли размыться. "
            "В следующий раз отправь «Поделиться» из Xiaomi <b>как файл (без сжатия)</b> или двумя обычными скринами.</i>"
        )
    elif not merged and any(row.get(k) is None for k in ("muscle_mass_kg", "water_pct", "visceral_fat")):
        lines.append(
            "\n💡 <i>Если мышцы, вода и висцеральный жир на втором экране — просто скинь второй скрин следом, я сама допишу их к этому замеру.</i>"
        )
    return "\n".join(lines).strip()


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
    cards = coach_knowledge.select_coach_principles(
        {
            "latest": latest_weight or {},
            "week_workouts": week_stats,
            "weight_goal": goal,
            "activity_level": user.get("activity_level"),
        },
        "силовая тренировка новичок",
        limit=1,
    )
    coach_hint = coach_knowledge.format_coach_knowledge_for_llm(cards)
    sys_prompt = (
        "Ты — бережный персональный тренер HiDL. Составь простую, безопасную домашнюю тренировку БЕЗ резкого хардкора. "
        "Ответь строго JSON-объектом вида: "
        "{\"title\": str, \"kcal_burned\": int, \"steps\": [str]}. "
        "В steps 5–7 коротких понятных шагов с разминкой, подходами и заминкой."
    )
    user_prompt = (
        f"Длительность: {duration_min} мин. Акцент: {FOCUS_LABELS.get(eff_focus, eff_focus)}.\n"
        f"Инвентарь дома: {eq_human}.\n"
        f"{get_coach_directive(user)} Вес: {weight_txt}.\n"
        f"Недавние акценты за неделю: {', '.join(recent_areas) or 'давно не тренировался'}."
        + (f"\n{coach_hint}" if coach_hint else "")
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
