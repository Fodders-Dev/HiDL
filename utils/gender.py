"""
Хелперы для персонализации по полу пользователя.

Поле user["gender"] может быть: "male", "female", "neutral"
"""

from typing import Dict, Any, Optional


def g(user: Dict[str, Any], male: str, female: str, neutral: Optional[str] = None) -> str:
    """
    Выбрать форму слова в зависимости от пола пользователя.
    
    Примеры:
        g(user, "сделал", "сделала")  # → "сделала" для женщины
        g(user, "ел", "ела")          # → "ел" для мужчины  
        g(user, "готов", "готова")    # → "готов" для neutral (default male form)
    
    Args:
        user: Словарь с данными пользователя (должен содержать ключ "gender")
        male: Форма слова для мужского рода
        female: Форма слова для женского рода
        neutral: Опциональная форма для нейтрального рода (по умолчанию = male)
    
    Returns:
        Выбранная форма слова
    """
    gender = user.get("gender", "neutral") if user else "neutral"
    
    if gender == "female":
        return female
    elif gender == "male":
        return male
    else:
        # Нейтральный пол — используем neutral форму или мужскую по умолчанию
        return neutral if neutral else male


def gender_verb(user: Dict[str, Any], verb_base: str) -> str:
    """
    Автоматически склонять глаголы прошедшего времени по полу.
    
    Примеры:
        gender_verb(user, "поел")     # → "поела" для женщины
        gender_verb(user, "сделал")   # → "сделала" для женщины
        gender_verb(user, "купил")    # → "купила" для женщины
    
    Работает только с простыми глаголами, оканчивающимися на -л.
    """
    gender = user.get("gender", "neutral") if user else "neutral"
    
    if gender == "female" and verb_base.endswith("л"):
        return verb_base + "а"
    
    return verb_base


# Готовые фразы для частых случаев
PHRASES = {
    "ate": {"male": "ел", "female": "ела", "neutral": "ел(а)"},
    "did": {"male": "сделал", "female": "сделала", "neutral": "сделал(а)"},
    "ready": {"male": "готов", "female": "готова", "neutral": "готов(а)"},
    "slept": {"male": "выспался", "female": "выспалась", "neutral": "выспался(ась)"},
    "forgot": {"male": "забыл", "female": "забыла", "neutral": "забыл(а)"},
}


def phrase(user: Dict[str, Any], key: str) -> str:
    """
    Получить готовую фразу с учётом пола.
    
    Примеры:
        phrase(user, "ate")   # → "ела" для женщины
        phrase(user, "ready") # → "готов" для мужчины
    """
    if key not in PHRASES:
        return key
    
    gender = user.get("gender", "neutral") if user else "neutral"
    forms = PHRASES[key]
    
    return forms.get(gender, forms.get("neutral", forms.get("male", key)))


def done_button_label(user: Dict[str, Any]) -> str:
    """
    Короткая подпись для кнопки подтверждения (без "Сделал(а)").

    - female -> "Сделала ✅"
    - male -> "Сделал ✅"
    - neutral/unknown -> "Готово ✅"
    """
    gender = user.get("gender", "neutral") if user else "neutral"
    if gender == "female":
        return "Сделала ✅"
    if gender == "male":
        return "Сделал ✅"
    return "Готово ✅"


def button_label(
    user: Dict[str, Any],
    male: str,
    female: str,
    neutral: str = "Готово ✅",
) -> str:
    """Короткая подпись для кнопок с учётом пола (без скобок по умолчанию)."""
    gender = user.get("gender", "neutral") if user else "neutral"
    if gender == "female":
        return female
    if gender == "male":
        return male
    return neutral
