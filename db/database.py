import asyncio
from contextlib import asynccontextmanager
import datetime
import os
import shutil
import tempfile
import urllib.request
from typing import Any, Dict, List

import aiosqlite
from db.knowledge_seed import VEGAN_TAG, VEGETARIAN_TAG


def _sqlite_path(database_url: str) -> str:
    """Extract SQLite file path from URL-like string."""
    if database_url.startswith("sqlite:///"):
        return database_url.replace("sqlite:///", "", 1)
    return database_url


def _is_sqlite_file(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            header = f.read(16)
        return header.startswith(b"SQLite format 3\x00")
    except Exception:
        return False


def _maybe_bootstrap_sqlite_db(path: str) -> None:
    """
    Optional one-time bootstrap for Railway volumes.

    If DATABASE_URL points to a SQLite file that doesn't exist yet and
    BOOTSTRAP_DB_URL is set, downloads the DB to the target path.
    """
    bootstrap_url = os.getenv("BOOTSTRAP_DB_URL", "").strip()
    if not bootstrap_url:
        return

    if os.path.exists(path) and os.path.getsize(path) > 0:
        return

    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    # IMPORTANT: create temp file on the same filesystem as the target path.
    # Railway volumes are mounted separately, and os.replace/rename from /tmp
    # would fail with EXDEV (Invalid cross-device link).
    with tempfile.NamedTemporaryFile(delete=False, dir=dir_name or None) as tmp:
        tmp_path = tmp.name

    try:
        with urllib.request.urlopen(bootstrap_url, timeout=60) as resp:
            with open(tmp_path, "wb") as out:
                shutil.copyfileobj(resp, out)

        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise RuntimeError("BOOTSTRAP_DB_URL download produced empty file")
        if not _is_sqlite_file(tmp_path):
            raise RuntimeError("BOOTSTRAP_DB_URL does not look like a SQLite database")

        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


async def connect(database_url: str) -> aiosqlite.Connection:
    """Open SQLite connection with row factory and WAL/timeout pragmas."""
    path = _sqlite_path(database_url)
    _maybe_bootstrap_sqlite_db(path)
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON;")
    await conn.execute("PRAGMA busy_timeout=5000;")
    is_memory = path == ":memory:" or path.startswith("file::memory:")
    if not is_memory:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


@asynccontextmanager
async def independent_connection(conn: aiosqlite.Connection):
    """Yield an isolated writer for a file database; reuse in-memory connections."""
    rows = await conn.execute_fetchall("PRAGMA database_list;")
    main = next((row for row in rows if row["name"] == "main"), None)
    path = str(main["file"] or "") if main else ""
    if not path or path == ":memory:":
        yield conn
        return
    writer = await connect(path)
    try:
        yield writer
    finally:
        await writer.close()


async def init_db(conn: aiosqlite.Connection) -> None:
    """Create tables and seed minimal data."""
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            name TEXT NOT NULL,
            timezone TEXT NOT NULL,
            wake_up_time TEXT NOT NULL,
            sleep_time TEXT NOT NULL,
            strictness TEXT DEFAULT 'neutral',
            goals TEXT DEFAULT '',
            pause_until TEXT,
            quiet_mode INTEGER DEFAULT 0,
            sleep_mode_enabled INTEGER DEFAULT 0,
            sleep_target_sleep TEXT,
            sleep_target_wake TEXT,
            sleep_shift_step INTEGER DEFAULT 30,
            sleep_shift_every INTEGER DEFAULT 2,
            sleep_last_shift_date TEXT,
            sleep_last_evening_date TEXT,
            sleep_last_morning_date TEXT,
            daily_brief_last_date TEXT,
            focus_strikes INTEGER DEFAULT 0,
            focus_cooldown_until TEXT,
            weekend_wake_up_time TEXT DEFAULT '',
            weekend_sleep_time TEXT DEFAULT '',
            pulse_enabled INTEGER DEFAULT 1,
            legacy_pings_enabled INTEGER DEFAULT 0,
            sick_until_utc TEXT,
            sick_uses_month TEXT DEFAULT '',
            sick_uses_count INTEGER DEFAULT 0,
            sleep_confirmed_date TEXT,
            sleep_confirmed_at TEXT,
            night_catch_last_at TEXT,
            profile_mode TEXT NOT NULL DEFAULT 'adult',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS households (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            invite_code TEXT UNIQUE,
            settings_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS routines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            routine_key TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            default_time TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS routine_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            routine_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (routine_id) REFERENCES routines(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS routine_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            routine_type TEXT NOT NULL,
            title TEXT NOT NULL,
            order_index INTEGER DEFAULT 0,
            points INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1,
            trigger_after_step_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS user_routines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            routine_id INTEGER NOT NULL,
            reminder_time TEXT NOT NULL,
            last_sent_date TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (routine_id) REFERENCES routines(id) ON DELETE CASCADE,
            UNIQUE(user_id, routine_id)
        );

        CREATE TABLE IF NOT EXISTS user_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            routine_id INTEGER NOT NULL,
            routine_date TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (routine_id) REFERENCES routines(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS knowledge_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            steps TEXT NOT NULL,
            created_at TEXT NOT NULL,
            tags TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS custom_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            reminder_time TEXT NOT NULL,
            frequency_days INTEGER DEFAULT 1,
            target_weekday INTEGER,
            last_sent_date TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS custom_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reminder_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reminder_date TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (reminder_id) REFERENCES custom_reminders(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS wellness_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            water_enabled INTEGER DEFAULT 0,
            meal_enabled INTEGER DEFAULT 0,
            focus_mode INTEGER DEFAULT 0,
            water_last_key TEXT DEFAULT '',
            meal_last_key TEXT DEFAULT '',
            focus_work INTEGER DEFAULT 20,
            focus_rest INTEGER DEFAULT 10,
            water_times TEXT DEFAULT '11:00,16:00',
            meal_times TEXT DEFAULT '13:00,19:00',
            tone TEXT DEFAULT 'neutral',
            meal_profile TEXT DEFAULT 'omnivore',
            expiring_window_days INTEGER DEFAULT 3,
            affirm_mode TEXT DEFAULT 'off',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id)
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            household_id INTEGER,
            scope TEXT NOT NULL DEFAULT 'personal',
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            monthly_limit REAL DEFAULT 0,
            payday_day INTEGER DEFAULT 1,
            food_budget REAL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id)
        );

        CREATE TABLE IF NOT EXISTS budget_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            limit_amount REAL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, category)
        );

        CREATE TABLE IF NOT EXISTS weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            weight REAL NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            household_id INTEGER,
            scope TEXT NOT NULL DEFAULT 'personal',
            title TEXT NOT NULL,
            amount REAL DEFAULT 0,
            day_of_month INTEGER DEFAULT 1,
            last_paid_month TEXT DEFAULT '',
            paid_by_user_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, title)
        );

        CREATE TABLE IF NOT EXISTS regular_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            frequency_days INTEGER NOT NULL,
            last_done_date TEXT,
            next_due_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS day_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_date TEXT NOT NULL,
            morning_sent TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, plan_date)
        );

        CREATE TABLE IF NOT EXISTS day_plan_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            is_important INTEGER DEFAULT 0,
            done INTEGER DEFAULT 0,
            synced_to_today INTEGER DEFAULT 0,
            external_key TEXT DEFAULT '',
            external_url TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (plan_id) REFERENCES day_plans(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS meds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            dose_text TEXT DEFAULT '',
            schedule_type TEXT NOT NULL,
            times TEXT NOT NULL,
            days_of_week TEXT,
            notes TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS med_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            med_id INTEGER NOT NULL,
            taken_at TEXT,
            plan_date TEXT NOT NULL,
            planned_time TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (med_id) REFERENCES meds(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS shopping_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            household_id INTEGER,
            scope TEXT DEFAULT 'household',
            item_name TEXT NOT NULL,
            quantity REAL DEFAULT 1,
            unit TEXT DEFAULT 'шт',
            category TEXT DEFAULT 'misc',
            is_bought INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS schedule_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            weekdays TEXT NOT NULL DEFAULT '0,1,2,3,4',
            location TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS schedule_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            title TEXT NOT NULL,
            category TEXT DEFAULT 'misc',
            source TEXT DEFAULT 'manual',
            external_key TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS cleaning_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            mode TEXT NOT NULL,
            zones_json TEXT NOT NULL,
            steps_json TEXT NOT NULL,
            current_step_index INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS focus_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_title TEXT NOT NULL,
            duration_min INTEGER NOT NULL,
            start_ts TEXT NOT NULL,
            checkin_ts TEXT NOT NULL,
            checkin_sent INTEGER DEFAULT 0,
            checkin_response TEXT DEFAULT '',
            end_ts TEXT NOT NULL,
            end_sent INTEGER DEFAULT 0,
            result TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS inbox_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            telegram_message_id INTEGER,
            source TEXT NOT NULL,
            telegram_file_id TEXT,
            telegram_file_unique_id TEXT,
            duration_sec INTEGER,
            raw_text TEXT,
            stt_provider TEXT DEFAULT '',
            stt_error TEXT DEFAULT '',
            llm_provider TEXT DEFAULT '',
            llm_model TEXT DEFAULT '',
            llm_raw_json TEXT DEFAULT '',
            llm_error TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'received',
            local_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, chat_id, telegram_message_id)
        );

        CREATE TABLE IF NOT EXISTS journal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            inbox_message_id INTEGER,
            entry_type TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'text',
            user_text TEXT NOT NULL,
            title TEXT NOT NULL,
            model_note TEXT DEFAULT '',
            tags_json TEXT DEFAULT '[]',
            is_deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (inbox_message_id) REFERENCES inbox_messages(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS inbox_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            inbox_message_id INTEGER NOT NULL,
            item_kind TEXT NOT NULL,
            target_table TEXT NOT NULL,
            target_id INTEGER,
            title TEXT NOT NULL,
            source_quote TEXT NOT NULL,
            payload_json TEXT DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (inbox_message_id) REFERENCES inbox_messages(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
            client_id TEXT PRIMARY KEY,
            client_secret_hash TEXT DEFAULT '',
            client_name TEXT DEFAULT '',
            redirect_uris_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
            code_hash TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            code_challenge TEXT NOT NULL,
            code_challenge_method TEXT NOT NULL,
            resource TEXT DEFAULT '',
            scope TEXT DEFAULT '',
            expires_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mcp_oauth_tokens (
            token_hash TEXT PRIMARY KEY,
            token_type TEXT NOT NULL,
            client_id TEXT NOT NULL,
            resource TEXT DEFAULT '',
            scope TEXT DEFAULT '',
            expires_at TEXT NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mcp_oauth_tokens_client_type
            ON mcp_oauth_tokens(client_id, token_type);
        CREATE INDEX IF NOT EXISTS idx_mcp_oauth_codes_expires
            ON mcp_oauth_codes(expires_at);

        CREATE TABLE IF NOT EXISTS user_karma_state (
            user_id INTEGER PRIMARY KEY,
            score INTEGER NOT NULL DEFAULT 70,
            sleep_debt INTEGER NOT NULL DEFAULT 0,
            food_debt INTEGER NOT NULL DEFAULT 0,
            tasks_debt INTEGER NOT NULL DEFAULT 0,
            hygiene_debt INTEGER NOT NULL DEFAULT 0,
            sleep_streak INTEGER NOT NULL DEFAULT 0,
            food_streak INTEGER NOT NULL DEFAULT 0,
            tasks_streak INTEGER NOT NULL DEFAULT 0,
            hygiene_streak INTEGER NOT NULL DEFAULT 0,
            last_event_summary TEXT DEFAULT '',
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS karma_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            domain TEXT NOT NULL,
            event_type TEXT NOT NULL,
            delta INTEGER NOT NULL,
            score_after INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_karma_events_user_date
            ON karma_events(user_id, local_date, id);

        CREATE TABLE IF NOT EXISTS pulse_checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            slot_key TEXT NOT NULL,
            domain TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            ping_count INTEGER NOT NULL DEFAULT 0,
            snooze_count INTEGER NOT NULL DEFAULT 0,
            scheduled_at_utc TEXT NOT NULL,
            next_ping_at_utc TEXT NOT NULL,
            last_ping_at_utc TEXT,
            resolved_at_utc TEXT,
            card_chat_id INTEGER,
            card_message_id INTEGER,
            meta_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, local_date, slot_key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_pulse_checkins_due
            ON pulse_checkins(status, next_ping_at_utc);

        CREATE TABLE IF NOT EXISTS user_profile_memory (
            user_id INTEGER PRIMARY KEY,
            portrait_text TEXT NOT NULL DEFAULT '',
            patterns_json TEXT NOT NULL DEFAULT '[]',
            last_refreshed_at TEXT,
            events_seen_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS meal_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            household_id INTEGER,
            plan_date TEXT NOT NULL,
            meal_slot TEXT NOT NULL,
            title TEXT NOT NULL,
            recipe_id TEXT DEFAULT '',
            time_minutes INTEGER DEFAULT 20,
            ingredients_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'planned',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, plan_date, meal_slot),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_meal_plans_user_date
            ON meal_plans(user_id, plan_date);

        CREATE TABLE IF NOT EXISTS user_recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            household_id INTEGER,
            recipe_key TEXT NOT NULL,
            title TEXT NOT NULL,
            desc TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            base_servings INTEGER NOT NULL DEFAULT 2,
            time_minutes INTEGER NOT NULL DEFAULT 20,
            ingredients_json TEXT NOT NULL DEFAULT '[]',
            steps_json TEXT NOT NULL DEFAULT '[]',
            nutrition_json TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'ai_chef',
            source_label TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, recipe_key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_user_recipes_user_updated
            ON user_recipes(user_id, updated_at);

        CREATE TABLE IF NOT EXISTS meal_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            meal_slot TEXT NOT NULL DEFAULT 'snack',
            title TEXT NOT NULL,
            recipe_id TEXT NOT NULL DEFAULT '',
            servings REAL NOT NULL DEFAULT 1.0,
            kcal REAL NOT NULL DEFAULT 0,
            protein_g REAL NOT NULL DEFAULT 0,
            fat_g REAL NOT NULL DEFAULT 0,
            carbs_g REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_meal_logs_user_date
            ON meal_logs(user_id, local_date);

        CREATE TABLE IF NOT EXISTS workout_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            title TEXT NOT NULL,
            focus_area TEXT NOT NULL DEFAULT 'full_body',
            duration_min INTEGER NOT NULL DEFAULT 5,
            equipment_used TEXT NOT NULL DEFAULT '',
            intensity TEXT NOT NULL DEFAULT 'easy',
            kcal_burned INTEGER NOT NULL DEFAULT 0,
            is_lazy_start INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_workout_logs_user_date
            ON workout_logs(user_id, local_date);

        CREATE TABLE IF NOT EXISTS user_calendar_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT 'Google Calendar',
            ics_url TEXT DEFAULT '',
            calendar_id TEXT DEFAULT 'primary',
            access_token TEXT DEFAULT '',
            refresh_token TEXT DEFAULT '',
            token_expires_at TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_sync_at TEXT,
            last_error TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, provider),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            external_uid TEXT NOT NULL,
            event_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            is_all_day INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL,
            location TEXT DEFAULT '',
            description TEXT DEFAULT '',
            is_busy INTEGER NOT NULL DEFAULT 1,
            pre_alert_sent INTEGER NOT NULL DEFAULT 0,
            alert_chat_id INTEGER,
            alert_message_id INTEGER,
            alert_end_utc TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, source, external_uid, event_date, start_time),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_calendar_events_user_date
            ON calendar_events(user_id, event_date, start_time);

        CREATE TABLE IF NOT EXISTS gcal_oauth_states (
            state_token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS desk_link_tokens (
            token TEXT PRIMARY KEY,
            desk_user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            display_name TEXT NOT NULL,
            timezone TEXT NOT NULL,
            expires_at_utc TEXT NOT NULL,
            consumed_at_utc TEXT,
            consumed_by_user_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY (consumed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_desk_tokens_expiry ON desk_link_tokens(expires_at_utc);
        CREATE TABLE IF NOT EXISTS desk_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            desk_user_id TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            linked_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS desk_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            desk_user_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            received_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS desk_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            priority TEXT NOT NULL,
            batch_key TEXT DEFAULT '',
            text TEXT NOT NULL,
            url TEXT DEFAULT '',
            button_label TEXT DEFAULT '',
            deliver_after_utc TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            sent_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_desk_outbox_due ON desk_outbox(status, deliver_after_utc, priority);
        CREATE INDEX IF NOT EXISTS idx_desk_outbox_batch ON desk_outbox(batch_key, status);
        """
    )
    await ensure_columns(conn)
    await seed_routines(conn)
    await seed_knowledge(conn)
    await conn.commit()


async def ensure_columns(conn: aiosqlite.Connection) -> None:
    """Ensure optional columns exist for backward compatibility."""
    async def add_column_if_missing(table: str, name: str, definition: str) -> None:
        """Recheck after ALTER errors so two WAL processes can start together safely."""
        info = await conn.execute_fetchall(f"PRAGMA table_info({table});")
        if name in {row["name"] for row in info}:
            return
        try:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition};")
        except Exception:
            info = await conn.execute_fetchall(f"PRAGMA table_info({table});")
            if name not in {row["name"] for row in info}:
                raise

    users_info = await conn.execute_fetchall("PRAGMA table_info(users);")
    user_cols = {row["name"] for row in users_info}
    if "profile_mode" not in user_cols:
        await add_column_if_missing("users", "profile_mode", "TEXT NOT NULL DEFAULT 'adult'")
    if "pause_until" not in user_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN pause_until TEXT;")
    if "quiet_mode" not in user_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN quiet_mode INTEGER DEFAULT 0;")
    if "sleep_mode_enabled" not in user_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN sleep_mode_enabled INTEGER DEFAULT 0;")
    if "sleep_target_sleep" not in user_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN sleep_target_sleep TEXT;")
    if "sleep_target_wake" not in user_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN sleep_target_wake TEXT;")
    if "sleep_shift_step" not in user_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN sleep_shift_step INTEGER DEFAULT 30;")
    if "sleep_shift_every" not in user_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN sleep_shift_every INTEGER DEFAULT 2;")
    if "sleep_last_shift_date" not in user_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN sleep_last_shift_date TEXT;")
    if "sleep_last_evening_date" not in user_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN sleep_last_evening_date TEXT;")
    if "sleep_last_morning_date" not in user_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN sleep_last_morning_date TEXT;")
    if "daily_brief_last_date" not in user_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN daily_brief_last_date TEXT;")
    if "focus_strikes" not in user_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN focus_strikes INTEGER DEFAULT 0;")
    if "focus_cooldown_until" not in user_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN focus_cooldown_until TEXT;")
    for col_name, col_def in (
        ("weekend_wake_up_time", "TEXT DEFAULT ''"),
        ("weekend_sleep_time", "TEXT DEFAULT ''"),
        ("pulse_enabled", "INTEGER DEFAULT 1"),
        ("legacy_pings_enabled", "INTEGER DEFAULT 0"),
        ("sick_until_utc", "TEXT"),
        ("sick_uses_month", "TEXT DEFAULT ''"),
        ("sick_uses_count", "INTEGER DEFAULT 0"),
        ("sleep_confirmed_date", "TEXT"),
        ("sleep_confirmed_at", "TEXT"),
        ("night_catch_last_at", "TEXT"),
        ("early_wake_alert_date", "TEXT DEFAULT ''"),
    ):
        if col_name not in user_cols:
            await conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def};")

    articles_info = await conn.execute_fetchall("PRAGMA table_info(knowledge_articles);")
    article_cols = {row["name"] for row in articles_info}
    if "tags" not in article_cols:
        await conn.execute("ALTER TABLE knowledge_articles ADD COLUMN tags TEXT DEFAULT '';")

    reminders_info = await conn.execute_fetchall("PRAGMA table_info(custom_reminders);")
    reminders_cols = {row["name"] for row in reminders_info}
    if reminders_info:
        if "target_weekday" not in reminders_cols:
            try:
                await conn.execute("ALTER TABLE custom_reminders ADD COLUMN target_weekday INTEGER;")
            except Exception:
                pass
        if "is_active" not in reminders_cols:
            try:
                await conn.execute("ALTER TABLE custom_reminders ADD COLUMN is_active INTEGER DEFAULT 1;")
            except Exception:
                pass

    wellness_info = await conn.execute_fetchall("PRAGMA table_info(wellness_settings);")
    wellness_cols = {row["name"] for row in wellness_info}
    if wellness_info:
        if "water_last_key" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN water_last_key TEXT DEFAULT '';")
        if "meal_last_key" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN meal_last_key TEXT DEFAULT '';")
        if "focus_work" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN focus_work INTEGER DEFAULT 20;")
        if "focus_rest" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN focus_rest INTEGER DEFAULT 10;")
        if "tone" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN tone TEXT DEFAULT 'neutral';")
        if "water_times" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN water_times TEXT DEFAULT '11:00,16:00';")
        if "meal_times" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN meal_times TEXT DEFAULT '13:00,19:00';")
        if "meal_profile" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN meal_profile TEXT DEFAULT 'omnivore';")
        if "expiring_window_days" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN expiring_window_days INTEGER DEFAULT 3;")
        if "affirm_mode" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN affirm_mode TEXT DEFAULT 'off';")
        # Новые поля для системы аффирмаций 2.0
        if "affirm_enabled" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN affirm_enabled INTEGER DEFAULT 0;")
        if "affirm_categories" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN affirm_categories TEXT DEFAULT '[\"motivation\",\"calm\"]';")
        if "affirm_frequency" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN affirm_frequency TEXT DEFAULT 'daily';")
        if "affirm_hours" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN affirm_hours TEXT DEFAULT '[9]';")
        if "meal_notify_enabled" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN meal_notify_enabled INTEGER DEFAULT 1;")
        if "affirm_last_key" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN affirm_last_key TEXT DEFAULT '';")
    
    # Добавляем поле gender в users
    users_gender_info = await conn.execute_fetchall("PRAGMA table_info(users);")
    users_gender_cols = {row["name"] for row in users_gender_info}
    if "gender" not in users_gender_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN gender TEXT DEFAULT 'neutral';")

    for table, additions in {
        "day_plan_items": (("external_key", "TEXT DEFAULT ''"), ("external_url", "TEXT DEFAULT ''")),
        "schedule_events": (("external_key", "TEXT DEFAULT ''"),),
    }.items():
        info = await conn.execute_fetchall(f"PRAGMA table_info({table});")
        cols = {row["name"] for row in info}
        for col_name, definition in additions:
            if col_name not in cols:
                await add_column_if_missing(table, col_name, definition)
    await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_day_plan_items_external ON day_plan_items(user_id, external_key) WHERE external_key != '';" )
    await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_schedule_events_external ON schedule_events(user_id, external_key) WHERE external_key != '';" )

    calendar_info = await conn.execute_fetchall("PRAGMA table_info(calendar_events);")
    if calendar_info:
        calendar_cols = {row["name"] for row in calendar_info}
        for col_name, definition in (("alert_chat_id", "INTEGER"), ("alert_message_id", "INTEGER"), ("alert_end_utc", "TEXT")):
            if col_name not in calendar_cols:
                await add_column_if_missing("calendar_events", col_name, definition)

    pulse_info = await conn.execute_fetchall("PRAGMA table_info(pulse_checkins);")
    if pulse_info:
        pulse_cols = {row["name"] for row in pulse_info}
        for col_name in ("card_chat_id", "card_message_id"):
            if col_name not in pulse_cols:
                await add_column_if_missing("pulse_checkins", col_name, "INTEGER")

    weights_info = await conn.execute_fetchall("PRAGMA table_info(weights);")
    if not weights_info:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                weight REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )

    # продукты на кухне
    pantry_info = await conn.execute_fetchall("PRAGMA table_info(pantry_items);")
    if not pantry_info:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pantry_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                household_id INTEGER,
                name TEXT NOT NULL,
                amount REAL DEFAULT 0,
                unit TEXT NOT NULL DEFAULT 'шт',
                expires_at TEXT,
                category TEXT NOT NULL DEFAULT 'прочее',
                low_threshold REAL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )
    else:
        pantry_cols = {row["name"] for row in pantry_info}
        if "low_threshold" not in pantry_cols:
            await conn.execute("ALTER TABLE pantry_items ADD COLUMN low_threshold REAL;")
        if "is_active" not in pantry_cols:
            await conn.execute("ALTER TABLE pantry_items ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;")
        if "household_id" not in pantry_cols:
            await conn.execute("ALTER TABLE pantry_items ADD COLUMN household_id INTEGER;")
            await conn.execute(
                """
                UPDATE pantry_items
                SET household_id = (
                    SELECT household_id FROM users WHERE users.id = pantry_items.user_id
                )
                WHERE household_id IS NULL
                """
            )



    shop_info = await conn.execute_fetchall("PRAGMA table_info(shopping_list);")
    if not shop_info:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shopping_list (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                household_id INTEGER,
                scope TEXT DEFAULT 'household',
                item_name TEXT NOT NULL,
                quantity REAL DEFAULT 1,
                unit TEXT DEFAULT 'шт',
                category TEXT DEFAULT 'misc',
                is_bought INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )
    else:
        shop_cols = {row["name"] for row in shop_info}
        if "household_id" not in shop_cols:
            try:
                await conn.execute("ALTER TABLE shopping_list ADD COLUMN household_id INTEGER;")
            except Exception:
                pass
        if "scope" not in shop_cols:
            try:
                await conn.execute("ALTER TABLE shopping_list ADD COLUMN scope TEXT DEFAULT 'household';")
            except Exception:
                pass
        try:
            await conn.execute(
                """
                UPDATE shopping_list
                SET household_id = (SELECT household_id FROM users WHERE users.id = shopping_list.user_id)
                WHERE (household_id IS NULL OR household_id = 0) AND (scope IS NULL OR scope = '' OR scope = 'household')
                """
            )
        except Exception:
            pass

    # бытовая химия и расходники (общий инвентарь по дому)
    supplies_info = await conn.execute_fetchall("PRAGMA table_info(supplies);")
    if not supplies_info:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supplies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'full',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )

    # фото чеков под будущий OCR
    receipt_info = await conn.execute_fetchall("PRAGMA table_info(receipt_photos);")
    if not receipt_info:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS receipt_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )

    bills_info = await conn.execute_fetchall("PRAGMA table_info(bills);")
    if not bills_info:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                amount REAL DEFAULT 0,
                day_of_month INTEGER DEFAULT 1,
                last_paid_month TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, title)
            );
            """
        )
    users_info2 = await conn.execute_fetchall("PRAGMA table_info(users);")
    user_cols2 = {row["name"] for row in users_info2}
    if "household_id" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN household_id INTEGER;")
    if "points_total" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN points_total INTEGER DEFAULT 0;")
    if "points_month" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN points_month INTEGER DEFAULT 0;")
    if "last_points_reset" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN last_points_reset TEXT DEFAULT '';")
    if "height_cm" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN height_cm REAL DEFAULT 0;")
    if "weight_goal" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN weight_goal TEXT DEFAULT '';")
    if "weight_target" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN weight_target REAL DEFAULT 0;")
    if "adhd_mode" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN adhd_mode INTEGER DEFAULT 0;")
    if "last_weight_prompt" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN last_weight_prompt TEXT DEFAULT '';")
    for col in ["last_care_dentist", "last_care_vision", "last_care_firstaid", "last_care_brush"]:
        if col not in user_cols2:
            await conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT '';")
    points_info = await conn.execute_fetchall("PRAGMA table_info(points_log);")
    if not points_info:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS points_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                points INTEGER NOT NULL,
                local_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )
    reg_info = await conn.execute_fetchall("PRAGMA table_info(regular_tasks);")
    if not reg_info:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS regular_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                frequency_days INTEGER NOT NULL,
                zone TEXT DEFAULT '',
                points INTEGER DEFAULT 3,
                is_active INTEGER DEFAULT 1,
                last_done_date TEXT,
                next_due_date TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
            """
        )
    else:
        reg_cols = {row["name"] for row in reg_info}
        if "zone" not in reg_cols:
            try:
                await conn.execute("ALTER TABLE regular_tasks ADD COLUMN zone TEXT DEFAULT '';")
            except Exception:
                pass
        if "points" not in reg_cols:
            try:
                await conn.execute("ALTER TABLE regular_tasks ADD COLUMN points INTEGER DEFAULT 3;")
            except Exception:
                pass
        if "is_active" not in reg_cols:
            try:
                await conn.execute("ALTER TABLE regular_tasks ADD COLUMN is_active INTEGER DEFAULT 1;")
            except Exception:
                pass
    budgets_info = await conn.execute_fetchall("PRAGMA table_info(budgets);")
    budget_cols = {row["name"] for row in budgets_info}
    if "payday_day" not in budget_cols:
        try:
            await conn.execute("ALTER TABLE budgets ADD COLUMN payday_day INTEGER DEFAULT 1;")
        except Exception:
            pass
    if "food_budget" not in budget_cols:
        try:
            await conn.execute("ALTER TABLE budgets ADD COLUMN food_budget REAL DEFAULT 0;")
        except Exception:
            pass

    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS inbox_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            telegram_message_id INTEGER,
            source TEXT NOT NULL,
            telegram_file_id TEXT,
            telegram_file_unique_id TEXT,
            duration_sec INTEGER,
            raw_text TEXT,
            stt_provider TEXT DEFAULT '',
            stt_error TEXT DEFAULT '',
            llm_provider TEXT DEFAULT '',
            llm_model TEXT DEFAULT '',
            llm_raw_json TEXT DEFAULT '',
            llm_error TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'received',
            local_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, chat_id, telegram_message_id)
        );

        CREATE TABLE IF NOT EXISTS journal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            inbox_message_id INTEGER,
            entry_type TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'text',
            user_text TEXT NOT NULL,
            title TEXT NOT NULL,
            model_note TEXT DEFAULT '',
            tags_json TEXT DEFAULT '[]',
            is_deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (inbox_message_id) REFERENCES inbox_messages(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS inbox_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            inbox_message_id INTEGER NOT NULL,
            item_kind TEXT NOT NULL,
            target_table TEXT NOT NULL,
            target_id INTEGER,
            title TEXT NOT NULL,
            source_quote TEXT NOT NULL,
            payload_json TEXT DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (inbox_message_id) REFERENCES inbox_messages(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
            client_id TEXT PRIMARY KEY,
            client_secret_hash TEXT DEFAULT '',
            client_name TEXT DEFAULT '',
            redirect_uris_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
            code_hash TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            code_challenge TEXT NOT NULL,
            code_challenge_method TEXT NOT NULL,
            resource TEXT DEFAULT '',
            scope TEXT DEFAULT '',
            expires_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mcp_oauth_tokens (
            token_hash TEXT PRIMARY KEY,
            token_type TEXT NOT NULL,
            client_id TEXT NOT NULL,
            resource TEXT DEFAULT '',
            scope TEXT DEFAULT '',
            expires_at TEXT NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mcp_oauth_tokens_client_type
            ON mcp_oauth_tokens(client_id, token_type);
        CREATE INDEX IF NOT EXISTS idx_mcp_oauth_codes_expires
            ON mcp_oauth_codes(expires_at);

        CREATE TABLE IF NOT EXISTS user_karma_state (
            user_id INTEGER PRIMARY KEY,
            score INTEGER NOT NULL DEFAULT 70,
            sleep_debt INTEGER NOT NULL DEFAULT 0,
            food_debt INTEGER NOT NULL DEFAULT 0,
            tasks_debt INTEGER NOT NULL DEFAULT 0,
            hygiene_debt INTEGER NOT NULL DEFAULT 0,
            sleep_streak INTEGER NOT NULL DEFAULT 0,
            food_streak INTEGER NOT NULL DEFAULT 0,
            tasks_streak INTEGER NOT NULL DEFAULT 0,
            hygiene_streak INTEGER NOT NULL DEFAULT 0,
            last_event_summary TEXT DEFAULT '',
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS karma_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            domain TEXT NOT NULL,
            event_type TEXT NOT NULL,
            delta INTEGER NOT NULL,
            score_after INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_karma_events_user_date
            ON karma_events(user_id, local_date, id);

        CREATE TABLE IF NOT EXISTS pulse_checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            slot_key TEXT NOT NULL,
            domain TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            ping_count INTEGER NOT NULL DEFAULT 0,
            snooze_count INTEGER NOT NULL DEFAULT 0,
            scheduled_at_utc TEXT NOT NULL,
            next_ping_at_utc TEXT NOT NULL,
            last_ping_at_utc TEXT,
            resolved_at_utc TEXT,
            card_chat_id INTEGER,
            card_message_id INTEGER,
            meta_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, local_date, slot_key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_pulse_checkins_due
            ON pulse_checkins(status, next_ping_at_utc);

        CREATE TABLE IF NOT EXISTS user_profile_memory (
            user_id INTEGER PRIMARY KEY,
            portrait_text TEXT NOT NULL DEFAULT '',
            patterns_json TEXT NOT NULL DEFAULT '[]',
            last_refreshed_at TEXT,
            events_seen_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS meal_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            household_id INTEGER,
            plan_date TEXT NOT NULL,
            meal_slot TEXT NOT NULL,
            title TEXT NOT NULL,
            recipe_id TEXT DEFAULT '',
            time_minutes INTEGER DEFAULT 20,
            ingredients_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'planned',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, plan_date, meal_slot),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_meal_plans_user_date
            ON meal_plans(user_id, plan_date);

        CREATE TABLE IF NOT EXISTS user_recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            household_id INTEGER,
            recipe_key TEXT NOT NULL,
            title TEXT NOT NULL,
            desc TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            base_servings INTEGER NOT NULL DEFAULT 2,
            time_minutes INTEGER NOT NULL DEFAULT 20,
            ingredients_json TEXT NOT NULL DEFAULT '[]',
            steps_json TEXT NOT NULL DEFAULT '[]',
            nutrition_json TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'ai_chef',
            source_label TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, recipe_key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_user_recipes_user_updated
            ON user_recipes(user_id, updated_at);

        CREATE TABLE IF NOT EXISTS meal_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            meal_slot TEXT NOT NULL DEFAULT 'snack',
            title TEXT NOT NULL,
            recipe_id TEXT NOT NULL DEFAULT '',
            servings REAL NOT NULL DEFAULT 1.0,
            kcal REAL NOT NULL DEFAULT 0,
            protein_g REAL NOT NULL DEFAULT 0,
            fat_g REAL NOT NULL DEFAULT 0,
            carbs_g REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_meal_logs_user_date
            ON meal_logs(user_id, local_date);

        CREATE TABLE IF NOT EXISTS workout_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            title TEXT NOT NULL,
            focus_area TEXT NOT NULL DEFAULT 'full_body',
            duration_min INTEGER NOT NULL DEFAULT 5,
            equipment_used TEXT NOT NULL DEFAULT '',
            intensity TEXT NOT NULL DEFAULT 'easy',
            kcal_burned INTEGER NOT NULL DEFAULT 0,
            is_lazy_start INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_workout_logs_user_date
            ON workout_logs(user_id, local_date);

        CREATE TABLE IF NOT EXISTS user_calendar_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT 'Google Calendar',
            ics_url TEXT DEFAULT '',
            calendar_id TEXT DEFAULT 'primary',
            access_token TEXT DEFAULT '',
            refresh_token TEXT DEFAULT '',
            token_expires_at TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_sync_at TEXT,
            last_error TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, provider),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            external_uid TEXT NOT NULL,
            event_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            is_all_day INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL,
            location TEXT DEFAULT '',
            description TEXT DEFAULT '',
            is_busy INTEGER NOT NULL DEFAULT 1,
            pre_alert_sent INTEGER NOT NULL DEFAULT 0,
            alert_chat_id INTEGER,
            alert_message_id INTEGER,
            alert_end_utc TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, source, external_uid, event_date, start_time),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_calendar_events_user_date
            ON calendar_events(user_id, event_date, start_time);

        CREATE TABLE IF NOT EXISTS gcal_oauth_states (
            state_token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )


async def seed_routines(conn: aiosqlite.Connection) -> None:
    """Insert or extend basic routine templates."""
    routines_data: List[Dict[str, Any]] = [
        {
            "routine_key": "morning",
            "title": "Утро",
            "default_time": "07:30",
            "items": [
                "Стакан воды (можно прямо у кровати)",
                "Умыться и почистить зубы",
                "Заправить кровать и открыть окно на 2–5 минут",
                "Зарядка 2–5 минут (шея/плечи/спина)",
                "Завтрак/перекус (без идеала, просто чтобы было топливо)",
                "Выбери 1 главное дело на сегодня (остальное — бонус)",
                "Проверь, что с собой ключи/телефон/карта (и зарядка, если нужно)",
            ],
        },
        {
            "routine_key": "day",
            "title": "День",
            "default_time": "13:00",
            "items": [
                "Поесть нормально (хоть на 10 минут, без идеала)",
                "Стакан воды",
                "Чуть подвигаться: 10–15 минут на улице или пройтись по дому",
                "Один маленький шаг по главному делу (5–10 минут)",
                "Мини‑порядок 2 минуты (стол/раковина/мусор)",
            ],
        },
        {
            "routine_key": "evening",
            "title": "Вечер",
            "default_time": "21:30",
            "items": [
                "Лёгкий ужин/перекус (чтобы не ложиться на пустой желудок)",
                "Гигиена: умыться и зубы",
                "5 минут на дом: посуда/поверхность/мусор",
                "Собрать на завтра: ключи/зарядка/документы",
                "Тёплый душ или растяжка 3 минуты (снять напряжение)",
                "Проветрить комнату перед сном",
            ],
        },
    ]

    existing_routines = await conn.execute_fetchall(
        "SELECT id, routine_key FROM routines;"
    )
    routine_map = {row["routine_key"]: row["id"] for row in existing_routines}

    for routine in routines_data:
        if routine["routine_key"] in routine_map:
            routine_id = routine_map[routine["routine_key"]]
        else:
            cursor = await conn.execute(
                "INSERT INTO routines (routine_key, title, default_time) VALUES (?, ?, ?)",
                (routine["routine_key"], routine["title"], routine["default_time"]),
            )
            routine_id = cursor.lastrowid

        # Мягкие правки копирайта в базовых рутинах (обновляем существующие строки).
        if routine["routine_key"] == "morning":
            await conn.execute(
                "UPDATE routine_items SET title = ? WHERE routine_id = ? AND title = ?",
                (
                    "Стакан воды (можно прямо у кровати)",
                    routine_id,
                    "Выпить воды",
                ),
            )
            await conn.execute(
                "UPDATE routine_items SET title = ? WHERE routine_id = ? AND title = ?",
                (
                    "Заправить кровать и открыть окно на 2–5 минут",
                    routine_id,
                    "Заправить кровать и открыть окно",
                ),
            )
            await conn.execute(
                "UPDATE routine_items SET title = ? WHERE routine_id = ? AND title = ?",
                (
                    "Завтрак/перекус (без идеала, просто чтобы было топливо)",
                    routine_id,
                    "Съесть что-то простое (хотя бы 5 минут)",
                ),
            )
            await conn.execute(
                "UPDATE routine_items SET title = ? WHERE routine_id = ? AND title = ?",
                (
                    "Проверь, что с собой ключи/телефон/карта (и зарядка, если нужно)",
                    routine_id,
                    "Проверь, что с собой ключи/кошелёк/телефон (и зарядка, если нужно)",
                ),
            )
            await conn.execute(
                "UPDATE routine_items SET title = ? WHERE routine_id = ? AND title = ?",
                (
                    "Проверь, что с собой ключи/телефон/карта (и зарядка, если нужно)",
                    routine_id,
                    "Проверь, что есть чистые вещи на день",
                ),
            )
            await conn.execute(
                "UPDATE routine_items SET title = ? WHERE routine_id = ? AND title = ?",
                (
                    "Завтрак/перекус (без идеала, просто чтобы было топливо)",
                    routine_id,
                    "Позавтракать чем угодно, не кофе",
                ),
            )
        if routine["routine_key"] == "day":
            await conn.execute(
                "UPDATE routine_items SET title = ? WHERE routine_id = ? AND title = ?",
                (
                    "Поесть нормально (хоть на 10 минут, без идеала)",
                    routine_id,
                    "Пообедать без фастфуда",
                ),
            )
            await conn.execute(
                "UPDATE routine_items SET title = ? WHERE routine_id = ? AND title = ?",
                (
                    "Стакан воды",
                    routine_id,
                    "Выпить воды",
                ),
            )
            await conn.execute(
                "UPDATE routine_items SET title = ? WHERE routine_id = ? AND title = ?",
                (
                    "Чуть подвигаться: 10–15 минут на улице или пройтись по дому",
                    routine_id,
                    "Выйти на улицу хотя бы на 15 минут",
                ),
            )
            await conn.execute(
                "UPDATE routine_items SET title = ? WHERE routine_id = ? AND title = ?",
                (
                    "Мини‑порядок 2 минуты (стол/раковина/мусор)",
                    routine_id,
                    "Разобрать посуду/кружки со стола",
                ),
            )
        if routine["routine_key"] == "evening":
            await conn.execute(
                "UPDATE routine_items SET title = ? WHERE routine_id = ? AND title = ?",
                (
                    "Гигиена: умыться и зубы",
                    routine_id,
                    "Короткий душ и гигиена",
                ),
            )
            await conn.execute(
                "UPDATE routine_items SET title = ? WHERE routine_id = ? AND title = ?",
                (
                    "5 минут на дом: посуда/поверхность/мусор",
                    routine_id,
                    "Помыть посуду",
                ),
            )
            await conn.execute(
                "UPDATE routine_items SET title = ? WHERE routine_id = ? AND title = ?",
                (
                    "Собрать на завтра: ключи/зарядка/документы",
                    routine_id,
                    "Подготовить одежду на завтра",
                ),
            )
            await conn.execute(
                "UPDATE routine_items SET title = ? WHERE routine_id = ? AND title = ?",
                (
                    "Собрать на завтра: ключи/зарядка/документы",
                    routine_id,
                    "Сложить вещи по местам",
                ),
            )

        existing_items = await conn.execute_fetchall(
            "SELECT title, sort_order FROM routine_items WHERE routine_id = ?",
            (routine_id,),
        )
        existing_titles = {row["title"] for row in existing_items}
        next_order = max([row["sort_order"] for row in existing_items], default=0) + 1
        for title in routine["items"]:
            if title in existing_titles:
                continue
            await conn.execute(
                "INSERT INTO routine_items (routine_id, title, sort_order) VALUES (?, ?, ?)",
                (routine_id, title, next_order),
            )
            next_order += 1

    await conn.commit()


async def seed_knowledge(conn: aiosqlite.Connection) -> None:
    """Insert or extend knowledge base entries."""
    now = datetime.datetime.utcnow().isoformat()
    articles = [
        {
            "category": "Кухня",
            "title": "Быстрый завтрак за 7 минут",
            "content": "Если совсем нет сил, сделай базовый завтрак, чтобы не жить на кофе.",
            "steps": "\n".join(
                [
                    "Поставь чайник или вскипяти воду.",
                    "Две опции: овсянка быстрого приготовления или яичница.",
                    "Овсянка: залей кипятком, добавь банан/яблоко/орехи, щепотку соли.",
                    "Яичница: сковородка, немного масла, два яйца, щепотка соли. Пока готовится — нарежь хлеб/овощи.",
                    "Выпей стакан воды перед едой.",
                ]
            ),
            "tags": f"{VEGETARIAN_TAG},{VEGAN_TAG}",
        },
        {
            "category": "Кухня",
            "title": "Ужин из трёх ингредиентов",
            "content": "Простой ужин без заморочек: основа + овощ + вкусное сверху.",
            "steps": "\n".join(
                [
                    "Выбери основу: макароны, рис или кус-кус — ставь вариться по инструкции.",
                    "Овощ: нарежь что есть (заморозка ок): лук/морковь/перец/стручковая фасоль. Быстро обжарь на масле 5–7 минут.",
                    "Вкусное сверху: тунец из банки, яйцо, сыр или фасоль из банки. Добавь к овощам.",
                    "Соединяй основу с овощами, посоли/поперчи. Если есть — добавь соевый соус или ложку сметаны.",
                    "Съешь тёплым и выпей воды. Остатки — в контейнер в холодильник.",
                ]
            ),
            "tags": f"{VEGETARIAN_TAG}",
        },
        {
            "category": "Кухня",
            "title": "Как хранить продукты, чтобы не тухло",
            "content": "Мини-гайд по холодильнику и сухим полкам.",
            "steps": "\n".join(
                [
                    "Молочку, мясо, рыбу держи на средней/нижней полке, не на дверце.",
                    "Овощи-фрукты — в ящики. Томаты и бананы лучше снаружи, не в холодильнике.",
                    "Хлеб — в пакете/контейнере, срок 2–3 дня. Остальное в морозилку порезанным.",
                    "Готовую еду — в контейнеры, подписывай дату. Если сомневаешься — выбрось.",
                    "Раз в неделю проверяй: вытереть подтёки, убрать старое, составить список докупок.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Стирка",
            "title": "Постирать тёмные вещи",
            "content": "Базовый сценарий для чёрных футболок, носков и джинс.",
            "steps": "\n".join(
                [
                    "Отсортируй: только тёмные вещи без белого. Проверяй карманы.",
                    "Загрузи барабан не больше чем на 2/3.",
                    "Порошок/гель: 1 мерный колпак или по инструкции, не пересыпай.",
                    "Режим: 'Хлопок' или 'Повседневный', температура 30–40°, отжим 800–1000.",
                    "После стирки сразу развесь, не держи в барабане.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Стирка",
            "title": "Постирать постельное",
            "content": "Чистое постельное = лучше спать и меньше пыли.",
            "steps": "\n".join(
                [
                    "Отсортируй: только постельное бельё и наволочки, без одежды.",
                    "Застегни пододеяльник/молнии, выверни наволочки.",
                    "Режим: 'Хлопок' или 'Постельное', температура 40–60°, отжим 800–1000.",
                    "Порошок/гель: по инструкции, не пересыпай. Кондиционер — по желанию.",
                    "После стирки сразу развесь, встряхни, суши до конца, потом сложи.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Стирка",
            "title": "Если вещи пахнут затхлостью",
            "content": "Как спасти вещи, если после стирки неприятный запах.",
            "steps": "\n".join(
                [
                    "Перестирай при 40–60° с 50–100 мл уксуса или спец-средством от запаха.",
                    "Не перегружай барабан и не пересыпай порошок.",
                    "Сразу развесь после стирки, не оставляй в машинке.",
                    "Проверь фильтр и резинку стиралки — там копится вода и грязь.",
                    "Дай машинке просохнуть с открытой дверцей и лотком.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Уборка",
            "title": "Быстрая уборка за 15 минут",
            "content": "Минимальный порядок без героизма.",
            "steps": "\n".join(
                [
                    "Запусти таймер на 15 минут.",
                    "Собери мусор и вынеси пакет.",
                    "Убери посуду в раковину/посудомойку, быстро сполосни тарелки.",
                    "Протри стол/рабочую поверхность влажной тряпкой.",
                    "Пройдись влажной салфеткой по раковине и крану.",
                    "Если успеваешь — быстро пройтись пылесосом по проходам.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Уборка",
            "title": "Чистая ванная без напряга",
            "content": "Быстрый цикл ухода за санузлом, чтобы не зарастал.",
            "steps": "\n".join(
                [
                    "Побрызгай средство для ванны/раковины/унитаза, дай постоять пару минут.",
                    "Пока ждёшь — убери лишние вещи, вытри зеркало сухой салфеткой.",
                    "Пройди губкой/тряпкой по раковине, крану, поверхности. Смывай водой.",
                    "Йорш + средство в унитаз, потом смой. Протри сиденье и кнопку сверху влажной салфеткой.",
                    "Вымой/замени тряпку, проветри. Добавь рулон бумаги и чистое полотенце, если надо.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Уборка",
            "title": "Разбор завалов за 10 минут",
            "content": "Когда всё валяется, но сил нет.",
            "steps": "\n".join(
                [
                    "Запусти таймер на 10 минут и возьми пакет для мусора.",
                    "Сначала мусор и очевидное: бутылки, упаковки, бумажки.",
                    "Собери грязную одежду в один пакет/корзину, не раскладывай сейчас.",
                    "Сложи чистое в одну стопку на кровати/столе, потом разнесёшь.",
                    "Заверши: протри стол/рабочую поверхность влажной салфеткой, открой окно.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Кухня",
            "title": "Быстрый обед в контейнер",
            "content": "Собери обед за 15 минут и убери в контейнер.",
            "steps": "\n".join(
                [
                    "Основа: макароны/рис/гречка — доведи до готовности.",
                    "Белок: тунец из банки, фасоль, яйцо или кусок курицы — добавь к основе.",
                    "Овощи: свежие или заморозка — обжарь 5–7 минут, посоли/перчи.",
                    "Соус: соевый, сметана, масло или йогурт. Перемешай всё вместе.",
                    "Разложи по контейнерам, остуди и в холодильник.",
                ]
            ),
            "tags": f"{VEGETARIAN_TAG},{VEGAN_TAG}",
        },
        {
            "category": "Уборка",
            "title": "Проверка холодильника за 10 минут",
            "content": "Раз в неделю пробегись по холодильнику, чтобы не воняло и не портилось.",
            "steps": "\n".join(
                [
                    "Возьми пакет для мусора, влажную тряпку и средство для кухни.",
                    "Выгрузи дверь и полки по очереди, выбрасывай явный мусор/старьё.",
                    "Протри полки/ящики влажной тряпкой, подсуши.",
                    "Верни продукты обратно, сгруппируй: готовое/молочка/соусы/овощи.",
                    "Составь короткий список докупок по итогам.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Уборка",
            "title": "Подготовка постели раз в неделю",
            "content": "Мини-ритуал, чтобы постель была свежей.",
            "steps": "\n".join(
                [
                    "Сними постельное, встряхни матрас и проветри комнату 10 минут.",
                    "Пока стирается — пройдись пылесосом по матрасу/основанию.",
                    "Надень чистое бельё, поставь чистое полотенце рядом.",
                    "Если есть запах — положи открытую соду/уголь на тумбочку на пару часов.",
                ]
            ),
            "tags": "",
        },
    ]

    # --- Stage 1: Общий дом (households, bills, expenses, meal_plans, user_recipes) ---
    households_info = await conn.execute_fetchall("PRAGMA table_info(households);")
    households_cols = {row["name"] for row in households_info}
    if households_info and "settings_json" not in households_cols:
        await conn.execute("ALTER TABLE households ADD COLUMN settings_json TEXT NOT NULL DEFAULT '{}';")

    expenses_info = await conn.execute_fetchall("PRAGMA table_info(expenses);")
    expenses_cols = {row["name"] for row in expenses_info}
    if expenses_info and "scope" not in expenses_cols:
        await conn.execute("ALTER TABLE expenses ADD COLUMN scope TEXT NOT NULL DEFAULT 'personal';")
    if expenses_info and "household_id" not in expenses_cols:
        await conn.execute("ALTER TABLE expenses ADD COLUMN household_id INTEGER;")

    bills_mig_info = await conn.execute_fetchall("PRAGMA table_info(bills);")
    bills_mig_cols = {row["name"] for row in bills_mig_info}
    if bills_mig_info and "scope" not in bills_mig_cols:
        await conn.execute("ALTER TABLE bills ADD COLUMN scope TEXT NOT NULL DEFAULT 'personal';")
    if bills_mig_info and "household_id" not in bills_mig_cols:
        await conn.execute("ALTER TABLE bills ADD COLUMN household_id INTEGER;")
    if bills_mig_info and "paid_by_user_id" not in bills_mig_cols:
        await conn.execute("ALTER TABLE bills ADD COLUMN paid_by_user_id INTEGER;")

    mp_info = await conn.execute_fetchall("PRAGMA table_info(meal_plans);")
    mp_cols = {row["name"] for row in mp_info}
    if mp_info and "household_id" not in mp_cols:
        await conn.execute("ALTER TABLE meal_plans ADD COLUMN household_id INTEGER;")
    if mp_info:
        await conn.execute(
            """
            UPDATE meal_plans
            SET household_id = (SELECT household_id FROM users WHERE users.id = meal_plans.user_id)
            WHERE household_id IS NULL
            """
        )

    ur_info = await conn.execute_fetchall("PRAGMA table_info(user_recipes);")
    ur_cols = {row["name"] for row in ur_info}
    if ur_info and "household_id" not in ur_cols:
        await conn.execute("ALTER TABLE user_recipes ADD COLUMN household_id INTEGER;")
    if ur_info:
        await conn.execute(
            """
            UPDATE user_recipes
            SET household_id = (SELECT household_id FROM users WHERE users.id = user_recipes.user_id)
            WHERE household_id IS NULL
            """
        )

    # --- Fitness, Smart Scales & КБЖУ ---
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meal_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            meal_slot TEXT NOT NULL DEFAULT 'snack',
            title TEXT NOT NULL,
            recipe_id TEXT NOT NULL DEFAULT '',
            servings REAL NOT NULL DEFAULT 1.0,
            kcal REAL NOT NULL DEFAULT 0,
            protein_g REAL NOT NULL DEFAULT 0,
            fat_g REAL NOT NULL DEFAULT 0,
            carbs_g REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_meal_logs_user_date
            ON meal_logs(user_id, local_date);

        CREATE TABLE IF NOT EXISTS workout_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            title TEXT NOT NULL,
            focus_area TEXT NOT NULL DEFAULT 'full_body',
            duration_min INTEGER NOT NULL DEFAULT 5,
            equipment_used TEXT NOT NULL DEFAULT '',
            intensity TEXT NOT NULL DEFAULT 'easy',
            kcal_burned INTEGER NOT NULL DEFAULT 0,
            is_lazy_start INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_workout_logs_user_date
            ON workout_logs(user_id, local_date);

        CREATE TABLE IF NOT EXISTS nutrition_lookup_cache (
            query_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            per_100g_json TEXT NOT NULL DEFAULT '{}',
            serving_g REAL NOT NULL DEFAULT 0,
            kcal_serving REAL NOT NULL DEFAULT 0,
            protein_g REAL NOT NULL DEFAULT 0,
            fat_g REAL NOT NULL DEFAULT 0,
            carbs_g REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_food_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name_key TEXT NOT NULL,
            title TEXT NOT NULL,
            brand TEXT NOT NULL DEFAULT '',
            per_100g_json TEXT NOT NULL DEFAULT '{}',
            default_serving_g REAL NOT NULL DEFAULT 100,
            source TEXT NOT NULL DEFAULT 'label',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, name_key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_user_food_products_user
            ON user_food_products(user_id, name_key);

        CREATE TABLE IF NOT EXISTS pending_meal_clarifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL DEFAULT 0,
            local_date TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            title TEXT NOT NULL,
            meal_slot TEXT NOT NULL DEFAULT 'snack',
            estimated_json TEXT NOT NULL DEFAULT '{}',
            question TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            source TEXT NOT NULL DEFAULT 'text',
            inbox_message_id INTEGER,
            created_at TEXT NOT NULL,
            auto_commit_after TEXT NOT NULL,
            resolved_meal_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pending_meals_user_status
            ON pending_meal_clarifications(user_id, status);
        CREATE INDEX IF NOT EXISTS idx_pending_meals_due
            ON pending_meal_clarifications(status, auto_commit_after);
        """
    )

    users_fit_info = await conn.execute_fetchall("PRAGMA table_info(users);")
    users_fit_cols = {row["name"] for row in users_fit_info}
    for col_name, col_def in (
        ("age_years", "INTEGER DEFAULT 0"),
        ("sex", "TEXT DEFAULT ''"),
        ("activity_level", "TEXT DEFAULT 'light'"),
        ("equipment_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("custom_kcal_target", "INTEGER DEFAULT 0"),
        ("coach_mode", "TEXT NOT NULL DEFAULT 'balanced'"),
        ("nutrition_checkin_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("nutrition_checkin_last_date", "TEXT NOT NULL DEFAULT ''"),
    ):
        if users_fit_info and col_name not in users_fit_cols:
            await conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def};")

    weights_mig_info = await conn.execute_fetchall("PRAGMA table_info(weights);")
    weights_mig_cols = {row["name"] for row in weights_mig_info}
    for col_name, col_def in (
        ("body_fat_pct", "REAL"),
        ("muscle_mass_kg", "REAL"),
        ("water_pct", "REAL"),
        ("visceral_fat", "REAL"),
        ("bmi", "REAL"),
        ("source", "TEXT NOT NULL DEFAULT 'manual'"),
        ("note", "TEXT NOT NULL DEFAULT ''"),
    ):
        if weights_mig_info and col_name not in weights_mig_cols:
            await conn.execute(f"ALTER TABLE weights ADD COLUMN {col_name} {col_def};")

    if ur_info and "nutrition_json" not in ur_cols:
        await conn.execute("ALTER TABLE user_recipes ADD COLUMN nutrition_json TEXT NOT NULL DEFAULT '';")

    reg_hh_info = await conn.execute_fetchall("PRAGMA table_info(regular_tasks);")
    reg_hh_cols = {row["name"] for row in reg_hh_info}
    for col_name, col_def in (
        ("household_id", "INTEGER"),
        ("last_done_by_user_id", "INTEGER"),
        ("last_done_by_name", "TEXT DEFAULT ''"),
    ):
        if reg_hh_info and col_name not in reg_hh_cols:
            await conn.execute(f"ALTER TABLE regular_tasks ADD COLUMN {col_name} {col_def};")
    if reg_hh_info:
        await conn.execute(
            """
            UPDATE regular_tasks
            SET household_id = (SELECT household_id FROM users WHERE users.id = regular_tasks.user_id)
            WHERE household_id IS NULL
            """
        )

    sup_hh_info = await conn.execute_fetchall("PRAGMA table_info(supplies);")
    sup_hh_cols = {row["name"] for row in sup_hh_info}
    for col_name, col_def in (
        ("household_id", "INTEGER"),
        ("updated_by_user_id", "INTEGER"),
        ("updated_by_name", "TEXT DEFAULT ''"),
    ):
        if sup_hh_info and col_name not in sup_hh_cols:
            await conn.execute(f"ALTER TABLE supplies ADD COLUMN {col_name} {col_def};")
    if sup_hh_info:
        await conn.execute(
            """
            UPDATE supplies
            SET household_id = (SELECT household_id FROM users WHERE users.id = supplies.user_id)
            WHERE household_id IS NULL
            """
        )

    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS household_dinner_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id INTEGER NOT NULL,
            created_by_user_id INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            options_json TEXT NOT NULL,
            votes_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'open',
            winner_recipe_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_hh_polls_hh_date
            ON household_dinner_polls(household_id, local_date, status);

        CREATE TABLE IF NOT EXISTS household_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            household_id INTEGER NOT NULL,
            from_user_id INTEGER NOT NULL,
            from_name TEXT NOT NULL DEFAULT '',
            to_user_id INTEGER,
            kind TEXT NOT NULL DEFAULT 'request',
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            response_note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_hh_reqs_hh_status
            ON household_requests(household_id, status, kind);
        """
    )

    existing_articles = await conn.execute_fetchall(
        "SELECT title FROM knowledge_articles;"
    )
    existing_titles = {row["title"] for row in existing_articles}

    for article in articles:
        if article["title"] in existing_titles:
            continue
        await conn.execute(
            """
            INSERT INTO knowledge_articles (category, title, content, steps, created_at, tags)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                article["category"],
                article["title"],
                article["content"],
                article["steps"],
                now,
                article.get("tags", ""),
            ),
        )

    await conn.commit()


async def main_init(database_url: str) -> None:
    """Helper to run initialization standalone."""
    conn = await connect(database_url)
    async with conn:
        await init_db(conn)


if __name__ == "__main__":
    db_url = os.getenv("DATABASE_URL", "sqlite:///hidl.db")
    asyncio.run(main_init(db_url))
