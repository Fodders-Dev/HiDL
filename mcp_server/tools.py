"""HiDL MCP tools and resources backed by HiDL's SQLite repositories."""

from __future__ import annotations

import datetime
import json
import math
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiosqlite

from config import MCPSettings
from db import repositories as repo
from services import calendar_service, context_builder, fitness_nutrition, karma_service, kitchen_ai, pulse_service
from services.inbox_parser import compute_one_time_last_sent
from utils.rows import row_to_dict, rows_to_dicts
from utils.time import format_time_local, local_date_str, parse_hhmm

_RECIPES_PATH = Path(__file__).resolve().parents[1] / "data" / "knowledge" / "recipes_core.json"
_DIET_TAGS = {
    "omnivore": None,
    "vegetarian": {"vegetarian", "vegan"},
    "vegan": {"vegan"},
}


class ToolError(Exception):
    """Domain or permission error surfaced to the MCP caller cleanly."""


def _now_utc_naive() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _load_recipes() -> list[dict[str, Any]]:
    if not _RECIPES_PATH.exists():
        return []
    try:
        data = json.loads(_RECIPES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _recipe_allowed(recipe: dict[str, Any], profile: str) -> bool:
    allowed = _DIET_TAGS.get(profile)
    if allowed is None:
        return True
    tags = set(recipe.get("tags") or [])
    return bool(tags.intersection(allowed))


def _scale_qty(qty: Any, factor: float, unit: str) -> float:
    val = float(qty or 0.0) * float(factor)
    unit_l = (unit or "").strip().lower()
    if unit_l in {"г", "g", "гр", "мл", "ml"}:
        return float(int(round(val)))
    if unit_l in {"кг", "kg", "л", "l"}:
        return round(val, 2)
    if unit_l in {"шт", "piece", "pieces"}:
        return float(int(math.ceil(val - 1e-9)))
    return round(val, 2)


def _scale_recipe(recipe: dict[str, Any], servings: int) -> dict[str, Any]:
    base = int(recipe.get("base_servings") or 1) or 1
    servings = max(1, int(servings or 2))
    factor = servings / base
    ingredients = []
    for ing in recipe.get("ingredients") or []:
        if not isinstance(ing, dict):
            continue
        unit = str(ing.get("unit") or "")
        ingredients.append(
            {
                "name": str(ing.get("name") or ""),
                "qty": _scale_qty(ing.get("qty", 0), factor, unit),
                "unit": unit,
            }
        )
    return {
        "id": recipe.get("id"),
        "title": recipe.get("title"),
        "desc": recipe.get("desc", ""),
        "time_minutes": int(recipe.get("time_minutes") or 0),
        "servings": servings,
        "tags": recipe.get("tags") or [],
        "ingredients": ingredients,
        "steps": recipe.get("steps") or [],
    }


def _validate_iso_date(raw: Any) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ToolError(f"Ожидалась дата в формате YYYY-MM-DD, получено: {text!r}") from exc


async def _resolve_owner_user(db: aiosqlite.Connection, settings: MCPSettings) -> dict[str, Any]:
    if settings.owner_telegram_id <= 0:
        raise ToolError("MCP_OWNER_TELEGRAM_ID не задан — владелец данных не определён.")
    row = await repo.get_user_by_telegram_id(db, settings.owner_telegram_id)
    if not row:
        raise ToolError(f"Пользователь с telegram_id={settings.owner_telegram_id} не найден в БД.")
    return row_to_dict(row)


def _item_url(settings: MCPSettings, kind: str, item_id: Any) -> str:
    return f"{settings.public_base_url.rstrip('/')}/items/{kind}/{item_id}"


async def _tool_get_today(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    now_utc = _now_utc_naive()
    tz = user.get("timezone") or "UTC"
    local_date = _validate_iso_date(args.get("date")) or local_date_str(now_utc, tz)
    local_hhmm = format_time_local(now_utc, tz)

    await repo.ensure_user_tasks_for_date(db, user["id"], local_date)
    await repo.ensure_regular_tasks(db, user["id"], local_date)

    routines = rows_to_dicts(await repo.list_user_routines(db, user["id"]))
    routine_tasks = rows_to_dicts(await repo.get_tasks_for_day(db, user["id"], local_date))
    status_by_rid = {t.get("routine_id"): t.get("status") for t in routine_tasks}

    plan_items = rows_to_dicts(await repo.list_day_plan_items(db, user["id"], local_date))
    custom_reminders = rows_to_dicts(await repo.list_custom_reminders(db, user["id"]))
    custom_statuses = await repo.custom_statuses_for_date(db, user["id"], local_date)
    reg_due = rows_to_dicts(
        await repo.list_regular_tasks(db, user["id"], due_only=True, local_date=local_date, due_in_days=1)
    )
    wellness = await repo.get_wellness(db, user["id"])
    window_days = int((wellness or {}).get("expiring_window_days", 3))
    soon_pantry, expired_pantry = await repo.pantry_expiring(db, user["id"], local_date, window_days=window_days)
    karma_state = await repo.get_or_create_karma_state(db, user["id"])
    karma_score = int(karma_state.get("score") if karma_state.get("score") is not None else 70)
    karma_mode = karma_service.get_karma_mode(karma_score)
    cal_events_today = await calendar_service.get_unified_events_for_date(db, user["id"], local_date)
    meal_plan_today = await repo.get_meal_plan_for_date(db, user["id"], local_date)

    payload: dict[str, Any] = {
        "local_date": local_date,
        "local_time": local_hhmm,
        "timezone": tz,
        "wake_up_time": user.get("wake_up_time"),
        "sleep_time": user.get("sleep_time"),
        "karma": {
            "score": karma_score,
            "mode": karma_mode["code"],
            "label": karma_mode["label"],
            "debts": {
                "sleep": int(karma_state.get("sleep_debt") or 0),
                "food": int(karma_state.get("food_debt") or 0),
                "tasks": int(karma_state.get("tasks_debt") or 0),
                "hygiene": int(karma_state.get("hygiene_debt") or 0),
            },
            "sick_freeze_active": karma_service.is_sick_freeze_active(user, now_utc),
        },
        "calendar_events": [
            {
                "event_date": e.get("event_date"),
                "start_time": e.get("start_time"),
                "end_time": e.get("end_time"),
                "is_all_day": bool(e.get("is_all_day")),
                "title": e.get("title"),
                "location": e.get("location") or "",
                "source": e.get("source"),
                "is_busy": bool(e.get("is_busy", 1)),
            }
            for e in cal_events_today
        ],
        "meal_plan_today": [
            {
                "meal_slot": m.get("meal_slot"),
                "title": m.get("title"),
                "recipe_id": m.get("recipe_id") or "",
                "time_minutes": int(m.get("time_minutes") or 20),
                "status": m.get("status") or "planned",
            }
            for m in meal_plan_today
        ],
        "routines": [
            {
                "routine_id": r.get("routine_id"),
                "title": r.get("title"),
                "reminder_time": r.get("reminder_time"),
                "status": status_by_rid.get(r.get("routine_id"), "pending"),
            }
            for r in routines
        ],
        "day_plan_tasks": [
            {
                "id": p.get("id"),
                "title": p.get("title"),
                "is_important": bool(p.get("is_important")),
                "done": bool(p.get("done")),
            }
            for p in plan_items
        ],
        "custom_reminders": [
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "reminder_time": c.get("reminder_time"),
                "frequency_days": c.get("frequency_days"),
                "last_sent_date": c.get("last_sent_date"),
                "today_status": custom_statuses.get(c.get("id"), "pending"),
            }
            for c in custom_reminders
        ],
        "home_tasks_due": [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "zone": t.get("zone"),
                "next_due_date": t.get("next_due_date"),
            }
            for t in reg_due[:10]
        ],
        "pantry_alerts": {
            "expiring_soon": [
                {"id": row["id"], "name": row["name"], "expires_at": row["expires_at"]}
                for row in soon_pantry
            ],
            "expired": [
                {"id": row["id"], "name": row["name"], "expires_at": row["expires_at"]}
                for row in expired_pantry
            ],
        },
        "diary_enabled": settings.allow_diary,
    }
    if settings.allow_diary:
        recent_diary = await repo.list_journal_entries(db, user["id"], limit=5)
        payload["recent_diary"] = [
            {
                "id": e["id"],
                "entry_date": e["entry_date"],
                "entry_type": e["entry_type"],
                "source": e["source"],
                "title": e["title"],
                "user_text": e["user_text"],
                "model_note": e.get("model_note") or "",
            }
            for e in recent_diary
        ]
    return payload


