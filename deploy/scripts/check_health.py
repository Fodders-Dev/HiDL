"""Non-secret preflight and post-deploy health check CLI for HiDL."""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import get_llm_settings, get_stt_settings
from llm_client import client as llm_client

CORE_TABLES = ("users", "routines", "custom_reminders", "day_plans", "day_plan_items")
SPRINT9_TABLES = ("inbox_messages", "journal_entries", "inbox_actions")
SENSITIVE_ENV_KEYS = (
    "BOT_TOKEN",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "GROQ_API_KEY",
    "QWEN_API_KEY",
    "LLM_API_KEY",
    "STT_API_KEY",
)


def check_database(db_path: Path, allow_pre_migration: bool = False) -> tuple[bool, list[str]]:
    lines: list[str] = []
    if not db_path.exists():
        lines.append(f"DB_ERROR: file not found at {db_path}")
        return False, lines

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check;").fetchone()[0])
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
            ).fetchall()
        }
        missing_core = [t for t in CORE_TABLES if t not in tables]
        missing_s9 = [t for t in SPRINT9_TABLES if t not in tables]
        users_cnt = conn.execute("SELECT COUNT(*) FROM users;").fetchone()[0] if "users" in tables else 0
    finally:
        conn.close()

    lines.append(
        f"DB_CHECK: path={db_path} size={db_path.stat().st_size} integrity={integrity} users={users_cnt}"
    )
    if integrity.lower() != "ok":
        lines.append("DB_ERROR: integrity_check != ok")
        return False, lines
    if missing_core:
        lines.append(f"DB_ERROR: missing core tables: {missing_core}")
        return False, lines
    if missing_s9:
        if allow_pre_migration:
            lines.append(f"DB_PRE_MIGRATION: Sprint 9 tables will be created on boot: {missing_s9}")
        else:
            lines.append(f"DB_ERROR: missing Sprint 9 tables: {missing_s9}")
            return False, lines
    else:
        lines.append("DB_SCHEMA: Sprint 9 tables present")
    return True, lines


def print_env_summary() -> None:
    presence = {k: ("SET" if bool(os.getenv(k, "").strip()) else "EMPTY") for k in SENSITIVE_ENV_KEYS}
    llm_cfg = get_llm_settings()
    stt_cfg = get_stt_settings()
    print("ENV_KEYS:", presence)
    print(
        "LLM_CONFIG:",
        {
            "enabled": llm_cfg.enabled,
            "primary_provider": llm_cfg.primary.provider,
            "primary_model": llm_cfg.primary.model or "(empty)",
            "primary_ready": llm_cfg.primary.is_ready,
            "failover_provider": llm_cfg.failover.provider if llm_cfg.failover else "none",
            "failover_model": llm_cfg.failover.model if llm_cfg.failover else "none",
            "timeout_sec": llm_cfg.timeout_sec,
            "max_tokens": llm_cfg.max_tokens,
        },
    )
    print(
        "STT_CONFIG:",
        {
            "mode": stt_cfg.provider,
            "active_routes": [r.provider for r in stt_cfg.routes],
        },
    )


async def probe_llm() -> bool:
    t0 = time.perf_counter()
    res = await llm_client.complete_json(
        'Return strict JSON object: {"items":[{"kind":"idea","title":"..."}]}',
        "Хочу когда-нибудь посадить дерево",
        temperature=0.1,
        max_tokens=150,
    )
    ms = int((time.perf_counter() - t0) * 1000)
    items_cnt = len(res.data.get("items", [])) if isinstance(res.data, dict) else 0
    print(
        f"LLM_PROBE: ok={res.ok} provider={res.provider} model={res.model} "
        f"latency_ms={ms} items={items_cnt} err={res.error}"
    )
    return bool(res.ok)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Non-secret health check for HiDL.")
    parser.add_argument("--db", help="Path to SQLite DB file to verify.")
    parser.add_argument(
        "--allow-pre-migration",
        action="store_true",
        help="Allow DB check before first startup creates Sprint 9 tables.",
    )
    parser.add_argument("--check-env", action="store_true", help="Print non-secret env summary.")
    parser.add_argument("--probe-llm", action="store_true", help="Run a live non-secret LLM JSON probe.")
    args = parser.parse_args(argv)

    ok = True
    if args.db:
        db_ok, lines = check_database(Path(args.db), allow_pre_migration=args.allow_pre_migration)
        for line in lines:
            print(line)
        ok = ok and db_ok

    if args.check_env:
        print_env_summary()

    if args.probe_llm:
        llm_ok = asyncio.run(probe_llm())
        ok = ok and llm_ok

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
