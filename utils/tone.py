from typing import Literal

Tone = Literal["soft", "neutral", "pushy"]


def tone_message(tone: Tone, base: str) -> str:
    """Adjust message by tone."""
    if tone == "soft":
        return base + "\n(Если не сейчас — ок, сделаешь позже. Ты молодец, что стараешься.)"
    if tone == "pushy":
        return base + "\nСделай сейчас, не откладывай."
    return base


def tone_short_ack(tone: Tone, action: str) -> str:
    if tone == "soft":
        return f"Супер, закрыто: {action}. Продолжай в своём темпе."
    if tone == "pushy":
        return f"Отмечено: {action}. Не сбавляй темп."
    return f"Отмечено: {action}."


def tone_ack(tone: Tone, text: str) -> str:
    """Женский тон короткого подтверждения."""
    if tone == "soft":
        return f"Готово: {text}. Можно выдохнуть."
    if tone == "pushy":
        return f"Готово: {text}. Дальше по плану!"
    return f"Готово: {text}."


def tone_error(tone: Tone, text: str) -> str:
    """Женский тон для ошибок/подсказок."""
    if tone == "soft":
        return f"Похоже, что-то пошло не так: {text}. Давай попробуем ещё раз?"
    if tone == "pushy":
        return f"Не сработало: {text}. Попробуй снова."
    return f"Не вышло: {text}."
