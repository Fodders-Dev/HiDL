import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite


def utc_now_str() -> str:
    return datetime.datetime.utcnow().isoformat()


def calc_next_due(base_date: str, freq_days: int) -> str:
    """Helper: add freq_days to base_date (ISO) and return ISO string."""
    try:
        base = datetime.date.fromisoformat(base_date)
    except Exception:
        base = datetime.date.today()
    return (base + datetime.timedelta(days=freq_days)).isoformat()


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
    gender: str = "neutral",
) -> int:
    now = utc_now_str()
    try:
        cursor = await conn.execute(
            """
            INSERT INTO users
            (telegram_id, name, timezone, wake_up_time, sleep_time, strictness, goals, gender, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                name,
                timezone,
                wake_up_time,
                sleep_time,
                strictness,
                goals,
                gender,
                now,
                now,
            ),
        )
    except Exception:
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


async def update_user_gender(
    conn: aiosqlite.Connection, user_id: int, gender: str
) -> None:
    """Update user's gender (male/female/neutral) for personalized messages."""
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET gender = ?, updated_at = ? WHERE id = ?",
        (gender, now, user_id),
    )
    await conn.commit()


# --- Households / общий дом ---


async def get_or_create_household(conn: aiosqlite.Connection, user_id: int) -> int:
    """
    Вернуть household_id пользователя, при необходимости создав личный дом.

    По умолчанию у каждого пользователя есть свой «личный дом». Если позже
    он создаст общий дом через настройки, household_id можно будет поменять.
    """
    cursor = await conn.execute(
        "SELECT household_id, name FROM users WHERE id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    if row and row["household_id"]:
        return int(row["household_id"])
    name = row["name"] if row else ""
    now = utc_now_str()
    invite_code = f"H{user_id}"
    cur = await conn.execute(
        "INSERT INTO households (name, invite_code, created_at) VALUES (?, ?, ?)",
        (name or "Дом", invite_code, now),
    )
    household_id = cur.lastrowid
    await conn.execute(
        "UPDATE users SET household_id = ? WHERE id = ?",
        (household_id, user_id),
    )
    await conn.commit()
    return household_id


async def get_household_by_code(
    conn: aiosqlite.Connection, invite_code: str
) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM households WHERE invite_code = ?", (invite_code.strip(),)
    )
    return await cursor.fetchone()


async def set_user_household(
    conn: aiosqlite.Connection, user_id: int, household_id: int
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET household_id = ?, updated_at = ? WHERE id = ?",
        (household_id, now, user_id),
    )
    await conn.commit()


# --- Day plans (evening / morning planning) ---


async def get_day_plan(
    conn: aiosqlite.Connection, user_id: int, plan_date: str
) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM day_plans WHERE user_id = ? AND plan_date = ?",
        (user_id, plan_date),
    )
    return await cursor.fetchone()


async def upsert_day_plan(
    conn: aiosqlite.Connection,
    user_id: int,
    plan_date: str,
    items: List[Dict[str, Any]],
) -> int:
    """
    Создать или обновить план дня и его пункты.

    items: список dict с ключами title, category, is_important (bool).
    """
    now = utc_now_str()
    cursor = await conn.execute(
        "SELECT id FROM day_plans WHERE user_id = ? AND plan_date = ?",
        (user_id, plan_date),
    )
    row = await cursor.fetchone()
    if row:
        plan_id = row["id"]
        await conn.execute(
            "UPDATE day_plans SET updated_at = ? WHERE id = ?",
            (now, plan_id),
        )
        await conn.execute("DELETE FROM day_plan_items WHERE plan_id = ?", (plan_id,))
    else:
        cursor = await conn.execute(
            """
            INSERT INTO day_plans (user_id, plan_date, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, plan_date, now, now),
        )
        plan_id = cursor.lastrowid
    for item in items:
        await conn.execute(
            """
            INSERT INTO day_plan_items
            (plan_id, user_id, title, category, is_important, done, synced_to_today, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (
                plan_id,
                user_id,
                item.get("title", "").strip(),
                item.get("category", "misc"),
                1 if item.get("is_important") else 0,
                now,
                now,
            ),
        )
    await conn.commit()
    return plan_id


async def list_day_plan_items(
    conn: aiosqlite.Connection, user_id: int, plan_date: str
) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        """
        SELECT dpi.*
        FROM day_plans dp
        JOIN day_plan_items dpi ON dpi.plan_id = dp.id
        WHERE dp.user_id = ? AND dp.plan_date = ?
        ORDER BY dpi.is_important DESC, dpi.id
        """,
        (user_id, plan_date),
    )
    return await cursor.fetchall()


