import datetime
import hashlib
import json
import re
import secrets
import uuid
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from utils.rows import row_to_dict, rows_to_dicts
from db.database import independent_connection


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


async def update_user_profile_mode(conn: aiosqlite.Connection, user_id: int, mode: str) -> None:
    mode = str(mode or "").strip().lower()
    if mode not in {"adult", "student"}:
        raise ValueError("profile mode must be 'adult' or 'student'")
    await conn.execute(
        "UPDATE users SET profile_mode = ?, updated_at = ? WHERE id = ?",
        (mode, utc_now_str(), int(user_id)),
    )
    await conn.commit()


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


async def update_sleep_mode(
    conn: aiosqlite.Connection,
    user_id: int,
    enabled: bool,
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET sleep_mode_enabled = ?, updated_at = ? WHERE id = ?",
        (1 if enabled else 0, now, user_id),
    )
    await conn.commit()


async def update_sleep_targets(
    conn: aiosqlite.Connection,
    user_id: int,
    target_sleep: str,
    target_wake: str,
) -> None:
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE users
        SET sleep_target_sleep = ?, sleep_target_wake = ?, updated_at = ?
        WHERE id = ?
        """,
        (target_sleep, target_wake, now, user_id),
    )
    await conn.commit()


async def update_sleep_shift_settings(
    conn: aiosqlite.Connection,
    user_id: int,
    shift_step: int,
    shift_every: int,
    last_shift_date: str | None = None,
) -> None:
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE users
        SET sleep_shift_step = ?, sleep_shift_every = ?, sleep_last_shift_date = ?, updated_at = ?
        WHERE id = ?
        """,
        (shift_step, shift_every, last_shift_date, now, user_id),
    )
    await conn.commit()


async def update_sleep_ping(
    conn: aiosqlite.Connection,
    user_id: int,
    kind: str,
    local_date: str,
) -> None:
    now = utc_now_str()
    if kind == "evening":
        field = "sleep_last_evening_date"
    elif kind == "morning":
        field = "sleep_last_morning_date"
    else:
        return
    await conn.execute(
        f"UPDATE users SET {field} = ?, updated_at = ? WHERE id = ?",
        (local_date, now, user_id),
    )
    await conn.commit()


async def update_sleep_last_shift(
    conn: aiosqlite.Connection, user_id: int, local_date: str
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET sleep_last_shift_date = ?, updated_at = ? WHERE id = ?",
        (local_date, now, user_id),
    )
    await conn.commit()


async def update_daily_brief_sent(
    conn: aiosqlite.Connection, user_id: int, local_date: str
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET daily_brief_last_date = ?, updated_at = ? WHERE id = ?",
        (local_date, now, user_id),
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

DEFAULT_HOUSEHOLD_SETTINGS: Dict[str, bool] = {
    "share_recipes": True,
    "share_meal_plan": True,
    "share_bills": True,
    "notify_partner": True,
    "share_home_tasks": True,
    "share_supplies": True,
}


async def _household_sharing_state(
    conn: aiosqlite.Connection, user_id: int, setting_key: str
) -> tuple[int, bool]:
    hid = await get_or_create_household(conn, int(user_id))
    house = await get_household(conn, hid)
    enabled = bool((house or {}).get("settings", {}).get(setting_key, True))
    return hid, enabled


def generate_household_invite_code() -> str:
    """Сгенерировать случайный неугадываемый код приглашения в дом."""
    return f"H-{secrets.token_hex(4).upper()}"


def _decode_household_settings(raw: Any) -> Dict[str, bool]:
    merged = dict(DEFAULT_HOUSEHOLD_SETTINGS)
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                for key in merged:
                    if key in parsed:
                        merged[key] = bool(parsed[key])
        except Exception:
            pass
    return merged


async def get_household(conn: aiosqlite.Connection, household_id: int) -> Optional[Dict[str, Any]]:
    """Получить данные дома и его декодированные настройки."""
    cursor = await conn.execute("SELECT * FROM households WHERE id = ?", (int(household_id),))
    row = await cursor.fetchone()
    if not row:
        return None
    d = row_to_dict(row)
    d["settings"] = _decode_household_settings(d.get("settings_json"))
    return d


async def get_or_create_household(conn: aiosqlite.Connection, user_id: int) -> int:
    """
    Вернуть household_id пользователя, при необходимости создав личный дом.
    """
    cursor = await conn.execute(
        "SELECT household_id, name FROM users WHERE id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    if row and row["household_id"]:
        return int(row["household_id"])
    name = row["name"] if row else ""
    now = utc_now_str()
    invite_code = generate_household_invite_code()
    cur = await conn.execute(
        "INSERT INTO households (name, invite_code, settings_json, created_at) VALUES (?, ?, ?, ?)",
        (name or "Дом", invite_code, json.dumps(DEFAULT_HOUSEHOLD_SETTINGS), now),
    )
    household_id = int(cur.lastrowid)
    await conn.execute(
        "UPDATE users SET household_id = ? WHERE id = ?",
        (household_id, user_id),
    )
    await conn.commit()
    return household_id


async def get_household_by_code(
    conn: aiosqlite.Connection, invite_code: str
) -> Optional[aiosqlite.Row]:
    clean = (invite_code or "").strip()
    if not clean:
        return None
    cursor = await conn.execute(
        "SELECT * FROM households WHERE UPPER(invite_code) = UPPER(?)", (clean,)
    )
    return await cursor.fetchone()


async def set_user_household(
    conn: aiosqlite.Connection, user_id: int, household_id: int
) -> None:
    now = utc_now_str()
    uid = int(user_id)
    hid = int(household_id)
    await conn.execute(
        "UPDATE users SET household_id = ?, updated_at = ? WHERE id = ?",
        (hid, now, uid),
    )
    await conn.execute(
        "UPDATE user_recipes SET household_id = ? WHERE user_id = ?",
        (hid, uid),
    )

    # Deduplicate shared regular_tasks when joining a household (keep newer copy)
    existing_tasks = rows_to_dicts(
        await conn.execute_fetchall(
            """
            SELECT id, title, COALESCE(last_done_date, '') AS last_done_date, COALESCE(updated_at, '') AS updated_at
            FROM regular_tasks
            WHERE household_id = ? AND user_id != ? AND (is_active IS NULL OR is_active=1)
            """,
            (hid, uid),
        )
    )
    existing_task_map = {str(r["title"] or "").strip().casefold(): r for r in existing_tasks}
    user_tasks = rows_to_dicts(
        await conn.execute_fetchall(
            """
            SELECT id, title, COALESCE(last_done_date, '') AS last_done_date, COALESCE(updated_at, '') AS updated_at
            FROM regular_tasks
            WHERE user_id = ? AND (is_active IS NULL OR is_active=1)
            """,
            (uid,),
        )
    )
    for ut in user_tasks:
        key = str(ut["title"] or "").strip().casefold()
        target = existing_task_map.get(key)
        if target:
            u_stamp = (ut["last_done_date"], ut["updated_at"], int(ut["id"]))
            t_stamp = (target["last_done_date"], target["updated_at"], int(target["id"]))
            if ut["last_done_date"] and u_stamp > t_stamp:
                await conn.execute(
                    "UPDATE regular_tasks SET is_active = 0, updated_at = ? WHERE id = ?",
                    (now, int(target["id"])),
                )
                await conn.execute(
                    "UPDATE regular_tasks SET household_id = ?, updated_at = ? WHERE id = ?",
                    (hid, now, int(ut["id"])),
                )
                existing_task_map[key] = ut
            else:
                await conn.execute(
                    "UPDATE regular_tasks SET household_id = ?, is_active = 0, updated_at = ? WHERE id = ?",
                    (hid, now, int(ut["id"])),
                )
        else:
            await conn.execute(
                "UPDATE regular_tasks SET household_id = ?, updated_at = ? WHERE id = ?",
                (hid, now, int(ut["id"])),
            )

    # Deduplicate shared supplies when joining a household
    existing_supplies = rows_to_dicts(
        await conn.execute_fetchall(
            "SELECT id, name, status, COALESCE(updated_at, '') AS updated_at FROM supplies WHERE household_id = ? AND user_id != ?",
            (hid, uid),
        )
    )
    existing_sup_map = {str(r["name"] or "").strip().casefold(): r for r in existing_supplies}
    user_supplies = rows_to_dicts(
        await conn.execute_fetchall(
            "SELECT id, name, status, COALESCE(updated_at, '') AS updated_at FROM supplies WHERE user_id = ?",
            (uid,),
        )
    )
    for us in user_supplies:
        key = str(us["name"] or "").strip().casefold()
        target = existing_sup_map.get(key)
        if target:
            if target["status"] == "full" and us["status"] != "full":
                await conn.execute(
                    "UPDATE supplies SET status = ?, updated_at = ? WHERE id = ?",
                    (us["status"], now, int(target["id"])),
                )
            await conn.execute("DELETE FROM supplies WHERE id = ?", (int(us["id"]),))
        else:
            await conn.execute(
                "UPDATE supplies SET household_id = ?, updated_at = ? WHERE id = ?",
                (hid, now, int(us["id"])),
            )

    await conn.commit()


async def regenerate_household_invite_code(conn: aiosqlite.Connection, household_id: int) -> str:
    """Выпустить новый случайный код приглашения в дом."""
    new_code = generate_household_invite_code()
    await conn.execute(
        "UPDATE households SET invite_code = ? WHERE id = ?",
        (new_code, int(household_id)),
    )
    await conn.commit()
    return new_code


async def update_household_settings(
    conn: aiosqlite.Connection, household_id: int, **updates: Any
) -> Dict[str, bool]:
    """Обновить булевые настройки общего дома."""
    house = await get_household(conn, household_id)
    settings = dict((house or {}).get("settings") or DEFAULT_HOUSEHOLD_SETTINGS)
    for key in DEFAULT_HOUSEHOLD_SETTINGS:
        if key in updates:
            settings[key] = bool(updates[key])
    await conn.execute(
        "UPDATE households SET settings_json = ? WHERE id = ?",
        (json.dumps(settings, ensure_ascii=False), int(household_id)),
    )
    await conn.commit()
    return settings


async def list_household_members(
    conn: aiosqlite.Connection, household_id: int
) -> List[Dict[str, Any]]:
    """Вернуть список участников дома."""
    cur = await conn.execute(
        "SELECT * FROM users WHERE household_id = ? ORDER BY id ASC",
        (int(household_id),),
    )
    return rows_to_dicts(await cur.fetchall())


async def leave_household(conn: aiosqlite.Connection, user_id: int) -> int:
    """Отсоединить пользователя в новый собственный дом со свежим кодом."""
    now = utc_now_str()
    cur_u = await conn.execute("SELECT name FROM users WHERE id = ?", (user_id,))
    u_row = await cur_u.fetchone()
    name = (u_row["name"] if u_row else "") or "Дом"
    new_code = generate_household_invite_code()
    cur = await conn.execute(
        "INSERT INTO households (name, invite_code, settings_json, created_at) VALUES (?, ?, ?, ?)",
        (name, new_code, json.dumps(DEFAULT_HOUSEHOLD_SETTINGS), now),
    )
    new_hid = int(cur.lastrowid)
    await conn.execute(
        "UPDATE users SET household_id = ?, updated_at = ? WHERE id = ?",
        (new_hid, now, user_id),
    )
    await conn.execute(
        "UPDATE user_recipes SET household_id = ? WHERE user_id = ?",
        (new_hid, user_id),
    )
    await conn.execute(
        "UPDATE regular_tasks SET household_id = ? WHERE user_id = ?",
        (new_hid, user_id),
    )
    await conn.execute(
        "UPDATE supplies SET household_id = ? WHERE user_id = ?",
        (new_hid, user_id),
    )
    await conn.commit()
    return new_hid


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
    else:
        await conn.execute(
            "INSERT OR IGNORE INTO day_plans (user_id, plan_date, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, plan_date, now, now),
        )
        plan_row = await (await conn.execute(
            "SELECT id FROM day_plans WHERE user_id=? AND plan_date=?", (user_id, plan_date)
        )).fetchone()
        plan_id = int(plan_row["id"])
    # Replacing a manually authored plan must leave Desk-managed items intact.
    await conn.execute("DELETE FROM day_plan_items WHERE plan_id = ? AND COALESCE(external_key, '') = ''", (plan_id,))
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
) -> int:
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
        await conn.execute(
            "INSERT OR IGNORE INTO day_plans (user_id, plan_date, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, plan_date, now, now),
        )
        plan_row = await (await conn.execute(
            "SELECT id FROM day_plans WHERE user_id=? AND plan_date=?", (user_id, plan_date)
        )).fetchone()
        plan_id = int(plan_row["id"])
    cursor = await conn.execute(
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
    return int(cursor.lastrowid)


async def get_day_plan_item(
    conn: aiosqlite.Connection, user_id: int, item_id: int
) -> Optional[Dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT * FROM day_plan_items WHERE id = ? AND user_id = ?",
        (item_id, user_id),
    )
    row = await cursor.fetchone()
    return row_to_dict(row) if row else None


async def update_day_plan_item_title(
    conn: aiosqlite.Connection, user_id: int, item_id: int, title: str
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE day_plan_items SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
        (title.strip(), now, item_id, user_id),
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
    """Создать базовый список бытовой химии и расходников для дома/пользователя."""
    uid = int(user_id)
    hid, shared = await _household_sharing_state(conn, uid, "share_supplies")
    await conn.execute(
        "UPDATE supplies SET household_id = ? WHERE user_id = ? AND household_id IS NULL",
        (hid, uid),
    )
    if shared:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS cnt FROM supplies WHERE household_id = ? OR user_id = ?",
            (hid, uid),
        )
    else:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS cnt FROM supplies WHERE user_id = ?", (uid,)
        )
    row = await cursor.fetchone()
    if row and row["cnt"] > 0:
        await conn.commit()
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
            INSERT INTO supplies (user_id, household_id, name, category, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'full', ?, ?)
            """,
            (uid, hid, name, category, now, now),
        )
    await conn.commit()


async def list_supplies(conn: aiosqlite.Connection, user_id: int) -> List[Dict[str, Any]]:
    """Вернуть все записи по бытовой химии и расходникам пользователя (или дома)."""
    uid = int(user_id)
    hid, shared = await _household_sharing_state(conn, uid, "share_supplies")
    await conn.execute(
        "UPDATE supplies SET household_id = ? WHERE user_id = ? AND household_id IS NULL",
        (hid, uid),
    )
    await conn.commit()
    if shared:
        rows = rows_to_dicts(
            await conn.execute_fetchall(
                """
                SELECT * FROM supplies
                WHERE household_id = ? OR user_id = ?
                ORDER BY datetime(updated_at) DESC, id DESC
                """,
                (hid, uid),
            )
        )
        dedup: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            k = str(r.get("name") or "").strip().casefold()
            if k and k not in dedup:
                dedup[k] = r
        return sorted(dedup.values(), key=lambda x: str(x.get("name") or "").casefold())
    rows = rows_to_dicts(
        await conn.execute_fetchall(
            """
            SELECT * FROM supplies
            WHERE user_id = ?
            ORDER BY name
            """,
            (uid,),
        )
    )
    return rows


async def get_supply_by_id(
    conn: aiosqlite.Connection, user_id: int, supply_id: int
) -> Optional[Dict[str, Any]]:
    uid = int(user_id)
    hid, shared = await _household_sharing_state(conn, uid, "share_supplies")
    if shared:
        cur = await conn.execute(
            "SELECT * FROM supplies WHERE id = ? AND (household_id = ? OR user_id = ?)",
            (int(supply_id), hid, uid),
        )
    else:
        cur = await conn.execute(
            "SELECT * FROM supplies WHERE id = ? AND user_id = ?",
            (int(supply_id), uid),
        )
    row = await cur.fetchone()
    return row_to_dict(row) if row else None


async def _resolve_actor_name(
    conn: aiosqlite.Connection, user_id: int, actor_name: str = ""
) -> str:
    clean_actor = str(actor_name or "").strip()
    if clean_actor:
        return clean_actor
    u = await get_user(conn, int(user_id))
    return str(row_to_dict(u).get("name") or "") if u else ""


async def update_supply_status(
    conn: aiosqlite.Connection,
    user_id: int,
    supply_id: int,
    status: str,
    *,
    actor_name: str = "",
) -> Optional[Dict[str, Any]]:
    """Обновить статус конкретного расходника (full/low/empty) с атрибуцией автора."""
    uid = int(user_id)
    item = await get_supply_by_id(conn, uid, supply_id)
    if not item:
        return None
    clean_actor = await _resolve_actor_name(conn, uid, actor_name)
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE supplies
        SET status = ?, updated_by_user_id = ?, updated_by_name = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, uid, clean_actor, now, int(supply_id)),
    )
    await conn.commit()
    return await get_supply_by_id(conn, uid, supply_id)


async def add_supply_item(
    conn: aiosqlite.Connection,
    user_id: int,
    name: str,
    category: str = "расходники",
) -> int:
    """Добавить новый расходник/бытовую химию в общий список дома."""
    uid = int(user_id)
    clean_name = str(name or "").strip()
    if not clean_name:
        return 0
    existing = await list_supplies(conn, uid)
    for row in existing:
        if str(row.get("name") or "").strip().casefold() == clean_name.casefold():
            return int(row["id"])
    hid = await get_or_create_household(conn, uid)
    actor_name = await _resolve_actor_name(conn, uid)
    now = utc_now_str()
    cur = await conn.execute(
        """
        INSERT INTO supplies (
            user_id, household_id, name, category, status,
            updated_by_user_id, updated_by_name, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 'full', ?, ?, ?, ?)
        """,
        (uid, hid, clean_name, str(category or "расходники").strip(), uid, actor_name, now, now),
    )
    await conn.commit()
    return int(cur.lastrowid)


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


def get_user_equipment(user: Any) -> List[str]:
    """Извлечь список домашнего инвентаря пользователя из users.equipment_json."""
    d = row_to_dict(user) if not isinstance(user, dict) else user
    raw = d.get("equipment_json")
    if isinstance(raw, list):
        items = raw
    else:
        try:
            parsed = json.loads(raw or "[]")
            items = parsed if isinstance(parsed, list) else []
        except Exception:
            items = []
    out: List[str] = []
    seen: set[str] = set()
    for item in items:
        key = str(item or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


async def update_user_body(
    conn: aiosqlite.Connection,
    user_id: int,
    height_cm: float | None = None,
    weight_goal: str | None = None,
    weight_target: float | None = None,
    *,
    age_years: int | None = None,
    sex: str | None = None,
    activity_level: str | None = None,
    equipment: List[str] | None = None,
    custom_kcal_target: int | None = None,
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
    if age_years is not None:
        fields.append("age_years = ?")
        values.append(max(0, int(age_years)))
    if sex is not None:
        fields.append("sex = ?")
        values.append(str(sex).strip().lower())
    if activity_level is not None:
        fields.append("activity_level = ?")
        values.append(str(activity_level).strip().lower())
    if equipment is not None:
        clean_eq = [str(e).strip() for e in equipment if str(e).strip()]
        fields.append("equipment_json = ?")
        values.append(json.dumps(list(dict.fromkeys(clean_eq)), ensure_ascii=False))
    if custom_kcal_target is not None:
        fields.append("custom_kcal_target = ?")
        values.append(max(0, int(custom_kcal_target)))
    if not fields:
        return
    fields.append("updated_at = ?")
    values.append(now)
    values.append(user_id)
    sql = f"UPDATE users SET {', '.join(fields)} WHERE id = ?"
    await conn.execute(sql, tuple(values))
    await conn.commit()


async def update_user_coach_settings(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    weight_goal: str | None = None,
    coach_mode: str | None = None,
    nutrition_checkin_enabled: bool | int | None = None,
    nutrition_checkin_last_date: str | None = None,
) -> None:
    """Обновить цель и настройки стиля коуча / вечернего разбора питания."""
    now = utc_now_str()
    fields = []
    values: list[Any] = []
    if weight_goal is not None:
        g = str(weight_goal).strip().lower()
        if g == "maintain":
            g = "keep"
        if g in {"loss", "recomp", "keep", "gain", "mindful"}:
            fields.append("weight_goal = ?")
            values.append(g)
    if coach_mode is not None:
        m = str(coach_mode).strip().lower()
        if m in {"gentle", "balanced", "strict"}:
            fields.append("coach_mode = ?")
            values.append(m)
    if nutrition_checkin_enabled is not None:
        fields.append("nutrition_checkin_enabled = ?")
        values.append(1 if nutrition_checkin_enabled else 0)
    if nutrition_checkin_last_date is not None:
        fields.append("nutrition_checkin_last_date = ?")
        values.append(str(nutrition_checkin_last_date).strip())
    if not fields:
        return
    fields.append("updated_at = ?")
    values.append(now)
    values.append(int(user_id))
    await conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", tuple(values))
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


async def set_quiet_mode(conn: aiosqlite.Connection, user_id: int, enabled: bool) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET quiet_mode = ?, updated_at = ? WHERE id = ?",
        (1 if enabled else 0, now, user_id),
    )
    await conn.commit()


async def update_focus_strikes(conn: aiosqlite.Connection, user_id: int, delta: int) -> int:
    now = utc_now_str()
    cursor = await conn.execute(
        "SELECT focus_strikes FROM users WHERE id = ?", (user_id,)
    )
    row = await cursor.fetchone()
    current = row["focus_strikes"] if row and row["focus_strikes"] is not None else 0
    new_val = max(0, current + delta)
    await conn.execute(
        "UPDATE users SET focus_strikes = ?, updated_at = ? WHERE id = ?",
        (new_val, now, user_id),
    )
    await conn.commit()
    return new_val


async def set_focus_cooldown(conn: aiosqlite.Connection, user_id: int, until_ts: str | None) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET focus_cooldown_until = ?, updated_at = ? WHERE id = ?",
        (until_ts, now, user_id),
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
        WHERE user_id = ? AND (is_active IS NULL OR is_active = 1)
        ORDER BY id DESC
        """,
        (user_id,),
    )
    return await cursor.fetchall()


