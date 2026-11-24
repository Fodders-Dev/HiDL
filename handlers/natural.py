import datetime
import re

from aiogram import Router, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext

from db import repositories as repo
from handlers.inbox import handle_free_text_inbox
from keyboards.common import main_menu_keyboard
from utils.menu_match import is_single_clause_reminder, looks_like_expense
from utils.time import local_date_str
from utils.user import ensure_user
from utils.nlp import match_simple_intent
from utils.profile import is_student_mode

router = Router()


def _extract_amount(text: str):
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except Exception:
        return None


def _extract_time(text: str):
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    return f"{hh:02d}:{mm:02d}"


@router.message(StateFilter(None), lambda m: m.text and not m.text.startswith("/"))
async def natural_handler(message: types.Message, db, state: FSMContext) -> None:
    if await state.get_state():
        return
    text_original = message.text or ""
    text = text_original.lower()
    profile = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    student = is_student_mode(profile)
    # Простые односоставные напоминания уже обработаны в handlers/custom_reminders.py.
    if is_single_clause_reminder(text_original):
        return
    # Если пользователь только что получил уточнение по еде — ответы вида "ок", "ладно", "200 г" ведём в inbox
    if not student and await repo.get_active_pending_meal(db, int(profile["id"])) is not None:
        await handle_free_text_inbox(message, db)
        return
    # быстрые намерения: сделал/позже/пропусти
    intent = match_simple_intent(text)
    if intent:
        user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
        today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
        # пытаемся закрыть рутину
        await repo.ensure_user_tasks_for_date(db, user["id"], today)
        tasks = await repo.get_tasks_for_day(db, user["id"], today)
        target_task = next((t for t in tasks if t["status"] not in {"done", "skip"}), None)
        if target_task:
            status_map = {"done": "done", "skip": "skip", "later": "later"}
            await repo.upsert_user_task(db, user["id"], target_task["routine_id"], today, status=status_map[intent])
            if intent == "done":
                await repo.add_points(db, user["id"], 5, local_date=today)
            await message.answer(f"Отметила рутину как {intent}.", reply_markup=main_menu_keyboard(profile))
            return
        # если нет рутины — пытаемся с напоминаниями
        custom = await repo.list_custom_reminders(db, user["id"])
        status_map = {"done": "done", "skip": "skip", "later": "later"}
        for c in custom:
            await repo.log_custom_task(
                db,
                reminder_id=c["id"],
                user_id=user["id"],
                reminder_date=today,
                status=status_map[intent],
            )
            if intent == "done":
                await repo.add_points(db, user["id"], 3, local_date=today)
            await message.answer(f"Отметила: {c['title']} — {intent}.", reply_markup=main_menu_keyboard(profile))
            return
        # если вообще нечего отмечать — падаем дальше по логике

    # Проверим, не это ли чистое время (HH:MM) - тогда игнорируем (FSM обработает)
    time_only = re.match(r"^\s*\d{1,2}:\d{2}\s*$", text_original)
    if time_only:
        # Это время для какого-то FSM диалога (например, регистрация, настройки)
        # Если FSM не активен, просто сообщим пользователю
        await message.answer(
            "Это похоже на время. Если ты хотела изменить время подъёма/отбоя, зайди в Настройки (⚙️).",
            reply_markup=main_menu_keyboard(profile),
        )
        return

    # Кладовка: короткий бытовой ввод вида "купила яблоки и бананы"
    pantry_patterns = [r"купил[аи]?", r"взял[аи]?", r"принес(?:ла)?", r"добавь в кладовку", r"закупил[аи]?"]
    _food_log_exclude = (
        "напом", "устал", "придумал", "иде", "надо", "нужно", "?",
        "съел", "выпил", "поел", "хочу", "думаю", "планирую",
        "в кафе", "в кофейн", "в теремк", "кофе", "латте", "капучино",
        "шаурм", "шаверм", "пицц", "бургер", "ккал", "на 100", "блин", "сырник",
    )
    is_single_sentence = (
        "\n" not in text_original
        and len([p for p in re.split(r"[.!?;]+", text_original) if p.strip()]) <= 1
        and not any(k in text for k in _food_log_exclude)
    )
    pantry_match = not student and is_single_sentence and any(re.search(p, text) for p in pantry_patterns)
    if pantry_match:
        # Извлекаем продукты из текста
        cleaned = text
        for pattern in pantry_patterns:
            cleaned = re.sub(pattern, "", cleaned)
        # Разделяем по " и ", ","
        items = re.split(r",|\s+и\s+", cleaned)
        items = [item.strip() for item in items if item.strip() and len(item.strip()) > 1]
        
        if items:
            user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
            added = []
            synced_removed = 0
            for item_name in items:
                clean_name = item_name.capitalize()
                await repo.create_pantry_item(db, user["id"], clean_name, amount=1, unit="шт", expires_at=None, category="продукты")
                if await repo.find_and_delete_shopping_item_by_name(db, user["id"], clean_name, scope="household"):
                    synced_removed += 1
                added.append(item_name)
            items_str = ", ".join(added)
            extra = f"\nИ вычеркнула из покупок: {synced_removed} поз." if synced_removed else ""
            await message.answer(
                f"Добавила в кладовку: {items_str}.{extra}\n"
                f"Посмотреть всё можно в 🍽 Еда → 📦 Кладовка.",
                reply_markup=main_menu_keyboard(profile),
            )
            return

    # траты
    if not student and looks_like_expense(text_original):
        amount = _extract_amount(text)
        if amount is None:
            await handle_free_text_inbox(message, db)
            return
        
        # Извлекаем категорию: ищем слово после "на" 
        # "потратила 500 на еду" → "еду"
        # "потратила 500 рублей на такси" → "такси"
        category = "другое"
        na_match = re.search(r"\bна\s+(\w+)", text)
        if na_match:
            category = na_match.group(1)
        else:
            # Fallback: ищем существительное после числа
            words = text.split()
            for i, w in enumerate(words):
                if re.match(r"\d", w) and i + 1 < len(words):
                    next_word = words[i + 1]
                    if next_word.isalpha() and next_word not in ("рублей", "руб", "р", "на"):
                        category = next_word
                        break
        
        user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
        await repo.add_expense(db, user["id"], amount, category)
        await message.answer(f"Записала трату: {amount:.0f} ₽ ({category}).", reply_markup=main_menu_keyboard(profile))
        return

    # Все остальные свободные сообщения разбираем через единый голосовой/текстовый inbox
    await handle_free_text_inbox(message, db)
