"""Release-readiness tests: SQLite WAL persistence, backup/migration, config scheme, Russian kind aliases."""

from __future__ import annotations

import datetime
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import get_llm_settings, get_stt_settings
from db import repositories as repo
from db.database import connect, init_db
from deploy.scripts import check_health
from scripts.backup_sqlite import create_verified_backup, verify_migration_on_copy
from services import inbox_service
from services.inbox_parser import validate_and_normalize_llm_items


class ReleaseReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_1_sqlite_wal_and_restart_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = Path(tmpdir) / "prod_sim.db"
            db_url = f"sqlite:///{db_file.as_posix()}"

            # 1st boot: initialize DB, check WAL mode, write records
            conn1 = await connect(db_url)
            try:
                await init_db(conn1)
                mode = (await (await conn1.execute("PRAGMA journal_mode;")).fetchone())[0]
                self.assertEqual(str(mode).lower(), "wal")

                uid = await repo.create_user(
                    conn1,
                    telegram_id=900101,
                    name="ПродТест",
                    timezone="Europe/Moscow",
                    wake_up_time="08:00",
                    sleep_time="23:30",
                    gender="male",
                )
                user = dict(await repo.get_user(conn1, uid))
                with patch.dict(os.environ, {"LLM_ENABLED": "0"}, clear=False):
                    outcome = await inbox_service.capture_and_process_text(
                        conn1,
                        user,
                        chat_id=900101,
                        message_id=10,
                        text="Надо купить гречку. Сегодня устал. Хочу когда-нибудь написать книгу. Напомни завтра в 12:00 позвонить маме.",
                        now_utc=datetime.datetime(2026, 11, 19, 9, 0, 0),
                    )
                self.assertEqual(outcome.status, "processed")
            finally:
                await conn1.close()

            # 2nd boot (simulating systemd restart): reopen same file & re-run init_db
            conn2 = await connect(db_url)
            try:
                await init_db(conn2)
                integrity = (await (await conn2.execute("PRAGMA integrity_check;")).fetchone())[0]
                self.assertEqual(str(integrity).lower(), "ok")

                u2 = await repo.get_user_by_telegram_id(conn2, 900101)
                self.assertIsNotNone(u2)
                uid2 = u2["id"]
                self.assertEqual(len(await repo.list_inbox_messages(conn2, uid2)), 1)
                self.assertEqual(len(await repo.list_journal_entries(conn2, uid2, entry_type="diary")), 1)
                self.assertEqual(len(await repo.list_journal_entries(conn2, uid2, entry_type="idea")), 1)
                self.assertEqual(len(await repo.list_shopping_items(conn2, uid2)), 1)
                self.assertEqual(len(await repo.list_custom_reminders(conn2, uid2)), 1)
            finally:
                await conn2.close()

    def test_2_backup_and_migration_verification_script(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "old.db"
            dst = Path(tmpdir) / "backups" / "old_backup.db"

            # Simulate pre-Sprint-9 legacy SQLite schema with user rows
            conn = sqlite3.connect(str(src))
            try:
                conn.executescript(
                    """
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        telegram_id INTEGER UNIQUE NOT NULL,
                        name TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        wake_up_time TEXT NOT NULL,
                        sleep_time TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    INSERT INTO users (telegram_id, name, timezone, wake_up_time, sleep_time, created_at, updated_at)
                    VALUES (12345, 'TestUser', 'Europe/Moscow', '08:00', '23:00', '2026-01-01', '2026-01-01');
                    """
                )
                conn.commit()
            finally:
                conn.close()

            res = create_verified_backup(src, dst)
            self.assertTrue(res["tables_matched"])
            self.assertEqual(res["table_counts"]["users"], 1)

            ok_pre, _ = check_health.check_database(dst, allow_pre_migration=True)
            # Pre-migration legacy mock DB lacks day_plans etc., so check_health rejects missing core tables
            self.assertFalse(ok_pre)

            mig = verify_migration_on_copy(dst, res["table_counts"])
            self.assertEqual(mig["migrate_integrity"].lower(), "ok")
            self.assertTrue(mig["new_tables_present"])

    def test_3_config_gamebot_scheme_and_stt_off(self):
        env_patch = {
            "LLM_ENABLED": "",
            "LLM_PROVIDER": "openai",
            "LLM_API_KEY": "",
            "LLM_BASE_URL": "",
            "LLM_MODEL": "",
            "LLM_FAILOVER_PROVIDER": "",
            "LLM_TIMEOUT_SEC": "",
            "LLM_MAX_TOKENS": "",
            "OPENAI_API_KEY": "sk-test-primary",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
            "OPENAI_MODEL": "gpt-6-luna",
            "DEEPSEEK_API_KEY": "sk-test-reserve",
            "DEEPSEEK_MODEL": "",
            "STT_PROVIDER": "off",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            llm_cfg = get_llm_settings()
            self.assertTrue(llm_cfg.enabled)
            self.assertEqual(llm_cfg.primary.provider, "openai")
            self.assertEqual(llm_cfg.primary.model, "gpt-6-luna")
            self.assertIsNotNone(llm_cfg.failover)
            self.assertEqual(llm_cfg.failover.provider, "deepseek")
            self.assertEqual(llm_cfg.failover.model, "deepseek-v4-flash")
            self.assertEqual(llm_cfg.timeout_sec, 15.0)
            self.assertEqual(llm_cfg.max_tokens, 1200)

            stt_cfg = get_stt_settings()
            self.assertEqual(stt_cfg.routes, [])

    def test_4_russian_kind_aliases_normalization(self):
        raw_items = [
            {"kind": "покупка", "title": "Гречка", "source_quote": "купить гречку"},
            {"kind": "идея", "title": "Новый проект", "source_quote": "идея для проекта"},
            {"kind": "задача", "title": "Позвонить", "source_quote": "надо позвонить"},
            {"kind": "дневник", "title": "Устал", "source_quote": "сегодня устал"},
            {"kind": "сделано", "title": "Помыл посуду", "source_quote": "помыл посуду"},
            {
                "kind": "напоминание",
                "title": "Врач",
                "source_quote": "напомни завтра в 12:00 врач",
                "date_hint": "завтра",
                "time_hint": "12:00",
            },
        ]
        items = validate_and_normalize_llm_items(
            raw_items,
            raw_text="test",
            now_utc=datetime.datetime(2026, 11, 19, 9, 0, 0),
            tz_str="Europe/Moscow",
        )
        kinds = [it.kind for it in items]
        self.assertEqual(kinds, ["shopping", "idea", "task", "diary", "done", "reminder"])
        self.assertEqual(items[-1].schedule.resolved_date, "2026-11-20")
        self.assertEqual(items[-1].schedule.resolved_time, "12:00")


if __name__ == "__main__":
    unittest.main()