async def get_custom_reminder(conn: aiosqlite.Connection, user_id: int, reminder_id: int) -> Optional[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM custom_reminders WHERE id = ? AND user_id = ?",
        (reminder_id, user_id),
    )
    return await cursor.fetchone()


async def archive_custom_reminder(conn: aiosqlite.Connection, user_id: int, reminder_id: int) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE custom_reminders SET is_active = 0, updated_at = ? WHERE id = ? AND user_id = ?",
        (now, reminder_id, user_id),
    )
    await conn.commit()


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


async def update_custom_reminder_title(
    conn: aiosqlite.Connection, user_id: int, reminder_id: int, title: str
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE custom_reminders SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
        (title.strip(), now, reminder_id, user_id),
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
    uid = int(user_id)
    hid, shared = await _household_sharing_state(conn, uid, "share_home_tasks")
    await conn.execute(
        "UPDATE regular_tasks SET household_id = ? WHERE user_id = ? AND household_id IS NULL",
        (hid, uid),
    )
    if shared:
        cursor = await conn.execute(
            "SELECT COUNT(*) as cnt FROM regular_tasks WHERE (household_id = ? OR user_id = ?) AND (is_active IS NULL OR is_active=1)",
            (hid, uid),
        )
    else:
        cursor = await conn.execute(
            "SELECT COUNT(*) as cnt FROM regular_tasks WHERE user_id = ? AND (is_active IS NULL OR is_active=1)",
            (uid,),
        )
    row = await cursor.fetchone()
    if row and row["cnt"] > 0:
        await conn.commit()
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
            INSERT INTO regular_tasks (
                user_id, household_id, title, frequency_days, zone, points, is_active,
                last_done_date, next_due_date, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, NULL, ?, ?, ?)
            """,
            (uid, hid, title, freq, zone, points, next_due.isoformat(), now, now),
        )
    await conn.commit()


async def list_regular_tasks(
    conn: aiosqlite.Connection,
    user_id: int,
    due_only: bool = False,
    local_date: Optional[str] = None,
    due_in_days: Optional[int] = None,
    include_inactive: bool = False,
) -> List[Dict[str, Any]]:
    uid = int(user_id)
    hid, shared = await _household_sharing_state(conn, uid, "share_home_tasks")
    await conn.execute(
        "UPDATE regular_tasks SET household_id = ? WHERE user_id = ? AND household_id IS NULL",
        (hid, uid),
    )
    await conn.commit()

    conditions = ["(household_id = ? OR user_id = ?)" if shared else "user_id = ?"]
    params: list[Any] = [hid, uid] if shared else [uid]
    if not include_inactive:
        conditions.append("(is_active IS NULL OR is_active=1)")
    where_clause = " AND ".join(conditions)
    raw_rows = rows_to_dicts(
        await conn.execute_fetchall(
            f"SELECT * FROM regular_tasks WHERE {where_clause} ORDER BY id ASC",
            params,
        )
    )
    if shared:
        best_by_title: Dict[str, Dict[str, Any]] = {}
        for r in raw_rows:
            k = str(r.get("title") or "").strip().casefold()
            if not k:
                continue
            prev = best_by_title.get(k)
            if prev is None:
                best_by_title[k] = r
            else:
                r_key = (str(r.get("last_done_date") or ""), str(r.get("updated_at") or ""), int(r["id"]))
                p_key = (str(prev.get("last_done_date") or ""), str(prev.get("updated_at") or ""), int(prev["id"]))
                if r_key > p_key:
                    best_by_title[k] = r
        rows = list(best_by_title.values())
    else:
        rows = raw_rows

    if (due_only or due_in_days is not None) and local_date:
        base_dt = datetime.date.fromisoformat(local_date)
        max_dt = (
            base_dt
            if due_only and due_in_days is None
            else (
                base_dt + datetime.timedelta(days=int(due_in_days))
                if due_in_days is not None
                else None
            )
        )
        filtered = []
        for r in rows:
            nd = str(r.get("next_due_date") or "").strip()
            if not nd:
                continue
            try:
                nd_dt = datetime.date.fromisoformat(nd[:10])
            except Exception:
                continue
            if due_only and due_in_days is None and nd_dt <= base_dt:
                filtered.append(r)
            elif max_dt is not None and nd_dt <= max_dt:
                filtered.append(r)
        rows = filtered

    rows.sort(key=lambda r: (str(r.get("next_due_date") or "9999-12-31"), int(r["id"])))
    return rows


async def next_regular_task_date(
    conn: aiosqlite.Connection, user_id: int
) -> Optional[str]:
    tasks = await list_regular_tasks(conn, user_id, include_inactive=False)
    dates = [str(t.get("next_due_date") or "") for t in tasks if t.get("next_due_date")]
    return min(dates) if dates else None


async def mark_regular_done(
    conn: aiosqlite.Connection,
    user_id: int,
    task_id: int,
    done_date: str,
    *,
    actor_name: str = "",
) -> Optional[Dict[str, Any]]:
    uid = int(user_id)
    hid, shared = await _household_sharing_state(conn, uid, "share_home_tasks")
    where = "id = ? AND (user_id = ? OR household_id = ?)" if shared else "id = ? AND user_id = ?"
    params: tuple[Any, ...] = (int(task_id), uid, hid) if shared else (int(task_id), uid)
    cursor = await conn.execute(f"SELECT * FROM regular_tasks WHERE {where}", params)
    row = await cursor.fetchone()
    if not row:
        return None
    freq = row["frequency_days"] or 7
    next_due = calc_next_due(done_date, freq)
    clean_actor = await _resolve_actor_name(conn, uid, actor_name)
    now = utc_now_str()
    await conn.execute(
        f"""
        UPDATE regular_tasks
        SET last_done_date = ?, next_due_date = ?,
            last_done_by_user_id = ?, last_done_by_name = ?, updated_at = ?
        WHERE {where}
        """,
        (done_date, next_due, uid, clean_actor, now, *params),
    )
    await conn.commit()
    cur2 = await conn.execute("SELECT * FROM regular_tasks WHERE id = ?", (int(task_id),))
    res = await cur2.fetchone()
    return row_to_dict(res) if res else None


async def postpone_regular_task(
    conn: aiosqlite.Connection, user_id: int, task_id: int, days: int = 1
) -> None:
    uid = int(user_id)
    hid, shared = await _household_sharing_state(conn, uid, "share_home_tasks")
    where = "id = ? AND (user_id = ? OR household_id = ?)" if shared else "id = ? AND user_id = ?"
    params: tuple[Any, ...] = (int(task_id), uid, hid) if shared else (int(task_id), uid)
    now = utc_now_str()
    cursor = await conn.execute(f"SELECT next_due_date FROM regular_tasks WHERE {where}", params)
    row = await cursor.fetchone()
    if not row:
        return
    current_due = row["next_due_date"] if row["next_due_date"] else datetime.date.today().isoformat()
    new_due = calc_next_due(current_due, days)
    await conn.execute(
        f"""
        UPDATE regular_tasks
        SET next_due_date = ?, updated_at = ?
        WHERE {where} AND (is_active IS NULL OR is_active=1)
        """,
        (new_due, now, *params),
    )
    await conn.commit()


async def set_regular_frequency(
    conn: aiosqlite.Connection, user_id: int, task_id: int, frequency_days: int
) -> None:
    uid = int(user_id)
    hid, shared = await _household_sharing_state(conn, uid, "share_home_tasks")
    where = "id = ? AND (user_id = ? OR household_id = ?)" if shared else "id = ? AND user_id = ?"
    params: tuple[Any, ...] = (int(task_id), uid, hid) if shared else (int(task_id), uid)
    freq = max(1, frequency_days)
    now = utc_now_str()
    await conn.execute(
        f"""
        UPDATE regular_tasks
        SET frequency_days = ?, next_due_date = date(coalesce(last_done_date, next_due_date), '+' || ? || ' day'), updated_at = ?
        WHERE {where} AND (is_active IS NULL OR is_active=1)
        """,
        (freq, freq, now, *params),
    )
    await conn.commit()


async def list_regular_tasks_done_on_date(
    conn: aiosqlite.Connection, user_id: int, done_date: str
) -> List[Dict[str, Any]]:
    uid = int(user_id)
    hid, shared = await _household_sharing_state(conn, uid, "share_home_tasks")
    if shared:
        return rows_to_dicts(
            await conn.execute_fetchall(
                """
                SELECT * FROM regular_tasks
                WHERE (household_id = ? OR user_id = ?) AND last_done_date = ? AND (is_active IS NULL OR is_active=1)
                ORDER BY id
                """,
                (hid, uid, done_date),
            )
        )
    return rows_to_dicts(
        await conn.execute_fetchall(
            """
            SELECT * FROM regular_tasks
            WHERE user_id = ? AND last_done_date = ? AND (is_active IS NULL OR is_active=1)
            ORDER BY id
            """,
            (uid, done_date),
        )
    )


async def list_regular_tasks_done_in_range(
    conn: aiosqlite.Connection, household_id: int, start_date: str, end_date: str
) -> List[Dict[str, Any]]:
    return rows_to_dicts(
        await conn.execute_fetchall(
            """
            SELECT * FROM regular_tasks
            WHERE household_id = ? AND last_done_date >= ? AND last_done_date <= ?
              AND (is_active IS NULL OR is_active=1)
            ORDER BY last_done_date DESC, id DESC
            """,
            (int(household_id), start_date, end_date),
        )
    )


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
async def add_weight(
    conn: aiosqlite.Connection,
    user_id: int,
    weight: float,
    *,
    body_fat_pct: float | None = None,
    muscle_mass_kg: float | None = None,
    water_pct: float | None = None,
    visceral_fat: float | None = None,
    bmi: float | None = None,
    source: str = "manual",
    note: str = "",
) -> int:
    now = utc_now_str()
    cur = await conn.execute(
        """
        INSERT INTO weights (
            user_id, weight, body_fat_pct, muscle_mass_kg, water_pct,
            visceral_fat, bmi, source, note, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            float(weight),
            body_fat_pct,
            muscle_mass_kg,
            water_pct,
            visceral_fat,
            bmi,
            str(source or "manual").strip() or "manual",
            str(note or "").strip(),
            now,
        ),
    )
    await conn.commit()
    return int(cur.lastrowid)


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
        "SELECT * FROM weights WHERE user_id = ? AND weight > 0 ORDER BY datetime(created_at) DESC, id DESC LIMIT ?",
        (user_id, limit),
    )
    return await cursor.fetchall()


async def get_latest_weight(conn: aiosqlite.Connection, user_id: int) -> Optional[Dict[str, Any]]:
    """Вернуть последнюю положительную запись весов пользователя (строго личную)."""
    cursor = await conn.execute(
        "SELECT * FROM weights WHERE user_id = ? AND weight > 0 ORDER BY datetime(created_at) DESC, id DESC LIMIT 1",
        (user_id,),
    )
    row = await cursor.fetchone()
    return row_to_dict(row) if row else None


async def merge_recent_weight_metrics(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    body_fat_pct: float | None = None,
    muscle_mass_kg: float | None = None,
    water_pct: float | None = None,
    visceral_fat: float | None = None,
    bmi: float | None = None,
    within_minutes: int = 120,
    now_utc: Optional[datetime.datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Дополнить свежий замер весов показателями со второго скрина (без перезаписи веса)."""
    latest = await get_latest_weight(conn, user_id)
    if not latest:
        return None
    raw_created = str(latest.get("created_at") or "").strip().rstrip("Z")
    try:
        created_dt = datetime.datetime.fromisoformat(raw_created)
        if created_dt.tzinfo is not None:
            created_dt = created_dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    except Exception:
        return None
    ref_now = now_utc or datetime.datetime.utcnow()
    if ref_now.tzinfo is not None:
        ref_now = ref_now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    if abs((ref_now - created_dt).total_seconds()) > max(1, int(within_minutes)) * 60:
        return None

    updates: Dict[str, Any] = {}
    for col, val in (
        ("body_fat_pct", body_fat_pct),
        ("muscle_mass_kg", muscle_mass_kg),
        ("water_pct", water_pct),
        ("visceral_fat", visceral_fat),
        ("bmi", bmi),
    ):
        if val is not None:
            updates[col] = float(val)
    if not updates:
        return None

    set_sql = ", ".join(f"{col} = ?" for col in updates)
    params = [*updates.values(), int(latest["id"]), int(user_id)]
    await conn.execute(
        f"UPDATE weights SET {set_sql} WHERE id = ? AND user_id = ?",
        tuple(params),
    )
    await conn.commit()
    cur = await conn.execute("SELECT * FROM weights WHERE id = ?", (int(latest["id"]),))
    refreshed = await cur.fetchone()
    return row_to_dict(refreshed) if refreshed else None


async def has_karma_event_today(
    conn: aiosqlite.Connection,
    user_id: int,
    local_date: str,
    event_type: str,
) -> bool:
    """Проверить, фиксировалось ли сегодня событие кармы указанного типа."""
    cur = await conn.execute(
        "SELECT 1 FROM karma_events WHERE user_id = ? AND local_date = ? AND event_type = ? LIMIT 1",
        (int(user_id), str(local_date), str(event_type)),
    )
    return (await cur.fetchone()) is not None


async def weight_trend(conn: aiosqlite.Connection, user_id: int, days: int = 30) -> float:
    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    cursor = await conn.execute(
        "SELECT weight, created_at FROM weights WHERE user_id = ? AND weight > 0 AND datetime(created_at) >= datetime(?) ORDER BY datetime(created_at) ASC, id ASC",
        (user_id, since.isoformat()),
    )
    rows = await cursor.fetchall()
    if len(rows) < 2:
        return 0.0
    return rows[-1]["weight"] - rows[0]["weight"]


async def body_metrics_trend(
    conn: aiosqlite.Connection, user_id: int, days: int = 30
) -> Dict[str, Any]:
    """Посчитать динамику веса, % жира, мышц и воды за N дней (строго личные замеры)."""
    since = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    cur = await conn.execute(
        """
        SELECT * FROM weights
        WHERE user_id = ? AND weight > 0 AND datetime(created_at) >= datetime(?)
        ORDER BY datetime(created_at) ASC, id ASC
        """,
        (user_id, since.isoformat()),
    )
    period_rows = rows_to_dicts(await cur.fetchall())
    latest = await get_latest_weight(conn, user_id)

    def _delta(field: str) -> Optional[float]:
        vals = [float(r[field]) for r in period_rows if r.get(field) is not None and float(r[field]) > 0]
        if len(vals) < 2:
            return None
        return round(vals[-1] - vals[0], 2)

    return {
        "latest": latest,
        "count": len(period_rows),
        "days": days,
        "weight_delta": _delta("weight"),
        "fat_delta": _delta("body_fat_pct"),
        "muscle_delta": _delta("muscle_mass_kg"),
        "water_delta": _delta("water_pct"),
        "visceral_delta": _delta("visceral_fat"),
    }


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
    uid = int(user_id)
    hid, shared = await _household_sharing_state(conn, uid, "share_home_tasks")
    now = utc_now_str()
    if shared:
        await conn.execute(
            "UPDATE regular_tasks SET is_active = 0, updated_at = ? WHERE id = ? AND (user_id = ? OR household_id = ?)",
            (now, int(task_id), uid, hid),
        )
    else:
        await conn.execute(
            "UPDATE regular_tasks SET is_active = 0, updated_at = ? WHERE id = ? AND user_id = ?",
            (now, int(task_id), uid),
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
async def add_expense(
    conn: aiosqlite.Connection,
    user_id: int,
    amount: float,
    category: str,
    *,
    scope: str = "personal",
) -> int:
    now = utc_now_str()
    clean_scope = "household" if scope == "household" else "personal"
    hid = await get_or_create_household(conn, user_id) if clean_scope == "household" else None
    cursor = await conn.execute(
        "INSERT INTO expenses (user_id, household_id, scope, amount, category, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, hid, clean_scope, amount, category, "", now),
    )
    await conn.commit()
    return cursor.lastrowid


async def household_monthly_expenses_summary(
    conn: aiosqlite.Connection,
    household_id: int,
    *,
    year_month: Optional[str] = None,
) -> Dict[str, Any]:
    """Сводка общих трат дома за месяц по участникам и категориям."""
    ym = year_month or datetime.datetime.utcnow().strftime("%Y-%m")
    rows = rows_to_dicts(
        await conn.execute_fetchall(
            """
            SELECT e.user_id, COALESCE(u.name, 'Участник') AS member_name, e.amount, e.category
            FROM expenses e
            LEFT JOIN users u ON u.id = e.user_id
            WHERE e.household_id = ? AND e.scope = 'household' AND strftime('%Y-%m', e.created_at) = ?
            ORDER BY e.id DESC
            """,
            (int(household_id), ym),
        )
    )
    total = 0.0
    by_member_map: Dict[int, Dict[str, Any]] = {}
    by_cat: Dict[str, float] = {}
    for r in rows:
        amt = float(r.get("amount") or 0.0)
        uid = int(r["user_id"])
        name = str(r.get("member_name") or "Участник")
        cat = str(r.get("category") or "другое")
        total += amt
        if uid not in by_member_map:
            by_member_map[uid] = {"user_id": uid, "name": name, "total": 0.0}
        by_member_map[uid]["total"] = round(by_member_map[uid]["total"] + amt, 2)
        by_cat[cat] = round(by_cat.get(cat, 0.0) + amt, 2)
    by_member = sorted(by_member_map.values(), key=lambda x: -x["total"])
    return {
        "month": ym,
        "total": round(total, 2),
        "by_member": by_member,
        "by_category": by_cat,
    }


async def delete_expense(conn: aiosqlite.Connection, user_id: int, expense_id: int) -> None:
    """Удалить запись траты пользователя по ID."""
    await conn.execute(
        "DELETE FROM expenses WHERE id = ? AND user_id = ?",
        (expense_id, user_id),
    )
    await conn.commit()


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
    hid = await get_or_create_household(conn, user_id)
    house = await get_household(conn, hid)
    share_bills = bool((house or {}).get("settings", {}).get("share_bills", True))
    if share_bills:
        cursor = await conn.execute(
            """
            SELECT b.*, u.name AS paid_by_name
            FROM bills b
            LEFT JOIN users u ON u.id = b.paid_by_user_id
            WHERE b.user_id = ? OR (b.scope = 'household' AND b.household_id = ?)
            ORDER BY b.day_of_month, b.id
            """,
            (user_id, hid),
        )
    else:
        cursor = await conn.execute(
            """
            SELECT b.*, u.name AS paid_by_name
            FROM bills b
            LEFT JOIN users u ON u.id = b.paid_by_user_id
            WHERE b.user_id = ?
            ORDER BY b.day_of_month, b.id
            """,
            (user_id,),
        )
    return await cursor.fetchall()


async def get_bill_by_id(
    conn: aiosqlite.Connection, user_id: int, bill_id: int
) -> Optional[Dict[str, Any]]:
    hid = await get_or_create_household(conn, user_id)
    cur = await conn.execute(
        """
        SELECT b.*, u.name AS paid_by_name
        FROM bills b
        LEFT JOIN users u ON u.id = b.paid_by_user_id
        WHERE b.id = ? AND (b.user_id = ? OR (b.scope = 'household' AND b.household_id = ?))
        """,
        (int(bill_id), user_id, hid),
    )
    row = await cur.fetchone()
    return row_to_dict(row) if row else None


async def upsert_bill(
    conn: aiosqlite.Connection,
    user_id: int,
    title: str,
    amount: float,
    day_of_month: int,
    *,
    scope: str = "personal",
) -> int:
    now = utc_now_str()
    clean_scope = "household" if scope == "household" else "personal"
    hid = await get_or_create_household(conn, user_id) if clean_scope == "household" else None
    cursor = await conn.execute(
        "SELECT id FROM bills WHERE user_id = ? AND title = ?", (user_id, title)
    )
    row = await cursor.fetchone()
    if row:
        await conn.execute(
            """
            UPDATE bills
            SET amount = ?, day_of_month = ?, scope = ?, household_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (amount, day_of_month, clean_scope, hid, now, row["id"]),
        )
        await conn.commit()
        return row["id"]
    cursor = await conn.execute(
        """
        INSERT INTO bills (
            user_id, household_id, scope, title, amount, day_of_month,
            last_paid_month, paid_by_user_id, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, '', NULL, ?, ?)
        """,
        (user_id, hid, clean_scope, title, amount, day_of_month, now, now),
    )
    await conn.commit()
    return cursor.lastrowid


async def mark_bill_paid(conn: aiosqlite.Connection, user_id: int, bill_id: int, month: str) -> None:
    now = utc_now_str()
    hid = await get_or_create_household(conn, user_id)
    house = await get_household(conn, hid)
    share_bills = bool((house or {}).get("settings", {}).get("share_bills", True))
    if share_bills:
        await conn.execute(
            """
            UPDATE bills
            SET last_paid_month = ?, paid_by_user_id = ?, updated_at = ?
            WHERE id = ? AND (user_id = ? OR (scope = 'household' AND household_id = ?))
            """,
            (month, user_id, now, bill_id, user_id, hid),
        )
    else:
        await conn.execute(
            "UPDATE bills SET last_paid_month = ?, paid_by_user_id = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (month, user_id, now, bill_id, user_id),
        )
    await conn.commit()


async def toggle_bill_scope(
    conn: aiosqlite.Connection, user_id: int, bill_id: int
) -> Optional[str]:
    """Переключить счёт между личным ('personal') и общим ('household')."""
    bill = await get_bill_by_id(conn, user_id, bill_id)
    if not bill:
        return None
    new_scope = "personal" if bill.get("scope") == "household" else "household"
    hid = await get_or_create_household(conn, user_id) if new_scope == "household" else None
    now = utc_now_str()
    await conn.execute(
        "UPDATE bills SET scope = ?, household_id = ?, updated_at = ? WHERE id = ?",
        (new_scope, hid, now, int(bill_id)),
    )
    await conn.commit()
    return new_scope


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


# Focus cafe sessions
async def create_focus_session(
    conn: aiosqlite.Connection,
    user_id: int,
    task_title: str,
    duration_min: int,
    start_ts: str,
    checkin_ts: str,
    end_ts: str,
) -> int:
    now = utc_now_str()
    cursor = await conn.execute(
        """
        INSERT INTO focus_sessions (
            user_id, task_title, duration_min, start_ts, checkin_ts, end_ts,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, task_title, duration_min, start_ts, checkin_ts, end_ts, now, now),
    )
    await conn.commit()
    return cursor.lastrowid


async def get_active_focus_session(conn: aiosqlite.Connection, user_id: int) -> Optional[Dict[str, Any]]:
    cursor = await conn.execute(
        """
        SELECT * FROM focus_sessions
        WHERE user_id = ? AND result = ''
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_active_focus_sessions(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    rows = await conn.execute_fetchall(
        "SELECT * FROM focus_sessions WHERE result = ''"
    )
    return [dict(r) for r in rows] if rows else []


async def mark_focus_checkin_sent(conn: aiosqlite.Connection, session_id: int) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE focus_sessions SET checkin_sent = 1, updated_at = ? WHERE id = ?",
        (now, session_id),
    )
    await conn.commit()


async def mark_focus_checkin_response(
    conn: aiosqlite.Connection, session_id: int, response: str
) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE focus_sessions SET checkin_response = ?, updated_at = ? WHERE id = ?",
        (response, now, session_id),
    )
    await conn.commit()


async def mark_focus_end_sent(conn: aiosqlite.Connection, session_id: int) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE focus_sessions SET end_sent = 1, updated_at = ? WHERE id = ?",
        (now, session_id),
    )
    await conn.commit()


