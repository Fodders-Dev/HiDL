from typing import Any, Mapping


def format_quantity(amount: float | int | None, unit: str | None) -> str:
    """
    Вернуть человекочитаемую строку количества: \"100 г\", \"2 шт\", \"1.5 л\".

    Используется для отображения запасов на кухне.
    """
    if amount is None:
        amount = 0
    try:
        value = float(amount)
    except (TypeError, ValueError):
        value = 0.0
    u = (unit or "").strip()
    # normalize common units for RU UI
    u_map = {"g": "г", "гр": "г", "kg": "кг", "ml": "мл", "l": "л"}
    u_norm = u_map.get(u.lower(), u)
    # Для красивого вывода избегаем длинных хвостов после запятой
    if abs(value - int(value)) < 1e-6:
        num = f"{int(value)}"
    else:
        num = f"{value:.2g}"
    return f"{num} {u_norm or 'шт'}".strip()


def is_low(item: Mapping[str, Any]) -> bool:
    """
    Определить, что продукт почти закончился.

    Приоритет: если есть user-defined low_threshold — используем его.
    Иначе применяем простые эвристики по единице измерения.
    """
    try:
        amount = float(item.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0

    low_threshold = item.get("low_threshold")
    if low_threshold is not None:
        try:
            threshold = float(low_threshold)
        except (TypeError, ValueError):
            threshold = 0.0
        return amount <= threshold

    unit = (item.get("unit") or "шт").lower()

    # Базовые пороги по умолчанию
    if unit in {"g", "гр", "ml", "мл"}:
        return amount <= 100  # меньше стакана/маленькой порции
    if unit in {"kg", "кг", "l", "л"}:
        return amount <= 0.25  # меньше четверти упаковки

    # Для штук — когда осталась одна или меньше
    return amount <= 1
