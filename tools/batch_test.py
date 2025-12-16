"""
Batch Test Runner for HiDL Bot.

Runs predefined test scenarios and outputs results to a file.
Designed for automated testing by AI assistants.

Usage:
    python tools/batch_test.py [scenario_name]
    python tools/batch_test.py all  # Run all scenarios
"""

from __future__ import annotations

import sys
import os
sys.path.append(os.getcwd())

import asyncio
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any

from tools.sim_scenarios import list_scenarios, ScenarioStep


# Output file for test results
RESULTS_FILE = Path(__file__).resolve().parents[1] / "test_results.md"


class TestResult:
    def __init__(self, scenario_name: str):
        self.scenario_name = scenario_name
        self.steps: List[Dict[str, Any]] = []
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.success = True

    def add_step(self, user_action: str, bot_response: str, buttons: List[str]):
        self.steps.append({
            "user": user_action,
            "bot": bot_response,
            "buttons": buttons,
        })

    def add_error(self, error: str):
        self.errors.append(error)
        self.success = False

    def add_warning(self, warning: str):
        self.warnings.append(warning)


class BatchTestRenderer:
    """Captures bot output instead of printing to console."""

    def __init__(self):
        self._last_response = ""
        self._last_buttons: List[str] = []
        self._inline_callbacks: Dict[str, str] = {}  # button text -> callback_data
        self._message_id = 0

    @property
    def last_response(self) -> str:
        return self._last_response

    @property
    def last_buttons(self) -> List[str]:
        return self._last_buttons

    async def send_message(self, text: str, reply_markup=None):
        from aiogram.types import (
            Chat, Message, User, ReplyKeyboardMarkup, InlineKeyboardMarkup
        )
        self._message_id += 1
        self._last_response = text or ""
        self._last_buttons = []
        self._inline_callbacks = {}

        if isinstance(reply_markup, ReplyKeyboardMarkup):
            for row in reply_markup.keyboard:
                for btn in row:
                    self._last_buttons.append(f"[R] {btn.text}")
        elif isinstance(reply_markup, InlineKeyboardMarkup):
            for row in reply_markup.inline_keyboard:
                for btn in row:
                    self._last_buttons.append(f"[I] {btn.text}")
                    # Capture callback_data
                    if btn.callback_data:
                        self._inline_callbacks[btn.text] = btn.callback_data

        return Message(
            message_id=self._message_id,
            date=datetime.utcnow(),
            chat=Chat(id=1, type="private"),
            from_user=User(id=1, is_bot=True, first_name="HiDL"),
            text=text,
        )

    async def edit_message_text(self, text: str, **kwargs):
        self._last_response = text or ""
        return await self.send_message(text, kwargs.get("reply_markup"))

    async def edit_message_reply_markup(self, **kwargs):
        return await self.send_message("(markup edit)", kwargs.get("reply_markup"))

    async def delete_message(self, chat_id: int, message_id: int):
        return True

    async def answer_callback_query(self, *args, **kwargs):
        return None


class BatchTestBot:
    """Bot proxy for batch testing."""

    def __init__(self, ctx, renderer: BatchTestRenderer):
        self._ctx = ctx
        self._renderer = renderer
        self.id = ctx.bot.id
        self.parse_mode = "HTML"

    async def send_message(self, chat_id: int, text: str, **kwargs):
        return await self._renderer.send_message(text, kwargs.get("reply_markup"))

    async def edit_message_text(self, text: str, **kwargs):
        return await self._renderer.edit_message_text(text, **kwargs)

    async def edit_message_reply_markup(self, **kwargs):
        return await self._renderer.edit_message_reply_markup(**kwargs)

    async def delete_message(self, chat_id: int, message_id: int):
        return await self._renderer.delete_message(chat_id, message_id)

    async def answer_callback_query(self, *args, **kwargs):
        return await self._renderer.answer_callback_query(*args, **kwargs)

    async def __call__(self, method):
        from aiogram.methods import (
            AnswerCallbackQuery, DeleteMessage, EditMessageReplyMarkup,
            EditMessageText, SendMessage
        )

        if isinstance(method, SendMessage):
            return await self.send_message(
                chat_id=method.chat_id,
                text=method.text,
                reply_markup=method.reply_markup,
            )
        if isinstance(method, EditMessageText):
            return await self.edit_message_text(
                text=method.text or "",
                reply_markup=method.reply_markup,
            )
        if isinstance(method, EditMessageReplyMarkup):
            return await self.edit_message_reply_markup(
                reply_markup=method.reply_markup,
            )
        if isinstance(method, DeleteMessage):
            return await self.delete_message(method.chat_id, method.message_id)
        if isinstance(method, AnswerCallbackQuery):
            return await self.answer_callback_query()
        return None