async def complete_focus_session(conn: aiosqlite.Connection, session_id: int, result: str) -> None:
    now = utc_now_str()
    await conn.execute(
        "UPDATE focus_sessions SET result = ?, updated_at = ? WHERE id = ?",
        (result, now, session_id),
    )
    await conn.commit()


async def reset_month_points(conn: aiosqlite.Connection, current_month: str) -> None:
    await conn.execute(
        "UPDATE users SET points_month = 0, last_points_reset = ?, updated_at = ?",
        (current_month, utc_now_str()),
    )
    await conn.commit()


# Shopping List Logic
async def create_shopping_item(
    conn: aiosqlite.Connection,
    user_id: int,
    name: str,
    qty: float = 1.0,
    unit: str = "шт",
    category: str = "misc",
    scope: str = "household",
) -> int:
    scope = scope if scope in {"household", "personal"} else "household"
    household_id = None
    if scope == "household":
        household_id = await get_or_create_household(conn, user_id)
        try:
            await conn.execute(
                """
                UPDATE shopping_list
                SET household_id = ?
                WHERE user_id = ? AND (household_id IS NULL OR household_id = 0)
                  AND (scope IS NULL OR scope = '' OR scope = 'household')
                """,
                (household_id, user_id),
            )
            await conn.commit()
        except Exception:
            pass
    cursor = await conn.execute(
        """
        SELECT id, quantity FROM shopping_list
        WHERE item_name = ? AND is_bought = 0
          AND (
            ((scope IS NULL OR scope = '' OR scope = 'household') AND (household_id = ? OR (household_id IS NULL AND user_id = ?)))
            OR (scope = 'personal' AND user_id = ?)
          )
        """,
        (name, household_id, user_id, user_id),
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
            """
            INSERT INTO shopping_list
            (user_id, household_id, scope, item_name, quantity, unit, category, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, household_id, scope, name, qty, unit, category, now, now),
        )
        await conn.commit()
        return cursor.lastrowid

async def list_shopping_items(conn: aiosqlite.Connection, user_id: int, scope: str = "household") -> List[aiosqlite.Row]:
    scope = scope if scope in {"household", "personal"} else "household"
    household_id = None
    if scope == "household":
        household_id = await get_or_create_household(conn, user_id)
        try:
            await conn.execute(
                """
                UPDATE shopping_list
                SET household_id = ?
                WHERE user_id = ? AND (household_id IS NULL OR household_id = 0)
                  AND (scope IS NULL OR scope = '' OR scope = 'household')
                """,
                (household_id, user_id),
            )
            await conn.commit()
        except Exception:
            pass
    cursor = await conn.execute(
        """
        SELECT * FROM shopping_list
        WHERE ((scope IS NULL OR scope = '' OR scope = 'household') AND (household_id = ? OR (household_id IS NULL AND user_id = ?)))
           OR (scope = 'personal' AND user_id = ?)
        ORDER BY is_bought, category, item_name
        """,
        (household_id, user_id, user_id),
    )
    return await cursor.fetchall()

async def mark_shopping_bought(
    conn: aiosqlite.Connection, user_id: int, item_id: int, bought: bool = True, scope: str = "household"
) -> None:
    scope = scope if scope in {"household", "personal"} else "household"
    household_id = None
    if scope == "household":
        household_id = await get_or_create_household(conn, user_id)
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE shopping_list
        SET is_bought = ?, updated_at = ?
        WHERE id = ?
          AND (
            ((scope IS NULL OR scope = '' OR scope = 'household') AND (household_id = ? OR (household_id IS NULL AND user_id = ?)))
            OR (scope = 'personal' AND user_id = ?)
          )
        """,
        (1 if bought else 0, now, item_id, household_id, user_id, user_id),
    )
    await conn.commit()

async def delete_shopping_item(conn: aiosqlite.Connection, user_id: int, item_id: int, scope: str = "household") -> None:
    scope = scope if scope in {"household", "personal"} else "household"
    household_id = None
    if scope == "household":
        household_id = await get_or_create_household(conn, user_id)
    await conn.execute(
        """
        DELETE FROM shopping_list
        WHERE id = ?
          AND (
            ((scope IS NULL OR scope = '' OR scope = 'household') AND (household_id = ? OR (household_id IS NULL AND user_id = ?)))
            OR (scope = 'personal' AND user_id = ?)
          )
        """,
        (item_id, household_id, user_id, user_id),
    )
    await conn.commit()


async def find_and_delete_shopping_item_by_name(
    conn: aiosqlite.Connection,
    user_id: int,
    query: str,
    scope: str = "household",
) -> Optional[Dict[str, Any]]:
    """Найти позицию в списке покупок по названию/основе слова, удалить её и вернуть данные."""
    needle = (query or "").strip().casefold().replace("ё", "е")
    if not needle:
        return None
    rows = [row_to_dict(r) for r in await list_shopping_items(conn, user_id, scope=scope)]
    if not rows:
        return None

    # Предпочитаем ещё не купленные позиции; сперва точное совпадение, затем по основе слова.
    ordered = sorted(rows, key=lambda r: int(bool(r.get("is_bought"))))
    for row in ordered:
        name_norm = str(row.get("item_name") or "").strip().casefold().replace("ё", "е")
        if name_norm == needle:
            await delete_shopping_item(conn, user_id, int(row["id"]), scope=scope)
            return row

    needle_tokens = re.findall(r"[a-zа-я0-9]+", needle)
    for row in ordered:
        name = str(row.get("item_name") or "")
        name_norm = name.casefold().replace("ё", "е")
        if _haystack_matches_query(name_norm, needle):
            await delete_shopping_item(conn, user_id, int(row["id"]), scope=scope)
            return row
        if needle_tokens:
            name_stems = {_ru_stem(w) for w in re.findall(r"[a-zа-я0-9]+", name_norm)}
            if all(_ru_stem(tok) in name_stems for tok in needle_tokens):
                await delete_shopping_item(conn, user_id, int(row["id"]), scope=scope)
                return row
    return None


async def update_shopping_item_name(
    conn: aiosqlite.Connection, user_id: int, item_id: int, name: str, scope: str = "household"
) -> None:
    scope = scope if scope in {"household", "personal"} else "household"
    household_id = None
    if scope == "household":
        household_id = await get_or_create_household(conn, user_id)
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE shopping_list
        SET item_name = ?, updated_at = ?
        WHERE id = ?
          AND (
            ((scope IS NULL OR scope = '' OR scope = 'household') AND (household_id = ? OR (household_id IS NULL AND user_id = ?)))
            OR (scope = 'personal' AND user_id = ?)
          )
        """,
        (name.strip(), now, item_id, household_id, user_id, user_id),
    )
    await conn.commit()

async def complete_shopping_trip(conn: aiosqlite.Connection, user_id: int, scope: str = "household") -> int:
    """Move bought items from shopping list to pantry."""
    scope = scope if scope in {"household", "personal"} else "household"
    now = utc_now_str()
    household_id = await get_or_create_household(conn, user_id)
    # 1. Select bought items
    if scope == "personal":
        cursor = await conn.execute(
            "SELECT * FROM shopping_list WHERE user_id = ? AND scope = 'personal' AND is_bought = 1",
            (user_id,),
        )
    else:
        cursor = await conn.execute(
            """
            SELECT * FROM shopping_list
            WHERE (scope IS NULL OR scope = '' OR scope = 'household')
              AND is_bought = 1
              AND (household_id = ? OR (household_id IS NULL AND user_id = ?))
            """,
            (household_id, user_id),
        )
    rows = await cursor.fetchall()
    count = 0
    for row in rows:
        pantry_cursor = await conn.execute(
            "SELECT id FROM pantry_items WHERE household_id = ? AND name = ? AND (is_active IS NULL OR is_active = 1)",
            (household_id, row["item_name"]),
        )
        pantry_row = await pantry_cursor.fetchone()
        if pantry_row:
             await conn.execute(
                 "UPDATE pantry_items SET amount = amount + ?, updated_at = ? WHERE id = ?",
                 (row["quantity"], now, pantry_row["id"])
             )
        else:
             await conn.execute(
                 """
                 INSERT INTO pantry_items
                 (user_id, household_id, name, amount, unit, category, is_active, created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                 """,
                 (user_id, household_id, row["item_name"], row["quantity"], row["unit"], row["category"], now, now),
             )
        count += 1
    
    # 3. Delete from shopping list (only bought ones)
    if scope == "personal":
        await conn.execute(
            "DELETE FROM shopping_list WHERE user_id = ? AND scope = 'personal' AND is_bought = 1",
            (user_id,),
        )
    else:
        await conn.execute(
            """
            DELETE FROM shopping_list
            WHERE (scope IS NULL OR scope = '' OR scope = 'household')
              AND is_bought = 1
              AND (household_id = ? OR (household_id IS NULL AND user_id = ?))
            """,
            (household_id, user_id),
        )
    await conn.commit()
    return count


# --- Schedule (daily planner) ---


async def create_schedule_block(
    conn: aiosqlite.Connection,
    user_id: int,
    title: str,
    start_time: str,
    end_time: str,
    weekdays: str,
    location: str = "",
) -> int:
    now = utc_now_str()
    cursor = await conn.execute(
        """
        INSERT INTO schedule_blocks
        (user_id, title, start_time, end_time, weekdays, location, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, title.strip(), start_time, end_time, weekdays, location.strip(), now, now),
    )
    await conn.commit()
    return cursor.lastrowid


async def list_schedule_blocks(conn: aiosqlite.Connection, user_id: int) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM schedule_blocks WHERE user_id = ? ORDER BY start_time",
        (user_id,),
    )
    return await cursor.fetchall()


async def delete_schedule_block(conn: aiosqlite.Connection, user_id: int, block_id: int) -> None:
    await conn.execute("DELETE FROM schedule_blocks WHERE id = ? AND user_id = ?", (block_id, user_id))
    await conn.commit()


async def create_schedule_event(
    conn: aiosqlite.Connection,
    user_id: int,
    event_date: str,
    start_time: str,
    end_time: str,
    title: str,
    category: str = "misc",
    source: str = "manual",
) -> int:
    now = utc_now_str()
    cursor = await conn.execute(
        """
        INSERT INTO schedule_events
        (user_id, event_date, start_time, end_time, title, category, source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, event_date, start_time, end_time, title.strip(), category, source, now, now),
    )
    await conn.commit()
    return cursor.lastrowid


async def list_schedule_events(conn: aiosqlite.Connection, user_id: int, event_date: str) -> List[aiosqlite.Row]:
    cursor = await conn.execute(
        "SELECT * FROM schedule_events WHERE user_id = ? AND event_date = ? ORDER BY start_time",
        (user_id, event_date),
    )
    return await cursor.fetchall()


async def delete_schedule_event(conn: aiosqlite.Connection, user_id: int, event_id: int) -> None:
    await conn.execute("DELETE FROM schedule_events WHERE id = ? AND user_id = ?", (event_id, user_id))
    await conn.commit()

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


# --- Inbox messages, Journal / Diary & Personal Data ---

_INBOX_MESSAGE_FIELDS = {
    "source",
    "telegram_file_id",
    "telegram_file_unique_id",
    "duration_sec",
    "raw_text",
    "stt_provider",
    "stt_error",
    "llm_provider",
    "llm_model",
    "llm_raw_json",
    "llm_error",
    "status",
    "local_date",
}

_JOURNAL_ENTRY_FIELDS = {
    "entry_type",
    "entry_date",
    "source",
    "user_text",
    "title",
    "model_note",
    "tags_json",
    "is_deleted",
}

_INBOX_ACTION_FIELDS = {
    "item_kind",
    "target_table",
    "target_id",
    "title",
    "source_quote",
    "payload_json",
    "status",
}


async def create_inbox_message_if_new(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    chat_id: int,
    telegram_message_id: Optional[int],
    source: str,
    local_date: str,
    raw_text: Optional[str] = None,
    telegram_file_id: Optional[str] = None,
    telegram_file_unique_id: Optional[str] = None,
    duration_sec: Optional[int] = None,
    status: str = "received",
) -> Tuple[Dict[str, Any], bool]:
    """Сохранить входящее сообщение до обращения к STT/LLM, идемпотентно по Telegram message_id."""
    now = utc_now_str()
    if telegram_message_id is not None:
        cursor = await conn.execute(
            """
            INSERT OR IGNORE INTO inbox_messages
            (user_id, chat_id, telegram_message_id, source, telegram_file_id, telegram_file_unique_id,
             duration_sec, raw_text, status, local_date, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                chat_id,
                telegram_message_id,
                source,
                telegram_file_id,
                telegram_file_unique_id,
                duration_sec,
                raw_text,
                status,
                local_date,
                now,
                now,
            ),
        )
        await conn.commit()
        created = cursor.rowcount == 1
        if created:
            row = await get_inbox_message(conn, user_id, int(cursor.lastrowid))
            return row or {}, True
        existing_cur = await conn.execute(
            """
            SELECT * FROM inbox_messages
            WHERE user_id = ? AND chat_id = ? AND telegram_message_id = ?
            """,
            (user_id, chat_id, telegram_message_id),
        )
        existing_row = await existing_cur.fetchone()
        return (row_to_dict(existing_row) if existing_row else {}), False

    cursor = await conn.execute(
        """
        INSERT INTO inbox_messages
        (user_id, chat_id, telegram_message_id, source, telegram_file_id, telegram_file_unique_id,
         duration_sec, raw_text, status, local_date, created_at, updated_at)
        VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            chat_id,
            source,
            telegram_file_id,
            telegram_file_unique_id,
            duration_sec,
            raw_text,
            status,
            local_date,
            now,
            now,
        ),
    )
    await conn.commit()
    row = await get_inbox_message(conn, user_id, int(cursor.lastrowid))
    return row or {}, True


async def get_inbox_message(
    conn: aiosqlite.Connection, user_id: int, inbox_id: int
) -> Optional[Dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT * FROM inbox_messages WHERE id = ? AND user_id = ?",
        (inbox_id, user_id),
    )
    row = await cursor.fetchone()
    return row_to_dict(row) if row else None


async def update_inbox_message(
    conn: aiosqlite.Connection, inbox_id: int, **fields: Any
) -> None:
    filtered = {k: v for k, v in fields.items() if k in _INBOX_MESSAGE_FIELDS}
    if not filtered:
        return
    filtered["updated_at"] = utc_now_str()
    cols = ", ".join(f"{k} = ?" for k in filtered)
    vals = list(filtered.values()) + [inbox_id]
    await conn.execute(f"UPDATE inbox_messages SET {cols} WHERE id = ?", vals)
    await conn.commit()


async def list_inbox_messages(
    conn: aiosqlite.Connection, user_id: int, limit: int = 20
) -> List[Dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT * FROM inbox_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    return rows_to_dicts(await cursor.fetchall())


async def create_journal_entry(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    entry_type: str,
    entry_date: str,
    source: str,
    user_text: str,
    title: str,
    model_note: str = "",
    tags: Optional[List[str]] = None,
    inbox_message_id: Optional[int] = None,
) -> int:
    now = utc_now_str()
    tags_json = json.dumps(tags or [], ensure_ascii=False)
    cursor = await conn.execute(
        """
        INSERT INTO journal_entries
        (user_id, inbox_message_id, entry_type, entry_date, source, user_text, title, model_note, tags_json, is_deleted, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            user_id,
            inbox_message_id,
            entry_type,
            entry_date,
            source,
            user_text.strip(),
            title.strip(),
            (model_note or "").strip(),
            tags_json,
            now,
            now,
        ),
    )
    await conn.commit()
    return int(cursor.lastrowid)


