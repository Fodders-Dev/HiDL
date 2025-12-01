"""Плейсхолдер для будущего LLM-клиента.

Сейчас возвращает заглушку, но структура позволяет быстро подключить внешнюю
модель (OpenAI, локальная, итд).
"""

from __future__ import annotations

from typing import Optional


class LLMClient:
    def __init__(self, model: str = "stub"):
        self.model = model

    async def ask(self, prompt: str, user_context: Optional[str] = None) -> str:
        """Вернуть ответ модели. Сейчас — заглушка."""
        base = "Я пока не подключена к нейросети, но готовлюсь к этому."
        if user_context:
            return f"{base}\nКонтекст: {user_context[:120]}"
        return base


client = LLMClient()