async def _tool_list_tasks(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    now_utc = _now_utc_naive()
    tz = user.get("timezone") or "UTC"
    target_date = _validate_iso_date(args.get("date")) or local_date_str(now_utc, tz)
    plan_items = rows_to_dicts(await repo.list_day_plan_items(db, user["id"], target_date))
    reminders = rows_to_dicts(await repo.list_custom_reminders(db, user["id"]))
    return {
        "date": target_date,
        "day_plan_tasks": [
            {
                "id": p.get("id"),
                "title": p.get("title"),
                "category": p.get("category"),
                "is_important": bool(p.get("is_important")),
                "done": bool(p.get("done")),
            }
            for p in plan_items
        ],
        "custom_reminders": [
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "reminder_time": c.get("reminder_time"),
                "frequency_days": c.get("frequency_days"),
                "target_weekday": c.get("target_weekday"),
                "last_sent_date": c.get("last_sent_date"),
            }
            for c in reminders
        ],
    }


async def _tool_list_shopping(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    scope = str(args.get("scope") or "all").strip().lower()
    if scope not in {"household", "personal", "all"}:
        raise ToolError("scope должен быть 'household', 'personal' или 'all'.")
    out: dict[str, Any] = {"scope": scope}
    scopes = ("household", "personal") if scope == "all" else (scope,)
    for sc in scopes:
        rows = rows_to_dicts(await repo.list_shopping_items(db, user["id"], scope=sc))
        out[sc] = [
            {
                "id": r.get("id"),
                "item_name": r.get("item_name"),
                "quantity": r.get("quantity"),
                "unit": r.get("unit"),
                "category": r.get("category"),
                "is_bought": bool(r.get("is_bought")),
            }
            for r in rows
        ]
    return out


async def _tool_get_kitchen(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    pantry = rows_to_dicts(await repo.list_pantry_items(db, user["id"]))
    wellness = await repo.get_wellness(db, user["id"])
    profile = (wellness or {}).get("meal_profile", "omnivore")
    if profile not in _DIET_TAGS:
        profile = "omnivore"

    servings = max(1, min(12, int(args.get("servings") or 2)))
    max_minutes = int(args["max_minutes"]) if args.get("max_minutes") is not None else None
    category = str(args.get("category") or "").strip().lower()

    all_recipes = await kitchen_ai.load_recipes_for_user(db, user["id"])
    recipes_out = []
    for r in all_recipes:
        if not _recipe_allowed(r, profile):
            continue
        t_min = int(r.get("time_minutes") or 0)
        if max_minutes is not None and t_min > max_minutes:
            continue
        tags = set(r.get("tags") or [])
        if category and category != "all" and category not in tags:
            continue
        recipes_out.append(_scale_recipe(r, servings))

    advice = await fitness_nutrition.get_daily_nutrition_advice(
        db, user, now_utc=_now_utc_naive()
    )
    return {
        "meal_profile": profile,
        "daily_nutrition": {
            "local_date": advice["local_date"],
            "targets": advice["targets"],
            "eaten": advice["eaten"],
            "remaining": advice["remaining"],
            "eat_more": advice["eat_more"],
            "go_easy_on": advice["go_easy_on"],
        },
        "pantry_items": [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "amount": p.get("amount"),
                "unit": p.get("unit"),
                "category": p.get("category"),
                "expires_at": p.get("expires_at"),
            }
            for p in pantry
        ],
        "recipes": recipes_out[:10],
    }


async def _tool_get_finance(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    days = max(1, min(90, int(args.get("days") or 7)))
    recent = rows_to_dicts(await repo.expenses_last_days(db, user["id"], days=days))
    month_total = float(await repo.monthly_expense_sum(db, user["id"]) or 0.0)
    budget = await repo.get_budget(db, user["id"])
    by_category: dict[str, float] = {}
    window_total = 0.0
    for e in recent:
        amt = float(e.get("amount") or 0.0)
        cat = str(e.get("category") or "другое")
        by_category[cat] = round(by_category.get(cat, 0.0) + amt, 2)
        window_total += amt
    return {
        "window_days": days,
        "window_total": round(window_total, 2),
        "month_total": round(month_total, 2),
        "monthly_limit": float(budget["monthly_limit"]) if budget and budget["monthly_limit"] else None,
        "by_category": by_category,
        "recent_expenses": [
            {
                "id": e.get("id"),
                "amount": e.get("amount"),
                "category": e.get("category"),
                "created_at": e.get("created_at"),
            }
            for e in recent[:20]
        ],
    }


async def _tool_get_diary(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    if not settings.allow_diary:
        raise ToolError("Доступ к личному дневнику отключён (MCP_ALLOW_DIARY=0).")
    raw_type = str(args.get("entry_type") or "all").strip().lower()
    entry_type = raw_type if raw_type in {"diary", "idea", "done"} else None
    limit = max(1, min(30, int(args.get("limit") or 10)))
    rows = await repo.list_journal_entries(db, user["id"], entry_type=entry_type, limit=limit)
    return {
        "entries": [
            {
                "id": r.get("id"),
                "entry_date": r.get("entry_date"),
                "entry_type": r.get("entry_type"),
                "source": r.get("source"),
                "title": r.get("title"),
                "user_text": r.get("user_text"),
                "model_note": r.get("model_note") or "",
            }
            for r in rows
        ]
    }


async def _tool_search_diary(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    if not settings.allow_diary:
        raise ToolError("Доступ к личному дневнику отключён (MCP_ALLOW_DIARY=0).")
    query = str(args.get("query") or "").strip()
    limit = max(1, min(30, int(args.get("limit") or 10)))
    matches = await repo.search_user_notes(db, user["id"], query, limit=limit)
    return {"query": query, "results": matches}


async def _tool_search(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    now_utc = _now_utc_naive()
    today = local_date_str(now_utc, user.get("timezone") or "UTC")
    results: list[dict[str, Any]] = []

    def _matches(txt: str) -> bool:
        if not query:
            return True
        return repo._haystack_matches_query(txt, query)  # noqa: SLF001

    for t in rows_to_dicts(await repo.list_day_plan_items(db, user["id"], today)):
        title = str(t.get("title") or "")
        if _matches(title):
            results.append(
                {
                    "id": f"task:{t['id']}",
                    "title": f"Задача ({today}): {title}",
                    "url": _item_url(settings, "task", t["id"]),
                    "text": f"Статус: {'выполнена' if t.get('done') else 'открыта'}",
                }
            )

    for r in rows_to_dicts(await repo.list_custom_reminders(db, user["id"])):
        title = str(r.get("title") or "")
        if _matches(title):
            results.append(
                {
                    "id": f"reminder:{r['id']}",
                    "title": f"Напоминание: {title} ({r.get('reminder_time')})",
                    "url": _item_url(settings, "reminder", r["id"]),
                    "text": f"Время: {r.get('reminder_time')}, повтор: {r.get('frequency_days')} дн.",
                }
            )

    for sc in ("household", "personal"):
        for s in rows_to_dicts(await repo.list_shopping_items(db, user["id"], scope=sc)):
            name = str(s.get("item_name") or "")
            if _matches(name):
                results.append(
                    {
                        "id": f"shopping:{s['id']}",
                        "title": f"Покупка ({sc}): {name}",
                        "url": _item_url(settings, "shopping", s["id"]),
                        "text": f"Количество: {s.get('quantity')} {s.get('unit')}, куплено={bool(s.get('is_bought'))}",
                    }
                )

    for p in rows_to_dicts(await repo.list_pantry_items(db, user["id"])):
        name = str(p.get("name") or "")
        if _matches(name):
            results.append(
                {
                    "id": f"pantry:{p['id']}",
                    "title": f"Холодильник: {name}",
                    "url": _item_url(settings, "pantry", p["id"]),
                    "text": f"{p.get('amount')} {p.get('unit')}, категория: {p.get('category')}",
                }
            )

    if settings.allow_diary:
        diary_rows = await repo.search_journal_entries(db, user["id"], query, limit=8)
        for d in diary_rows:
            results.append(
                {
                    "id": f"diary:{d['id']}",
                    "title": f"Дневник ({d.get('entry_date')}): {d.get('title')}",
                    "url": _item_url(settings, "diary", d["id"]),
                    "text": str(d.get("user_text") or ""),
                }
            )

    return {"results": results[:20]}


async def _tool_fetch(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    raw_id = str(args.get("id") or "").strip()
    if ":" not in raw_id:
        raise ToolError("Некорректный id: ожидался формат <type>:<numeric_id>.")
    kind, id_part = raw_id.split(":", 1)
    try:
        item_id = int(id_part)
    except ValueError as exc:
        raise ToolError(f"Некорректный числовой id: {raw_id!r}") from exc

    uid = int(user["id"])
    if kind == "task":
        row = await repo.get_day_plan_item(db, uid, item_id)
        if not row:
            raise ToolError(f"Задача {raw_id} не найдена.")
        d = row_to_dict(row)
        return {
            "id": raw_id,
            "title": d.get("title"),
            "text": f"Задача на план дня: {d.get('title')} (выполнена={bool(d.get('done'))})",
            "url": _item_url(settings, "task", item_id),
            "metadata": d,
        }
    if kind == "reminder":
        row = await repo.get_custom_reminder(db, uid, item_id)
        if not row:
            raise ToolError(f"Напоминание {raw_id} не найдено.")
        d = row_to_dict(row)
        return {
            "id": raw_id,
            "title": d.get("title"),
            "text": f"Напоминание на {d.get('reminder_time')}: {d.get('title')}",
            "url": _item_url(settings, "reminder", item_id),
            "metadata": d,
        }
    if kind == "shopping":
        cur = await db.execute("SELECT * FROM shopping_list WHERE id = ?", (item_id,))
        row = await cur.fetchone()
        if not row:
            raise ToolError(f"Позиция покупок {raw_id} не найдена.")
        d = row_to_dict(row)
        hh_id = await repo.get_or_create_household(db, uid)
        if d.get("user_id") != uid and d.get("household_id") != hh_id:
            raise ToolError(f"Нет доступа к {raw_id}.")
        return {
            "id": raw_id,
            "title": d.get("item_name"),
            "text": f"{d.get('item_name')} — {d.get('quantity')} {d.get('unit')}",
            "url": _item_url(settings, "shopping", item_id),
            "metadata": d,
        }
    if kind == "pantry":
        hh_id = await repo.get_or_create_household(db, uid)
        cur = await db.execute(
            "SELECT * FROM pantry_items WHERE id = ? AND household_id = ?",
            (item_id, hh_id),
        )
        row = await cur.fetchone()
        if not row:
            raise ToolError(f"Продукт {raw_id} не найден.")
        d = row_to_dict(row)
        return {
            "id": raw_id,
            "title": d.get("name"),
            "text": f"{d.get('name')} — {d.get('amount')} {d.get('unit')}",
            "url": _item_url(settings, "pantry", item_id),
            "metadata": d,
        }
    if kind in {"diary", "inbox"}:
        if not settings.allow_diary:
            raise ToolError("Доступ к личному дневнику отключён (MCP_ALLOW_DIARY=0).")
        d = await repo.get_journal_entry(db, uid, item_id)
        if not d:
            raise ToolError(f"Запись {raw_id} не найдена.")
        return {
            "id": raw_id,
            "title": d.get("title"),
            "text": d.get("user_text"),
            "url": _item_url(settings, "diary", item_id),
            "metadata": d,
        }
    raise ToolError(f"Неизвестный тип объекта: {kind!r}")


async def _tool_add_task(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    title = str(args.get("title") or "").strip()
    if not title:
        raise ToolError("Параметр title обязателен.")
    tz = user.get("timezone") or "UTC"
    target_date = _validate_iso_date(args.get("date")) or local_date_str(_now_utc_naive(), tz)
    is_important = bool(args.get("is_important", False))
    task_id = await repo.add_day_plan_item(
        db,
        user["id"],
        target_date,
        title,
        category="mcp_chatgpt",
        is_important=is_important,
    )
    return {"ok": True, "task_id": task_id, "date": target_date, "title": title, "is_important": is_important}


async def _tool_set_task_done(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    if args.get("task_id") is None:
        raise ToolError("Параметр task_id обязателен.")
    try:
        task_id = int(args["task_id"])
    except (TypeError, ValueError) as exc:
        raise ToolError("task_id должен быть целым числом.") from exc
    done = bool(args.get("done", True))
    item = await repo.get_day_plan_item(db, user["id"], task_id)
    if not item:
        raise ToolError(f"Задача #{task_id} не найдена у пользователя.")
    await repo.mark_day_plan_item_done(db, task_id, done=done)
    return {"ok": True, "task_id": task_id, "title": item["title"], "done": done}


async def _tool_add_reminder(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    title = str(args.get("title") or "").strip()
    if not title:
        raise ToolError("Параметр title обязателен.")
    raw_time = str(args.get("reminder_time") or "").strip()
    parsed_time = parse_hhmm(raw_time)
    if not parsed_time:
        raise ToolError("Параметр reminder_time обязателен в формате HH:MM (например 14:30).")
    hhmm = parsed_time.strftime("%H:%M")
    target_date = _validate_iso_date(args.get("date"))
    freq = int(args.get("frequency_days") or 9999)
    if freq < 1:
        freq = 9999

    rem_id = await repo.create_custom_reminder(
        db,
        user_id=user["id"],
        title=title,
        reminder_time=hhmm,
        frequency_days=freq,
    )
    today_str = local_date_str(_now_utc_naive(), user.get("timezone") or "UTC")
    if target_date and freq >= 9999 and target_date > today_str:
        last_sent = compute_one_time_last_sent(target_date, today_str, freq)
        if last_sent:
            await repo.set_custom_reminder_sent(db, rem_id, last_sent)
    return {
        "ok": True,
        "reminder_id": rem_id,
        "title": title,
        "reminder_time": hhmm,
        "target_date": target_date or today_str,
        "frequency_days": freq,
    }


async def _tool_manage_shopping(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    action = str(args.get("action") or "add").strip().lower()
    name = str(args.get("item_name") or "").strip()
    scope = str(args.get("scope") or "household").strip().lower()
    if scope not in {"household", "personal"}:
        scope = "household"
    if not name:
        raise ToolError("Параметр item_name обязателен.")

    if action == "add":
        qty = float(args.get("quantity") or 1.0)
        if qty <= 0:
            qty = 1.0
        unit = str(args.get("unit") or "шт").strip() or "шт"
        item_id = await repo.create_shopping_item(
            db, user["id"], name, qty=qty, unit=unit, category="food", scope=scope
        )
        return {"ok": True, "action": "added", "item_id": item_id, "item_name": name, "quantity": qty, "unit": unit, "scope": scope}

    if action == "remove":
        removed = await repo.find_and_delete_shopping_item_by_name(db, user["id"], name, scope=scope)
        if not removed:
            return {"ok": False, "action": "remove", "found": False, "item_name": name}
        return {"ok": True, "action": "removed", "found": True, "item_name": removed.get("item_name")}

    if action == "mark_bought":
        rows = rows_to_dicts(await repo.list_shopping_items(db, user["id"], scope=scope))
        target = next(
            (r for r in rows if repo._haystack_matches_query(str(r.get("item_name") or ""), name)),  # noqa: SLF001
            None,
        )
        if not target:
            return {"ok": False, "action": "mark_bought", "found": False, "item_name": name}
        await repo.mark_shopping_bought(db, user["id"], int(target["id"]), bought=True, scope=scope)
        return {"ok": True, "action": "marked_bought", "found": True, "item_name": target.get("item_name")}

    raise ToolError("action должен быть 'add', 'remove' или 'mark_bought'.")


async def _tool_add_pantry_item(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    name = str(args.get("name") or "").strip()
    if not name:
        raise ToolError("Параметр name обязателен.")
    amount = float(args.get("amount") or 1.0)
    unit = str(args.get("unit") or "шт").strip() or "шт"
    category = str(args.get("category") or "прочее").strip() or "прочее"
    expires_at = _validate_iso_date(args.get("expires_at"))
    item_id = await repo.create_pantry_item(
        db,
        user["id"],
        name=name,
        amount=amount,
        unit=unit,
        expires_at=expires_at,
        category=category,
    )
    return {
        "ok": True,
        "pantry_item_id": item_id,
        "name": name,
        "amount": amount,
        "unit": unit,
        "category": category,
        "expires_at": expires_at,
    }


async def _tool_add_expense(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    try:
        amount = float(args.get("amount"))
    except (TypeError, ValueError) as exc:
        raise ToolError("Параметр amount должен быть положительным числом.") from exc
    if amount <= 0:
        raise ToolError("Сумма траты должна быть больше 0.")
    category = str(args.get("category") or "другое").strip() or "другое"
    exp_id = await repo.add_expense(db, user["id"], amount, category)
    return {"ok": True, "expense_id": exp_id, "amount": amount, "category": category}


async def _tool_add_diary_entry(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    if not settings.allow_diary:
        raise ToolError("Доступ к личному дневнику отключён (MCP_ALLOW_DIARY=0).")
    entry_type = str(args.get("entry_type") or "diary").strip().lower()
    if entry_type not in {"diary", "idea", "done"}:
        raise ToolError("entry_type должен быть 'diary', 'idea' или 'done'.")
    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolError("Параметр text обязателен.")
    title = str(args.get("title") or text[:60]).strip()
    model_note = str(args.get("model_note") or "").strip()
    raw_tags = args.get("tags")
    tags = [str(t).strip() for t in raw_tags if str(t).strip()][:6] if isinstance(raw_tags, list) else []
    entry_date = local_date_str(_now_utc_naive(), user.get("timezone") or "UTC")

    entry_id = await repo.create_journal_entry(
        db,
        user_id=user["id"],
        entry_type=entry_type,
        entry_date=entry_date,
        source="mcp_chatgpt",
        user_text=text,
        title=title,
        model_note=model_note,
        tags=tags,
    )
    return {
        "ok": True,
        "entry_id": entry_id,
        "entry_type": entry_type,
        "entry_date": entry_date,
        "source": "mcp_chatgpt",
        "title": title,
    }


async def _tool_get_karma(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    now_utc = _now_utc_naive()
    tz = user.get("timezone") or "UTC"
    local_date = _validate_iso_date(args.get("date")) or local_date_str(now_utc, tz)
    uid = int(user["id"])
    state = await repo.get_or_create_karma_state(db, uid)
    score = int(state.get("score") if state.get("score") is not None else 70)
    mode = karma_service.get_karma_mode(score)
    events = await repo.list_karma_events(db, uid, limit=10)
    checkins = await repo.list_pulse_checkins_for_date(db, uid, local_date)
    return {
        "local_date": local_date,
        "score": score,
        "mode": mode["code"],
        "label": mode["label"],
        "sick_freeze_active": karma_service.is_sick_freeze_active(user, now_utc),
        "sick_until_utc": user.get("sick_until_utc"),
        "debts": {
            "sleep": int(state.get("sleep_debt") or 0),
            "food": int(state.get("food_debt") or 0),
            "tasks": int(state.get("tasks_debt") or 0),
            "hygiene": int(state.get("hygiene_debt") or 0),
        },
        "streaks": {
            "sleep": int(state.get("sleep_streak") or 0),
            "food": int(state.get("food_streak") or 0),
            "tasks": int(state.get("tasks_streak") or 0),
            "hygiene": int(state.get("hygiene_streak") or 0),
        },
        "pulse_checkins": [
            {
                "slot_key": c.get("slot_key"),
                "domain": c.get("domain"),
                "status": c.get("status"),
                "snooze_count": int(c.get("snooze_count") or 0),
            }
            for c in checkins
        ],
        "recent_events": [
            {
                "local_date": e.get("local_date"),
                "domain": e.get("domain"),
                "event_type": e.get("event_type"),
                "delta": int(e.get("delta") or 0),
                "score_after": int(e.get("score_after") or 0),
                "reason": e.get("reason") or "",
            }
            for e in events
        ],
    }


async def _tool_get_portrait(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    now_utc = _now_utc_naive()
    uid = int(user["id"])
    memory = await context_builder.refresh_user_portrait_if_needed(db, user, now_utc=now_utc)
    karma_state = await repo.get_or_create_karma_state(db, uid)
    wellness = await repo.get_wellness(db, uid)
    meal_profile = str((dict(wellness) if wellness else {}).get("meal_profile") or "omnivore")
    try:
        patterns = json.loads(memory.get("patterns_json") or "[]")
        if not isinstance(patterns, list):
            patterns = []
    except Exception:
        patterns = []
    score = int(karma_state.get("score") if karma_state.get("score") is not None else 70)
    return {
        "karma_score": score,
        "karma_mode": karma_service.get_karma_mode(score)["code"],
        "meal_profile": meal_profile,
        "schedule": {
            "weekday_wake": user.get("wake_up_time") or "08:00",
            "weekday_sleep": user.get("sleep_time") or "23:00",
            "weekend_wake": user.get("weekend_wake_up_time") or user.get("wake_up_time") or "08:00",
            "weekend_sleep": user.get("weekend_sleep_time") or user.get("sleep_time") or "23:00",
        },
        "patterns": patterns,
        "portrait_text": memory.get("portrait_text") or "",
        "last_refreshed_at": memory.get("last_refreshed_at"),
    }


async def _tool_get_calendar(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    now_utc = _now_utc_naive()
    tz = user.get("timezone") or "UTC"
    start_date = _validate_iso_date(args.get("date")) or local_date_str(now_utc, tz)
    days = max(1, min(14, int(args.get("days") or 3)))
    end_date = (datetime.date.fromisoformat(start_date) + datetime.timedelta(days=days - 1)).isoformat()

    conns = await repo.list_user_calendar_connections(db, int(user["id"]), enabled_only=False)
    events = await calendar_service.get_unified_events_range(db, int(user["id"]), start_date, end_date)
    return {
        "start_date": start_date,
        "end_date": end_date,
        "days": days,
        "connections": [
            {
                "provider": c.get("provider"),
                "label": c.get("label"),
                "enabled": bool(c.get("enabled")),
                "last_sync_at": c.get("last_sync_at"),
                "last_error": c.get("last_error") or "",
            }
            for c in conns
        ],
        "events": [
            {
                "event_date": e.get("event_date"),
                "start_time": e.get("start_time"),
                "end_time": e.get("end_time"),
                "is_all_day": bool(e.get("is_all_day")),
                "title": e.get("title"),
                "location": e.get("location") or "",
                "source": e.get("source"),
                "is_busy": bool(e.get("is_busy", 1)),
            }
            for e in events
        ],
    }


async def _tool_add_calendar_event(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    title = str(args.get("title") or "").strip()
    if not title:
        raise ToolError("Параметр title обязателен.")
    event_date = _validate_iso_date(args.get("date"))
    if not event_date:
        raise ToolError("Параметр date (YYYY-MM-DD) обязателен.")
    st_raw = str(args.get("start_time") or "").strip()
    en_raw = str(args.get("end_time") or "").strip()
    st_t = parse_hhmm(st_raw)
    en_t = parse_hhmm(en_raw)
    if not st_t or not en_t:
        raise ToolError("Параметры start_time и end_time обязательны в формате HH:MM.")
    if en_t <= st_t:
        raise ToolError("end_time должен быть позже start_time.")

    res = await calendar_service.create_unified_event(
        db,
        user,
        title=title,
        event_date=event_date,
        start_time=st_t.strftime("%H:%M"),
        end_time=en_t.strftime("%H:%M"),
        location=str(args.get("location") or "").strip(),
        description=str(args.get("description") or "").strip(),
        category="mcp_chatgpt",
        push_to_google=True,
    )
    return res


async def _tool_get_meal_plan(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    now_utc = _now_utc_naive()
    tz = user.get("timezone") or "UTC"
    start_date = _validate_iso_date(args.get("date")) or local_date_str(now_utc, tz)
    days = max(1, min(7, int(args.get("days") or 3)))
    return await kitchen_ai.get_meal_plan_view(
        db, user, days=days, start_date=start_date, auto_generate=False, now_utc=now_utc
    )


async def _tool_set_meal_plan(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    now_utc = _now_utc_naive()
    tz = user.get("timezone") or "UTC"
    mode = str(args.get("mode") or "generate").strip().lower()
    add_missing = bool(args.get("add_missing_to_shopping", False))

    if mode == "generate":
        days = 4 if int(args.get("days") or 3) == 4 else 3
        start_date = _validate_iso_date(args.get("start_date") or args.get("date")) or local_date_str(now_utc, tz)
        plan_data = await kitchen_ai.generate_meal_plan(
            db, user, days=days, start_date=start_date, now_utc=now_utc
        )
        out: dict[str, Any] = {"ok": True, "mode": "generate", **plan_data}
        if add_missing and plan_data.get("missing_shopping"):
            shop_res = await kitchen_ai.add_missing_ingredients_to_shopping(
                db, int(user["id"]), plan_data["missing_shopping"], scope="household"
            )
            out["added_to_shopping"] = shop_res["added"]
        return out

    if mode == "set_slot":
        target_date = _validate_iso_date(args.get("date") or args.get("start_date")) or local_date_str(now_utc, tz)
        meal_slot = str(args.get("meal_slot") or "").strip().lower()
        if meal_slot not in {"breakfast", "lunch", "dinner"}:
            raise ToolError("meal_slot должен быть 'breakfast', 'lunch' или 'dinner'.")
        title = str(args.get("title") or "").strip()
        recipe_id = str(args.get("recipe_id") or "").strip()
        rec = await kitchen_ai.get_recipe_for_user(db, int(user["id"]), recipe_id) if recipe_id else None
        if not title and rec:
            title = str(rec.get("title") or "")
        if not title:
            raise ToolError("Параметр title (или валидный recipe_id) обязателен.")
        t_min = int(args.get("time_minutes") or (rec.get("time_minutes") if rec else 20) or 20)
        raw_ings = args.get("ingredients")
        if isinstance(raw_ings, list):
            ingredients = [i for i in raw_ings if isinstance(i, dict) and i.get("name")]
        elif rec:
            ingredients = list(rec.get("ingredients") or [])
        else:
            ingredients = []
        row = await repo.upsert_meal_plan_slot(
            db,
            int(user["id"]),
            target_date,
            meal_slot,
            title=title,
            recipe_id=recipe_id,
            time_minutes=t_min,
            ingredients=ingredients,
            status="planned",
        )
        missing = await kitchen_ai.compute_meal_plan_missing_shopping(db, user, [row])
        out = {"ok": True, "mode": "set_slot", "slot": row, "missing_shopping": missing}
        if add_missing and missing:
            shop_res = await kitchen_ai.add_missing_ingredients_to_shopping(
                db, int(user["id"]), missing, scope="household"
            )
            out["added_to_shopping"] = shop_res["added"]
        return out

    raise ToolError("mode должен быть 'generate' или 'set_slot'.")


async def _tool_import_cart(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    raw_items = args.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ToolError("Параметр items (непустой список продуктов) обязателен.")
    cleaned_items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        copy = dict(item)
        if copy.get("expires_at"):
            copy["expires_at"] = _validate_iso_date(copy["expires_at"])
        cleaned_items.append(copy)
    if not cleaned_items:
        raise ToolError("Не найдено ни одного валидного продукта с полем name.")

    now_utc = _now_utc_naive()
    total_amount = args.get("total_amount")
    expense_category = str(args.get("expense_category") or "продукты").strip() or "продукты"
    sync_shopping = bool(args.get("sync_shopping", True))
    store = str(args.get("store") or "").strip()

    res = await kitchen_ai.import_cart_items(
        db,
        user,
        cleaned_items,
        total_amount=total_amount,
        expense_category=expense_category,
        sync_shopping=sync_shopping,
        now_utc=now_utc,
    )
    sug = await kitchen_ai.suggest_meals_now(db, user, limit=3, now_utc=now_utc)
    quick_suggestions = [
        {
            "id": str(s["recipe"].get("id") or ""),
            "title": str(s["recipe"].get("title") or ""),
            "time_minutes": int(s["recipe"].get("time_minutes") or 0),
            "coverage": round(float(s.get("coverage") or 0.0), 2),
            "missing_count": len(s.get("missing") or []),
        }
        for s in (sug.get("suggestions") or [])
    ]
    return {
        "ok": True,
        "store": store,
        **res,
        "quick_suggestions": quick_suggestions,
    }


async def _tool_add_recipe(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    title = str(args.get("title") or "").strip()
    if not title:
        raise ToolError("Параметр title обязателен.")
    raw_ings = args.get("ingredients")
    if not isinstance(raw_ings, list) or not raw_ings:
        raise ToolError("Параметр ingredients (непустой список) обязателен.")
    norm_ings = [
        {
            "name": str(i.get("name") or "").strip(),
            "qty": float(i.get("qty") if i.get("qty") is not None else (i.get("amount") or 1.0)),
            "unit": str(i.get("unit") or "шт").strip() or "шт",
        }
        for i in raw_ings
        if isinstance(i, dict) and str(i.get("name") or "").strip()
    ]
    if not norm_ings:
        raise ToolError("Каждый ингредиент должен содержать непустое поле name.")
    raw_steps = args.get("steps")
    if not isinstance(raw_steps, list) or not [str(s).strip() for s in raw_steps if str(s).strip()]:
        raise ToolError("Параметр steps (непустой список шагов) обязателен.")
    norm_steps = [str(s).strip() for s in raw_steps if str(s).strip()]

    tags = [str(t).strip() for t in (args.get("tags") or []) if str(t).strip()]
    saved = await repo.upsert_user_recipe(
        db,
        int(user["id"]),
        recipe_key=str(args.get("recipe_id") or "").strip() or None,
        title=title,
        desc=str(args.get("desc") or "").strip(),
        tags=tags,
        base_servings=max(1, int(args.get("base_servings") or 2)),
        time_minutes=max(1, int(args.get("time_minutes") or 20)),
        ingredients=norm_ings,
        steps=norm_steps,
        source="mcp",
        source_label=str(args.get("source_label") or "").strip(),
    )
    return {
        "ok": True,
        "recipe_id": saved.get("id"),
        "title": saved.get("title"),
        "recipe": saved,
    }


async def _tool_get_fitness_nutrition(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    _ = (settings, args)
    now_utc = _now_utc_naive()
    user_id = int(user["id"])
    tz = str(user.get("timezone") or "UTC")
    local_date = local_date_str(now_utc, tz)
    advice = await fitness_nutrition.get_daily_nutrition_advice(db, user, now_utc=now_utc)
    trend = await repo.body_metrics_trend(db, user_id, days=30)
    workouts_week = await repo.workout_stats_week(db, user_id, local_date)
    equipment = repo.get_user_equipment(user)
    coach_mode = str(user.get("coach_mode") or "balanced").strip().lower()
    coach_emoji, coach_label, _ = fitness_nutrition.COACH_MODES.get(
        coach_mode, fitness_nutrition.COACH_MODES["balanced"]
    )
    remembered_products_count = await repo.count_user_food_products(db, user_id)
    return {
        "local_date": advice["local_date"],
        "body_profile": {
            "height_cm": user.get("height_cm"),
            "age_years": user.get("age_years"),
            "sex": user.get("sex") or user.get("gender") or "",
            "activity_level": user.get("activity_level") or "light",
            "weight_goal": user.get("weight_goal") or "loss",
            "goal_label": advice["targets"]["goal_label"],
            "weight_target": user.get("weight_target"),
            "coach_mode": coach_mode,
            "coach_label": f"{coach_emoji} {coach_label}",
            "nutrition_checkin_enabled": bool(int(user.get("nutrition_checkin_enabled", 1))),
        },
        "remembered_products_count": remembered_products_count,
        "equipment": equipment,
        "latest_scale": trend.get("latest"),
        "body_trend_30d": {
            "count": trend.get("count"),
            "weight_delta": trend.get("weight_delta"),
            "fat_delta": trend.get("fat_delta"),
            "muscle_delta": trend.get("muscle_delta"),
            "water_delta": trend.get("water_delta"),
            "feedback": fitness_nutrition.format_body_trend_feedback(
                trend, str(user.get("weight_goal") or "loss")
            ),
        },
        "daily_nutrition": {
            "targets": advice["targets"],
            "eaten": advice["eaten"],
            "remaining": advice["remaining"],
            "over": advice["over"],
            "eat_more": advice["eat_more"],
            "go_easy_on": advice["go_easy_on"],
            "meals": advice["meals"],
        },
        "workouts_week": workouts_week,
    }


async def _tool_log_meal(
    db: aiosqlite.Connection,
    user: dict[str, Any],
    settings: MCPSettings,
    args: dict[str, Any],
) -> dict[str, Any]:
    _ = settings
    now_utc = _now_utc_naive()
    tz = str(user.get("timezone") or "UTC")
    target_date = _validate_iso_date(args.get("date")) or local_date_str(now_utc, tz)
    raw_text = str(args.get("text") or "").strip()
    title = str(args.get("title") or "").strip()
    slot_raw = str(args.get("meal_slot") or "").strip().lower()
    servings = max(0.1, float(args.get("servings") or 1.0))

    kcal_raw = args.get("kcal")
    if kcal_raw is not None:
        try:
            kcal_val = float(kcal_raw)
        except (TypeError, ValueError):
            kcal_val = 0.0
        if kcal_val <= 0:
            raise ToolError("Параметр kcal должен быть положительным числом.")
        if not title:
            title = raw_text or "Приём пищи"
        protein_g = float(args.get("protein_g") or 0.0)
        fat_g = float(args.get("fat_g") or 0.0)
        carbs_g = float(args.get("carbs_g") or 0.0)
        meal_slot = slot_raw if slot_raw in {"breakfast", "lunch", "dinner", "snack"} else "snack"
    else:
        query_text = raw_text or title
        if not query_text:
            raise ToolError("Передай title или text описания еды (либо явные kcal/protein_g/fat_g/carbs_g).")
        parsed = await fitness_nutrition.parse_meal_input(raw_text=query_text)
        if not parsed.get("ok"):
            raise ToolError("Не удалось оценить КБЖУ по описанию — передай явно kcal, protein_g, fat_g, carbs_g.")
        title = title or str(parsed["title"])
        kcal_val = float(parsed["kcal"])
        protein_g = float(parsed["protein_g"])
        fat_g = float(parsed["fat_g"])
        carbs_g = float(parsed["carbs_g"])
        meal_slot = slot_raw if slot_raw in {"breakfast", "lunch", "dinner", "snack"} else str(parsed.get("meal_slot") or "snack")

    log_id = await repo.add_meal_log(
        db,
        int(user["id"]),
        target_date,
        title,
        meal_slot=meal_slot,
        servings=servings,
        kcal=round(kcal_val * servings, 1),
        protein_g=round(protein_g * servings, 1),
        fat_g=round(fat_g * servings, 1),
        carbs_g=round(carbs_g * servings, 1),
        source="mcp",
    )
    advice = await fitness_nutrition.get_daily_nutrition_advice(db, user, now_utc=now_utc)
    return {
        "ok": True,
        "log_id": log_id,
        "local_date": target_date,
        "logged": {
            "title": title,
            "meal_slot": meal_slot,
            "servings": servings,
            "kcal": round(kcal_val * servings, 1),
            "protein_g": round(protein_g * servings, 1),
            "fat_g": round(fat_g * servings, 1),
            "carbs_g": round(carbs_g * servings, 1),
        },
        "daily_nutrition": {
            "targets": advice["targets"],
            "eaten": advice["eaten"],
            "remaining": advice["remaining"],
            "eat_more": advice["eat_more"],
            "go_easy_on": advice["go_easy_on"],
        },
    }


ToolHandler = Callable[[aiosqlite.Connection, dict[str, Any], MCPSettings, dict[str, Any]], Awaitable[dict[str, Any]]]

_TOOL_REGISTRY: list[dict[str, Any]] = [
    {
        "name": "hidl_get_today",
        "description": "Получить сводку HiDL на день: рутины, задачи плана дня, напоминания, дела по дому и продукты с истекающим сроком.",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "Дата YYYY-MM-DD (по умолчанию сегодня локально)."}},
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": _tool_get_today,
        "requires_write": False,
        "requires_diary": False,
    },
    {
        "name": "hidl_list_tasks",
        "description": "Показать список задач плана дня на выбранную дату и активные напоминания пользователя.",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "Дата YYYY-MM-DD (по умолчанию сегодня)."}},
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": _tool_list_tasks,
        "requires_write": False,
        "requires_diary": False,
    },
    {
        "name": "hidl_list_shopping",
        "description": "Получить текущий список покупок HiDL (общий и/или личный).",
        "inputSchema": {
            "type": "object",
            "properties": {"scope": {"type": "string", "enum": ["household", "personal", "all"]}},
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": _tool_list_shopping,
        "requires_write": False,
        "requires_diary": False,
    },
    {
        "name": "hidl_get_kitchen",
        "description": "Показать продукты в холодильнике, профиль питания (например веган) и подходящие рецепты на нужное число порций.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "servings": {"type": "integer", "minimum": 1, "maximum": 12},
                "max_minutes": {"type": "integer", "minimum": 1},
                "category": {"type": "string"},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": _tool_get_kitchen,
        "requires_write": False,
        "requires_diary": False,
    },
    {
        "name": "hidl_get_finance",
        "description": "Получить сводку расходов из HiDL за последние дни и за месяц.",
        "inputSchema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 90}},
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": _tool_get_finance,
        "requires_write": False,
        "requires_diary": False,
    },
    {
        "name": "hidl_get_diary",
        "description": "Прочитать последние записи дневника, идей или выполненных дел (разделяя текст пользователя и пометки модели).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_type": {"type": "string", "enum": ["diary", "idea", "done", "all"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": _tool_get_diary,
        "requires_write": False,
        "requires_diary": True,
    },
    {
        "name": "hidl_search_diary",
        "description": "Поиск по заметкам дневника и входящим сообщениям с учётом русских словоформ.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": _tool_search_diary,
        "requires_write": False,
        "requires_diary": True,
    },
    {
        "name": "search",
        "description": "Поиск по данным HiDL (задачи, напоминания, покупки, холодильник и дневник при разрешённом доступе).",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": _tool_search,
        "requires_write": False,
        "requires_diary": False,
    },
    {
        "name": "fetch",
        "description": "Получить полные детали объекта HiDL по идентификатору из инструмента search.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": _tool_fetch,
        "requires_write": False,
        "requires_diary": False,
    },
    {
        "name": "hidl_add_task",
        "description": "Добавить задачу в план дня HiDL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD (по умолчанию сегодня)."},
                "is_important": {"type": "boolean"},
            },
            "required": ["title"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "handler": _tool_add_task,
        "requires_write": True,
        "requires_diary": False,
    },
    {
        "name": "hidl_set_task_done",
        "description": "Отметить задачу плана дня выполненной или вернуть в открытые.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "done": {"type": "boolean"},
            },
            "required": ["task_id"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "handler": _tool_set_task_done,
        "requires_write": True,
        "requires_diary": False,
    },
    {
        "name": "hidl_add_reminder",
        "description": "Создать напоминание в HiDL на указанное время (HH:MM) и дату (одноразовое или повторяющееся).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "reminder_time": {"type": "string", "description": "Время HH:MM."},
                "date": {"type": "string", "description": "Дата YYYY-MM-DD для одноразового напоминания."},
                "frequency_days": {"type": "integer", "minimum": 1},
            },
            "required": ["title", "reminder_time"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "handler": _tool_add_reminder,
        "requires_write": True,
        "requires_diary": False,
    },
    {
        "name": "hidl_manage_shopping",
        "description": "Добавить товар в список покупок HiDL, отметить купленным или убрать из списка.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "remove", "mark_bought"]},
                "item_name": {"type": "string"},
                "quantity": {"type": "number"},
                "unit": {"type": "string"},
                "scope": {"type": "string", "enum": ["household", "personal"]},
            },
            "required": ["action", "item_name"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "handler": _tool_manage_shopping,
        "requires_write": True,
        "requires_diary": False,
    },
    {
        "name": "hidl_add_pantry_item",
        "description": "Добавить продукт в холодильник/кладовку HiDL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "amount": {"type": "number"},
                "unit": {"type": "string"},
                "category": {"type": "string"},
                "expires_at": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["name"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "handler": _tool_add_pantry_item,
        "requires_write": True,
        "requires_diary": False,
    },
    {
        "name": "hidl_add_expense",
        "description": "Записать денежную трату в HiDL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "category": {"type": "string"},
            },
            "required": ["amount"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "handler": _tool_add_expense,
        "requires_write": True,
        "requires_diary": False,
    },
    {
        "name": "hidl_add_diary_entry",
        "description": "Сохранить мысль, идею или выполненное дело в дневник HiDL (с пометкой источника mcp_chatgpt).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_type": {"type": "string", "enum": ["diary", "idea", "done"]},
                "title": {"type": "string"},
                "text": {"type": "string"},
                "model_note": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "handler": _tool_add_diary_entry,
        "requires_write": True,
        "requires_diary": True,
    },
    {
        "name": "hidl_get_karma",
        "description": "Получить карму HiDL (0–100), режим строгости мамы, долги, серии, ЧП-заморозку и статус Пульса дня.",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "Дата YYYY-MM-DD (по умолчанию сегодня)."}},
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": _tool_get_karma,
        "requires_write": False,
        "requires_diary": False,
    },
    {
        "name": "hidl_get_portrait",
        "description": "Получить обновлённый портрет привычек пользователя (паттерны сна, еды, переносов и расписание подъёма/отбоя).",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": _tool_get_portrait,
        "requires_write": False,
        "requires_diary": False,
    },
    {
        "name": "hidl_get_calendar",
        "description": "Получить расписание и события Google Calendar + локального расписания за выбранный период (без секретных ссылок).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Начальная дата YYYY-MM-DD (по умолчанию сегодня)."},
                "days": {"type": "integer", "minimum": 1, "maximum": 14, "description": "Число дней (по умолчанию 3)."},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": _tool_get_calendar,
        "requires_write": False,
        "requires_diary": False,
    },
    {
        "name": "hidl_add_calendar_event",
        "description": "Создать событие в расписании HiDL (и отправить в Google Calendar, если подключён OAuth).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "date": {"type": "string", "description": "Дата YYYY-MM-DD."},
                "start_time": {"type": "string", "description": "Время начала HH:MM."},
                "end_time": {"type": "string", "description": "Время окончания HH:MM."},
                "location": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["title", "date", "start_time", "end_time"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "handler": _tool_add_calendar_event,
        "requires_write": True,
        "requires_diary": False,
    },
    {
        "name": "hidl_get_meal_plan",
        "description": "Получить текущее меню питания на несколько дней и недостающие продукты за вычетом холодильника.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Начальная дата YYYY-MM-DD (по умолчанию сегодня)."},
                "days": {"type": "integer", "minimum": 1, "maximum": 7, "description": "Дней (по умолчанию 3)."},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "handler": _tool_get_meal_plan,
        "requires_write": False,
        "requires_diary": False,
    },
    {
        "name": "hidl_set_meal_plan",
        "description": "Сгенерировать меню на 3–4 дня под диету/холодильник или задать конкретное блюдо в слот, с опцией добавления покупок.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["generate", "set_slot"]},
                "days": {"type": "integer", "minimum": 3, "maximum": 4},
                "date": {"type": "string", "description": "YYYY-MM-DD."},
                "meal_slot": {"type": "string", "enum": ["breakfast", "lunch", "dinner"]},
                "title": {"type": "string"},
                "recipe_id": {"type": "string"},
                "time_minutes": {"type": "integer"},
                "ingredients": {"type": "array", "items": {"type": "object"}},
                "add_missing_to_shopping": {"type": "boolean"},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "handler": _tool_set_meal_plan,
        "requires_write": True,
        "requires_diary": False,
    },
    {
        "name": "hidl_import_cart",
        "description": "Пакетно перенести корзину продуктов из магазина (например Перекрёсток) в холодильник HiDL, вычеркнуть из покупок и опционально записать сумму траты.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Список продуктов корзины.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "amount": {"type": "number"},
                            "unit": {"type": "string"},
                            "category": {"type": "string"},
                            "expires_at": {"type": "string", "description": "YYYY-MM-DD"},
                        },
                        "required": ["name"],
                    },
                },
                "total_amount": {"type": "number", "description": "Итоговая сумма корзины (если > 0, запишется в траты)."},
                "expense_category": {"type": "string", "description": "Категория траты (по умолчанию 'продукты')."},
                "sync_shopping": {"type": "boolean", "description": "Вычеркнуть купленное из списка покупок (по умолчанию true)."},
                "store": {"type": "string", "description": "Магазин (например 'Перекрёсток')."},
            },
            "required": ["items"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "handler": _tool_import_cart,
        "requires_write": True,
        "requires_diary": False,
    },
    {
        "name": "hidl_add_recipe",
        "description": "Сохранить рецепт (от ассистента или из личной книги) в личную книгу рецептов пользователя HiDL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipe_id": {"type": "string", "description": "Опциональный стабильный ключ рецепта."},
                "title": {"type": "string"},
                "desc": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "base_servings": {"type": "integer", "minimum": 1, "maximum": 12},
                "time_minutes": {"type": "integer", "minimum": 1, "maximum": 240},
                "ingredients": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "qty": {"type": "number"},
                            "unit": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
                "steps": {"type": "array", "items": {"type": "string"}},
                "source_label": {"type": "string"},
            },
            "required": ["title", "ingredients", "steps"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "handler": _tool_add_recipe,
        "requires_write": True,
        "requires_diary": False,
    },
    {
        "name": "hidl_get_fitness_nutrition",
        "description": "Получить профиль тела, замеры умных весов и тренд за 30 дней, домашний инвентарь, суточный КБЖУ (цель/съедено/осталось/советы) и статистику тренировок за неделю.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        "handler": _tool_get_fitness_nutrition,
        "requires_write": False,
        "requires_diary": False,
    },
    {
        "name": "hidl_log_meal",
        "description": "Записать приём пищи в личный дневник КБЖУ HiDL (по явным цифрам КБЖУ или текстовому описанию) и вернуть обновлённый остаток на день.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название блюда."},
                "text": {"type": "string", "description": "Текстовое описание еды для оценки КБЖУ, если цифры не переданы."},
                "meal_slot": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack"]},
                "date": {"type": "string", "description": "YYYY-MM-DD (по умолчанию сегодня)."},
                "servings": {"type": "number", "minimum": 0.1, "maximum": 10},
                "kcal": {"type": "number", "minimum": 1},
                "protein_g": {"type": "number", "minimum": 0},
                "fat_g": {"type": "number", "minimum": 0},
                "carbs_g": {"type": "number", "minimum": 0},
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        "handler": _tool_log_meal,
        "requires_write": True,
        "requires_diary": False,
    },
]


def list_enabled_tools(settings: MCPSettings) -> list[dict[str, Any]]:
    """Return MCP tool definitions filtered by readonly and allow_diary flags."""
    tools: list[dict[str, Any]] = []
    for entry in _TOOL_REGISTRY:
        if entry["requires_write"] and settings.readonly:
            continue
        if entry["requires_diary"] and not settings.allow_diary:
            continue
        tools.append(
            {
                "name": entry["name"],
                "description": entry["description"],
                "inputSchema": entry["inputSchema"],
                "annotations": entry["annotations"],
            }
        )
    return tools


async def call_mcp_tool(
    db: aiosqlite.Connection,
    settings: MCPSettings,
    name: str,
    arguments: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Execute one MCP tool call and format the standard MCP CallToolResult."""
    entry = next((t for t in _TOOL_REGISTRY if t["name"] == name), None)
    if entry is None:
        return {"content": [{"type": "text", "text": f"Неизвестный инструмент: {name}"}], "isError": True}
    if entry["requires_write"] and settings.readonly:
        return {"content": [{"type": "text", "text": "Сервер в режиме только чтения (MCP_READONLY=1)."}], "isError": True}
    if entry["requires_diary"] and not settings.allow_diary:
        return {"content": [{"type": "text", "text": "Доступ к личному дневнику отключён (MCP_ALLOW_DIARY=0)."}], "isError": True}

    args = arguments if isinstance(arguments, dict) else {}
    try:
        user = await _resolve_owner_user(db, settings)
        handler: ToolHandler = entry["handler"]
        result_obj = await handler(db, user, settings, args)
        return {
            "content": [{"type": "text", "text": json.dumps(result_obj, ensure_ascii=False, indent=2)}],
            "isError": False,
        }
    except ToolError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except Exception as exc:  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"Ошибка выполнения: {exc.__class__.__name__}"}], "isError": True}


def list_enabled_resources(settings: MCPSettings) -> list[dict[str, Any]]:
    """Return MCP resource descriptors honoring allow_diary."""
    resources = [
        {
            "uri": "hidl://today",
            "name": "План дня HiDL",
            "description": "Рутины, задачи плана дня, напоминания и домашние дела на сегодня.",
            "mimeType": "application/json",
        },
        {
            "uri": "hidl://shopping",
            "name": "Список покупок HiDL",
            "description": "Текущий общий и личный списки покупок.",
            "mimeType": "application/json",
        },
        {
            "uri": "hidl://pantry",
            "name": "Холодильник и рецепты HiDL",
            "description": "Запасы продуктов, профиль питания и подходящие рецепты.",
            "mimeType": "application/json",
        },
        {
            "uri": "hidl://karma",
            "name": "Карма и Пульс дня HiDL",
            "description": "Балл кармы, долги по доменам, статус чек-инов Пульса и история событий.",
            "mimeType": "application/json",
        },
        {
            "uri": "hidl://calendar",
            "name": "Календарь HiDL на 3 дня",
            "description": "Ближайшие события из Google Calendar и локального расписания.",
            "mimeType": "application/json",
        },
    ]
    if settings.allow_diary:
        resources.append(
            {
                "uri": "hidl://diary/recent",
                "name": "Последние записи дневника HiDL",
                "description": "Последние мысли, идеи и выполненные дела пользователя.",
                "mimeType": "application/json",
            }
        )
    return resources


async def read_mcp_resource(
    db: aiosqlite.Connection,
    settings: MCPSettings,
    uri: str,
) -> dict[str, Any]:
    """Read a resource by URI."""
    user = await _resolve_owner_user(db, settings)
    if uri == "hidl://today":
        data = await _tool_get_today(db, user, settings, {})
    elif uri == "hidl://shopping":
        data = await _tool_list_shopping(db, user, settings, {"scope": "all"})
    elif uri == "hidl://pantry":
        data = await _tool_get_kitchen(db, user, settings, {"servings": 2})
    elif uri == "hidl://karma":
        data = await _tool_get_karma(db, user, settings, {})
    elif uri == "hidl://calendar":
        data = await _tool_get_calendar(db, user, settings, {"days": 3})
    elif uri == "hidl://diary/recent":
        if not settings.allow_diary:
            raise ToolError("Доступ к личному дневнику отключён (MCP_ALLOW_DIARY=0).")
        data = await _tool_get_diary(db, user, settings, {"entry_type": "all", "limit": 10})
    else:
        raise ToolError(f"Неизвестный ресурс: {uri}")
    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(data, ensure_ascii=False, indent=2),
            }
        ]
    }
