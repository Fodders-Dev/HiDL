"""Простейший дымовой тест для ручного запуска.

Запускает импорты ключевых модулей, пробует прочитать базы знаний
и валится с ненулевым кодом при первой ошибке. Нужен для быстрой
проверки перед пушем/деплоем.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


MODULES = [
    "main",
    "handlers.menu",
    "handlers.finance",
    "handlers.custom_reminders",
    "handlers.home_tasks",
    "handlers.ask_mom",
    "handlers.focus_cafe",
    "handlers.routine_items",
    "handlers.talk",
    "utils.today",
    "utils.nl_parser",
    "utils.formatting",
    "db.repositories",
]


def import_modules() -> None:
    for name in MODULES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:  # noqa: PERF203
            if exc.name and "aiogram" in exc.name:
                raise RuntimeError("aiogram не установлен. Выполни pip install -r requirements.txt") from exc
            raise


def check_mom_tips() -> None:
    tips_path = ROOT / "data" / "mom_tips.json"
    data = json.loads(tips_path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise AssertionError("mom_tips.json пустой или неверный формат")
    required_fields = {"id", "title", "tags"}
    for tip in data[:5]:
        if not required_fields.issubset(set(tip.keys())):
            raise AssertionError(f"mom_tips.json: нет нужных полей в {tip}")
        if not isinstance(tip.get("body") or tip.get("tips"), list):
            raise AssertionError(f"mom_tips.json: ожидается список в body/tips у {tip.get('id')}")


def main() -> int:
    try:
        import_modules()
        check_mom_tips()
    except Exception as exc:  # noqa: BLE001
        print(f"[SMOKE] FAIL: {exc}", file=sys.stderr)
        return 1
    print("[SMOKE] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