async def get_journal_entry(
    conn: aiosqlite.Connection, user_id: int, entry_id: int
) -> Optional[Dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT * FROM journal_entries WHERE id = ? AND user_id = ? AND is_deleted = 0",
        (entry_id, user_id),
    )
    row = await cursor.fetchone()
    return row_to_dict(row) if row else None


async def list_journal_entries(
    conn: aiosqlite.Connection,
    user_id: int,
    entry_type: Optional[str] = None,
    limit: int = 15,
) -> List[Dict[str, Any]]:
    if entry_type:
        cursor = await conn.execute(
            """
            SELECT * FROM journal_entries
            WHERE user_id = ? AND is_deleted = 0 AND entry_type = ?
            ORDER BY id DESC LIMIT ?
            """,
            (user_id, entry_type, limit),
        )
    else:
        cursor = await conn.execute(
            """
            SELECT * FROM journal_entries
            WHERE user_id = ? AND is_deleted = 0
            ORDER BY id DESC LIMIT ?
            """,
            (user_id, limit),
        )
    return rows_to_dicts(await cursor.fetchall())


_RU_ENDINGS = (
    "иями", "ями", "ами", "ого", "ему", "ыми", "ими", "ться", "тся",
    "ее", "ие", "ые", "ое", "ей", "ой", "ий", "ый",
    "ам", "ям", "ах", "ях", "ом", "ем", "ым", "им",
    "ую", "юю", "ая", "яя", "ою", "ею",
    "ть", "ти", "ов", "ев", "ия", "ья", "ье",
    "ла", "ло", "ли",
    "а", "я", "у", "ю", "е", "о", "ы", "и", "ь", "й",
)


def _ru_stem(word: str) -> str:
    """Упрощённый стеммер для русских слов (оставляет не менее 3 символов)."""
    w = (word or "").strip().casefold().replace("ё", "е")
    if len(w) < 4:
        return w
    for end in _RU_ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= 3:
            return w[: -len(end)]
    return w


def _haystack_matches_query(haystack: str, query: str) -> bool:
    """Проверить, что основы всех токенов запроса встречаются в тексте."""
    needle = (query or "").strip().casefold().replace("ё", "е")
    if not needle:
        return True
    hay_norm = (haystack or "").casefold().replace("ё", "е")
    if needle in hay_norm:
        return True
    tokens = re.findall(r"[a-zа-я0-9]+", needle)
    if not tokens:
        return False
    hay_words = re.findall(r"[a-zа-я0-9]+", hay_norm)
    for tok in tokens:
        if len(tok) <= 3:
            if tok not in hay_norm:
                return False
        else:
            stem = _ru_stem(tok)
            if tok not in hay_norm and not any(w.startswith(stem) for w in hay_words):
                return False
    return True


async def search_journal_entries(
    conn: aiosqlite.Connection,
    user_id: int,
    query: str,
    limit: int = 15,
) -> List[Dict[str, Any]]:
    needle = (query or "").strip().casefold()
    if not needle:
        return await list_journal_entries(conn, user_id, limit=limit)
    cursor = await conn.execute(
        "SELECT * FROM journal_entries WHERE user_id = ? AND is_deleted = 0 ORDER BY id DESC",
        (user_id,),
    )
    all_rows = rows_to_dicts(await cursor.fetchall())
    matches: List[Dict[str, Any]] = []
    for row in all_rows:
        haystack = " ".join(
            [
                str(row.get("user_text") or ""),
                str(row.get("title") or ""),
                str(row.get("model_note") or ""),
                str(row.get("tags_json") or ""),
            ]
        )
        if _haystack_matches_query(haystack, needle):
            matches.append(row)
            if len(matches) >= limit:
                break
    return matches


async def search_user_notes(
    conn: aiosqlite.Connection,
    user_id: int,
    query: str,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Поиск по дневнику, активным действиям входящих и сырым входящим сообщениям."""
    needle = (query or "").strip()
    if not needle:
        rows = await list_journal_entries(conn, user_id, limit=limit)
        return [
            {
                "entry_date": r.get("entry_date") or "",
                "source": r.get("source") or "text",
                "title": r.get("title") or "",
                "user_text": r.get("user_text") or "",
                "kind": r.get("entry_type") or "diary",
            }
            for r in rows
        ]

    results: List[Dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    matched_inbox_ids: set[int] = set()

    def _add_result(entry_date: str, source: str, title: str, user_text: str, kind: str) -> bool:
        norm = re.sub(
            r"\s+",
            " ",
            re.sub(r"[^\w\s]+", " ", (user_text or title).casefold().replace("ё", "е")),
        ).strip()
        key = (entry_date, norm)
        if key in seen_keys:
            return False
        seen_keys.add(key)
        results.append(
            {
                "entry_date": entry_date,
                "source": source,
                "title": title,
                "user_text": user_text,
                "kind": kind,
            }
        )
        return len(results) >= limit

    # 1. journal_entries (не удалённые)
    j_cur = await conn.execute(
        "SELECT * FROM journal_entries WHERE user_id = ? AND is_deleted = 0 ORDER BY id DESC",
        (user_id,),
    )
    for row in rows_to_dicts(await j_cur.fetchall()):
        hay = " ".join(
            [
                str(row.get("user_text") or ""),
                str(row.get("title") or ""),
                str(row.get("model_note") or ""),
                str(row.get("tags_json") or ""),
            ]
        )
        if _haystack_matches_query(hay, needle):
            if row.get("inbox_message_id"):
                matched_inbox_ids.add(int(row["inbox_message_id"]))
            if _add_result(
                str(row.get("entry_date") or ""),
                str(row.get("source") or "text"),
                str(row.get("title") or ""),
                str(row.get("user_text") or ""),
                str(row.get("entry_type") or "diary"),
            ):
                return results

    # 2. Активные inbox_actions
    a_cur = await conn.execute(
        """
        SELECT a.*, m.local_date, m.source AS msg_source
        FROM inbox_actions a
        LEFT JOIN inbox_messages m ON m.id = a.inbox_message_id
        WHERE a.user_id = ? AND a.status IN ('active', 'clarifying')
        ORDER BY a.id DESC
        """,
        (user_id,),
    )
    for row in rows_to_dicts(await a_cur.fetchall()):
        hay = f"{row.get('title') or ''} {row.get('source_quote') or ''}"
        if _haystack_matches_query(hay, needle):
            if row.get("inbox_message_id"):
                matched_inbox_ids.add(int(row["inbox_message_id"]))
            edate = str(row.get("local_date") or str(row.get("created_at") or "")[:10])
            if _add_result(
                edate,
                str(row.get("msg_source") or "text"),
                str(row.get("title") or ""),
                str(row.get("source_quote") or row.get("title") or ""),
                str(row.get("item_kind") or "diary"),
            ):
                return results

    # 3. Сырые inbox_messages (исключаем отменённые и служебные ответы/вопросы)
    m_cur = await conn.execute(
        """
        SELECT * FROM inbox_messages
        WHERE user_id = ?
          AND raw_text IS NOT NULL
          AND TRIM(raw_text) != ''
          AND status NOT IN ('undone', 'answered', 'replied')
        ORDER BY id DESC
        """,
        (user_id,),
    )
    for row in rows_to_dicts(await m_cur.fetchall()):
        mid = row.get("id")
        if mid and int(mid) in matched_inbox_ids:
            continue
        raw = str(row.get("raw_text") or "").strip()
        if _haystack_matches_query(raw, needle):
            edate = str(row.get("local_date") or str(row.get("created_at") or "")[:10])
            if _add_result(
                edate,
                str(row.get("source") or "text"),
                raw[:80],
                raw,
                "raw",
            ):
                return results

    return results


async def update_journal_entry(
    conn: aiosqlite.Connection, user_id: int, entry_id: int, **fields: Any
) -> None:
    filtered = {k: v for k, v in fields.items() if k in _JOURNAL_ENTRY_FIELDS}
    if not filtered:
        return
    filtered["updated_at"] = utc_now_str()
    cols = ", ".join(f"{k} = ?" for k in filtered)
    vals = list(filtered.values()) + [entry_id, user_id]
    await conn.execute(
        f"UPDATE journal_entries SET {cols} WHERE id = ? AND user_id = ?",
        vals,
    )
    await conn.commit()


async def delete_journal_entry(
    conn: aiosqlite.Connection, user_id: int, entry_id: int, hard: bool = False
) -> None:
    if hard:
        await conn.execute(
            "DELETE FROM journal_entries WHERE id = ? AND user_id = ?",
            (entry_id, user_id),
        )
    else:
        await conn.execute(
            "UPDATE journal_entries SET is_deleted = 1, updated_at = ? WHERE id = ? AND user_id = ?",
            (utc_now_str(), entry_id, user_id),
        )
    await conn.commit()


async def create_inbox_action(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    inbox_message_id: int,
    item_kind: str,
    target_table: str,
    target_id: Optional[int],
    title: str,
    source_quote: str,
    payload: Optional[Dict[str, Any]] = None,
    status: str = "active",
) -> int:
    now = utc_now_str()
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    cursor = await conn.execute(
        """
        INSERT INTO inbox_actions
        (user_id, inbox_message_id, item_kind, target_table, target_id, title, source_quote, payload_json, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            inbox_message_id,
            item_kind,
            target_table,
            target_id,
            title.strip(),
            source_quote.strip(),
            payload_json,
            status,
            now,
            now,
        ),
    )
    await conn.commit()
    return int(cursor.lastrowid)


async def get_inbox_action(
    conn: aiosqlite.Connection, user_id: int, action_id: int
) -> Optional[Dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT * FROM inbox_actions WHERE id = ? AND user_id = ?",
        (action_id, user_id),
    )
    row = await cursor.fetchone()
    return row_to_dict(row) if row else None


async def list_inbox_actions(
    conn: aiosqlite.Connection,
    user_id: int,
    inbox_message_id: int,
    include_undone: bool = False,
) -> List[Dict[str, Any]]:
    if include_undone:
        cursor = await conn.execute(
            "SELECT * FROM inbox_actions WHERE user_id = ? AND inbox_message_id = ? ORDER BY id ASC",
            (user_id, inbox_message_id),
        )
    else:
        cursor = await conn.execute(
            """
            SELECT * FROM inbox_actions
            WHERE user_id = ? AND inbox_message_id = ? AND status != 'undone'
            ORDER BY id ASC
            """,
            (user_id, inbox_message_id),
        )
    return rows_to_dicts(await cursor.fetchall())


async def update_inbox_action(
    conn: aiosqlite.Connection, user_id: int, action_id: int, **fields: Any
) -> None:
    filtered = {k: v for k, v in fields.items() if k in _INBOX_ACTION_FIELDS}
    if not filtered:
        return
    filtered["updated_at"] = utc_now_str()
    cols = ", ".join(f"{k} = ?" for k in filtered)
    vals = list(filtered.values()) + [action_id, user_id]
    await conn.execute(
        f"UPDATE inbox_actions SET {cols} WHERE id = ? AND user_id = ?",
        vals,
    )
    await conn.commit()


async def export_user_personal_data(
    conn: aiosqlite.Connection, user_id: int
) -> Dict[str, Any]:
    """Экспорт личных записей пользователя (без токенов и данных других пользователей)."""
    inbox_cur = await conn.execute(
        "SELECT * FROM inbox_messages WHERE user_id = ? ORDER BY id ASC",
        (user_id,),
    )
    journal_cur = await conn.execute(
        "SELECT * FROM journal_entries WHERE user_id = ? AND is_deleted = 0 ORDER BY id ASC",
        (user_id,),
    )
    actions_cur = await conn.execute(
        "SELECT * FROM inbox_actions WHERE user_id = ? ORDER BY id ASC",
        (user_id,),
    )
    return {
        "exported_at": utc_now_str(),
        "inbox_messages": rows_to_dicts(await inbox_cur.fetchall()),
        "journal_entries": rows_to_dicts(await journal_cur.fetchall()),
        "inbox_actions": rows_to_dicts(await actions_cur.fetchall()),
    }


async def delete_user_personal_data(
    conn: aiosqlite.Connection, user_id: int
) -> Dict[str, int]:
    """Удалить все личные дневниковые записи, действия и сырые входящие сообщения пользователя."""
    c1 = await conn.execute("DELETE FROM inbox_actions WHERE user_id = ?", (user_id,))
    c2 = await conn.execute("DELETE FROM journal_entries WHERE user_id = ?", (user_id,))
    c3 = await conn.execute("DELETE FROM inbox_messages WHERE user_id = ?", (user_id,))
    await conn.commit()
    return {
        "inbox_actions": int(c1.rowcount or 0),
        "journal_entries": int(c2.rowcount or 0),
        "inbox_messages": int(c3.rowcount or 0),
    }


_KARMA_STATE_FIELDS = {
    "score",
    "sleep_debt",
    "food_debt",
    "tasks_debt",
    "hygiene_debt",
    "sleep_streak",
    "food_streak",
    "tasks_streak",
    "hygiene_streak",
    "last_event_summary",
    "updated_at",
}

_PULSE_CHECKIN_FIELDS = {
    "domain",
    "status",
    "ping_count",
    "snooze_count",
    "scheduled_at_utc",
    "next_ping_at_utc",
    "last_ping_at_utc",
    "resolved_at_utc",
    "card_chat_id",
    "card_message_id",
    "meta_json",
    "updated_at",
}


async def get_or_create_karma_state(
    conn: aiosqlite.Connection, user_id: int
) -> Dict[str, Any]:
    """Вернуть состояние кармы пользователя, создав базовую запись при первом обращении."""
    cur = await conn.execute(
        "SELECT * FROM user_karma_state WHERE user_id = ?",
        (user_id,),
    )
    row = await cur.fetchone()
    if row:
        return dict(row)
    now = utc_now_str()
    await conn.execute(
        """
        INSERT OR IGNORE INTO user_karma_state (user_id, updated_at)
        VALUES (?, ?)
        """,
        (user_id, now),
    )
    await conn.commit()
    cur = await conn.execute(
        "SELECT * FROM user_karma_state WHERE user_id = ?",
        (user_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else {"user_id": user_id, "score": 70}


async def update_karma_state(
    conn: aiosqlite.Connection, user_id: int, **fields: Any
) -> None:
    """Обновить разрешённые поля состояния кармы."""
    await get_or_create_karma_state(conn, user_id)
    filtered = {k: v for k, v in fields.items() if k in _KARMA_STATE_FIELDS}
    if not filtered:
        return
    filtered.setdefault("updated_at", utc_now_str())
    cols = ", ".join(f"{k} = ?" for k in filtered)
    vals = list(filtered.values()) + [user_id]
    await conn.execute(
        f"UPDATE user_karma_state SET {cols} WHERE user_id = ?",
        vals,
    )
    await conn.commit()


async def insert_karma_event(
    conn: aiosqlite.Connection,
    user_id: int,
    local_date: str,
    domain: str,
    event_type: str,
    delta: int,
    score_after: int,
    reason: str,
    created_at: str | None = None,
) -> int:
    """Добавить событие изменения кармы."""
    ts = created_at or utc_now_str()
    cur = await conn.execute(
        """
        INSERT INTO karma_events
        (user_id, local_date, domain, event_type, delta, score_after, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, local_date, domain, event_type, int(delta), int(score_after), reason, ts),
    )
    await conn.commit()
    return int(cur.lastrowid)


async def list_karma_events(
    conn: aiosqlite.Connection, user_id: int, limit: int = 10
) -> List[Dict[str, Any]]:
    """Вернуть последние события кармы (сначала новые)."""
    cur = await conn.execute(
        """
        SELECT * FROM karma_events
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, max(1, int(limit))),
    )
    return rows_to_dicts(await cur.fetchall())


async def sum_karma_delta_for_date(
    conn: aiosqlite.Connection, user_id: int, local_date: str
) -> int:
    """Посчитать суммарную дельту кармы пользователя за локальную дату."""
    cur = await conn.execute(
        """
        SELECT COALESCE(SUM(delta), 0) AS total
        FROM karma_events
        WHERE user_id = ? AND local_date = ?
        """,
        (user_id, local_date),
    )
    row = await cur.fetchone()
    return int(row["total"] or 0) if row else 0


async def update_user_weekend_schedule(
    conn: aiosqlite.Connection,
    user_id: int,
    weekend_wake: str,
    weekend_sleep: str,
) -> None:
    """Обновить расписание подъёма и отбоя на выходные."""
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE users
        SET weekend_wake_up_time = ?, weekend_sleep_time = ?, updated_at = ?
        WHERE id = ?
        """,
        (weekend_wake, weekend_sleep, now, user_id),
    )
    await conn.commit()


async def update_user_pulse_flags(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    pulse_enabled: Optional[bool] = None,
    legacy_pings_enabled: Optional[bool] = None,
) -> None:
    """Обновить флаги Пульса дня и legacy-уведомлений."""
    updates: Dict[str, Any] = {}
    if pulse_enabled is not None:
        updates["pulse_enabled"] = 1 if pulse_enabled else 0
    if legacy_pings_enabled is not None:
        updates["legacy_pings_enabled"] = 1 if legacy_pings_enabled else 0
    if not updates:
        return
    updates["updated_at"] = utc_now_str()
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [user_id]
    await conn.execute(f"UPDATE users SET {cols} WHERE id = ?", vals)
    await conn.commit()


async def update_user_sick_freeze(
    conn: aiosqlite.Connection,
    user_id: int,
    sick_until_utc: Optional[str],
    sick_uses_month: str,
    sick_uses_count: int,
) -> None:
    """Сохранить заморозку «Болею / форс-мажор» и месячный счётчик."""
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE users
        SET sick_until_utc = ?, sick_uses_month = ?, sick_uses_count = ?, updated_at = ?
        WHERE id = ?
        """,
        (sick_until_utc, sick_uses_month, int(sick_uses_count), now, user_id),
    )
    await conn.commit()


async def update_user_sleep_confirmed(
    conn: aiosqlite.Connection,
    user_id: int,
    local_date: str,
    confirmed_at_utc: str,
) -> None:
    """Зафиксировать, что пользователь подтвердил отбой («убрал мобилу»)."""
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE users
        SET sleep_confirmed_date = ?, sleep_confirmed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (local_date, confirmed_at_utc, now, user_id),
    )
    await conn.commit()


