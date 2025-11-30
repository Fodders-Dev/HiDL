import sys
import traceback


def log_info(msg: str):
    print(f"[INFO] {msg}", file=sys.stdout, flush=True)


def log_error(msg: str, exc: Exception | None = None):
    print(f"[ERROR] {msg}", file=sys.stderr, flush=True)
    if exc:
        traceback.print_exc()
