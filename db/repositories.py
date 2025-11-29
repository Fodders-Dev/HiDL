import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite


def utc_now_str() -> str:
    return datetime.datetime.utcnow().isoformat()


async def get_user_by_telegram_id(
    conn: aiosqlite.Connection, telegram_id: int
) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    )
    return await cursor.fetchone()


async def list_users(conn: aiosqlite.Connection) -> List[aiosqlite.Row]:
    cursor = await conn.execute("SELECT * FROM users ORDER BY id")
    return await cursor.fetchall()


async def create_user(
    conn: aiosqlite.Connection,
    telegram_id: int,
    name: str,
    timezone: str,
    wake_up_time: str,
    sleep_time: str,
    goals: str = "",
    strictness: str = "neutral",
) -> int:
    now = utc_now_str()
    cursor = await conn.execute(
        """
        INSERT INTO users
        (telegram_id, name, timezone, wake_up_time, sleep_time, strictness, goals, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            telegram_id,
            name,
            timezone,
            wake_up_time,
            sleep_time,
            strictness,
            goals,
            now,
            now,
        ),
    )
    await conn.commit()
    return cursor.lastrowid


async def get_user(conn: aiosqlite.Connection, user_id: int) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    return await cursor.fetchone()


async def update_user_timezone(
    conn: aiosqlite.Connection, user_id: int, timezone: str
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET timezone = ?, updated_at = ? WHERE id = ?",
        (timezone, now, user_id),
    )
    await conn.commit()


async def update_user_schedule(
    conn: aiosqlite.Connection, user_id: int, wake_up: str, sleep: str
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET wake_up_time = ?, sleep_time = ?, updated_at = ? WHERE id = ?",
        (wake_up, sleep, now, user_id),
    )
    await conn.commit()


async def update_user_wake(conn: aiosqlite.Connection, user_id: int, wake_up: str) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET wake_up_time = ?, updated_at = ? WHERE id = ?",
        (wake_up, now, user_id),
    )
    await conn.commit()


async def update_user_sleep(
    conn: aiosqlite.Connection, user_id: int, sleep_time: str
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET sleep_time = ?, updated_at = ? WHERE id = ?",
        (sleep_time, now, user_id),
    )
    await conn.commit()


async def update_user_goals(
    conn: aiosqlite.Connection, user_id: int, goals: str
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET goals = ?, updated_at = ? WHERE id = ?",
        (goals, now, user_id),
    )
    await conn.commit()

async def update_user_body(
    conn: aiosqlite.Connection,
    user_id: int,
    height_cm: float | None = None,
    weight_goal: str | None = None,
    weight_target: float | None = None,
) -> None:
    now = utc_now_str()
    fields = []
    values = []
    if height_cm is not None:
        fields.append("height_cm = ?")
        values.append(height_cm)
    if weight_goal is not None:
        fields.append("weight_goal = ?")
        values.append(weight_goal)
    if weight_target is not None:
        fields.append("weight_target = ?")
        values.append(weight_target)
    if not fields:
        return
    fields.append("updated_at = ?")
    values.append(now)
    values.append(user_id)
    sql = f"UPDATE users SET {', '.join(fields)} WHERE id = ?"
    await conn.execute(sql, tuple(values))
    await conn.commit()


async def set_user_pause(
    conn: aiosqlite.Connection, user_id: int, pause_until: str
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET pause_until = ?, updated_at = ? WHERE id = ?",
        (pause_until, now, user_id),
    )
    await conn.commit()


async def clear_user_pause(conn: aiosqlite.Connection, user_id: int) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET pause_until = NULL, updated_at = ? WHERE id = ?",
        (now, user_id),
    )
    await conn.commit()


async def toggle_adhd(conn: aiosqlite.Connection, user_id: int, enabled: bool) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET adhd_mode = ?, updated_at = ? WHERE id = ?",
        (1 if enabled else 0, now, user_id),
    )
    await conn.commit()


async def update_weight_prompt(conn: aiosqlite.Connection, user_id: int, date_str: str) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET last_weight_prompt = ?, updated_at = ? WHERE id = ?",
        (date_str, now, user_id),
    )
    await conn.commit()


async def update_care_date(conn: aiosqlite.Connection, user_id: int, column: str, date_str: str) -> None:
    now = utc_now_str()
    await conn.execute(
        f"UPDATE users SET {column} = ?, updated_at = ? WHERE id = ?",
        (date_str, now, user_id),
    )
    await conn.commit()


async def list_routines(conn: aiosqlite.Connection) -> List[aiosqlite.Row]:
    cursor = await conn.execute("SELECT * FROM routines ORDER BY id")
    return await cursor.fetchall()


async def get_routine_by_key(
    conn: aiosqlite.Connection, routine_key: str
) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM routines WHERE routine_key = ?", (routine_key,)
    )
    return await cursor.fetchone()


async def get_routine_by_id(
    conn: aiosqlite.Connection, routine_id: int
) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM routines WHERE id = ?", (routine_id,)
    )
    return await cursor.fetchone()


async def get_routine_items(
    conn: aiosqlite.Connection, routine_id: int
) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM routine_items WHERE routine_id = ? ORDER BY sort_order",
        (routine_id,),
    )
    return await cursor.fetchall()


async def ensure_user_routines(conn: aiosqlite.Connection, user_id: int) -> None:
    routines = await list_routines(conn)
    for routine in routines:
        cursor = await conn.execute(
            "SELECT id FROM user_routines WHERE user_id = ? AND routine_id = ?",
            (user_id, routine["id"]),
        )
        exists = await cursor.fetchone()
        if exists:
            continue
        await conn.execute(
            """
            INSERT INTO user_routines (user_id, routine_id, reminder_time, last_sent_date)
            VALUES (?, ?, ?, NULL)
            """,
            (user_id, routine["id"], routine["default_time"]),
        )
    await conn.commit()


async def list_user_routines(
    conn: aiosqlite.Connection, user_id: int
) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        """
        SELECT ur.*, r.title, r.routine_key
        FROM user_routines ur
        JOIN routines r ON r.id = ur.routine_id
        WHERE ur.user_id = ?
        ORDER BY r.id
        """,
        (user_id,),
    )
    return await cursor.fetchall()


async def get_user_routine(
    conn: aiosqlite.Connection, user_id: int, routine_id: int
) -> Optional[aiosqlite.Row]:
    """Return user's routine settings with joined routine meta."""
    cursor = await conn.execute(
        """
        SELECT ur.*, r.title, r.routine_key, r.default_time
        FROM user_routines ur
        JOIN routines r ON r.id = ur.routine_id
        WHERE ur.user_id = ? AND ur.routine_id = ?
        """,
        (user_id, routine_id),
    )
    return await cursor.fetchone()


async def update_user_routine_time(
    conn: aiosqlite.Connection, user_id: int, routine_key: str, hhmm: str
) -> None:
    """Update reminder time for a specific routine of user by routine_key."""
    routine = await get_routine_by_key(conn, routine_key)
    if not routine:
        return
    await conn.execute(
        """
        UPDATE user_routines
        SET reminder_time = ?
        WHERE user_id = ? AND routine_id = ?
        """,
        (hhmm, user_id, routine["id"]),
    )
    await conn.commit()


# Custom reminders
async def create_custom_reminder(
    conn: aiosqlite.Connection,
    user_id: int,
    title: str,
    reminder_time: str,
    frequency_days: int = 1,
    target_weekday: Optional[int] = None,
) -> int:
    now = utc_now_str()
    cursor = await conn.execute(
        """
        INSERT INTO custom_reminders (user_id, title, reminder_time, frequency_days, target_weekday, last_sent_date, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (user_id, title, reminder_time, frequency_days, target_weekday, now, now),
    )
    await conn.commit()
    return cursor.lastrowid


async def list_custom_reminders(conn: aiosqlite.Connection, user_id: int) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        """
        SELECT * FROM custom_reminders
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (user_id,),
    )
    return await cursor.fetchall()


async def delete_custom_reminder(conn: aiosqlite.Connection, user_id: int, reminder_id: int) -> None:
    await conn.execute(
        "DELETE FROM custom_reminders WHERE id = ? AND user_id = ?",
        (reminder_id, user_id),
    )
    await conn.commit()


async def set_custom_reminder_sent(
    conn: aiosqlite.Connection, reminder_id: int, sent_date: str
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE custom_reminders SET last_sent_date = ?, updated_at = ? WHERE id = ?",
        (sent_date, now, reminder_id),
    )
    await conn.commit()


async def log_custom_task(
    conn: aiosqlite.Connection,
    reminder_id: int,
    user_id: int,
    reminder_date: str,
    status: str,
) -> None:
    now = utc_now_str()
    existing = await conn.execute_fetchall(
        """
        SELECT id FROM custom_tasks
        WHERE reminder_id = ? AND user_id = ? AND reminder_date = ?
        """,
        (reminder_id, user_id, reminder_date),
    )
    if existing:
        await conn.execute(
            """
            UPDATE custom_tasks
            SET status = ?, updated_at = ?
            WHERE reminder_id = ? AND user_id = ? AND reminder_date = ?
            """,
            (status, now, reminder_id, user_id, reminder_date),
        )
    else:
        await conn.execute(
            """
            INSERT INTO custom_tasks
            (reminder_id, user_id, reminder_date, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (reminder_id, user_id, reminder_date, status, now, now),
        )
    await conn.commit()


# Regular home tasks (полотенца, постель и т.д.)
async def ensure_regular_tasks(
    conn: aiosqlite.Connection, user_id: int, local_date: Optional[str] = None
) -> None:
    """Seed default home tasks with zones/points if none exist."""
    cursor = await conn.execute(
        "SELECT COUNT(*) as cnt FROM regular_tasks WHERE user_id = ? AND (is_active IS NULL OR is_active=1)",
        (user_id,),
    )
    row = await cursor.fetchone()
    if row and row["cnt"] > 0:
        return
    today = local_date or datetime.date.today().isoformat()
    now = utc_now_str()
    defaults = [
        ("Полотенца", 7, "bathroom", 3),
        ("Полы/пылесос", 7, "hallway", 3),
        ("Ванна/раковина/унитаз", 7, "bathroom", 3),
        ("Проверить холодильник", 7, "fridge", 3),
        ("Стол/поверхности на кухне", 7, "kitchen", 3),
        ("Постельное бельё", 14, "bedroom", 3),
        ("Генерально помыть холодильник", 30, "fridge", 5),
        ("Прожиг стиралки", 30, "laundry", 5),
        ("Плинтусы/ручки/выключатели", 30, "misc", 4),
        ("Уборка труднодоступных мест", 30, "misc", 4),
        ("Фильтр стиралки/пылесоса", 90, "laundry", 5),
        ("Разобрать аптечку", 90, "misc", 5),
        ("Разобрать хаос-угол", 90, "misc", 4),
    ]
    for title, freq, zone, points in defaults:
        next_due = datetime.date.fromisoformat(today) + datetime.timedelta(days=freq)
        await conn.execute(
            """
            INSERT INTO regular_tasks (user_id, title, frequency_days, zone, points, is_active, last_done_date, next_due_date, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, NULL, ?, ?, ?)
            """,
            (user_id, title, freq, zone, points, next_due.isoformat(), now, now),
        )
    await conn.commit()


async def list_regular_tasks(
    conn: aiosqlite.Connection,
    user_id: int,
    due_only: bool = False,
    local_date: Optional[str] = None,
    due_in_days: Optional[int] = None,
    include_inactive: bool = False,
) -> List[aiosqlite.Row]:
    conditions = ["user_id = ?"]
    params: list[Any] = [user_id]
    if not include_inactive:
        conditions.append("(is_active IS NULL OR is_active=1)")
    if due_only and local_date:
        conditions.append("date(next_due_date) <= date(?)")
        params.append(local_date)
    if due_in_days is not None and local_date:
        conditions.append("date(next_due_date) <= date(?, '+' || ? || ' day')")
        params.extend([local_date, due_in_days])
    where_clause = " AND ".join(conditions)
    cursor = await conn.execute(
        f"SELECT * FROM regular_tasks WHERE {where_clause} ORDER BY date(next_due_date), id",
        params,
    )
    return await cursor.fetchall()


async def next_regular_task_date(
    conn: aiosqlite.Connection, user_id: int
) -> Optional[str]:
    cursor = await conn.execute(
        "SELECT next_due_date FROM regular_tasks WHERE user_id = ? AND (is_active IS NULL OR is_active=1) ORDER BY date(next_due_date) LIMIT 1",
        (user_id,),
    )
    row = await cursor.fetchone()
    return row["next_due_date"] if row else None


async def mark_regular_done(
    conn: aiosqlite.Connection, user_id: int, task_id: int, done_date: str
) -> None:
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE regular_tasks
        SET last_done_date = ?, next_due_date = date(?, '+' || frequency_days || ' day'), updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (done_date, done_date, now, task_id, user_id),
    )
    await conn.commit()


async def postpone_regular_task(
    conn: aiosqlite.Connection, user_id: int, task_id: int, days: int = 1
) -> None:
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE regular_tasks
        SET next_due_date = date(next_due_date, '+' || ? || ' day'), updated_at = ?
        WHERE id = ? AND user_id = ? AND (is_active IS NULL OR is_active=1)
        """,
        (days, now, task_id, user_id),
    )
    await conn.commit()


async def set_regular_frequency(
    conn: aiosqlite.Connection, user_id: int, task_id: int, frequency_days: int
) -> None:
    freq = max(1, frequency_days)
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE regular_tasks
        SET frequency_days = ?, next_due_date = date(coalesce(last_done_date, next_due_date), '+' || ? || ' day'), updated_at = ?
        WHERE id = ? AND user_id = ? AND (is_active IS NULL OR is_active=1)
        """,
        (freq, freq, now, task_id, user_id),
    )
    await conn.commit()


async def list_regular_tasks_done_on_date(
    conn: aiosqlite.Connection, user_id: int, done_date: str
) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        """
        SELECT * FROM regular_tasks
        WHERE user_id = ? AND last_done_date = ? AND (is_active IS NULL OR is_active=1)
        ORDER BY id
        """,
        (user_id, done_date),
    )
    return await cursor.fetchall()


async def custom_statuses_for_date(
    conn: aiosqlite.Connection, user_id: int, reminder_date: str
) -> Dict[int, str]:
    cursor = await conn.execute(
        """
        SELECT reminder_id, status
        FROM custom_tasks
        WHERE user_id = ? AND reminder_date = ?
        """,
        (user_id, reminder_date),
    )
    rows = await cursor.fetchall()
    result: Dict[int, str] = {}
    for row in rows:
        result[row["reminder_id"]] = row["status"]
    return result


# Weights tracking
async def add_weight(conn: aiosqlite.Connection, user_id: int, weight: float) -> None:
    now = utc_now_str()
    await conn.execute(
        "INSERT INTO weights (user_id, weight, created_at) VALUES (?, ?, ?)",
        (user_id, weight, now),
    )
    await conn.commit()


async def set_body_profile(conn: aiosqlite.Connection, user_id: int, height: float, goal: str) -> None:
    now = utc_now_str()
    await conn.execute(
        """
        INSERT INTO weights (user_id, weight, created_at)
        VALUES (?, ?, ?)
        """,
        (user_id, -abs(height), now),
    )
    await conn.commit()


async def list_weights(conn: aiosqlite.Connection, user_id: int, limit: int = 10) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM weights WHERE user_id = ? ORDER BY datetime(created_at) DESC LIMIT ?",
        (user_id, limit),
    )
    return await cursor.fetchall()


async def weight_trend(conn: aiosqlite.Connection, user_id: int, days: int = 30) -> float:
    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    cursor = await conn.execute(
        "SELECT weight, created_at FROM weights WHERE user_id = ? AND datetime(created_at) >= datetime(?) ORDER BY datetime(created_at) ASC",
        (user_id, since.isoformat()),
    )
    rows = await cursor.fetchall()
    if len(rows) < 2:
        return 0.0
    return rows[-1]["weight"] - rows[0]["weight"]


async def upsert_regular_task(
    conn: aiosqlite.Connection,
    user_id: int,
    title: str,
    frequency_days: int,
    last_done_date: Optional[str],
    next_due_date: str,
    zone: str = "",
    points: int = 3,
    is_active: int = 1,
) -> None:
    now = utc_now_str()
    cursor = await conn.execute(
        "SELECT id FROM regular_tasks WHERE user_id = ? AND title = ?", (user_id, title)
    )
    row = await cursor.fetchone()
    if row:
        await conn.execute(
            """
            UPDATE regular_tasks
            SET frequency_days = ?, last_done_date = ?, next_due_date = ?, zone = ?, points = ?, is_active = ?, updated_at = ?
            WHERE id = ?
            """,
            (frequency_days, last_done_date, next_due_date, zone, points, is_active, now, row["id"]),
        )
    else:
        await conn.execute(
            """
            INSERT INTO regular_tasks (user_id, title, frequency_days, zone, points, is_active, last_done_date, next_due_date, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, title, frequency_days, zone, points, is_active, last_done_date, next_due_date, now, now),
        )
    await conn.commit()


