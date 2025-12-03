"""
Терминальный симулятор UI HiDL.

Режимы:

* interactive – ручной ввод команд и нажатий кнопок;
* scenario <name> – прогон заранее описанного сценария.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import string
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional, Sequence

from aiogram import Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    Update,
    User,
)

from hidl.app import AppContext, create_app
from tools.sim_scenarios import Scenario, list_scenarios


logger = logging.getLogger(__name__)

SIM_USER_ID = 424242
SIM_CHAT_ID = 424242


@dataclass
class KeyboardState:
    reply_buttons: List[str]
    inline_buttons: List[str]
    inline_callback_data: List[str]


class TerminalRenderer:
    """
    Простой рендерер сообщений HiDL в терминал.

    Он не делает сетевых вызовов, а только печатает текст и запоминает
    последние клавиатуры, чтобы интерактивный режим мог реагировать
    на выбор пользователя.
    """

    def __init__(self) -> None:
        self._last_keyboard = KeyboardState([], [], [])
        self._message_id = 0

    @property
    def last_keyboard(self) -> KeyboardState:
        return self._last_keyboard

    async def send_message(
        self,
        text: str,
        reply_markup: Optional[ReplyKeyboardMarkup | InlineKeyboardMarkup] = None,
    ) -> Message:
        self._message_id += 1
        message = Message(
            message_id=self._message_id,
            date=datetime.utcnow(),
            chat=Chat(id=SIM_CHAT_ID, type="private"),
            from_user=User(id=SIM_USER_ID, is_bot=True, first_name="HiDL"),
            text=text,
        )
        self._render(message, reply_markup)
        return message

    async def edit_message_text(
        self,
        message: Message,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> Message:
        edited = message.copy(update={"text": text})
        self._render(edited, reply_markup)
        return edited

    def _render(
        self,
        message: Message,
        reply_markup: Optional[ReplyKeyboardMarkup | InlineKeyboardMarkup],
    ) -> None:
        print("[HiDL]")
        print(message.text or "")
        print()

        reply_buttons: List[str] = []
        inline_buttons: List[str] = []
        inline_callback_data: List[str] = []

        if isinstance(reply_markup, ReplyKeyboardMarkup):
            for row in reply_markup.keyboard:
                for button in row:
                    reply_buttons.append(button.text)
        elif isinstance(reply_markup, InlineKeyboardMarkup):
            for row in reply_markup.inline_keyboard:
                for button in row:
                    inline_buttons.append(button.text or "")
                    inline_callback_data.append(button.callback_data or "")

        # Reply‑кнопки.
        print("Reply‑кнопки:")
        if reply_buttons:
            for i, label in enumerate(reply_buttons, start=1):
                print(f"  ({i}) {label}")
        else:
            print("  — нет —")

        # Inline‑кнопки.
        print()
        print("Inline‑кнопки:")
        if inline_buttons:
            for idx, label in enumerate(inline_buttons):
                letter = string.ascii_lowercase[idx]
                print(f"  ({letter}) {label}")
        else:
            print("  — нет —")

        print()

        self._last_keyboard = KeyboardState(
            reply_buttons=reply_buttons,
            inline_buttons=inline_buttons,
            inline_callback_data=inline_callback_data,
        )


class TerminalBotProxy:
    """
    Обёртка над Bot, которая перехватывает отправку сообщений и
    использует TerminalRenderer.

    Важно: только небольшой подмножество методов бота покрыто, которого
    достаточно для симуляции основных сценариев (send_message /
    edit_message_text).
    """

    def __init__(self, ctx: AppContext, renderer: TerminalRenderer) -> None:
        self._ctx = ctx
        self._renderer = renderer
        self.id = ctx.bot.id  # для совместимости с aiogram
        # некоторые хендлеры могут читать parse_mode
        self.parse_mode = "HTML"

    # Методы, которые вызывают хендлеры через message.answer / edit_text.

    async def send_message(self, chat_id: int, text: str, **kwargs) -> Message:  # type: ignore[override]
        reply_markup = kwargs.get("reply_markup")
        return await self._renderer.send_message(text=text, reply_markup=reply_markup)

    async def edit_message_text(self, text: str, chat_id: int | None = None, message_id: int | None = None, **kwargs) -> Message:  # type: ignore[override]
        # В терминальном режиме достаточно обновить текст последнего сообщения.
        dummy = Message(
            message_id=message_id or 0,
            date=datetime.utcnow(),
            chat=Chat(id=SIM_CHAT_ID, type="private"),
            from_user=User(id=SIM_USER_ID, is_bot=True, first_name="HiDL"),
            text=text,
        )
        reply_markup = kwargs.get("reply_markup")
        return await self._renderer.edit_message_text(dummy, text=text, reply_markup=reply_markup)

    async def answer_callback_query(self, *_, **__) -> None:  # type: ignore[override]
        # Сообщения об обработке callback в терминале не нужны.
        return None

    async def __call__(self, method: TelegramMethod[Any]) -> Any:  # type: ignore[override]
        """
        Aiogram ожидает, что Bot является вызываемым объектом и умеет
        исполнять TelegramMethod. Здесь мы перехватываем только
        используемые методы (SendMessage, EditMessageText,
        AnswerCallbackQuery) и отправляем их в TerminalRenderer.
        """

        if isinstance(method, SendMessage):
            return await self.send_message(
                chat_id=method.chat_id,
                text=method.text,
                reply_markup=method.reply_markup,
            )

        if isinstance(method, EditMessageText):
            dummy = Message(
                message_id=method.message_id or 0,
                date=datetime.utcnow(),
                chat=build_chat(),
                from_user=User(id=SIM_USER_ID, is_bot=True, first_name="HiDL"),
                text=method.text or "",
            )
            return await self.edit_message_text(
                message=dummy,
                text=method.text or "",
                reply_markup=method.reply_markup,
            )

        if isinstance(method, AnswerCallbackQuery):
            return await self.answer_callback_query()

        logger.warning("TerminalBotProxy: unsupported method %r", method)
        return None


def build_user() -> User:
    return User(id=SIM_USER_ID, is_bot=False, first_name="SimUser")


def build_chat() -> Chat:
    return Chat(id=SIM_CHAT_ID, type="private")


async def _feed_message(dp: Dispatcher, bot: TerminalBotProxy, text: str) -> None:
    message = Message(
        message_id=1,
        date=datetime.utcnow(),
        chat=build_chat(),
        from_user=build_user(),
        text=text,
    )
    update = Update(update_id=0, message=message)
    await dp.feed_update(bot, update)


async def _feed_callback(dp: Dispatcher, bot: TerminalBotProxy, callback_data: str) -> None:
    message = Message(
        message_id=1,
        date=datetime.utcnow(),
        chat=build_chat(),
        from_user=build_user(),
        text="",
    )
    callback = CallbackQuery(id="1", from_user=build_user(), chat_instance="sim", data=callback_data, message=message)
    update = Update(update_id=0, callback_query=callback)
    await dp.feed_update(bot, update)


async def interactive(ctx: AppContext) -> None:
    renderer = TerminalRenderer()
    bot = TerminalBotProxy(ctx, renderer)
    dp = ctx.dp

    print("HiDL terminal simulator (interactive). Введи текст или номер/букву кнопки.")
    print("Пример: /start или 1 или a. Ctrl+C чтобы выйти.\n")

    loop = asyncio.get_running_loop()
    while True:
        try:
            user_input = await loop.run_in_executor(None, input, "> ")
        except (EOFError, KeyboardInterrupt):
            print("\nВыход из симулятора.")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        kb = renderer.last_keyboard

        # Reply‑кнопки по числам.
        if user_input.isdigit():
            idx = int(user_input) - 1
            if 0 <= idx < len(kb.reply_buttons):
                text = kb.reply_buttons[idx]
                await _feed_message(dp, bot, text)
                continue

        # Inline‑кнопки по буквам.
        if len(user_input) == 1 and user_input in string.ascii_lowercase:
            idx = string.ascii_lowercase.index(user_input)
            if 0 <= idx < len(kb.inline_callback_data):
                data = kb.inline_callback_data[idx]
                await _feed_callback(dp, bot, data)
                continue

        # Обычный текст.
        await _feed_message(dp, bot, user_input)


async def run_scenario(ctx: AppContext, name: str) -> None:
    scenarios = list_scenarios()
    if name not in scenarios:
        print(f"Неизвестный сценарий: {name}")
        print("Доступные сценарии:", ", ".join(sorted(scenarios.keys())))
        return

    renderer = TerminalRenderer()
    bot = TerminalBotProxy(ctx, renderer)
    dp = ctx.dp

    scenario: Scenario = scenarios[name]
    print(f"=== Сценарий: {name} ===\n")

    for step in scenario:
        if step.type == "message" and step.text is not None:
            print(f"[USER] {step.text}")
            await _feed_message(dp, bot, step.text)
        elif step.type == "button_reply" and step.label is not None:
            print(f"[USER REPLY] {step.label}")
            await _feed_message(dp, bot, step.label)
        elif step.type == "button_inline" and step.label is not None:
            # Поиск callback по подписи.
            kb = renderer.last_keyboard
            if step.label in kb.inline_buttons:
                idx = kb.inline_buttons.index(step.label)
                data = kb.inline_callback_data[idx]
                print(f"[USER INLINE] {step.label}")
                await _feed_callback(dp, bot, data)
            else:
                print(f"[WARN] Не нашли inline‑кнопку с текстом '{step.label}', пропускаю шаг.")


async def main_async(args: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(description="HiDL terminal UI simulator")
    subparsers = parser.add_subparsers(dest="mode")

    subparsers.add_parser("interactive", help="интерактивный режим")

    scenario_parser = subparsers.add_parser("scenario", help="прогнать заранее описанный сценарий")
    scenario_parser.add_argument("name", help="имя сценария")

    parsed = parser.parse_args(list(args))

    ctx = await create_app(test_mode=True)

    if parsed.mode == "interactive" or parsed.mode is None:
        await interactive(ctx)
    elif parsed.mode == "scenario":
        await run_scenario(ctx, parsed.name)
    else:
        parser.print_help()


def main() -> None:
    asyncio.run(main_async(__import__("sys").argv[1:]))


if __name__ == "__main__":
    main()
