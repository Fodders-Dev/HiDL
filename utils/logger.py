import os
import sys
import traceback

# DEBUG_LOG можно включить через переменную окружения (1/true/yes).
# Если флаг не выставлен — log_debug молчит.
DEBUG_ENABLED = os.getenv("DEBUG_LOG", "0").lower() in {"1", "true", "yes"}


def log_info(msg: str):
    print(f"[INFO] {msg}", file=sys.stdout, flush=True)


def log_debug(msg: str):
    if DEBUG_ENABLED:
        print(f"[DEBUG] {msg}", file=sys.stdout, flush=True)


def log_error(msg: str, exc: Exception | None = None):
    print(f"[ERROR] {msg}", file=sys.stderr, flush=True)
    if exc:
        traceback.print_exc()