async def deactivate_regular_task(conn: aiosqlite.Connection, user_id: int, task_id: int) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE regular_tasks SET is_active = 0, updated_at = ? WHERE id = ? AND user_id = ?",
        (now, task_id, user_id),
    )
    await conn.commit()


async def custom_stats(
    conn: aiosqlite.Connection, user_id: int, days: int = 7
) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        """
        SELECT reminder_date, status, COUNT(*) as cnt
        FROM custom_tasks
        WHERE user_id = ? AND date(reminder_date) >= date('now', ?)
        GROUP BY reminder_date, status
        """,
        (user_id, f"-{days} days"),
    )
    return await cursor.fetchall()


async def routine_stats(
    conn: aiosqlite.Connection, user_id: int, days: int = 7
) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        """
        SELECT routine_date, status, COUNT(*) as cnt
        FROM user_tasks
        WHERE user_id = ? AND date(routine_date) >= date('now', ?)
        GROUP BY routine_date, status
        """,
        (user_id, f"-{days} days"),
    )
    return await cursor.fetchall()


# Wellness settings
async def get_wellness(conn: aiosqlite.Connection, user_id: int) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM wellness_settings WHERE user_id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def upsert_wellness(
    conn: aiosqlite.Connection,
    user_id: int,
    water_enabled: Optional[int] = None,
    meal_enabled: Optional[int] = None,
    focus_mode: Optional[int] = None,
    water_last_key: Optional[str] = None,
    meal_last_key: Optional[str] = None,
    focus_work: Optional[int] = None,
    focus_rest: Optional[int] = None,
    tone: Optional[str] = None,
    water_times: Optional[str] = None,
    meal_times: Optional[str] = None,
    meal_profile: Optional[str] = None,
) -> None:
    now = utc_now_str()
    existing = await get_wellness(conn, user_id)
    if existing:
        water = water_enabled if water_enabled is not None else existing["water_enabled"]
        meal = meal_enabled if meal_enabled is not None else existing["meal_enabled"]
        focus = focus_mode if focus_mode is not None else existing["focus_mode"]
        wkey = water_last_key if water_last_key is not None else existing.get("water_last_key", "")
        mkey = meal_last_key if meal_last_key is not None else existing.get("meal_last_key", "")
        work = focus_work if focus_work is not None else existing.get("focus_work", 20)
        rest = focus_rest if focus_rest is not None else existing.get("focus_rest", 10)
        tone_val = tone if tone is not None else existing.get("tone", "neutral")
        wtimes = water_times if water_times is not None else existing.get("water_times", "11:00,16:00")
        mtimes = meal_times if meal_times is not None else existing.get("meal_times", "13:00,19:00")
        mprof = meal_profile if meal_profile is not None else existing.get("meal_profile", "omnivore")
        await conn.execute(
            """
            UPDATE wellness_settings
            SET water_enabled = ?, meal_enabled = ?, focus_mode = ?, water_last_key = ?, meal_last_key = ?, focus_work = ?, focus_rest = ?, tone = ?, water_times = ?, meal_times = ?, meal_profile = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (water, meal, focus, wkey, mkey, work, rest, tone_val, wtimes, mtimes, mprof, now, user_id),
        )
    else:
        await conn.execute(
            """
            INSERT INTO wellness_settings (user_id, water_enabled, meal_enabled, focus_mode, water_last_key, meal_last_key, focus_work, focus_rest, tone, water_times, meal_times, meal_profile, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                water_enabled or 0,
                meal_enabled or 0,
                focus_mode or 0,
                water_last_key or "",
                meal_last_key or "",
                focus_work if focus_work is not None else 20,
                focus_rest if focus_rest is not None else 10,
                tone or "neutral",
                water_times or "11:00,16:00",
                meal_times or "13:00,19:00",
                meal_profile or "omnivore",
                now,
                now,
            ),
        )
    await conn.commit()