async def run_scenario(scenario_name: str, ctx) -> TestResult:
    """Run a single scenario and return results."""
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    result = TestResult(scenario_name)
    scenarios = list_scenarios()

    if scenario_name not in scenarios:
        result.add_error(f"Unknown scenario: {scenario_name}")
        return result

    renderer = BatchTestRenderer()
    bot = BatchTestBot(ctx, renderer)
    dp = ctx.dp
    
    # Store callback_data mapping: button text -> callback_data
    inline_callbacks: Dict[str, str] = {}

    scenario = scenarios[scenario_name]
    message_id_counter = 100

    for step in scenario:
        try:
            user_action = ""
            if step.type == "message" and step.text:
                user_action = f"msg: {step.text}"
                message_id_counter += 1
                message = Message(
                    message_id=message_id_counter,
                    date=datetime.utcnow(),
                    chat=Chat(id=1, type="private"),
                    from_user=User(id=424242, is_bot=False, first_name="Tester"),
                    text=step.text,
                )
                update = Update(update_id=0, message=message)
                await dp.feed_update(bot, update)
                
                # Update inline_callbacks from last response
                inline_callbacks = _extract_inline_callbacks(renderer)

            elif step.type == "button_reply" and step.label:
                user_action = f"reply_btn: {step.label}"
                message_id_counter += 1
                message = Message(
                    message_id=message_id_counter,
                    date=datetime.utcnow(),
                    chat=Chat(id=1, type="private"),
                    from_user=User(id=424242, is_bot=False, first_name="Tester"),
                    text=step.label,
                )
                update = Update(update_id=0, message=message)
                await dp.feed_update(bot, update)
                
                # Update inline_callbacks from last response
                inline_callbacks = _extract_inline_callbacks(renderer)

            elif step.type == "button_inline" and step.label:
                user_action = f"inline_btn: {step.label}"
                
                # Find callback_data by label
                callback_data = None
                for btn_text, cb_data in inline_callbacks.items():
                    if step.label in btn_text:
                        callback_data = cb_data
                        break
                
                if not callback_data:
                    result.add_warning(f"Inline button '{step.label}' not found in last response")
                else:
                    # Create and send CallbackQuery
                    message_id_counter += 1
                    callback_message = Message(
                        message_id=message_id_counter - 1,  # Refers to the message with buttons
                        date=datetime.utcnow(),
                        chat=Chat(id=1, type="private"),
                        from_user=User(id=1, is_bot=True, first_name="HiDL"),
                        text=renderer.last_response,
                    )
                    callback_query = CallbackQuery(
                        id="test_callback_" + str(message_id_counter),
                        chat_instance="test",
                        from_user=User(id=424242, is_bot=False, first_name="Tester"),
                        message=callback_message,
                        data=callback_data,
                    )
                    update = Update(update_id=0, callback_query=callback_query)
                    await dp.feed_update(bot, update)
                    
                    # Update inline_callbacks from last response
                    inline_callbacks = _extract_inline_callbacks(renderer)

            result.add_step(user_action, renderer.last_response, renderer.last_buttons.copy())

        except Exception as e:
            result.add_error(f"Step failed: {step} - {e}")

    return result


def _extract_inline_callbacks(renderer: BatchTestRenderer) -> Dict[str, str]:
    """Extract button text -> callback_data mapping from last captured markup."""
    # This is a limitation - we need to capture callback_data when sending message
    # For now, return empty dict - we need to enhance BatchTestRenderer
    return getattr(renderer, '_inline_callbacks', {})


def format_results(results: List[TestResult]) -> str:
    """Format test results as markdown."""
    lines = [
        f"# HiDL Bot Test Results",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    passed = sum(1 for r in results if r.success)
    total = len(results)
    lines.append(f"**Summary:** {passed}/{total} scenarios passed")
    lines.append("")

    for result in results:
        status = "✅" if result.success else "❌"
        lines.append(f"## {status} {result.scenario_name}")
        lines.append("")

        if result.errors:
            lines.append("### Errors")
            for err in result.errors:
                lines.append(f"- {err}")
            lines.append("")

        if result.warnings:
            lines.append("### Warnings")
            for warn in result.warnings:
                lines.append(f"- {warn}")
            lines.append("")

        lines.append("### Steps")
        for i, step in enumerate(result.steps, 1):
            lines.append(f"**{i}. {step['user']}**")
            # Truncate long responses
            response = step['bot'][:200] + "..." if len(step['bot']) > 200 else step['bot']
            lines.append(f"> {response}")
            if step['buttons']:
                lines.append(f"> Buttons: {', '.join(step['buttons'][:5])}")
            lines.append("")

    return "\n".join(lines)


async def main():
    from hidl.app import create_app
    
    scenarios_to_run = sys.argv[1:] if len(sys.argv) > 1 else ["onboarding"]

    if "all" in scenarios_to_run:
        scenarios_to_run = list(list_scenarios().keys())

    print(f"Running {len(scenarios_to_run)} scenario(s)...")

    # Для batch-тестов нам важнее предсказуемость, чем файл на диске.
    # Используем in-memory БД, чтобы не упираться в lock'и Windows.
    os.environ["HIDL_SIM_DB_URL"] = "sqlite:///:memory:"

    # Create app once
    ctx = await create_app(test_mode=True)
    try:
        results = []
        for name in scenarios_to_run:
            print(f"  - {name}...", end=" ")
            result = await run_scenario(name, ctx)
            results.append(result)
            print("OK" if result.success else "FAIL")

        # Write results to file
        output = format_results(results)
        RESULTS_FILE.write_text(output, encoding="utf-8")
        print(f"\nResults written to: {RESULTS_FILE}")
    finally:
        # Закрываем БД/сессию бота, иначе процесс может "зависнуть" после завершения.
        try:
            await ctx.db_conn.close()
        except Exception:
            pass
        try:
            await ctx.bot.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
