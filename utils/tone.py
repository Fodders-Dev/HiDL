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
        return f"Супер, ты закрыл(а) {action}. Продолжай в своём темпе."
    if tone == "pushy":
        return f"Отметил {action}. Не сбавляй темп."
    return f"Отметил {action}."