# Expenses
async def add_expense(conn: aiosqlite.Connection, user_id: int, amount: float, category: str) -> int:
    now = utc_now_str()
    cursor = await conn.execute(
        "INSERT INTO expenses (user_id, amount, category, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, amount, category, "", now),
    )
    await conn.commit()
    return cursor.lastrowid


async def expenses_last_days(
    conn: aiosqlite.Connection, user_id: int, days: int = 7
) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        """
        SELECT * FROM expenses
        WHERE user_id = ? AND datetime(created_at) >= datetime('now', ?)
        ORDER BY created_at DESC
        """,
        (user_id, f"-{days} days"),
    )
    return await cursor.fetchall()


async def monthly_expense_sum(conn: aiosqlite.Connection, user_id: int) -> float:
    cursor = await conn.execute(
        """
        SELECT COALESCE(SUM(amount),0) as total
        FROM expenses
        WHERE user_id = ? AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        """,
        (user_id,),
    )
    row = await cursor.fetchone()
    return row["total"] if row else 0.0


async def category_expense_sum(
    conn: aiosqlite.Connection, user_id: int, category: str, days: int = 30
) -> float:
    cursor = await conn.execute(
        """
        SELECT COALESCE(SUM(amount),0) as total
        FROM expenses
        WHERE user_id = ? AND category = ? AND datetime(created_at) >= datetime('now', ?)
        """,
        (user_id, category, f"-{days} days"),
    )
    row = await cursor.fetchone()
    return row["total"] if row else 0.0