async def mark_day_plan_item_done(
    conn: aiosqlite.Connection, item_id: int, done: bool = True
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE day_plan_items SET done = ?, updated_at = ? WHERE id = ?",
        (1 if done else 0, now, item_id),
    )
    await conn.commit()


async def mark_day_plan_items_synced(
    conn: aiosqlite.Connection, item_ids: List[int]
) -> None:
    if not item_ids:
        return
    now = utc_now_str()
    placeholders = ",".join("?" for _ in item_ids)
    params: Tuple[Any, ...] = tuple(item_ids)
    await conn.execute(
        f"UPDATE day_plan_items SET synced_to_today = 1, updated_at = ? WHERE id IN ({placeholders})",
        (now, *params),
    )
    await conn.commit()


async def mark_day_plan_morning_sent(
    conn: aiosqlite.Connection, plan_id: int, date_str: str
) -> None:
    """Пометить, что утренний пинг по плану был отправлен."""
    now = utc_now_str()
    await conn.execute(
        "UPDATE day_plans SET morning_sent = ?, updated_at = ? WHERE id = ?",
        (date_str, now, plan_id),
    )
    await conn.commit()


async def add_day_plan_item(
    conn: aiosqlite.Connection,
    user_id: int,
    plan_date: str,
    title: str,
    category: str = "misc",
    is_important: bool = False,
) -> None:
    """Добавить пункт в план дня, создавая план при необходимости."""
    now = utc_now_str()
    plan = await get_day_plan(conn, user_id, plan_date)
    if plan:
        plan_id = plan["id"]
        await conn.execute(
            "UPDATE day_plans SET updated_at = ? WHERE id = ?",
            (now, plan_id),
        )
    else:
        cursor = await conn.execute(
            """
            INSERT INTO day_plans (user_id, plan_date, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, plan_date, now, now),
        )
        plan_id = cursor.lastrowid
    await conn.execute(
        """
        INSERT INTO day_plan_items
        (plan_id, user_id, title, category, is_important, done, synced_to_today, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)
        """,
        (
            plan_id,
            user_id,
            title.strip(),
            category,
            1 if is_important else 0,
            now,
            now,
        ),
    )
    await conn.commit()


async def delete_day_plan_item(
    conn: aiosqlite.Connection, user_id: int, item_id: int
) -> None:
    """Удалить пункт плана дня пользователя."""
    await conn.execute(
        "DELETE FROM day_plan_items WHERE id = ? AND user_id = ?",
        (item_id, user_id),
    )
    await conn.commit()


# --- Продукты на кухне (pantry) ---


async def create_pantry_item(
    conn: aiosqlite.Connection,
    user_id: int,
    name: str,
    amount: float,
    unit: str,
    expires_at: Optional[str],
    category: str,
    low_threshold: Optional[float] = None,
) -> int:
    """Создать запись о продукте на кухне."""
    now = utc_now_str()
    household_id = await get_or_create_household(conn, user_id)
    low_value = float(low_threshold) if low_threshold is not None else None
    cursor = await conn.execute(
        """
        INSERT INTO pantry_items (user_id, household_id, name, amount, unit, expires_at, category, low_threshold, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (user_id, household_id, name.strip(), float(amount or 0), unit.strip(), expires_at, category.strip(), low_value, now, now),
    )
    await conn.commit()
    return cursor.lastrowid


async def list_pantry_items(conn: aiosqlite.Connection, user_id: int) -> List[aiosqlite.Row]:
    """Получить все продукты пользователя, отсортированные по категории и названию."""
    household_id = await get_or_create_household(conn, user_id)
    cursor = await conn.execute(
        """
        SELECT * FROM pantry_items
        WHERE household_id = ? AND (is_active IS NULL OR is_active = 1)
        ORDER BY category, name
        """,
        (household_id,),
    )
    return await cursor.fetchall()


async def update_pantry_item(
    conn: aiosqlite.Connection,
    user_id: int,
    item_id: int,
    amount: Optional[float] = None,
    expires_at: Optional[str] = None,
    low_threshold: Optional[float] = None,
    is_active: Optional[bool] = None,
) -> None:
    """Обновить количество, срок годности и дополнительные поля продукта."""
    fields: List[str] = []
    params: List[Any] = []
    if amount is not None:
        fields.append("amount = ?")
        params.append(float(amount))
    if expires_at is not None:
        fields.append("expires_at = ?")
        params.append(expires_at)
    if low_threshold is not None:
        fields.append("low_threshold = ?")
        params.append(float(low_threshold))
    if is_active is not None:
        fields.append("is_active = ?")
        params.append(1 if is_active else 0)
    if not fields:
        return
    now = utc_now_str()
    household_id = await get_or_create_household(conn, user_id)
    fields.append("updated_at = ?")
    params.append(now)
    params.extend([household_id, item_id])
    sql = f"UPDATE pantry_items SET {', '.join(fields)} WHERE household_id = ? AND id = ?"
    await conn.execute(sql, tuple(params))
    await conn.commit()


async def delete_pantry_item(
    conn: aiosqlite.Connection, user_id: int, item_id: int
) -> None:
    """Мягко удалить продукт из кладовки (отметить как неактивный)."""
    now = utc_now_str()
    household_id = await get_or_create_household(conn, user_id)
    await conn.execute(
        "UPDATE pantry_items SET is_active = 0, updated_at = ? WHERE household_id = ? AND id = ?",
        (now, household_id, item_id),
    )
    await conn.commit()


async def pantry_expiring(
    conn: aiosqlite.Connection,
    user_id: int,
    local_date: str,
    window_days: int = 5,
) -> Tuple[List[aiosqlite.Row], List[aiosqlite.Row]]:
    """
    Вернуть продукты, у которых скоро истекает срок, и уже просроченные.

    :returns: (soon, expired)
    """
    household_id = await get_or_create_household(conn, user_id)
    cursor = await conn.execute(
        """
        SELECT * FROM pantry_items
        WHERE household_id = ? AND expires_at IS NOT NULL AND (is_active IS NULL OR is_active = 1)
        """,
        (household_id,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return [], []
    today = datetime.date.fromisoformat(local_date)
    soon: List[aiosqlite.Row] = []
    expired: List[aiosqlite.Row] = []
    for row in rows:
        try:
            exp_dt = datetime.date.fromisoformat(row["expires_at"])
        except Exception:
            continue
        delta = (exp_dt - today).days
        if delta < 0:
            expired.append(row)
        elif delta <= window_days:
            soon.append(row)
    return soon, expired


# --- Бытовая химия и расходники (supplies) ---


async def ensure_supplies(conn: aiosqlite.Connection, user_id: int) -> None:
    """
    Создать базовый список бытовой химии для пользователя, если его ещё нет.

    Это аналог ensure_regular_tasks, но для расходников.
    """
    cursor = await conn.execute(
        "SELECT COUNT(*) AS cnt FROM supplies WHERE user_id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    if row and row["cnt"] > 0:
        return
    now = utc_now_str()
    defaults = [
        ("Мусорные пакеты", "расходники"),
        ("Средство для посуды", "бытовая химия"),
        ("Средство для пола", "бытовая химия"),
        ("Средство для унитаза", "бытовая химия"),
        ("Губки/тряпки", "расходники"),
    ]
    for name, category in defaults:
        await conn.execute(
            """
            INSERT INTO supplies (user_id, name, category, status, created_at, updated_at)
            VALUES (?, ?, ?, 'full', ?, ?)
            """,
            (user_id, name, category, now, now),
        )
    await conn.commit()


async def list_supplies(conn: aiosqlite.Connection, user_id: int) -> List[aiosqlite.Row]:
    """Вернуть все записи по бытовой химии и расходникам пользователя."""
    cursor = await conn.execute(
        """
        SELECT * FROM supplies
        WHERE user_id = ?
        ORDER BY name
        """,
        (user_id,),
    )
    return await cursor.fetchall()


async def update_supply_status(
    conn: aiosqlite.Connection, user_id: int, supply_id: int, status: str
) -> None:
    """Обновить статус конкретного расходника (full/low/empty)."""
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE supplies
        SET status = ?, updated_at = ?
        WHERE user_id = ? AND id = ?
        """,
        (status, now, user_id, supply_id),
    )
    await conn.commit()


async def insert_receipt_photo(
    conn: aiosqlite.Connection,
    user_id: int,
    file_id: str,
) -> int:
    """Сохранить фото чека для будущего распознавания."""
    now = utc_now_str()
    cursor = await conn.execute(
        """
        INSERT INTO receipt_photos (user_id, file_id, created_at)
        VALUES (?, ?, ?)
        """,
        (user_id, file_id, now),
    )
    await conn.commit()
    return cursor.lastrowid


# --- Meds / vitamins ---


async def create_med(
    conn: aiosqlite.Connection,
    user_id: int,
    name: str,
    dose_text: str,
    schedule_type: str,
    times: str,
    days_of_week: str | None,
    notes: str = "",
) -> int:
    now = utc_now_str()
    cursor = await conn.execute(
        """
        INSERT INTO meds (user_id, name, dose_text, schedule_type, times, days_of_week, notes, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (user_id, name.strip(), dose_text.strip(), schedule_type, times, days_of_week, notes.strip(), now, now),
    )
    await conn.commit()
    return cursor.lastrowid


async def list_meds(conn: aiosqlite.Connection, user_id: int, active_only: bool = True) -> List[aiosqlite.Row]:
    if active_only:
        cursor = await conn.execute(
            "SELECT * FROM meds WHERE user_id = ? AND active = 1 ORDER BY id",
            (user_id,),
        )
    else:
        cursor = await conn.execute(
            "SELECT * FROM meds WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
    return await cursor.fetchall()


async def get_med(conn: aiosqlite.Connection, med_id: int) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute("SELECT * FROM meds WHERE id = ?", (med_id,))
    return await cursor.fetchone()


async def set_med_active(conn: aiosqlite.Connection, med_id: int, active: bool) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE meds SET active = ?, updated_at = ? WHERE id = ?",
        (1 if active else 0, now, med_id),
    )
    await conn.commit()


async def update_med_times(
    conn: aiosqlite.Connection,
    med_id: int,
    schedule_type: str,
    times: str,
) -> None:
    """Обновить тип расписания и часы приёма для курса витаминов/лекарств."""
    now = utc_now_str()
    await conn.execute(
        "UPDATE meds SET schedule_type = ?, times = ?, updated_at = ? WHERE id = ?",
        (schedule_type, times, now, med_id),
    )
    await conn.commit()


async def insert_med_log(
    conn: aiosqlite.Connection,
    user_id: int,
    med_id: int,
    plan_date: str,
    planned_time: str,
) -> int:
    now = utc_now_str()
    cursor = await conn.execute(
        """
        INSERT INTO med_logs (user_id, med_id, taken_at, plan_date, planned_time, created_at, updated_at)
        VALUES (?, ?, NULL, ?, ?, ?, ?)
        """,
        (user_id, med_id, plan_date, planned_time, now, now),
    )
    await conn.commit()
    return cursor.lastrowid


async def get_med_log_by_key(
    conn: aiosqlite.Connection,
    user_id: int,
    med_id: int,
    plan_date: str,
    planned_time: str,
) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute(
        """
        SELECT * FROM med_logs
        WHERE user_id = ? AND med_id = ? AND plan_date = ? AND planned_time = ?
        """,
        (user_id, med_id, plan_date, planned_time),
    )
    return await cursor.fetchone()


async def get_med_log(conn: aiosqlite.Connection, log_id: int) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute("SELECT * FROM med_logs WHERE id = ?", (log_id,))
    return await cursor.fetchone()


async def set_med_taken(conn: aiosqlite.Connection, log_id: int, taken_at: str | None = None) -> None:
    now = utc_now_str()
    taken = taken_at or now
    await conn.execute(
        "UPDATE med_logs SET taken_at = ?, updated_at = ? WHERE id = ?",
        (taken, now, log_id),
    )
    await conn.commit()


async def meds_stats_for_date(
    conn: aiosqlite.Connection, user_id: int, plan_date: str
) -> tuple[int, int]:
    cursor = await conn.execute(
        """
        SELECT
          COUNT(*) as total,
          SUM(CASE WHEN taken_at IS NOT NULL THEN 1 ELSE 0 END) as taken
        FROM med_logs
        WHERE user_id = ? AND plan_date = ?
        """,
        (user_id, plan_date),
    )
    row = await cursor.fetchone()
    if not row:
        return 0, 0
    total = row["total"] or 0
    taken = row["taken"] or 0
    return total, taken


async def list_med_logs_for_date(
    conn: aiosqlite.Connection, user_id: int, plan_date: str
) -> List[aiosqlite.Row]:
    """
    Вернуть все напоминания по таблеткам/витаминам на выбранную дату
    вместе с названиями курсов.
    """
    cursor = await conn.execute(
        """
        SELECT l.*, m.name, m.dose_text
        FROM med_logs l
        JOIN meds m ON m.id = l.med_id
        WHERE l.user_id = ? AND l.plan_date = ?
        ORDER BY l.planned_time
        """,
        (user_id, plan_date),
    )
    return await cursor.fetchall()


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


# --- routine_steps: пользовательские шаги рутин ---


async def ensure_routine_steps(conn: aiosqlite.Connection, user_id: int) -> None:
    """Создать пользовательские шаги рутины из шаблонов, если их ещё нет."""
    cursor = await conn.execute(
        "SELECT COUNT(*) as cnt FROM routine_steps WHERE user_id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    if row and row["cnt"] > 0:
        await _migrate_routine_steps(conn, user_id)
        return
    routines = await list_routines(conn)
    now = utc_now_str()
    for routine in routines:
        routine_type = routine["routine_key"]
        items = await get_routine_items(conn, routine["id"])
        order = 1
        for item in items:
            title = item["title"]
            await conn.execute(
                """
                INSERT INTO routine_steps
                (user_id, routine_type, title, order_index, points, is_active, trigger_after_step_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, 1, NULL, ?, ?)
                """,
                (user_id, routine_type, title, order, now, now),
            )
            order += 1
    await conn.commit()
    await _migrate_routine_steps(conn, user_id)


async def list_routine_steps_for_routine(
    conn: aiosqlite.Connection, user_id: int, routine_id: int, include_inactive: bool = False
) -> List[aiosqlite.Row]:
    """Вернуть шаги рутины для пользователя (с учётом активности и порядка)."""
    await ensure_routine_steps(conn, user_id)
    routine = await get_routine_by_id(conn, routine_id)
    if not routine:
        return []
    routine_type = routine["routine_key"]
    if include_inactive:
        cursor = await conn.execute(
            """
            SELECT * FROM routine_steps
            WHERE user_id = ? AND routine_type = ?
            ORDER BY order_index, id
            """,
            (user_id, routine_type),
        )
    else:
        cursor = await conn.execute(
            """
            SELECT * FROM routine_steps
            WHERE user_id = ? AND routine_type = ? AND is_active = 1
            ORDER BY order_index, id
            """,
            (user_id, routine_type),
        )
    return await cursor.fetchall()


async def _migrate_routine_steps(conn: aiosqlite.Connection, user_id: int) -> None:
    """
    Лёгкая миграция пользовательских шагов рутин:
    - правим устаревшие формулировки (без удаления пользовательских пунктов);
    - добавляем недостающие базовые пункты (идемпотентно).
    """
    now = utc_now_str()
    recommended: dict[str, list[str]] = {
        "morning": [
            "Стакан воды (можно прямо у кровати)",
            "Умыться и почистить зубы",
            "Заправить кровать и открыть окно на 2–5 минут",
            "Зарядка 2–5 минут (шея/плечи/спина)",
            "Завтрак/перекус (без идеала, просто чтобы было топливо)",
            "Выбери 1 главное дело на сегодня (остальное — бонус)",
            "Проверь, что с собой ключи/телефон/карта (и зарядка, если нужно)",
        ],
        "day": [
            "Поесть нормально (хоть на 10 минут, без идеала)",
            "Стакан воды",
            "Чуть подвигаться: 10–15 минут на улице или пройтись по дому",
            "Один маленький шаг по главному делу (5–10 минут)",
            "Мини‑порядок 2 минуты (стол/раковина/мусор)",
        ],
        "evening": [
            "Лёгкий ужин/перекус (чтобы не ложиться на пустой желудок)",
            "Гигиена: умыться и зубы",
            "5 минут на дом: посуда/поверхность/мусор",
            "Собрать на завтра: ключи/зарядка/документы",
            "Тёплый душ или растяжка 3 минуты (снять напряжение)",
            "Проветрить комнату перед сном",
        ],
    }

    replacements: list[tuple[str, str]] = [
        ("Выпить воды", "Стакан воды (можно прямо у кровати)"),
        ("Заправить кровать и открыть окно", "Заправить кровать и открыть окно на 2–5 минут"),
        ("Съесть что-то простое (хотя бы 5 минут)", "Завтрак/перекус (без идеала, просто чтобы было топливо)"),
        ("Позавтракать чем угодно, не кофе", "Завтрак/перекус (без идеала, просто чтобы было топливо)"),
        ("Проверь, что есть чистые вещи на день", "Проверь, что с собой ключи/телефон/карта (и зарядка, если нужно)"),
        ("Проверь, что с собой ключи/кошелёк/телефон (и зарядка, если нужно)", "Проверь, что с собой ключи/телефон/карта (и зарядка, если нужно)"),
        ("Пообедать без фастфуда", "Поесть нормально (хоть на 10 минут, без идеала)"),
        ("Выйти на улицу хотя бы на 15 минут", "Чуть подвигаться: 10–15 минут на улице или пройтись по дому"),
        ("Разобрать посуду/кружки со стола", "Мини‑порядок 2 минуты (стол/раковина/мусор)"),
        ("Короткий душ и гигиена", "Гигиена: умыться и зубы"),
        ("Помыть посуду", "5 минут на дом: посуда/поверхность/мусор"),
        ("Подготовить одежду на завтра", "Собрать на завтра: ключи/зарядка/документы"),
        ("Сложить вещи по местам", "Собрать на завтра: ключи/зарядка/документы"),
    ]

    for routine_type in ("morning", "day", "evening"):
        for old, new in replacements:
            await conn.execute(
                """
                UPDATE routine_steps
                SET title = ?, updated_at = ?
                WHERE user_id = ? AND routine_type = ? AND title = ?
                """,
                (new, now, user_id, routine_type, old),
            )

        existing_rows = await conn.execute_fetchall(
            "SELECT title, order_index FROM routine_steps WHERE user_id = ? AND routine_type = ?",
            (user_id, routine_type),
        )
        existing_titles = {row["title"] for row in existing_rows}
        order_index = max([row["order_index"] for row in existing_rows], default=0)
        for title in recommended.get(routine_type, []):
            if title in existing_titles:
                continue
            order_index += 1
            await conn.execute(
                """
                INSERT INTO routine_steps
                (user_id, routine_type, title, order_index, points, is_active, trigger_after_step_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, 1, NULL, ?, ?)
                """,
                (user_id, routine_type, title, order_index, now, now),
            )

    await conn.commit()


async def toggle_routine_step(
    conn: aiosqlite.Connection, user_id: int, step_id: int
) -> None:
    """Включить/выключить шаг рутины."""
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE routine_steps
        SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END,
            updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (now, step_id, user_id),
    )
    await conn.commit()


async def update_routine_step_title(
    conn: aiosqlite.Connection, user_id: int, step_id: int, title: str
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE routine_steps SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
        (title.strip(), now, step_id, user_id),
    )
    await conn.commit()


async def add_routine_step(
    conn: aiosqlite.Connection,
    user_id: int,
    routine_type: str,
    title: str,
    after_step_id: Optional[int] = None,
    points: int = 1,
) -> int:
    """Добавить шаг в конец рутины или после указанного шага."""
    await ensure_routine_steps(conn, user_id)
    cursor = await conn.execute(
        "SELECT MAX(order_index) as mx FROM routine_steps WHERE user_id = ? AND routine_type = ?",
        (user_id, routine_type),
    )
    row = await cursor.fetchone()
    order_index = (row["mx"] or 0) + 1
    now = utc_now_str()
    cursor = await conn.execute(
        """
        INSERT INTO routine_steps
        (user_id, routine_type, title, order_index, points, is_active, trigger_after_step_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (user_id, routine_type, title.strip(), order_index, points, after_step_id, now, now),
    )
    await conn.commit()
    return cursor.lastrowid


async def set_routine_step_trigger(
    conn: aiosqlite.Connection,
    user_id: int,
    step_id: int,
    trigger_after_step_id: Optional[int],
) -> None:
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE routine_steps
        SET trigger_after_step_id = ?, updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (trigger_after_step_id, now, step_id, user_id),
    )
    await conn.commit()


async def get_routine_by_step(
    conn: aiosqlite.Connection, user_id: int, step_id: int
) -> Optional[Dict[str, Any]]:
    cursor = await conn.execute(
        """
        SELECT rs.*, r.id as routine_id
        FROM routine_steps rs
        JOIN routines r ON r.routine_key = rs.routine_type
        WHERE rs.user_id = ? AND rs.id = ?
        """,
        (user_id, step_id),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


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


async def update_custom_reminder_time(
    conn: aiosqlite.Connection, user_id: int, reminder_id: int, reminder_time: str
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE custom_reminders SET reminder_time = ?, updated_at = ? WHERE id = ? AND user_id = ?",
        (reminder_time, now, reminder_id, user_id),
    )
    await conn.commit()


async def update_custom_reminder_freq(
    conn: aiosqlite.Connection,
    user_id: int,
    reminder_id: int,
    frequency_days: int,
    target_weekday: Optional[int] = None,
) -> None:
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE custom_reminders
        SET frequency_days = ?, target_weekday = ?, updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (frequency_days, target_weekday, now, reminder_id, user_id),
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
    cursor = await conn.execute(
        "SELECT frequency_days FROM regular_tasks WHERE id = ? AND user_id = ?",
        (task_id, user_id),
    )
    row = await cursor.fetchone()
    freq = row["frequency_days"] if row else 7
    next_due = calc_next_due(done_date, freq)
    await conn.execute(
        """
        UPDATE regular_tasks
        SET last_done_date = ?, next_due_date = ?, updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (done_date, next_due, now, task_id, user_id),
    )
    await conn.commit()


async def postpone_regular_task(
    conn: aiosqlite.Connection, user_id: int, task_id: int, days: int = 1
) -> None:
    now = utc_now_str()
    cursor = await conn.execute(
        "SELECT next_due_date FROM regular_tasks WHERE id = ? AND user_id = ?",
        (task_id, user_id),
    )
    row = await cursor.fetchone()
    current_due = row["next_due_date"] if row and row["next_due_date"] else datetime.date.today().isoformat()
    new_due = calc_next_due(current_due, days)
    await conn.execute(
        """
        UPDATE regular_tasks
        SET next_due_date = ?, updated_at = ?
        WHERE id = ? AND user_id = ? AND (is_active IS NULL OR is_active=1)
        """,
        (new_due, now, task_id, user_id),
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
    expiring_window_days: Optional[int] = None,
    affirm_mode: Optional[str] = None,
    # Новые поля для аффирмаций 2.0
    affirm_enabled: Optional[int] = None,
    affirm_categories: Optional[str] = None,
    affirm_frequency: Optional[str] = None,
    affirm_hours: Optional[str] = None,
    meal_notify_enabled: Optional[int] = None,
    affirm_last_key: Optional[str] = None,
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
        exp_days = (
            expiring_window_days
            if expiring_window_days is not None
            else existing.get("expiring_window_days", 3)
        )
        affirm_val = affirm_mode if affirm_mode is not None else existing.get("affirm_mode", "off")
        # Новые поля
        affirm_en = affirm_enabled if affirm_enabled is not None else existing.get("affirm_enabled", 0)
        affirm_cats = affirm_categories if affirm_categories is not None else existing.get("affirm_categories", '["motivation","calm"]')
        affirm_freq = affirm_frequency if affirm_frequency is not None else existing.get("affirm_frequency", "daily")
        affirm_hrs = affirm_hours if affirm_hours is not None else existing.get("affirm_hours", "[9]")
        meal_notify = meal_notify_enabled if meal_notify_enabled is not None else existing.get("meal_notify_enabled", 1)
        affirm_lkey = affirm_last_key if affirm_last_key is not None else existing.get("affirm_last_key", "")
        
        await conn.execute(
            """
            UPDATE wellness_settings
            SET water_enabled = ?, meal_enabled = ?, focus_mode = ?, water_last_key = ?, meal_last_key = ?, 
                focus_work = ?, focus_rest = ?, tone = ?, water_times = ?, meal_times = ?, meal_profile = ?, 
                expiring_window_days = ?, affirm_mode = ?, affirm_enabled = ?, affirm_categories = ?,
                affirm_frequency = ?, affirm_hours = ?, meal_notify_enabled = ?, affirm_last_key = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (water, meal, focus, wkey, mkey, work, rest, tone_val, wtimes, mtimes, mprof, 
             exp_days, affirm_val, affirm_en, affirm_cats, affirm_freq, affirm_hrs, meal_notify, affirm_lkey, now, user_id),
        )
    else:
        await conn.execute(
            """
            INSERT INTO wellness_settings (user_id, water_enabled, meal_enabled, focus_mode, water_last_key, meal_last_key, 
                focus_work, focus_rest, tone, water_times, meal_times, meal_profile, expiring_window_days, affirm_mode,
                affirm_enabled, affirm_categories, affirm_frequency, affirm_hours, meal_notify_enabled, affirm_last_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                expiring_window_days if expiring_window_days is not None else 3,
                affirm_mode or "off",
                affirm_enabled or 0,
                affirm_categories or '["motivation","calm"]',
                affirm_frequency or "daily",
                affirm_hours or "[9]",
                meal_notify_enabled if meal_notify_enabled is not None else 1,
                affirm_last_key or "",
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


async def points_week(conn: aiosqlite.Connection, user_id: int, local_date: str) -> int:
    """Очки за текущую неделю (с понедельника по local_date включительно)."""
    try:
        d = datetime.date.fromisoformat(local_date)
    except Exception:
        d = datetime.date.today()
    week_start = d - datetime.timedelta(days=d.weekday())  # Monday
    cursor = await conn.execute(
        "SELECT COALESCE(SUM(points),0) as pts FROM points_log WHERE user_id = ? AND local_date >= ?",
        (user_id, week_start.isoformat()),
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


async def home_stats_since(conn: aiosqlite.Connection, user_id: int, since_date: str) -> Tuple[int, int]:
    """Количество дел по дому и очков с даты (YYYY-MM-DD) включительно."""
    cursor = await conn.execute(
        """
        SELECT COUNT(*) as cnt, COALESCE(SUM(points),0) as pts
        FROM regular_tasks
        WHERE user_id = ? AND last_done_date IS NOT NULL AND date(last_done_date) >= date(?)
          AND (is_active IS NULL OR is_active=1)
        """,
        (user_id, since_date),
    )
    row = await cursor.fetchone()
    return (row["cnt"] if row else 0, row["pts"] if row else 0)


async def reset_month_points(conn: aiosqlite.Connection, current_month: str) -> None:
    await conn.execute(
        "UPDATE users SET points_month = 0, last_points_reset = ?, updated_at = ?",
        (current_month, utc_now_str()),
    )
    await conn.commit()


# Shopping List Logic
async def create_shopping_item(
    conn: aiosqlite.Connection, user_id: int, name: str, qty: float = 1.0, unit: str = "шт", category: str = "misc"
) -> int:
    cursor = await conn.execute(
        "SELECT id, quantity FROM shopping_list WHERE user_id = ? AND item_name = ? AND is_bought = 0",
        (user_id, name)
    )
    row = await cursor.fetchone()
    now = utc_now_str()
    if row:
        await conn.execute(
            "UPDATE shopping_list SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
            (qty, now, row["id"])
        )
        await conn.commit()
        return row["id"]
    else:
        cursor = await conn.execute(
            "INSERT INTO shopping_list (user_id, item_name, quantity, unit, category, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, qty, unit, category, now, now)
        )
        await conn.commit()
        return cursor.lastrowid

async def list_shopping_items(conn: aiosqlite.Connection, user_id: int) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM shopping_list WHERE user_id = ? AND is_bought = 0 ORDER BY category, item_name",
        (user_id,)
    )
    return await cursor.fetchall()

async def mark_shopping_bought(conn: aiosqlite.Connection, user_id: int, item_id: int, bought: bool = True) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE shopping_list SET is_bought = ?, updated_at = ? WHERE id = ? AND user_id = ?",
        (1 if bought else 0, now, item_id, user_id)
    )
    await conn.commit()

async def delete_shopping_item(conn: aiosqlite.Connection, user_id: int, item_id: int) -> None:
    await conn.execute("DELETE FROM shopping_list WHERE id = ? AND user_id = ?", (item_id, user_id))
    await conn.commit()

async def complete_shopping_trip(conn: aiosqlite.Connection, user_id: int) -> int:
    """Move bought items from shopping list to pantry."""
    now = utc_now_str()
    # 1. Select bought items
    cursor = await conn.execute(
        "SELECT * FROM shopping_list WHERE user_id = ? AND is_bought = 1", (user_id,)
    )
    rows = await cursor.fetchall()
    count = 0
    for row in rows:
        pantry_cursor = await conn.execute(
            "SELECT id FROM pantry_items WHERE user_id = ? AND name = ?", (user_id, row["item_name"])
        )
        pantry_row = await pantry_cursor.fetchone()
        if pantry_row:
             await conn.execute(
                 "UPDATE pantry_items SET amount = amount + ?, updated_at = ? WHERE id = ?",
                 (row["quantity"], now, pantry_row["id"])
             )
        else:
             await conn.execute(
                 "INSERT INTO pantry_items (user_id, name, amount, unit, category, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (user_id, row["item_name"], row["quantity"], row["unit"], row["category"], now, now)
             )
        count += 1
    
    # 3. Delete from shopping list (only bought ones)
    await conn.execute("DELETE FROM shopping_list WHERE user_id = ? AND is_bought = 1", (user_id,))
    await conn.commit()
    return count


# Cleaning Session Logic

async def create_cleaning_session(
    conn: aiosqlite.Connection, user_id: int, mode: str, zones_json: str, steps_json: str
) -> int:
    now = utc_now_str()
    # Close any existing active sessions
    await conn.execute(
        "UPDATE cleaning_sessions SET status = 'abandoned', updated_at = ? WHERE user_id = ? AND status = 'active'",
        (now, user_id)
    )
    cursor = await conn.execute(
        "INSERT INTO cleaning_sessions (user_id, status, mode, zones_json, steps_json, current_step_index, created_at, updated_at) VALUES (?, 'active', ?, ?, ?, 0, ?, ?)",
        (user_id, mode, zones_json, steps_json, now, now)
    )
    await conn.commit()
    return cursor.lastrowid

async def get_active_session(conn: aiosqlite.Connection, user_id: int) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM cleaning_sessions WHERE user_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    )
    return await cursor.fetchone()

async def update_session_progress(
    conn: aiosqlite.Connection, session_id: int, current_step_index: int
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE cleaning_sessions SET current_step_index = ?, updated_at = ? WHERE id = ?",
        (current_step_index, now, session_id)
    )
    await conn.commit()

async def complete_session(conn: aiosqlite.Connection, session_id: int) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE cleaning_sessions SET status = 'completed', updated_at = ? WHERE id = ?",
        (now, session_id)
    )
    await conn.commit()

async def delete_session(conn: aiosqlite.Connection, session_id: int) -> None:
    await conn.execute("DELETE FROM cleaning_sessions WHERE id = ?", (session_id,))
    await conn.commit()