async def clear_user_sleep_confirmed(conn: aiosqlite.Connection, user_id: int) -> None:
    """Сбросить активное состояние ночного отбоя при пробуждении."""
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE users
        SET sleep_confirmed_date = NULL, sleep_confirmed_at = NULL, updated_at = ?
        WHERE id = ?
        """,
        (now, user_id),
    )
    await conn.commit()


async def update_user_night_catch(
    conn: aiosqlite.Connection, user_id: int, caught_at_utc: str
) -> None:
    """Сохранить время последнего ночного предупреждения."""
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET night_catch_last_at = ?, updated_at = ? WHERE id = ?",
        (caught_at_utc, now, user_id),
    )
    await conn.commit()


async def get_pulse_checkin(
    conn: aiosqlite.Connection, user_id: int, local_date: str, slot_key: str
) -> Optional[Dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT * FROM pulse_checkins
        WHERE user_id = ? AND local_date = ? AND slot_key = ?
        """,
        (user_id, local_date, slot_key),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_pulse_checkin_by_id(
    conn: aiosqlite.Connection, checkin_id: int
) -> Optional[Dict[str, Any]]:
    cur = await conn.execute(
        "SELECT * FROM pulse_checkins WHERE id = ?",
        (checkin_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def upsert_pulse_checkin(
    conn: aiosqlite.Connection,
    user_id: int,
    local_date: str,
    slot_key: str,
    domain: str,
    scheduled_at_utc: str,
    next_ping_at_utc: str,
    status: str = "pending",
    meta_json: str = "{}",
) -> Dict[str, Any]:
    """Создать чек-ин слота, если он ещё не создан (или вернуть существующий)."""
    now = utc_now_str()
    await conn.execute(
        """
        INSERT OR IGNORE INTO pulse_checkins
        (user_id, local_date, slot_key, domain, status, ping_count, snooze_count,
         scheduled_at_utc, next_ping_at_utc, meta_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            local_date,
            slot_key,
            domain,
            status,
            scheduled_at_utc,
            next_ping_at_utc,
            meta_json,
            now,
            now,
        ),
    )
    await conn.commit()
    row = await get_pulse_checkin(conn, user_id, local_date, slot_key)
    return row or {}


async def update_pulse_checkin(
    conn: aiosqlite.Connection, checkin_id: int, **fields: Any
) -> Optional[Dict[str, Any]]:
    filtered = {k: v for k, v in fields.items() if k in _PULSE_CHECKIN_FIELDS}
    if not filtered:
        return await get_pulse_checkin_by_id(conn, checkin_id)
    filtered.setdefault("updated_at", utc_now_str())
    cols = ", ".join(f"{k} = ?" for k in filtered)
    vals = list(filtered.values()) + [checkin_id]
    await conn.execute(
        f"UPDATE pulse_checkins SET {cols} WHERE id = ?",
        vals,
    )
    await conn.commit()
    return await get_pulse_checkin_by_id(conn, checkin_id)


async def transition_pulse_checkin(
    conn: aiosqlite.Connection,
    checkin_id: int,
    *,
    allowed_statuses,
    user_id: int | None = None,
    expected_fields: Dict[str, Any] | None = None,
    **fields: Any,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Atomically transition a Pulse row if its current state still matches."""
    filtered = {key: value for key, value in fields.items() if key in _PULSE_CHECKIN_FIELDS}
    expected = dict(expected_fields or {})
    unknown_expected = set(expected) - _PULSE_CHECKIN_FIELDS
    if unknown_expected:
        raise ValueError(f"unsupported expected Pulse fields: {', '.join(sorted(unknown_expected))}")
    statuses = tuple(str(status) for status in allowed_statuses)
    if not statuses:
        row = await get_pulse_checkin_by_id(conn, checkin_id)
        if row and user_id is not None and int(row["user_id"]) != int(user_id):
            return False, None
        return False, row

    filtered.setdefault("updated_at", utc_now_str())
    assignments = ", ".join(f"{key} = ?" for key in filtered)
    conditions = ["id = ?", f"status IN ({','.join('?' for _ in statuses)})"]
    params: list[Any] = [*filtered.values(), int(checkin_id), *statuses]
    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(int(user_id))
    for key, value in expected.items():
        conditions.append(f"{key} IS ?")
        params.append(value)

    updated_rows = await conn.execute_fetchall(
        f"UPDATE pulse_checkins SET {assignments} WHERE {' AND '.join(conditions)} RETURNING *",
        params,
    )
    await conn.commit()
    if len(updated_rows) == 1:
        return True, dict(updated_rows[0])
    row = await get_pulse_checkin_by_id(conn, checkin_id)
    if row and user_id is not None and int(row["user_id"]) != int(user_id):
        return False, None
    return False, row


async def list_pulse_checkins_for_date(
    conn: aiosqlite.Connection, user_id: int, local_date: str
) -> List[Dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT * FROM pulse_checkins
        WHERE user_id = ? AND local_date = ?
        ORDER BY scheduled_at_utc ASC, id ASC
        """,
        (user_id, local_date),
    )
    return rows_to_dicts(await cur.fetchall())


async def list_due_pulse_checkins(
    conn: aiosqlite.Connection, now_utc_iso: str
) -> List[Dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT * FROM pulse_checkins
        WHERE status IN ('pending', 'snoozed') AND next_ping_at_utc <= ?
        ORDER BY next_ping_at_utc ASC, id ASC
        """,
        (now_utc_iso,),
    )
    return rows_to_dicts(await cur.fetchall())


async def rollover_unfinished_day_plan_items(
    conn: aiosqlite.Connection, user_id: int, from_date: str, to_date: str
) -> int:
    """Перенести незавершённые дела дня с from_date на to_date без дублей."""
    src_cur = await conn.execute(
        """
        SELECT dpi.id, dpi.title, dpi.category, dpi.is_important
        FROM day_plans dp
        JOIN day_plan_items dpi ON dpi.plan_id = dp.id
        WHERE dp.user_id = ? AND dp.plan_date = ? AND COALESCE(dpi.done, 0) = 0
        ORDER BY dpi.is_important DESC, dpi.id ASC
        """,
        (user_id, from_date),
    )
    src_rows = rows_to_dicts(await src_cur.fetchall())
    if not src_rows:
        return 0

    now = utc_now_str()
    plan_cur = await conn.execute(
        "SELECT id FROM day_plans WHERE user_id = ? AND plan_date = ?",
        (user_id, to_date),
    )
    plan_row = await plan_cur.fetchone()
    if plan_row:
        target_plan_id = int(plan_row["id"])
        await conn.execute(
            "UPDATE day_plans SET updated_at = ? WHERE id = ?",
            (now, target_plan_id),
        )
    else:
        ins_cur = await conn.execute(
            """
            INSERT INTO day_plans (user_id, plan_date, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, to_date, now, now),
        )
        target_plan_id = int(ins_cur.lastrowid)

    existing_cur = await conn.execute(
        """
        SELECT dpi.title
        FROM day_plans dp
        JOIN day_plan_items dpi ON dpi.plan_id = dp.id
        WHERE dp.user_id = ? AND dp.plan_date = ?
        """,
        (user_id, to_date),
    )
    existing_titles = {
        str(r["title"] or "").strip().lower() for r in await existing_cur.fetchall()
    }

    moved_ids: list[int] = []
    inserted = 0
    for item in src_rows:
        title = str(item.get("title") or "").strip()
        norm = title.lower()
        moved_ids.append(int(item["id"]))
        if not title or norm in existing_titles:
            continue
        existing_titles.add(norm)
        await conn.execute(
            """
            INSERT INTO day_plan_items
            (plan_id, user_id, title, category, is_important, done, synced_to_today, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (
                target_plan_id,
                user_id,
                title,
                item.get("category") or "misc",
                1 if item.get("is_important") else 0,
                now,
                now,
            ),
        )
        inserted += 1

    if moved_ids:
        placeholders = ",".join("?" for _ in moved_ids)
        await conn.execute(
            f"DELETE FROM day_plan_items WHERE user_id = ? AND id IN ({placeholders})",
            (user_id, *moved_ids),
        )
    await conn.commit()
    return inserted


async def sync_overdue_day_plan_items(
    conn: aiosqlite.Connection,
    user_id: int,
    target_date: str,
    *,
    lookback_days: int = 7,
) -> int:
    """Автоматически подтянуть незакрытые дела за последние N дней в актуальный день target_date."""
    try:
        min_date = (
            datetime.date.fromisoformat(str(target_date))
            - datetime.timedelta(days=max(1, int(lookback_days)))
        ).isoformat()
    except Exception:
        return 0

    src_cur = await conn.execute(
        """
        SELECT dpi.id, dpi.title
        FROM day_plans dp
        JOIN day_plan_items dpi ON dpi.plan_id = dp.id
        WHERE dp.user_id = ? AND dp.plan_date >= ? AND dp.plan_date < ? AND COALESCE(dpi.done, 0) = 0
        ORDER BY dp.plan_date ASC, dpi.is_important DESC, dpi.id ASC
        """,
        (int(user_id), min_date, str(target_date)),
    )
    overdue_rows = rows_to_dicts(await src_cur.fetchall())
    if not overdue_rows:
        return 0

    now = utc_now_str()
    plan_cur = await conn.execute(
        "SELECT id FROM day_plans WHERE user_id = ? AND plan_date = ?",
        (int(user_id), str(target_date)),
    )
    plan_row = await plan_cur.fetchone()
    if plan_row:
        target_plan_id = int(plan_row["id"])
        await conn.execute(
            "UPDATE day_plans SET updated_at = ? WHERE id = ?",
            (now, target_plan_id),
        )
    else:
        ins_cur = await conn.execute(
            """
            INSERT INTO day_plans (user_id, plan_date, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (int(user_id), str(target_date), now, now),
        )
        target_plan_id = int(ins_cur.lastrowid)

    existing_cur = await conn.execute(
        """
        SELECT dpi.title
        FROM day_plans dp
        JOIN day_plan_items dpi ON dpi.plan_id = dp.id
        WHERE dp.user_id = ? AND dp.plan_date = ?
        """,
        (int(user_id), str(target_date)),
    )
    existing_titles = {
        str(r["title"] or "").strip().lower() for r in await existing_cur.fetchall()
    }

    moved = 0
    for item in overdue_rows:
        title = str(item.get("title") or "").strip()
        key = title.lower()
        if not title or key in existing_titles:
            continue
        await conn.execute(
            "UPDATE day_plan_items SET plan_id = ?, updated_at = ? WHERE id = ?",
            (target_plan_id, now, int(item["id"])),
        )
        existing_titles.add(key)
        moved += 1

    if moved:
        await conn.commit()
    return moved


async def get_or_create_profile_memory(
    conn: aiosqlite.Connection, user_id: int
) -> Dict[str, Any]:
    """Вернуть память/портрет привычек пользователя, создав строку при необходимости."""
    cur = await conn.execute(
        "SELECT * FROM user_profile_memory WHERE user_id = ?",
        (user_id,),
    )
    row = await cur.fetchone()
    if row:
        return dict(row)
    now = utc_now_str()
    await conn.execute(
        """
        INSERT OR IGNORE INTO user_profile_memory
        (user_id, portrait_text, patterns_json, last_refreshed_at, events_seen_count, updated_at)
        VALUES (?, '', '[]', NULL, 0, ?)
        """,
        (user_id, now),
    )
    await conn.commit()
    cur = await conn.execute(
        "SELECT * FROM user_profile_memory WHERE user_id = ?",
        (user_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else {
        "user_id": user_id,
        "portrait_text": "",
        "patterns_json": "[]",
        "last_refreshed_at": None,
        "events_seen_count": 0,
    }


async def update_profile_memory(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    portrait_text: str,
    patterns_json: str,
    last_refreshed_at: str,
    events_seen_count: int,
) -> None:
    """Обновить портрет привычек пользователя."""
    await get_or_create_profile_memory(conn, user_id)
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE user_profile_memory
        SET portrait_text = ?, patterns_json = ?, last_refreshed_at = ?,
            events_seen_count = ?, updated_at = ?
        WHERE user_id = ?
        """,
        (
            portrait_text,
            patterns_json,
            last_refreshed_at,
            int(events_seen_count),
            now,
            user_id,
        ),
    )
    await conn.commit()


async def count_karma_events(conn: aiosqlite.Connection, user_id: int) -> int:
    cur = await conn.execute(
        "SELECT COUNT(*) AS cnt FROM karma_events WHERE user_id = ?",
        (user_id,),
    )
    row = await cur.fetchone()
    return int(row["cnt"] or 0) if row else 0


async def list_karma_events_since(
    conn: aiosqlite.Connection, user_id: int, since_date: str
) -> List[Dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT * FROM karma_events
        WHERE user_id = ? AND local_date >= ?
        ORDER BY id DESC
        """,
        (user_id, since_date),
    )
    return rows_to_dicts(await cur.fetchall())


async def list_pulse_checkins_since(
    conn: aiosqlite.Connection, user_id: int, since_date: str
) -> List[Dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT * FROM pulse_checkins
        WHERE user_id = ? AND local_date >= ?
        ORDER BY local_date DESC, id DESC
        """,
        (user_id, since_date),
    )
    return rows_to_dicts(await cur.fetchall())


async def find_and_delete_pantry_item_by_name(
    conn: aiosqlite.Connection,
    user_id: int,
    query: str,
) -> Optional[Dict[str, Any]]:
    """Найти активный продукт в холодильнике по названию/основе, мягко удалить и вернуть данные."""
    needle = (query or "").strip().casefold().replace("ё", "е")
    if not needle:
        return None
    rows = [row_to_dict(r) for r in await list_pantry_items(conn, user_id)]
    if not rows:
        return None

    for row in rows:
        name_norm = str(row.get("name") or "").strip().casefold().replace("ё", "е")
        if name_norm == needle:
            await delete_pantry_item(conn, user_id, int(row["id"]))
            return row

    needle_tokens = re.findall(r"[a-zа-я0-9]+", needle)
    for row in rows:
        name_norm = str(row.get("name") or "").strip().casefold().replace("ё", "е")
        if _haystack_matches_query(name_norm, needle):
            await delete_pantry_item(conn, user_id, int(row["id"]))
            return row
        if needle_tokens:
            name_stems = {_ru_stem(w) for w in re.findall(r"[a-zа-я0-9]+", name_norm)}
            if all(_ru_stem(tok) in name_stems for tok in needle_tokens):
                await delete_pantry_item(conn, user_id, int(row["id"]))
                return row
    return None


async def restore_pantry_item(
    conn: aiosqlite.Connection,
    user_id: int,
    item_data: Dict[str, Any],
) -> int:
    """Восстановить ранее удалённый продукт в холодильник (для Undo)."""
    now = utc_now_str()
    household_id = await get_or_create_household(conn, user_id)
    item_id = item_data.get("id")
    if item_id:
        cur = await conn.execute(
            "SELECT id FROM pantry_items WHERE id = ? AND household_id = ?",
            (int(item_id), household_id),
        )
        if await cur.fetchone():
            await conn.execute(
                "UPDATE pantry_items SET is_active = 1, updated_at = ? WHERE id = ? AND household_id = ?",
                (now, int(item_id), household_id),
            )
            await conn.commit()
            return int(item_id)
    return await create_pantry_item(
        conn,
        user_id,
        str(item_data.get("name") or "Продукт"),
        float(item_data.get("amount") or 1.0),
        str(item_data.get("unit") or "шт"),
        item_data.get("expires_at"),
        str(item_data.get("category") or "продукты"),
        item_data.get("low_threshold"),
    )


_TASK_FILLER_WORDS = {
    "задача",
    "задачу",
    "задачка",
    "дело",
    "выполнена",
    "выполнено",
    "выполнил",
    "выполнила",
    "сделано",
    "сделал",
    "сделала",
    "готово",
    "только",
    "что",
    "уже",
    "я",
    "все",
    "всё",
    "пункт",
}


def _normalize_task_tokens(text: str) -> tuple[str, list[str]]:
    """Нормализовать текст задачи и вернуть очищенную строку и основы слов."""
    norm = re.sub(r"[^a-zа-я0-9ё\s-]+", " ", (text or "").strip().casefold()).replace("ё", "е")
    words = [w.strip("-") for w in norm.split() if w.strip("-")]
    filtered: list[str] = []
    stems: list[str] = []
    for w in words:
        if w in _TASK_FILLER_WORDS:
            continue
        filtered.append(w)
        base = w
        for suffix in ("ться", "тся", "лся", "лась", "лись", "лось", "ся", "сь"):
            if base.endswith(suffix) and len(base) - len(suffix) >= 3:
                base = base[: -len(suffix)]
                break
        stem = _ru_stem(base) or base
        if len(stem) >= 2:
            stems.append(stem)
    return " ".join(filtered), stems


async def find_open_day_plan_item_by_title(
    conn: aiosqlite.Connection,
    user_id: int,
    query: str,
    local_date: str,
) -> Optional[Dict[str, Any]]:
    """Найти незакрытый пункт плана дня за сегодня (или вчера) по названию/основе."""
    q_clean, q_stems = _normalize_task_tokens(query)
    if not q_clean and not q_stems:
        return None

    dates_to_check = [local_date]
    try:
        prev_date = (datetime.date.fromisoformat(local_date) - datetime.timedelta(days=1)).isoformat()
        dates_to_check.append(prev_date)
    except Exception:
        pass

    q_stem_set = set(q_stems)
    for d in dates_to_check:
        rows = [row_to_dict(r) for r in await list_day_plan_items(conn, user_id, d) if not r["done"]]
        if not rows:
            continue
        # 1. Точное нормализованное совпадение
        for row in rows:
            t_clean, _ = _normalize_task_tokens(str(row.get("title") or ""))
            if t_clean and t_clean == q_clean:
                return row
        # 2. Подстрока / поиск по основам существующего хелпера
        for row in rows:
            title_raw = str(row.get("title") or "")
            t_clean, _ = _normalize_task_tokens(title_raw)
            if q_clean and t_clean and (q_clean in t_clean or t_clean in q_clean):
                return row
            if q_clean and _haystack_matches_query(title_raw, q_clean):
                return row
        # 3. Пересечение основ с учётом возвратных глаголов (Искупаться <-> искупался)
        if q_stem_set:
            for row in rows:
                _, t_stems = _normalize_task_tokens(str(row.get("title") or ""))
                t_stem_set = set(t_stems)
                if not t_stem_set:
                    continue
                if q_stem_set.issubset(t_stem_set) or t_stem_set.issubset(q_stem_set):
                    return row
    return None


# --- Меню на несколько дней (meal_plans) ---


_MEAL_SLOT_ORDER = {"breakfast": 1, "lunch": 2, "dinner": 3}


def _decode_meal_plan_row(row: aiosqlite.Row) -> Dict[str, Any]:
    d = row_to_dict(row)
    try:
        ings = json.loads(d.get("ingredients_json") or "[]")
        d["ingredients"] = ings if isinstance(ings, list) else []
    except Exception:
        d["ingredients"] = []
    return d


async def upsert_meal_plan_slot(
    conn: aiosqlite.Connection,
    user_id: int,
    plan_date: str,
    meal_slot: str,
    *,
    title: str,
    recipe_id: str = "",
    time_minutes: int = 20,
    ingredients: Optional[List[Dict[str, Any]]] = None,
    status: str = "planned",
) -> Dict[str, Any]:
    """Создать или обновить слот питания (breakfast/lunch/dinner) на дату."""
    now = utc_now_str()
    hid = await get_or_create_household(conn, user_id)
    ing_json = json.dumps(list(ingredients or []), ensure_ascii=False)
    await conn.execute(
        """
        INSERT INTO meal_plans
        (user_id, household_id, plan_date, meal_slot, title, recipe_id, time_minutes, ingredients_json, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, plan_date, meal_slot) DO UPDATE SET
            household_id = excluded.household_id,
            title = excluded.title,
            recipe_id = excluded.recipe_id,
            time_minutes = excluded.time_minutes,
            ingredients_json = excluded.ingredients_json,
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (
            user_id,
            hid,
            plan_date,
            meal_slot,
            title.strip(),
            str(recipe_id or ""),
            int(time_minutes or 20),
            ing_json,
            status,
            now,
            now,
        ),
    )
    await conn.commit()
    cur = await conn.execute(
        "SELECT * FROM meal_plans WHERE user_id = ? AND plan_date = ? AND meal_slot = ?",
        (user_id, plan_date, meal_slot),
    )
    row = await cur.fetchone()
    return _decode_meal_plan_row(row) if row else {}


async def list_meal_plans_range(
    conn: aiosqlite.Connection,
    user_id: int,
    start_date: str,
    end_date: str,
) -> List[Dict[str, Any]]:
    """Получить расписание блюд в диапазоне дат, отсортированное по дате и слоту."""
    hid = await get_or_create_household(conn, user_id)
    house = await get_household(conn, hid)
    share_plan = bool((house or {}).get("settings", {}).get("share_meal_plan", True))
    if share_plan:
        cur = await conn.execute(
            """
            SELECT * FROM meal_plans
            WHERE (household_id = ? OR user_id = ?) AND plan_date >= ? AND plan_date <= ?
            ORDER BY updated_at ASC, id ASC
            """,
            (hid, user_id, start_date, end_date),
        )
    else:
        cur = await conn.execute(
            """
            SELECT * FROM meal_plans
            WHERE user_id = ? AND plan_date >= ? AND plan_date <= ?
            ORDER BY updated_at ASC, id ASC
            """,
            (user_id, start_date, end_date),
        )
    by_slot: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in await cur.fetchall():
        d = _decode_meal_plan_row(r)
        key = (str(d.get("plan_date") or ""), str(d.get("meal_slot") or ""))
        by_slot[key] = d
    items = list(by_slot.values())
    items.sort(key=lambda r: (str(r.get("plan_date") or ""), _MEAL_SLOT_ORDER.get(str(r.get("meal_slot") or ""), 9)))
    return items


async def get_meal_plan_for_date(
    conn: aiosqlite.Connection,
    user_id: int,
    plan_date: str,
) -> List[Dict[str, Any]]:
    """Получить меню на конкретную дату."""
    return await list_meal_plans_range(conn, user_id, plan_date, plan_date)


async def mark_meal_plan_status(
    conn: aiosqlite.Connection,
    user_id: int,
    plan_date: str,
    meal_slot: str,
    status: str,
) -> bool:
    """Обновить статус блюда в меню (planned/cooked/skipped)."""
    now = utc_now_str()
    hid = await get_or_create_household(conn, user_id)
    house = await get_household(conn, hid)
    share_plan = bool((house or {}).get("settings", {}).get("share_meal_plan", True))
    if share_plan:
        cur = await conn.execute(
            """
            UPDATE meal_plans
            SET status = ?, updated_at = ?
            WHERE (household_id = ? OR user_id = ?) AND plan_date = ? AND meal_slot = ?
            """,
            (status, now, hid, user_id, plan_date, meal_slot),
        )
    else:
        cur = await conn.execute(
            """
            UPDATE meal_plans
            SET status = ?, updated_at = ?
            WHERE user_id = ? AND plan_date = ? AND meal_slot = ?
            """,
            (status, now, user_id, plan_date, meal_slot),
        )
    await conn.commit()
    return (cur.rowcount or 0) > 0


async def get_recent_meal_recipe_ids(
    conn: aiosqlite.Connection,
    user_id: int,
    since_date: str,
) -> List[str]:
    """Вернуть список ID рецептов из недавнего/текущего меню с даты since_date."""
    hid = await get_or_create_household(conn, user_id)
    house = await get_household(conn, hid)
    share_plan = bool((house or {}).get("settings", {}).get("share_meal_plan", True))
    if share_plan:
        cur = await conn.execute(
            """
            SELECT DISTINCT recipe_id FROM meal_plans
            WHERE (household_id = ? OR user_id = ?) AND plan_date >= ? AND recipe_id != ''
            """,
            (hid, user_id, since_date),
        )
    else:
        cur = await conn.execute(
            """
            SELECT DISTINCT recipe_id FROM meal_plans
            WHERE user_id = ? AND plan_date >= ? AND recipe_id != ''
            """,
            (user_id, since_date),
        )
    return [str(r["recipe_id"]) for r in await cur.fetchall() if r["recipe_id"]]


async def clear_meal_plans_range(
    conn: aiosqlite.Connection,
    user_id: int,
    start_date: str,
    end_date: str,
) -> int:
    """Удалить слоты меню в диапазоне дат (например, при пересборке)."""
    hid = await get_or_create_household(conn, user_id)
    house = await get_household(conn, hid)
    share_plan = bool((house or {}).get("settings", {}).get("share_meal_plan", True))
    if share_plan:
        cur = await conn.execute(
            "DELETE FROM meal_plans WHERE (household_id = ? OR user_id = ?) AND plan_date >= ? AND plan_date <= ?",
            (hid, user_id, start_date, end_date),
        )
    else:
        cur = await conn.execute(
            "DELETE FROM meal_plans WHERE user_id = ? AND plan_date >= ? AND plan_date <= ?",
            (user_id, start_date, end_date),
        )
    await conn.commit()
    return int(cur.rowcount or 0)


# --- Внешний календарь (Google Calendar .ics / OAuth 2.0) ---


async def upsert_calendar_connection(
    conn: aiosqlite.Connection,
    user_id: int,
    provider: str,
    *,
    label: str = "Google Calendar",
    ics_url: str = "",
    calendar_id: str = "primary",
    access_token: str = "",
    refresh_token: str = "",
    token_expires_at: Optional[str] = None,
    enabled: bool = True,
) -> Dict[str, Any]:
    """Создать или обновить подключение внешнего календаря (одна запись на user_id + provider)."""
    now = utc_now_str()
    await conn.execute(
        """
        INSERT INTO user_calendar_connections
        (user_id, provider, label, ics_url, calendar_id, access_token, refresh_token, token_expires_at,
         enabled, last_sync_at, last_error, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', ?, ?)
        ON CONFLICT(user_id, provider) DO UPDATE SET
            label = excluded.label,
            ics_url = CASE WHEN excluded.ics_url != '' THEN excluded.ics_url ELSE user_calendar_connections.ics_url END,
            calendar_id = excluded.calendar_id,
            access_token = CASE WHEN excluded.access_token != '' THEN excluded.access_token ELSE user_calendar_connections.access_token END,
            refresh_token = CASE WHEN excluded.refresh_token != '' THEN excluded.refresh_token ELSE user_calendar_connections.refresh_token END,
            token_expires_at = COALESCE(excluded.token_expires_at, user_calendar_connections.token_expires_at),
            enabled = excluded.enabled,
            last_error = '',
            updated_at = excluded.updated_at
        """,
        (
            user_id,
            provider,
            label.strip() or "Google Calendar",
            ics_url.strip(),
            calendar_id.strip() or "primary",
            access_token.strip(),
            refresh_token.strip(),
            token_expires_at,
            1 if enabled else 0,
            now,
            now,
        ),
    )
    await conn.commit()
    cur = await conn.execute(
        "SELECT * FROM user_calendar_connections WHERE user_id = ? AND provider = ?",
        (user_id, provider),
    )
    row = await cur.fetchone()
    return row_to_dict(row) if row else {}


async def list_user_calendar_connections(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    enabled_only: bool = True,
) -> List[Dict[str, Any]]:
    """Вернуть подключения календарей пользователя."""
    sql = "SELECT * FROM user_calendar_connections WHERE user_id = ?"
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY id ASC"
    cur = await conn.execute(sql, (user_id,))
    return rows_to_dicts(await cur.fetchall())


async def list_all_enabled_calendar_connections(
    conn: aiosqlite.Connection,
) -> List[Dict[str, Any]]:
    """Вернуть все активные подключения календарей для фоновой синхронизации."""
    cur = await conn.execute(
        "SELECT * FROM user_calendar_connections WHERE enabled = 1 ORDER BY user_id ASC, id ASC"
    )
    return rows_to_dicts(await cur.fetchall())


async def update_calendar_connection_sync(
    conn: aiosqlite.Connection,
    conn_id: int,
    *,
    last_sync_at: Optional[str] = None,
    last_error: str = "",
    access_token: Optional[str] = None,
    refresh_token: Optional[str] = None,
    token_expires_at: Optional[str] = None,
) -> None:
    """Обновить статус синхронизации и/или OAuth-токены подключения."""
    now = utc_now_str()
    fields = ["last_error = ?", "updated_at = ?"]
    params: List[Any] = [str(last_error or ""), now]
    if last_sync_at is not None:
        fields.append("last_sync_at = ?")
        params.append(last_sync_at)
    if access_token is not None:
        fields.append("access_token = ?")
        params.append(access_token)
    if refresh_token is not None:
        fields.append("refresh_token = ?")
        params.append(refresh_token)
    if token_expires_at is not None:
        fields.append("token_expires_at = ?")
        params.append(token_expires_at)
    params.append(int(conn_id))
    await conn.execute(
        f"UPDATE user_calendar_connections SET {', '.join(fields)} WHERE id = ?",
        tuple(params),
    )
    await conn.commit()


async def delete_calendar_connections(
    conn: aiosqlite.Connection,
    user_id: int,
    provider: Optional[str] = None,
) -> int:
    """Удалить подключения календаря (и кэшированные события этого источника)."""
    if provider:
        cur = await conn.execute(
            "DELETE FROM user_calendar_connections WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
        await conn.execute(
            "DELETE FROM calendar_events WHERE user_id = ? AND source = ?",
            (user_id, provider),
        )
    else:
        cur = await conn.execute(
            "DELETE FROM user_calendar_connections WHERE user_id = ?",
            (user_id,),
        )
        await conn.execute(
            "DELETE FROM calendar_events WHERE user_id = ?",
            (user_id,),
        )
    await conn.commit()
    return int(cur.rowcount or 0)


async def replace_calendar_events_window(
    conn: aiosqlite.Connection,
    user_id: int,
    source: str,
    start_date: str,
    end_date: str,
    events: List[Dict[str, Any]],
) -> int:
    """Заменить кэш событий в окне [start_date, end_date], сохраняя pre_alert_sent для неизменных событий."""
    now = utc_now_str()
    inserted = 0
    seen_keys: set[tuple[str, str, str]] = set()
    prepared: List[tuple[tuple[str, str, str], Dict[str, Any]]] = []
    for ev in events:
        uid = str(ev.get("external_uid") or "").strip()
        ev_date = str(ev.get("event_date") or "").strip()
        st = str(ev.get("start_time") or "00:00").strip()
        en = str(ev.get("end_time") or st).strip()
        title = str(ev.get("title") or "Событие").strip() or "Событие"
        if not uid or not ev_date or ev_date < start_date or ev_date > end_date:
            continue
        key = (uid, ev_date, st)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        prepared.append((key, {
            "end_time": en,
            "is_all_day": 1 if ev.get("is_all_day") else 0,
            "title": title,
            "location": str(ev.get("location") or "").strip(),
            "description": str(ev.get("description") or "").strip(),
            "is_busy": 1 if ev.get("is_busy", 1) else 0,
            "pre_alert_sent": int(ev.get("pre_alert_sent") or 0),
            "alert_end_utc": ev.get("alert_end_utc"),
        }))

    # Keep stable row IDs so a concurrent scheduler delete/mark refers to the
    # same event while calendar sync refreshes its details.
    async with independent_connection(conn) as writer:
        await writer.execute("BEGIN IMMEDIATE")
        try:
            cur = await writer.execute(
                "SELECT id, external_uid, event_date, start_time, pre_alert_sent, alert_chat_id, alert_message_id "
                "FROM calendar_events WHERE user_id = ? AND source = ? AND event_date >= ? AND event_date <= ?",
                (user_id, source, start_date, end_date),
            )
            old_rows = await cur.fetchall()
            old_ids: Dict[tuple[str, str, str], int] = {}
            alerted_keys: Dict[tuple[str, str, str], tuple[Optional[int], Optional[int]]] = {}
            for row in old_rows:
                key = (str(row["external_uid"]), str(row["event_date"]), str(row["start_time"]))
                old_ids[key] = int(row["id"])
                if row["pre_alert_sent"]:
                    alerted_keys[key] = (row["alert_chat_id"], row["alert_message_id"])

            await writer.execute(
                "DELETE FROM calendar_events WHERE user_id = ? AND source = ? AND event_date >= ? AND event_date <= ?",
                (user_id, source, start_date, end_date),
            )
            for key, values in prepared:
                uid, ev_date, st = key
                alert_chat_id, alert_message_id = alerted_keys.get(key, (None, None))
                pre_alert_sent = 1 if key in alerted_keys else values["pre_alert_sent"]
                await writer.execute(
                    """
                    INSERT INTO calendar_events
                    (id, user_id, source, external_uid, event_date, start_time, end_time,
                     is_all_day, title, location, description, is_busy, pre_alert_sent,
                     alert_chat_id, alert_message_id, alert_end_utc, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (old_ids.get(key), user_id, source, uid, ev_date, st, values["end_time"],
                     values["is_all_day"], values["title"], values["location"],
                     values["description"], values["is_busy"], pre_alert_sent,
                     alert_chat_id, alert_message_id, values["alert_end_utc"], now),
                )
                inserted += 1
            await writer.commit()
        except Exception:
            await writer.rollback()
            raise
    return inserted


async def upsert_single_calendar_event(
    conn: aiosqlite.Connection,
    user_id: int,
    source: str,
    event: Dict[str, Any],
) -> Dict[str, Any]:
    """Добавить или обновить одно событие в кэше calendar_events."""
    now = utc_now_str()
    uid = str(event.get("external_uid") or "").strip()
    ev_date = str(event.get("event_date") or "").strip()
    st = str(event.get("start_time") or "00:00").strip()
    en = str(event.get("end_time") or st).strip()
    alert_end_utc = event.get("alert_end_utc")
    await conn.execute(
        """
        INSERT INTO calendar_events
        (user_id, source, external_uid, event_date, start_time, end_time,
         is_all_day, title, location, description, is_busy, pre_alert_sent, alert_end_utc, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, source, external_uid, event_date, start_time) DO UPDATE SET
            end_time = excluded.end_time,
            is_all_day = excluded.is_all_day,
            title = excluded.title,
            location = excluded.location,
            description = excluded.description,
            is_busy = excluded.is_busy,
            alert_end_utc = excluded.alert_end_utc,
            updated_at = excluded.updated_at
        """,
        (
            user_id,
            source,
            uid,
            ev_date,
            st,
            en,
            1 if event.get("is_all_day") else 0,
            str(event.get("title") or "Событие").strip() or "Событие",
            str(event.get("location") or "").strip(),
            str(event.get("description") or "").strip(),
            1 if event.get("is_busy", 1) else 0,
            int(event.get("pre_alert_sent") or 0),
            alert_end_utc,
            now,
        ),
    )
    await conn.commit()
    cur = await conn.execute(
        """
        SELECT * FROM calendar_events
        WHERE user_id = ? AND source = ? AND external_uid = ? AND event_date = ? AND start_time = ?
        """,
        (user_id, source, uid, ev_date, st),
    )
    row = await cur.fetchone()
    return row_to_dict(row) if row else {}


async def list_calendar_events_range(
    conn: aiosqlite.Connection,
    user_id: int,
    start_date: str,
    end_date: str,
) -> List[Dict[str, Any]]:
    """Получить события внешнего календаря в диапазоне дат."""
    cur = await conn.execute(
        """
        SELECT * FROM calendar_events
        WHERE user_id = ? AND event_date >= ? AND event_date <= ?
        ORDER BY event_date ASC, is_all_day DESC, start_time ASC, id ASC
        """,
        (user_id, start_date, end_date),
    )
    return rows_to_dicts(await cur.fetchall())


async def mark_calendar_event_alert_sent(
    conn: aiosqlite.Connection,
    event_id: int,
    *,
    chat_id: Optional[int] = None,
    message_id: Optional[int] = None,
) -> None:
    """Отметить, что пред-событийное предупреждение уже отправлено."""
    now = utc_now_str()
    await conn.execute(
        "UPDATE calendar_events SET pre_alert_sent = 1, alert_chat_id = ?, alert_message_id = ?, updated_at = ? WHERE id = ?",
        (chat_id, message_id, now, int(event_id)),
    )
    await conn.commit()


async def clear_calendar_event_alert_message(conn: aiosqlite.Connection, event_id: int) -> None:
    """Forget a Telegram alert reference without allowing the alert to resend."""
    await conn.execute(
        "UPDATE calendar_events SET alert_chat_id = NULL, alert_message_id = NULL, updated_at = ? WHERE id = ?",
        (utc_now_str(), int(event_id)),
    )
    await conn.commit()


async def list_calendar_events_with_active_alert(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    """List active Telegram alert refs, including events from previous dates."""
    cur = await conn.execute(
        "SELECT * FROM calendar_events WHERE alert_message_id IS NOT NULL ORDER BY event_date, start_time, id"
    )
    return rows_to_dicts(await cur.fetchall())


async def create_gcal_oauth_state(
    conn: aiosqlite.Connection,
    user_id: int,
    state_token: str,
    expires_at: str,
) -> None:
    """Сохранить одноразовый OAuth state для авторизации Google Calendar."""
    now = utc_now_str()
    await conn.execute(
        """
        INSERT INTO gcal_oauth_states (state_token, user_id, expires_at, used, created_at)
        VALUES (?, ?, ?, 0, ?)
        """,
        (state_token, user_id, expires_at, now),
    )
    await conn.commit()


async def consume_gcal_oauth_state(
    conn: aiosqlite.Connection,
    state_token: str,
    now_iso: str,
) -> Optional[int]:
    """Проверить и однократно погасить OAuth state, вернув user_id."""
    cur = await conn.execute(
        "SELECT * FROM gcal_oauth_states WHERE state_token = ?",
        (state_token,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    d = row_to_dict(row)
    if d.get("used") or str(d.get("expires_at") or "") < now_iso:
        return None
    cur_upd = await conn.execute(
        "UPDATE gcal_oauth_states SET used = 1 WHERE state_token = ? AND used = 0",
        (state_token,),
    )
    await conn.commit()
    if (cur_upd.rowcount or 0) <= 0:
        return None
    return int(d["user_id"])


async def update_user_early_wake_alert_date(
    conn: aiosqlite.Connection,
    user_id: int,
    local_date: str,
) -> None:
    """Сохранить дату последнего вечернего напоминания о раннем подъёме."""
    now = utc_now_str()
    await conn.execute(
        "UPDATE users SET early_wake_alert_date = ?, updated_at = ? WHERE id = ?",
        (local_date, now, user_id),
    )
    await conn.commit()


async def list_schedule_events_range(
    conn: aiosqlite.Connection,
    user_id: int,
    start_date: str,
    end_date: str,
) -> List[Dict[str, Any]]:
    """Получить локальные события расписания за диапазон дат."""
    cur = await conn.execute(
        """
        SELECT * FROM schedule_events
        WHERE user_id = ? AND event_date >= ? AND event_date <= ?
        ORDER BY event_date ASC, start_time ASC, id ASC
        """,
        (user_id, start_date, end_date),
    )
    return rows_to_dicts(await cur.fetchall())


def _decode_recipe_json_list(raw: Any) -> List[Any]:
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _decode_recipe_nutrition(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, dict):
        return raw if raw else None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) and parsed else None
    except Exception:
        return None


def _user_recipe_row_to_dict(row: Any) -> Dict[str, Any]:
    d = row_to_dict(row)
    out = {
        "id": str(d.get("recipe_key") or ""),
        "db_id": int(d["id"]) if d.get("id") is not None else None,
        "title": str(d.get("title") or ""),
        "desc": str(d.get("desc") or ""),
        "tags": [str(t) for t in _decode_recipe_json_list(d.get("tags_json")) if str(t).strip()],
        "base_servings": max(1, int(d.get("base_servings") or 2)),
        "time_minutes": max(1, int(d.get("time_minutes") or 20)),
        "ingredients": [
            i for i in _decode_recipe_json_list(d.get("ingredients_json"))
            if isinstance(i, dict) and str(i.get("name") or "").strip()
        ],
        "steps": [str(s) for s in _decode_recipe_json_list(d.get("steps_json")) if str(s).strip()],
        "source": str(d.get("source") or "ai_chef"),
        "source_label": str(d.get("source_label") or ""),
        "is_user_recipe": True,
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
    }
    nutr = _decode_recipe_nutrition(d.get("nutrition_json"))
    if nutr is not None:
        out["nutrition"] = nutr
    return out


def generate_user_recipe_key() -> str:
    """Сгенерировать короткий ключ рецепта, умещающийся в callback_data Telegram."""
    return f"ur_{uuid.uuid4().hex[:10]}"


async def upsert_user_recipe(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    recipe_key: Optional[str] = None,
    title: str,
    desc: str = "",
    tags: Optional[List[str]] = None,
    base_servings: int = 2,
    time_minutes: int = 20,
    ingredients: Optional[List[Dict[str, Any]]] = None,
    steps: Optional[List[str]] = None,
    nutrition: Optional[Dict[str, Any]] = None,
    source: str = "ai_chef",
    source_label: str = "",
) -> Dict[str, Any]:
    """Сохранить или обновить личный рецепт пользователя в таблице user_recipes."""
    now = utc_now_str()
    hid = await get_or_create_household(conn, user_id)
    clean_key = (recipe_key or "").strip() or generate_user_recipe_key()
    clean_title = str(title or "Рецепт").strip()
    clean_desc = str(desc or "").strip()
    tags_list = [str(t).strip() for t in (tags or []) if str(t).strip()]
    ing_list = [i for i in (ingredients or []) if isinstance(i, dict) and str(i.get("name") or "").strip()]
    steps_list = [str(s).strip() for s in (steps or []) if str(s).strip()]
    nutr_json = json.dumps(nutrition, ensure_ascii=False) if isinstance(nutrition, dict) and nutrition else ""
    serv = max(1, int(base_servings or 2))
    t_min = max(1, int(time_minutes or 20))
    await conn.execute(
        """
        INSERT INTO user_recipes (
            user_id, household_id, recipe_key, title, desc, tags_json, base_servings, time_minutes,
            ingredients_json, steps_json, nutrition_json, source, source_label, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, recipe_key) DO UPDATE SET
            household_id = excluded.household_id,
            title = excluded.title,
            desc = excluded.desc,
            tags_json = excluded.tags_json,
            base_servings = excluded.base_servings,
            time_minutes = excluded.time_minutes,
            ingredients_json = excluded.ingredients_json,
            steps_json = excluded.steps_json,
            nutrition_json = CASE
                WHEN excluded.nutrition_json <> '' THEN excluded.nutrition_json
                ELSE user_recipes.nutrition_json
            END,
            source = excluded.source,
            source_label = excluded.source_label,
            updated_at = excluded.updated_at
        """,
        (
            user_id,
            hid,
            clean_key,
            clean_title,
            clean_desc,
            json.dumps(tags_list, ensure_ascii=False),
            serv,
            t_min,
            json.dumps(ing_list, ensure_ascii=False),
            json.dumps(steps_list, ensure_ascii=False),
            nutr_json,
            str(source or "ai_chef").strip(),
            str(source_label or "").strip(),
            now,
            now,
        ),
    )
    await conn.commit()
    return await get_user_recipe_by_key(conn, user_id, clean_key) or {}


async def list_user_recipes(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    limit: int = 300,
) -> List[Dict[str, Any]]:
    """Вернуть рецепты пользователя (и общего дома при включённом share_recipes)."""
    hid = await get_or_create_household(conn, user_id)
    house = await get_household(conn, hid)
    share_recipes = bool((house or {}).get("settings", {}).get("share_recipes", True))
    eff_limit = max(1, int(limit or 300))
    if share_recipes:
        cur = await conn.execute(
            """
            SELECT * FROM user_recipes
            WHERE user_id = ? OR household_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (user_id, hid, eff_limit),
        )
    else:
        cur = await conn.execute(
            """
            SELECT * FROM user_recipes
            WHERE user_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (user_id, eff_limit),
        )
    seen_keys: set[str] = set()
    out: List[Dict[str, Any]] = []
    for row in await cur.fetchall():
        item = _user_recipe_row_to_dict(row)
        k = item["id"]
        if k in seen_keys:
            continue
        seen_keys.add(k)
        out.append(item)
    return out


async def get_user_recipe_by_key(
    conn: aiosqlite.Connection,
    user_id: int,
    recipe_key: str,
) -> Optional[Dict[str, Any]]:
    """Получить рецепт пользователя (или дома) по ключу recipe_key."""
    key = str(recipe_key or "").strip()
    if not key:
        return None
    hid = await get_or_create_household(conn, user_id)
    house = await get_household(conn, hid)
    share_recipes = bool((house or {}).get("settings", {}).get("share_recipes", True))
    if share_recipes:
        cur = await conn.execute(
            """
            SELECT * FROM user_recipes
            WHERE (user_id = ? OR household_id = ?) AND recipe_key = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (user_id, hid, key),
        )
    else:
        cur = await conn.execute(
            "SELECT * FROM user_recipes WHERE user_id = ? AND recipe_key = ?",
            (user_id, key),
        )
    row = await cur.fetchone()
    return _user_recipe_row_to_dict(row) if row else None


async def delete_user_recipe_by_key(
    conn: aiosqlite.Connection,
    user_id: int,
    recipe_key: str,
) -> bool:
    """Удалить личный рецепт пользователя."""
    key = str(recipe_key or "").strip()
    if not key:
        return False
    cur = await conn.execute(
        "DELETE FROM user_recipes WHERE user_id = ? AND recipe_key = ?",
        (user_id, key),
    )
    await conn.commit()
    return (cur.rowcount or 0) > 0


# --- Personal КБЖУ Meal Logs (strictly user-private) ---

async def add_meal_log(
    conn: aiosqlite.Connection,
    user_id: int,
    local_date: str,
    title: str,
    *,
    meal_slot: str = "snack",
    recipe_id: str = "",
    servings: float = 1.0,
    kcal: float = 0.0,
    protein_g: float = 0.0,
    fat_g: float = 0.0,
    carbs_g: float = 0.0,
    source: str = "manual",
) -> int:
    """Добавить запись приёма пищи в личный дневник КБЖУ."""
    now = utc_now_str()
    slot = meal_slot if meal_slot in {"breakfast", "lunch", "dinner", "snack"} else "snack"
    cur = await conn.execute(
        """
        INSERT INTO meal_logs (
            user_id, local_date, meal_slot, title, recipe_id, servings,
            kcal, protein_g, fat_g, carbs_g, source, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            str(local_date),
            slot,
            str(title or "Приём пищи").strip(),
            str(recipe_id or "").strip(),
            round(max(0.1, float(servings or 1.0)), 2),
            round(max(0.0, float(kcal or 0.0)), 1),
            round(max(0.0, float(protein_g or 0.0)), 1),
            round(max(0.0, float(fat_g or 0.0)), 1),
            round(max(0.0, float(carbs_g or 0.0)), 1),
            str(source or "manual").strip() or "manual",
            now,
        ),
    )
    await conn.commit()
    return int(cur.lastrowid)


async def list_meal_logs(
    conn: aiosqlite.Connection, user_id: int, local_date: str
) -> List[Dict[str, Any]]:
    """Вернуть личные приёмы пищи пользователя за указанную локальную дату."""
    cur = await conn.execute(
        """
        SELECT * FROM meal_logs
        WHERE user_id = ? AND local_date = ?
        ORDER BY id ASC
        """,
        (int(user_id), str(local_date)),
    )
    return rows_to_dicts(await cur.fetchall())


async def delete_meal_log(
    conn: aiosqlite.Connection, user_id: int, log_id: int
) -> bool:
    """Удалить личную запись приёма пищи по id."""
    cur = await conn.execute(
        "DELETE FROM meal_logs WHERE user_id = ? AND id = ?",
        (int(user_id), int(log_id)),
    )
    await conn.commit()
    return (cur.rowcount or 0) > 0


async def daily_nutrition_totals(
    conn: aiosqlite.Connection, user_id: int, local_date: str
) -> Dict[str, Any]:
    """Суммировать личные калории и БЖУ пользователя за дату."""
    cur = await conn.execute(
        """
        SELECT
            COALESCE(SUM(kcal), 0) AS kcal,
            COALESCE(SUM(protein_g), 0) AS protein_g,
            COALESCE(SUM(fat_g), 0) AS fat_g,
            COALESCE(SUM(carbs_g), 0) AS carbs_g,
            COUNT(*) AS meals_count
        FROM meal_logs
        WHERE user_id = ? AND local_date = ?
        """,
        (int(user_id), str(local_date)),
    )
    row = await cur.fetchone()
    d = row_to_dict(row) if row else {}
    return {
        "kcal": round(float(d.get("kcal") or 0.0)),
        "protein_g": round(float(d.get("protein_g") or 0.0), 1),
        "fat_g": round(float(d.get("fat_g") or 0.0), 1),
        "carbs_g": round(float(d.get("carbs_g") or 0.0), 1),
        "meals_count": int(d.get("meals_count") or 0),
    }


# --- Personal Workout Logs (strictly user-private) ---

async def add_workout_log(
    conn: aiosqlite.Connection,
    user_id: int,
    local_date: str,
    title: str,
    *,
    focus_area: str = "full_body",
    duration_min: int = 5,
    equipment_used: str = "",
    intensity: str = "easy",
    kcal_burned: int = 0,
    is_lazy_start: bool = False,
) -> int:
    """Записать выполненную тренировку или микро-старт в личную историю."""
    now = utc_now_str()
    cur = await conn.execute(
        """
        INSERT INTO workout_logs (
            user_id, local_date, title, focus_area, duration_min,
            equipment_used, intensity, kcal_burned, is_lazy_start, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            str(local_date),
            str(title or "Тренировка").strip(),
            str(focus_area or "full_body").strip(),
            max(1, int(duration_min or 5)),
            str(equipment_used or "").strip(),
            str(intensity or "easy").strip(),
            max(0, int(kcal_burned or 0)),
            1 if is_lazy_start else 0,
            now,
        ),
    )
    await conn.commit()
    return int(cur.lastrowid)


async def list_workout_logs(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    limit: int = 10,
    since_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Вернуть последние личные тренировки пользователя."""
    eff_limit = max(1, int(limit or 10))
    if since_date:
        cur = await conn.execute(
            """
            SELECT * FROM workout_logs
            WHERE user_id = ? AND local_date >= ?
            ORDER BY local_date DESC, id DESC
            LIMIT ?
            """,
            (int(user_id), str(since_date), eff_limit),
        )
    else:
        cur = await conn.execute(
            """
            SELECT * FROM workout_logs
            WHERE user_id = ?
            ORDER BY local_date DESC, id DESC
            LIMIT ?
            """,
            (int(user_id), eff_limit),
        )
    return rows_to_dicts(await cur.fetchall())


async def workout_stats_week(
    conn: aiosqlite.Connection, user_id: int, local_date: str
) -> Dict[str, Any]:
    """Сводка тренировок за последние 7 дней (включая сегодня) и за сегодня."""
    try:
        end_d = datetime.date.fromisoformat(str(local_date))
    except Exception:
        end_d = datetime.date.today()
    since_str = (end_d - datetime.timedelta(days=6)).isoformat()
    logs = await list_workout_logs(conn, user_id, limit=50, since_date=since_str)
    today_str = end_d.isoformat()
    recent_areas: List[str] = []
    for row in logs:
        area = str(row.get("focus_area") or "").strip()
        if area and area not in recent_areas:
            recent_areas.append(area)
    return {
        "since_date": since_str,
        "count": len(logs),
        "total_minutes": sum(int(r.get("duration_min") or 0) for r in logs),
        "lazy_starts": sum(1 for r in logs if int(r.get("is_lazy_start") or 0) == 1),
        "kcal_burned": sum(int(r.get("kcal_burned") or 0) for r in logs),
        "today_kcal_burned": sum(
            int(r.get("kcal_burned") or 0)
            for r in logs
            if str(r.get("local_date") or "") == today_str
        ),
        "recent_focus_areas": recent_areas,
        "latest": logs[0] if logs else None,
    }


async def get_meal_log(
    conn: aiosqlite.Connection, user_id: int, log_id: int
) -> Optional[Dict[str, Any]]:
    """Вернуть одну запись приёма пищи пользователя."""
    cur = await conn.execute(
        "SELECT * FROM meal_logs WHERE user_id = ? AND id = ?",
        (int(user_id), int(log_id)),
    )
    row = await cur.fetchone()
    return row_to_dict(row) if row else None


async def update_meal_log(
    conn: aiosqlite.Connection,
    user_id: int,
    log_id: int,
    *,
    title: Optional[str] = None,
    meal_slot: Optional[str] = None,
    kcal: Optional[float] = None,
    protein_g: Optional[float] = None,
    fat_g: Optional[float] = None,
    carbs_g: Optional[float] = None,
    servings: Optional[float] = None,
    source: Optional[str] = None,
) -> bool:
    """Обновить существующую запись КБЖУ."""
    fields = []
    values: list[Any] = []
    if title is not None:
        clean_title = str(title).strip()
        if clean_title:
            fields.append("title = ?")
            values.append(clean_title)
    if meal_slot is not None and meal_slot in {"breakfast", "lunch", "dinner", "snack"}:
        fields.append("meal_slot = ?")
        values.append(meal_slot)
    if kcal is not None:
        fields.append("kcal = ?")
        values.append(round(max(0.0, float(kcal)), 1))
    if protein_g is not None:
        fields.append("protein_g = ?")
        values.append(round(max(0.0, float(protein_g)), 1))
    if fat_g is not None:
        fields.append("fat_g = ?")
        values.append(round(max(0.0, float(fat_g)), 1))
    if carbs_g is not None:
        fields.append("carbs_g = ?")
        values.append(round(max(0.0, float(carbs_g)), 1))
    if servings is not None:
        fields.append("servings = ?")
        values.append(round(max(0.1, float(servings)), 2))
    if source is not None and str(source).strip():
        fields.append("source = ?")
        values.append(str(source).strip())
    if not fields:
        return False
    values.extend([int(user_id), int(log_id)])
    cur = await conn.execute(
        f"UPDATE meal_logs SET {', '.join(fields)} WHERE user_id = ? AND id = ?",
        tuple(values),
    )
    await conn.commit()
    return (cur.rowcount or 0) > 0


async def delete_workout_log(
    conn: aiosqlite.Connection, user_id: int, log_id: int
) -> bool:
    """Удалить личную запись тренировки по id."""
    cur = await conn.execute(
        "DELETE FROM workout_logs WHERE user_id = ? AND id = ?",
        (int(user_id), int(log_id)),
    )
    await conn.commit()
    return (cur.rowcount or 0) > 0


async def delete_weight_entry(
    conn: aiosqlite.Connection, user_id: int, weight_id: int
) -> bool:
    """Удалить личную запись взвешивания по id."""
    cur = await conn.execute(
        "DELETE FROM weights WHERE user_id = ? AND id = ?",
        (int(user_id), int(weight_id)),
    )
    await conn.commit()
    return (cur.rowcount or 0) > 0


def _norm_food_key(raw: str) -> str:
    txt = str(raw or "").casefold().replace("ё", "е")
    txt = re.sub(r"[^\w\s]+", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


async def get_nutrition_cache(
    conn: aiosqlite.Connection, query_key: str, *, max_age_days: int = 30
) -> Optional[Dict[str, Any]]:
    """Получить закэшированный онлайн-результат КБЖУ, если он не устарел."""
    qkey = _norm_food_key(query_key)
    if not qkey:
        return None
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=max(1, int(max_age_days)))).isoformat()
    cur = await conn.execute(
        "SELECT * FROM nutrition_lookup_cache WHERE query_key = ? AND datetime(updated_at) >= datetime(?)",
        (qkey, cutoff),
    )
    row = await cur.fetchone()
    if not row:
        return None
    d = row_to_dict(row)
    try:
        per_100 = json.loads(d.get("per_100g_json") or "{}")
    except Exception:
        per_100 = {}
    return {
        "query_key": d["query_key"],
        "title": d["title"],
        "source": d.get("source") or "cache",
        "source_url": d.get("source_url") or "",
        "per_100g": per_100 if isinstance(per_100, dict) else {},
        "serving_g": float(d.get("serving_g") or 0),
        "kcal_serving": float(d.get("kcal_serving") or 0),
        "protein_g": float(d.get("protein_g") or 0),
        "fat_g": float(d.get("fat_g") or 0),
        "carbs_g": float(d.get("carbs_g") or 0),
        "cached": True,
    }


async def upsert_nutrition_cache(
    conn: aiosqlite.Connection,
    query_key: str,
    *,
    title: str,
    source: str = "web_search",
    source_url: str = "",
    per_100g: Optional[Dict[str, Any]] = None,
    serving_g: float = 0.0,
    kcal_serving: float = 0.0,
    protein_g: float = 0.0,
    fat_g: float = 0.0,
    carbs_g: float = 0.0,
) -> None:
    """Сохранить найденный в сети КБЖУ-справочник в кэш."""
    qkey = _norm_food_key(query_key)
    if not qkey:
        return
    now = utc_now_str()
    await conn.execute(
        """
        INSERT INTO nutrition_lookup_cache (
            query_key, title, source, source_url, per_100g_json,
            serving_g, kcal_serving, protein_g, fat_g, carbs_g, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(query_key) DO UPDATE SET
            title = excluded.title,
            source = excluded.source,
            source_url = excluded.source_url,
            per_100g_json = excluded.per_100g_json,
            serving_g = excluded.serving_g,
            kcal_serving = excluded.kcal_serving,
            protein_g = excluded.protein_g,
            fat_g = excluded.fat_g,
            carbs_g = excluded.carbs_g,
            updated_at = excluded.updated_at
        """,
        (
            qkey,
            str(title or qkey).strip(),
            str(source or "web_search").strip(),
            str(source_url or "").strip(),
            json.dumps(per_100g or {}, ensure_ascii=False),
            round(max(0.0, float(serving_g or 0.0)), 1),
            round(max(0.0, float(kcal_serving or 0.0)), 1),
            round(max(0.0, float(protein_g or 0.0)), 1),
            round(max(0.0, float(fat_g or 0.0)), 1),
            round(max(0.0, float(carbs_g or 0.0)), 1),
            now,
        ),
    )
    await conn.commit()


async def upsert_user_food_product(
    conn: aiosqlite.Connection,
    user_id: int,
    name_key: str,
    title: str,
    *,
    brand: str = "",
    per_100g: Optional[Dict[str, Any]] = None,
    default_serving_g: float = 100.0,
    source: str = "label",
) -> None:
    """Запомнить личный продукт (с этикетки/упаковки) пользователя по КБЖУ на 100 г."""
    nkey = _norm_food_key(name_key or title)
    if not nkey:
        return
    now = utc_now_str()
    await conn.execute(
        """
        INSERT INTO user_food_products (
            user_id, name_key, title, brand, per_100g_json,
            default_serving_g, source, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, name_key) DO UPDATE SET
            title = excluded.title,
            brand = excluded.brand,
            per_100g_json = excluded.per_100g_json,
            default_serving_g = excluded.default_serving_g,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        (
            int(user_id),
            nkey,
            str(title or name_key).strip(),
            str(brand or "").strip(),
            json.dumps(per_100g or {}, ensure_ascii=False),
            round(max(1.0, float(default_serving_g or 100.0)), 1),
            str(source or "label").strip(),
            now,
            now,
        ),
    )
    await conn.commit()


async def find_user_food_product(
    conn: aiosqlite.Connection, user_id: int, query_text: str
) -> Optional[Dict[str, Any]]:
    """Найти сохранённый продукт с этикетки пользователя по названию/ключу."""
    qnorm = _norm_food_key(query_text)
    if not qnorm:
        return None
    cur = await conn.execute(
        "SELECT * FROM user_food_products WHERE user_id = ? ORDER BY datetime(updated_at) DESC LIMIT 80",
        (int(user_id),),
    )
    rows = rows_to_dicts(await cur.fetchall())
    if not rows:
        return None
    q_tokens = {w for w in qnorm.split() if len(w) >= 3}
    best_row: Optional[Dict[str, Any]] = None
    best_score = 0
    for row in rows:
        nkey = _norm_food_key(row.get("name_key") or row.get("title") or "")
        if not nkey:
            continue
        if nkey == qnorm:
            best_row = row
            best_score = 100
            break
        if len(nkey) >= 4 and (nkey in qnorm or qnorm in nkey):
            score = 50 + len(nkey)
        else:
            p_tokens = {w for w in nkey.split() if len(w) >= 3}
            if not p_tokens:
                continue
            overlap = len(q_tokens & p_tokens)
            if overlap == 0 or overlap < max(1, len(p_tokens) // 2 + len(p_tokens) % 2):
                continue
            score = overlap * 10
        if score > best_score:
            best_score = score
            best_row = row
    if not best_row:
        return None
    try:
        per_100 = json.loads(best_row.get("per_100g_json") or "{}")
    except Exception:
        per_100 = {}
    return {
        "id": int(best_row["id"]),
        "name_key": best_row["name_key"],
        "title": best_row["title"],
        "brand": best_row.get("brand") or "",
        "per_100g": per_100 if isinstance(per_100, dict) else {},
        "serving_g": float(best_row.get("default_serving_g") or 100.0),
        "source": "my_label",
        "source_url": "",
    }


async def count_user_food_products(conn: aiosqlite.Connection, user_id: int) -> int:
    cur = await conn.execute(
        "SELECT COUNT(*) AS cnt FROM user_food_products WHERE user_id = ?",
        (int(user_id),),
    )
    row = await cur.fetchone()
    return int(row["cnt"]) if row else 0


async def create_pending_meal_clarification(
    conn: aiosqlite.Connection,
    user_id: int,
    chat_id: int,
    local_date: str,
    raw_text: str,
    title: str,
    meal_slot: str,
    estimated_dict: Dict[str, Any],
    question: str,
    *,
    status: str = "pending",
    source: str = "text",
    inbox_message_id: Optional[int] = None,
    auto_commit_minutes: int = 10,
    now_utc: Optional[datetime.datetime] = None,
) -> int:
    """Создать черновик уточнения КБЖУ (с автокоммитом через N минут для ленивых)."""
    now = now_utc or datetime.datetime.utcnow()
    if now.tzinfo is not None:
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    auto_after = now + datetime.timedelta(minutes=max(1, int(auto_commit_minutes or 10)))
    eff_status = status if status in {"pending", "check"} else "pending"
    cur = await conn.execute(
        """
        INSERT INTO pending_meal_clarifications (
            user_id, chat_id, local_date, raw_text, title, meal_slot,
            estimated_json, question, status, source, inbox_message_id,
            created_at, auto_commit_after
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            int(chat_id or 0),
            str(local_date),
            str(raw_text or "").strip(),
            str(title or "Приём пищи").strip(),
            meal_slot if meal_slot in {"breakfast", "lunch", "dinner", "snack"} else "snack",
            json.dumps(estimated_dict or {}, ensure_ascii=False),
            str(question or "").strip(),
            eff_status,
            str(source or "text").strip(),
            int(inbox_message_id) if inbox_message_id is not None else None,
            now.isoformat(),
            auto_after.isoformat(),
        ),
    )
    await conn.commit()
    return int(cur.lastrowid)


def _decode_pending_meal(row: Any) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = row_to_dict(row)
    try:
        d["estimated"] = json.loads(d.get("estimated_json") or "{}")
    except Exception:
        d["estimated"] = {}
    return d


async def get_active_pending_meal(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    max_age_minutes: int = 45,
    now_utc: Optional[datetime.datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Вернуть последний активный черновик уточнения еды для пользователя."""
    now = now_utc or datetime.datetime.utcnow()
    if now.tzinfo is not None:
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    cutoff = (now - datetime.timedelta(minutes=max(5, int(max_age_minutes or 45)))).isoformat()
    cur = await conn.execute(
        """
        SELECT * FROM pending_meal_clarifications
        WHERE user_id = ? AND status = 'pending' AND datetime(created_at) >= datetime(?)
        ORDER BY id DESC LIMIT 1
        """,
        (int(user_id), cutoff),
    )
    return _decode_pending_meal(await cur.fetchone())


async def get_pending_meal_by_id(
    conn: aiosqlite.Connection, user_id: int, pending_id: int
) -> Optional[Dict[str, Any]]:
    cur = await conn.execute(
        "SELECT * FROM pending_meal_clarifications WHERE user_id = ? AND id = ?",
        (int(user_id), int(pending_id)),
    )
    return _decode_pending_meal(await cur.fetchone())


async def list_due_pending_meals(
    conn: aiosqlite.Connection, now_utc: Optional[datetime.datetime] = None
) -> List[Dict[str, Any]]:
    """Вернуть просроченные pending-черновики еды для тихого автокоммита."""
    now = now_utc or datetime.datetime.utcnow()
    if now.tzinfo is not None:
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    cur = await conn.execute(
        """
        SELECT * FROM pending_meal_clarifications
        WHERE status = 'pending' AND datetime(auto_commit_after) <= datetime(?)
        ORDER BY id ASC
        LIMIT 50
        """,
        (now.isoformat(),),
    )
    return [d for r in await cur.fetchall() if (d := _decode_pending_meal(r))]


async def resolve_pending_meal(
    conn: aiosqlite.Connection,
    pending_id: int,
    *,
    status: str = "committed",
    resolved_meal_id: Optional[int] = None,
) -> None:
    eff_status = (
        status
        if status in {"committed", "auto_committed", "cancelled", "refined"}
        else "committed"
    )
    await conn.execute(
        """
        UPDATE pending_meal_clarifications
        SET status = ?, resolved_meal_id = COALESCE(?, resolved_meal_id)
        WHERE id = ?
        """,
        (eff_status, int(resolved_meal_id) if resolved_meal_id is not None else None, int(pending_id)),
    )
    await conn.commit()


# --- Household Stage 2: dinner polls, requests, care nudges, weekly stats ---


def _decode_dinner_poll(row: Optional[aiosqlite.Row | Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    d = row_to_dict(row)
    try:
        d["options"] = json.loads(d.get("options_json") or "[]")
    except Exception:
        d["options"] = []
    try:
        votes_raw = json.loads(d.get("votes_json") or "{}")
        d["votes"] = {str(k): int(v) for k, v in votes_raw.items()} if isinstance(votes_raw, dict) else {}
    except Exception:
        d["votes"] = {}
    return d


async def create_dinner_poll(
    conn: aiosqlite.Connection,
    household_id: int,
    created_by_user_id: int,
    local_date: str,
    options: List[Dict[str, Any]],
) -> Dict[str, Any]:
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE household_dinner_polls
        SET status = 'closed', updated_at = ?
        WHERE household_id = ? AND local_date = ? AND status = 'open'
        """,
        (now, int(household_id), str(local_date)),
    )
    cur = await conn.execute(
        """
        INSERT INTO household_dinner_polls (
            household_id, created_by_user_id, local_date, options_json,
            votes_json, status, winner_recipe_id, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, '{}', 'open', '', ?, ?)
        """,
        (
            int(household_id),
            int(created_by_user_id),
            str(local_date),
            json.dumps(options, ensure_ascii=False),
            now,
            now,
        ),
    )
    await conn.commit()
    return await get_dinner_poll(conn, int(cur.lastrowid))  # type: ignore[return-value]


async def get_dinner_poll(
    conn: aiosqlite.Connection, poll_id: int
) -> Optional[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM household_dinner_polls WHERE id = ?", (int(poll_id),))
    return _decode_dinner_poll(await cur.fetchone())


async def get_latest_open_dinner_poll(
    conn: aiosqlite.Connection, household_id: int, local_date: str
) -> Optional[Dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT * FROM household_dinner_polls
        WHERE household_id = ? AND local_date = ? AND status = 'open'
        ORDER BY id DESC LIMIT 1
        """,
        (int(household_id), str(local_date)),
    )
    return _decode_dinner_poll(await cur.fetchone())


async def vote_dinner_poll(
    conn: aiosqlite.Connection, poll_id: int, user_id: int, option_idx: int
) -> Optional[Dict[str, Any]]:
    poll = await get_dinner_poll(conn, poll_id)
    if not poll:
        return None
    options = poll.get("options") or []
    if option_idx < 0 or option_idx >= len(options):
        return poll
    votes = dict(poll.get("votes") or {})
    votes[str(int(user_id))] = int(option_idx)
    now = utc_now_str()
    await conn.execute(
        "UPDATE household_dinner_polls SET votes_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(votes, ensure_ascii=False), now, int(poll_id)),
    )
    await conn.commit()
    return await get_dinner_poll(conn, poll_id)


async def decide_dinner_poll(
    conn: aiosqlite.Connection, poll_id: int, option_idx: int
) -> Optional[Dict[str, Any]]:
    poll = await get_dinner_poll(conn, poll_id)
    if not poll:
        return None
    options = poll.get("options") or []
    if option_idx < 0 or option_idx >= len(options):
        return poll
    winner_id = str(options[option_idx].get("id") or "")
    now = utc_now_str()
    await conn.execute(
        """
        UPDATE household_dinner_polls
        SET status = 'decided', winner_recipe_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (winner_id, now, int(poll_id)),
    )
    await conn.commit()
    return await get_dinner_poll(conn, poll_id)


async def create_household_request(
    conn: aiosqlite.Connection,
    household_id: int,
    from_user_id: int,
    from_name: str,
    text: str,
    *,
    kind: str = "request",
    to_user_id: Optional[int] = None,
    status: str = "open",
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    now = (
        now_utc.astimezone(datetime.timezone.utc).replace(tzinfo=None).isoformat()
        if (now_utc and now_utc.tzinfo is not None)
        else (now_utc.isoformat() if now_utc else utc_now_str())
    )
    cur = await conn.execute(
        """
        INSERT INTO household_requests (
            household_id, from_user_id, from_name, to_user_id, kind,
            text, status, response_note, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)
        """,
        (
            int(household_id),
            int(from_user_id),
            str(from_name or "").strip(),
            int(to_user_id) if to_user_id is not None else None,
            str(kind or "request").strip(),
            str(text or "").strip(),
            str(status or "open").strip(),
            now,
            now,
        ),
    )
    await conn.commit()
    return await get_household_request(conn, int(cur.lastrowid))  # type: ignore[return-value]


async def get_household_request(
    conn: aiosqlite.Connection, req_id: int
) -> Optional[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM household_requests WHERE id = ?", (int(req_id),))
    row = await cur.fetchone()
    return row_to_dict(row) if row else None


async def list_household_requests(
    conn: aiosqlite.Connection,
    household_id: int,
    *,
    kind: Optional[str] = "request",
    statuses: Optional[tuple[str, ...]] = ("open", "accepted", "snoozed"),
    limit: int = 20,
) -> List[Dict[str, Any]]:
    conds = ["household_id = ?"]
    params: list[Any] = [int(household_id)]
    if kind:
        conds.append("kind = ?")
        params.append(kind)
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        conds.append(f"status IN ({placeholders})")
        params.extend(statuses)
    params.append(int(limit))
    rows = await conn.execute_fetchall(
        f"""
        SELECT * FROM household_requests
        WHERE {' AND '.join(conds)}
        ORDER BY id DESC LIMIT ?
        """,
        params,
    )
    return rows_to_dicts(rows)


async def update_household_request_status(
    conn: aiosqlite.Connection,
    household_id: int,
    req_id: int,
    status: str,
    *,
    response_note: str = "",
    now_utc: Optional[datetime.datetime] = None,
) -> Optional[Dict[str, Any]]:
    now = (
        now_utc.astimezone(datetime.timezone.utc).replace(tzinfo=None).isoformat()
        if (now_utc and now_utc.tzinfo is not None)
        else (now_utc.isoformat() if now_utc else utc_now_str())
    )
    await conn.execute(
        """
        UPDATE household_requests
        SET status = ?, response_note = ?, updated_at = ?
        WHERE id = ? AND household_id = ?
        """,
        (str(status), str(response_note or ""), now, int(req_id), int(household_id)),
    )
    await conn.commit()
    return await get_household_request(conn, req_id)


async def recent_household_nudge_exists(
    conn: aiosqlite.Connection,
    household_id: int,
    from_user_id: int,
    kind: str,
    *,
    within_hours: int = 2,
    now_utc: Optional[datetime.datetime] = None,
) -> bool:
    now = now_utc or datetime.datetime.utcnow()
    if now.tzinfo is not None:
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    cutoff = (now - datetime.timedelta(hours=max(1, int(within_hours)))).isoformat()
    cur = await conn.execute(
        """
        SELECT 1 FROM household_requests
        WHERE household_id = ? AND from_user_id = ? AND kind = ?
          AND datetime(created_at) >= datetime(?)
        LIMIT 1
        """,
        (int(household_id), int(from_user_id), str(kind), cutoff),
    )
    return (await cur.fetchone()) is not None


async def count_household_weekly_activity(
    conn: aiosqlite.Connection,
    household_id: int,
    *,
    since_date: str,
    since_utc_iso: str,
) -> Dict[str, Any]:
    hid = int(household_id)
    task_rows = rows_to_dicts(
        await conn.execute_fetchall(
            """
            SELECT last_done_by_user_id, COALESCE(NULLIF(last_done_by_name, ''), 'Участник') AS actor_name,
                   COUNT(*) AS cnt
            FROM regular_tasks
            WHERE household_id = ? AND last_done_date >= ? AND (is_active IS NULL OR is_active=1)
            GROUP BY last_done_by_user_id, actor_name
            """,
            (hid, since_date),
        )
    )
    done_tasks_total = sum(int(r.get("cnt") or 0) for r in task_rows)
    by_actor = {str(r["actor_name"]): int(r["cnt"]) for r in task_rows if int(r.get("cnt") or 0) > 0}

    cur_req_done = await conn.execute(
        """
        SELECT COUNT(*) FROM household_requests
        WHERE household_id = ? AND kind = 'request' AND status = 'done'
          AND datetime(updated_at) >= datetime(?)
        """,
        (hid, since_utc_iso),
    )
    done_requests = int((await cur_req_done.fetchone())[0])

    cur_req_open = await conn.execute(
        """
        SELECT COUNT(*) FROM household_requests
        WHERE household_id = ? AND kind = 'request' AND status IN ('open', 'accepted', 'snoozed')
        """,
        (hid,),
    )
    open_requests = int((await cur_req_open.fetchone())[0])

    cur_thanks = await conn.execute(
        """
        SELECT COUNT(*) FROM household_requests
        WHERE household_id = ? AND kind = 'nudge_thanks' AND datetime(created_at) >= datetime(?)
        """,
        (hid, since_utc_iso),
    )
    thanks_count = int((await cur_thanks.fetchone())[0])

    cur_nudges = await conn.execute(
        """
        SELECT COUNT(*) FROM household_requests
        WHERE household_id = ? AND kind LIKE 'nudge_%' AND datetime(created_at) >= datetime(?)
        """,
        (hid, since_utc_iso),
    )
    nudges_count = int((await cur_nudges.fetchone())[0])

    cur_exp = await conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) FROM expenses
        WHERE household_id = ? AND scope = 'household' AND datetime(created_at) >= datetime(?)
        """,
        (hid, since_utc_iso),
    )
    expenses_7d = float((await cur_exp.fetchone())[0] or 0.0)

    return {
        "done_tasks_total": done_tasks_total,
        "done_tasks_by_actor": by_actor,
        "done_requests": done_requests,
        "open_requests": open_requests,
        "thanks_count": thanks_count,
        "nudges_count": nudges_count,
        "expenses_7d": round(expenses_7d, 2),
    }


# --- Multi-user MCP Auth Helpers ---

_MCP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _hash_mcp_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_mcp_utc_dt(raw_iso: Any) -> Optional[datetime.datetime]:
    try:
        dt = datetime.datetime.fromisoformat(str(raw_iso or "").strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


async def create_mcp_connect_code(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    ttl_minutes: int = 15,
) -> Dict[str, Any]:
    """Выпустить одноразовый код подключения OAuth для конкретного пользователя HiDL."""
    uid = int(user_id)
    eff_ttl = max(1, min(60, int(ttl_minutes or 15)))
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    expires_dt = now_dt + datetime.timedelta(minutes=eff_ttl)
    part1 = "".join(secrets.choice(_MCP_CODE_ALPHABET) for _ in range(5))
    part2 = "".join(secrets.choice(_MCP_CODE_ALPHABET) for _ in range(5))
    raw_code = f"HIDL-{part1}-{part2}"
    code_hash = _hash_mcp_secret(raw_code)

    await conn.execute(
        "UPDATE mcp_user_connect_codes SET used = 1 WHERE user_id = ? AND used = 0",
        (uid,),
    )
    await conn.execute(
        """
        INSERT INTO mcp_user_connect_codes (code_hash, user_id, expires_at, used, created_at)
        VALUES (?, ?, ?, 0, ?)
        """,
        (code_hash, uid, expires_dt.isoformat(), now_dt.isoformat()),
    )
    await conn.commit()
    return {
        "code": raw_code,
        "expires_at": expires_dt.isoformat(),
        "ttl_minutes": eff_ttl,
    }


async def consume_mcp_connect_code(
    conn: aiosqlite.Connection,
    raw_code: str,
) -> Optional[int]:
    """Проверить и однократно погасить код подключения MCP, вернув user_id владельца."""
    clean = re.sub(r"\s+", "", (raw_code or "").strip()).upper()
    if not clean:
        return None
    if re.fullmatch(r"HIDL[A-Z2-9]{10}", clean):
        clean = f"HIDL-{clean[4:9]}-{clean[9:14]}"

    code_hash = _hash_mcp_secret(clean)
    cur = await conn.execute(
        "SELECT user_id, expires_at, used FROM mcp_user_connect_codes WHERE code_hash = ?",
        (code_hash,),
    )
    row = await cur.fetchone()
    if not row or int(row["used"] or 0) != 0:
        return None
    exp = _parse_mcp_utc_dt(row["expires_at"])
    if not exp or exp <= datetime.datetime.now(datetime.timezone.utc):
        return None

    upd = await conn.execute(
        "UPDATE mcp_user_connect_codes SET used = 1 WHERE code_hash = ? AND used = 0",
        (code_hash,),
    )
    await conn.commit()
    if int(upd.rowcount or 0) != 1:
        return None
    return int(row["user_id"])


async def create_mcp_personal_token(
    conn: aiosqlite.Connection,
    user_id: int,
    *,
    label: str = "Личный токен",
    ttl_days: int = 90,
) -> Dict[str, Any]:
    """Выпустить долгоживущий персональный Bearer-токен MCP для клиента без OAuth."""
    uid = int(user_id)
    eff_days = max(1, min(365, int(ttl_days or 90)))
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    expires_dt = now_dt + datetime.timedelta(days=eff_days)
    raw_token = f"hidl_pat_{secrets.token_urlsafe(32)}"
    token_hash = _hash_mcp_secret(raw_token)
    clean_label = str(label or "Личный токен").strip()[:80]

    await conn.execute(
        """
        INSERT INTO mcp_oauth_tokens
        (token_hash, token_type, client_id, resource, scope, user_id, expires_at, revoked, created_at)
        VALUES (?, 'access', 'personal_bearer', ?, 'hidl:read hidl:write', ?, ?, 0, ?)
        """,
        (token_hash, clean_label, uid, expires_dt.isoformat(), now_dt.isoformat()),
    )
    await conn.commit()
    return {
        "token": raw_token,
        "expires_at": expires_dt.isoformat(),
        "ttl_days": eff_days,
    }


async def list_user_mcp_connections(
    conn: aiosqlite.Connection,
    user_id: int,
) -> List[Dict[str, Any]]:
    """Вернуть активные (не отозванные и не истекшие) MCP-подключения пользователя."""
    uid = int(user_id)
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    cur = await conn.execute(
        """
        SELECT t.token_type, t.client_id, t.resource, t.expires_at, t.created_at, c.client_name
        FROM mcp_oauth_tokens t
        LEFT JOIN mcp_oauth_clients c ON c.client_id = t.client_id
        WHERE t.user_id = ? AND t.revoked = 0
        ORDER BY t.created_at DESC
        """,
        (uid,),
    )
    rows = await cur.fetchall()
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        exp = _parse_mcp_utc_dt(row["expires_at"])
        if not exp or exp <= now_dt:
            continue
        cid = str(row["client_id"] or "")
        display_name = (
            "Личный Bearer-токен"
            if cid == "personal_bearer"
            else (str(row["client_name"] or "").strip() or cid)
        )
        created_at = str(row["created_at"] or "")
        expires_at = str(row["expires_at"] or "")
        if cid not in grouped:
            grouped[cid] = {
                "client_id": cid,
                "client_name": display_name,
                "active_tokens": 1,
                "created_at": created_at,
                "expires_at": expires_at,
            }
        else:
            entry = grouped[cid]
            entry["active_tokens"] += 1
            if created_at > entry["created_at"]:
                entry["created_at"] = created_at
            prev_exp = _parse_mcp_utc_dt(entry["expires_at"])
            if not prev_exp or exp > prev_exp:
                entry["expires_at"] = expires_at
    return list(grouped.values())


async def revoke_user_mcp_tokens(conn: aiosqlite.Connection, user_id: int) -> int:
    """Отозвать все MCP-токены (OAuth и Bearer) и погасить неиспользованные коды пользователя."""
    uid = int(user_id)
    cur = await conn.execute(
        "UPDATE mcp_oauth_tokens SET revoked = 1 WHERE user_id = ? AND revoked = 0",
        (uid,),
    )
    revoked_count = int(cur.rowcount or 0)
    await conn.execute(
        "UPDATE mcp_user_connect_codes SET used = 1 WHERE user_id = ? AND used = 0",
        (uid,),
    )
    await conn.commit()
    return revoked_count