# Budget limits
async def get_budget(conn: aiosqlite.Connection, user_id: int) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute("SELECT * FROM budgets WHERE user_id = ?", (user_id,))
    return await cursor.fetchone()


async def upsert_budget(
    conn: aiosqlite.Connection, user_id: int, monthly_limit: float, payday_day: int | None = None, food_budget: float | None = None
) -> None:
    now = utc_now_str()
    existing = await get_budget(conn, user_id)
    payday_val = payday_day if payday_day is not None else None
    food_val = food_budget if food_budget is not None else None
    if existing:
        cols = ["monthly_limit = ?"]
        params = [monthly_limit]
        if payday_val is not None:
            cols.append("payday_day = ?")
            params.append(payday_val)
        if food_val is not None:
            cols.append("food_budget = ?")
            params.append(food_val)
        params += [now, user_id]
        await conn.execute(
            f"UPDATE budgets SET {', '.join(cols)}, updated_at = ? WHERE user_id = ?",
            params,
        )
    else:
        payday_to_save = payday_val if payday_val is not None else 1
        food_to_save = food_val if food_val is not None else 0
        await conn.execute(
            "INSERT INTO budgets (user_id, monthly_limit, payday_day, food_budget, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, monthly_limit, payday_to_save, food_to_save, now, now),
        )
    await conn.commit()


async def upsert_budget_category(
    conn: aiosqlite.Connection, user_id: int, category: str, limit_amount: float
) -> None:
    now = utc_now_str()
    existing = await conn.execute_fetchall(
        "SELECT id FROM budget_categories WHERE user_id = ? AND category = ?",
        (user_id, category),
    )
    if existing:
        await conn.execute(
            "UPDATE budget_categories SET limit_amount = ?, updated_at = ? WHERE user_id = ? AND category = ?",
            (limit_amount, now, user_id, category),
        )
    else:
        await conn.execute(
            """
            INSERT INTO budget_categories (user_id, category, limit_amount, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, category, limit_amount, now, now),
        )
    await conn.commit()


async def list_budget_categories(conn: aiosqlite.Connection, user_id: int) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM budget_categories WHERE user_id = ? ORDER BY category", (user_id,)
    )
    return await cursor.fetchall()


async def expenses_between(
    conn: aiosqlite.Connection, user_id: int, date_from: str, date_to: str, categories: list[str] | None = None
) -> float:
    """Sum expenses in [date_from, date_to) based on date(created_at)."""
    sql = """
        SELECT COALESCE(SUM(amount),0) as total
        FROM expenses
        WHERE user_id = ?
          AND date(created_at) >= date(?)
          AND date(created_at) < date(?)
    """
    params: list[Any] = [user_id, date_from, date_to]
    if categories:
        placeholders = ",".join("?" for _ in categories)
        sql += f" AND lower(category) IN ({placeholders})"
        params.extend([c.lower() for c in categories])
    cursor = await conn.execute(sql, params)
    row = await cursor.fetchone()
    return row["total"] if row else 0.0


async def set_routine_sent(
    conn: aiosqlite.Connection, user_id: int, routine_id: int, local_date: str
) -> None:
    await conn.execute(
        """
        UPDATE user_routines
        SET last_sent_date = ?
        WHERE user_id = ? AND routine_id = ?
        """,
        (local_date, user_id, routine_id),
    )
    await conn.commit()


async def ensure_user_tasks_for_date(
    conn: aiosqlite.Connection, user_id: int, routine_date: str
) -> None:
    """Ensure pending tasks exist for all routines for a given date."""
    routines = await list_user_routines(conn, user_id)
    now = utc_now_str()
    for routine in routines:
        existing = await conn.execute_fetchall(
            """
            SELECT id FROM user_tasks
            WHERE user_id = ? AND routine_id = ? AND routine_date = ?
            """,
            (user_id, routine["routine_id"], routine_date),
        )
        if existing:
            continue
        await conn.execute(
            """
            INSERT INTO user_tasks
            (user_id, routine_id, routine_date, status, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, '', ?, ?)
            """,
            (user_id, routine["routine_id"], routine_date, "pending", now, now),
        )
    await conn.commit()


async def upsert_user_task(
    conn: aiosqlite.Connection,
    user_id: int,
    routine_id: int,
    routine_date: str,
    status: str,
    note: str = "",
) -> None:
    now = utc_now_str()
    existing = await conn.execute_fetchall(
        """
        SELECT id FROM user_tasks
        WHERE user_id = ? AND routine_id = ? AND routine_date = ?
        """,
        (user_id, routine_id, routine_date),
    )
    if existing:
        await conn.execute(
            """
            UPDATE user_tasks
            SET status = ?, note = ?, updated_at = ?
            WHERE user_id = ? AND routine_id = ? AND routine_date = ?
            """,
            (status, note, now, user_id, routine_id, routine_date),
        )
    else:
        await conn.execute(
            """
            INSERT INTO user_tasks
            (user_id, routine_id, routine_date, status, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, routine_id, routine_date, status, note, now, now),
        )
    await conn.commit()


async def get_tasks_for_day(
    conn: aiosqlite.Connection, user_id: int, routine_date: str
) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        """
        SELECT ut.*, r.title
        FROM user_tasks ut
        JOIN routines r ON r.id = ut.routine_id
        WHERE ut.user_id = ? AND ut.routine_date = ?
        ORDER BY ut.routine_id
        """,
        (user_id, routine_date),
    )
    return await cursor.fetchall()


async def get_user_task(conn: aiosqlite.Connection, user_id: int, routine_id: int, routine_date: str) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute(
        """
        SELECT * FROM user_tasks
        WHERE user_id = ? AND routine_id = ? AND routine_date = ?
        """,
        (user_id, routine_id, routine_date),
    )
    return await cursor.fetchone()


async def update_task_note(conn: aiosqlite.Connection, user_id: int, routine_id: int, routine_date: str, note: str) -> None:
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE user_tasks
        SET note = ?, updated_at = ?
        WHERE user_id = ? AND routine_id = ? AND routine_date = ?
        """,
        (note, now, user_id, routine_id, routine_date),
    )
    await conn.commit()


async def list_articles_by_category(
    conn: aiosqlite.Connection, category: str
) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        """
        SELECT * FROM knowledge_articles
        WHERE category = ?
        ORDER BY id
        """,
        (category,),
    )
    return await cursor.fetchall()


async def list_articles_by_tag(
    conn: aiosqlite.Connection, tag: str
) -> List[aiosqlite.Row]:
    like = f"%{tag}%"
    cursor = await conn.execute(
        """
        SELECT * FROM knowledge_articles
        WHERE tags LIKE ?
        ORDER BY id
        """,
        (like,),
    )
    return await cursor.fetchall()


async def get_article(
    conn: aiosqlite.Connection, article_id: int
) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM knowledge_articles WHERE id = ?", (article_id,)
    )
    return await cursor.fetchone()


# Bills
async def list_bills(conn: aiosqlite.Connection, user_id: int) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM bills WHERE user_id = ? ORDER BY day_of_month", (user_id,)
    )
    return await cursor.fetchall()


async def upsert_bill(
    conn: aiosqlite.Connection, user_id: int, title: str, amount: float, day_of_month: int
) -> int:
    now = utc_now_str()
    cursor = await conn.execute(
        "SELECT id FROM bills WHERE user_id = ? AND title = ?", (user_id, title)
    )
    row = await cursor.fetchone()
    if row:
        await conn.execute(
            """
            UPDATE bills SET amount = ?, day_of_month = ?, updated_at = ?
            WHERE id = ?
            """,
            (amount, day_of_month, now, row["id"]),
        )
        await conn.commit()
        return row["id"]
    cursor = await conn.execute(
        """
        INSERT INTO bills (user_id, title, amount, day_of_month, last_paid_month, created_at, updated_at)
        VALUES (?, ?, ?, ?, '', ?, ?)
        """,
        (user_id, title, amount, day_of_month, now, now),
    )
    await conn.commit()
    return cursor.lastrowid


async def mark_bill_paid(conn: aiosqlite.Connection, user_id: int, bill_id: int, month: str) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE bills SET last_paid_month = ?, updated_at = ? WHERE id = ? AND user_id = ?",
        (month, now, bill_id, user_id),
    )
    await conn.commit()


async def bills_due_soon(
    conn: aiosqlite.Connection, user_id: int, local_date: str, days_ahead: int = 3
) -> List[Dict[str, Any]]:
    bills = await list_bills(conn, user_id)
    today = datetime.date.fromisoformat(local_date)
    window = today + datetime.timedelta(days=days_ahead)
    current_month = today.strftime("%Y-%m")
    due = []
    for b in bills:
        day = max(1, min(28, int(b["day_of_month"] or 1)))
        try:
            due_date = today.replace(day=day)
        except ValueError:
            # handle feb, etc.
            last_day = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)
            due_date = last_day
        if b["last_paid_month"] == current_month:
            continue
        if today <= due_date <= window:
            due.append(dict(b) | {"due_date": due_date.isoformat()})
    return due


# Points / геймификация
async def add_points(
    conn: aiosqlite.Connection, user_id: int, delta: int, local_date: Optional[str] = None
) -> None:
    """Изменить очки, не опускаясь ниже нуля."""
    if delta == 0:
        return
    if local_date is None:
        local_date = datetime.date.today().isoformat()
    now = utc_now_str()
    cursor = await conn.execute(
        "SELECT points_total, points_month FROM users WHERE id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    total = max(0, (row["points_total"] if row else 0) + delta)
    month = max(0, (row["points_month"] if row else 0) + delta)
    await conn.execute(
        "UPDATE users SET points_total = ?, points_month = ?, updated_at = ? WHERE id = ?",
        (total, month, now, user_id),
    )
    await conn.execute(
        "INSERT INTO points_log (user_id, points, local_date, created_at) VALUES (?, ?, ?, ?)",
        (user_id, delta, local_date, now),
    )
    await conn.commit()


async def points_window(conn: aiosqlite.Connection, user_id: int, days: int = 7) -> int:
    since = datetime.date.today() - datetime.timedelta(days=days - 1)
    cursor = await conn.execute(
        "SELECT COALESCE(SUM(points),0) as pts FROM points_log WHERE user_id = ? AND local_date >= ?",
        (user_id, since.isoformat()),
    )
    row = await cursor.fetchone()
    return row["pts"] if row else 0


async def points_today(conn: aiosqlite.Connection, user_id: int, local_date: Optional[str] = None) -> int:
    if local_date is None:
        local_date = datetime.date.today().isoformat()
    cursor = await conn.execute(
        "SELECT COALESCE(SUM(points),0) as pts FROM points_log WHERE user_id = ? AND local_date = ?",
        (user_id, local_date),
    )
    row = await cursor.fetchone()
    return row["pts"] if row else 0


async def points_streak(conn: aiosqlite.Connection, user_id: int, today: Optional[str] = None) -> int:
    """Подряд дней с очками, включая сегодня (или переданную дату)."""
    if today is None:
        today = datetime.date.today().isoformat()
    cursor = await conn.execute(
        "SELECT local_date, SUM(points) as pts FROM points_log WHERE user_id = ? GROUP BY local_date ORDER BY local_date DESC",
        (user_id,),
    )
    rows = await cursor.fetchall()
    dates_with_points = {row["local_date"] for row in rows if row["pts"] and row["pts"] > 0}
    streak = 0
    current = datetime.date.fromisoformat(today)
    while True:
        if current.isoformat() in dates_with_points:
            streak += 1
            current -= datetime.timedelta(days=1)
        else:
            break
    return streak


async def home_stats_window(conn: aiosqlite.Connection, user_id: int, days: int = 7) -> Tuple[int, int]:
    """Количество дел по дому и очков за последние N дней."""
    since = datetime.date.today() - datetime.timedelta(days=days - 1)
    cursor = await conn.execute(
        """
        SELECT COUNT(*) as cnt, COALESCE(SUM(points),0) as pts
        FROM regular_tasks
        WHERE user_id = ? AND last_done_date IS NOT NULL AND date(last_done_date) >= date(?)
          AND (is_active IS NULL OR is_active=1)
        """,
        (user_id, since.isoformat()),
    )
    row = await cursor.fetchone()
    return (row["cnt"] if row else 0, row["pts"] if row else 0)


async def reset_month_points(conn: aiosqlite.Connection, current_month: str) -> None:
    await conn.execute(
        "UPDATE users SET points_month = 0, last_points_reset = ?, updated_at = ?",
        (current_month, utc_now_str()),
    )
    await conn.commit()
