import datetime
from collections import defaultdict

from aiogram import Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.rows import row_to_dict
from utils.time import local_date_str
from utils.tone import tone_message
from utils.finance import payday_summary
from utils.texts import register_text
from utils.nl_parser import parse_command
from utils.formatting import format_money
from utils.texts import error as gentle_error

router = Router()


async def _ensure_user(db, telegram_id: int, full_name: str):
    user = await repo.get_user_by_telegram_id(db, telegram_id)
    if user:
        return dict(user)
    name = full_name or "Друг"
    user_id = await repo.create_user(db, telegram_id, name, "UTC", "08:00", "23:00")
    await repo.ensure_user_routines(db, user_id)
    created = await repo.get_user(db, user_id)
    return dict(created) if created else {"id": user_id, "name": name, "timezone": "UTC"}


class SpendState(StatesGroup):
    amount = State()
    category = State()
    bill_title = State()
    bill_amount = State()
    bill_day = State()
    payday_day = State()
    payday_budget = State()


@router.message(Command("budget"))
async def budget_info(message: types.Message) -> None:
    await message.answer(
        "Простая схема 50/30/20 (можно адаптировать):\n"
        "• 50% — обязательные расходы (жильё, еда, связь, транспорт).\n"
        "• 30% — хотелки (развлечения, покупки).\n"
        "• 20% — накопления/подушка.\n"
        "Запиши пару трат через кнопку «Записать трату», сводка — «Отчёт за неделю».",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("spent"))
async def spent_add(message: types.Message, db) -> None:
    user = await _ensure_user(db, message.from_user.id, message.from_user.full_name)
    parsed = parse_command(message.text or "")
    if parsed and parsed.type == "expense":
        amount = parsed.payload.get("amount")
        category = parsed.payload.get("category", "другое")
        if amount:
            await repo.add_expense(db, user["id"], amount, category)
            await message.answer(f"Записала трату: {amount:.0f} ₽, категория {category}.", reply_markup=main_menu_keyboard())
            return
    await message.answer("Через команду /spent можно добавить трату так: /spent 500 еда. Но проще пользоваться кнопкой «Записать трату» в разделе Деньги.")
    budget = await repo.get_budget(db, user["id"])
    if budget and budget["monthly_limit"] > 0:
        total = await repo.monthly_expense_sum(db, user["id"])
        if total > budget["monthly_limit"]:
            await message.answer(
                f"⚠️ Ты превысил лимит {budget['monthly_limit']:.0f}. Текущий месяц: {total:.0f}."
            )


@router.message(Command("spent_week"))
async def spent_week(message: types.Message, db) -> None:
    user = await _ensure_user(db, message.from_user.id, message.from_user.full_name)
    text = await _compose_spent_week(db, user)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="➕ Записать трату", callback_data="money:spent")]
        ]
    )
    await message.answer(text, reply_markup=kb)


async def _compose_spent_week(db, user) -> str:
    rows = await repo.expenses_last_days(db, user["id"], days=7)
    per_cat = defaultdict(float)
    total = 0.0
    for e in rows:
        row = dict(e)
        per_cat[row["category"]] += row["amount"]
        total += row["amount"]
    if total == 0:
        return "За последние 7 дней расходов не записано."
    lines = [f"{cat}: {format_money(amt)}" for cat, amt in per_cat.items()]
    text = f"Траты за 7 дней: {format_money(total)}\n" + "\n".join(lines)
    
    import random
    phrases = [
        "Ты молодец, что следишь за этим.",
        "Цифры — это просто цифры, главное — осознанность.",
        "Заглядывать в расходы полезно, чтобы не тревожиться.",
        "Всё под контролем.",
    ]
    text += f"\n\n<i>{random.choice(phrases)}</i>"

    budget = await repo.get_budget(db, user["id"])
    if budget:
        budget = dict(budget)
    if budget and budget["monthly_limit"] > 0:
        month_total = await repo.monthly_expense_sum(db, user["id"])
        text += f"\nМесяц: {format_money(month_total)} / лимит {format_money(budget['monthly_limit'])}"
        # грубая оценка дневного лимита до зарплаты
        today = datetime.date.today()
        payday = int(budget.get("payday_day") or 1)
        year = today.year
        month = today.month
        if today.day > payday:
            month += 1
            if month > 12:
                month = 1
                year += 1
        next_pay = datetime.date(year, month, payday)
        days_left = max(1, (next_pay - today).days)
        left_money = max(0.0, (budget["monthly_limit"] - month_total))
        text += f"\nДо зарплаты {days_left} дн., можно тратить ≈{format_money(left_money/days_left)} ₽/день."
    # категории лимитов
    cats = await repo.list_budget_categories(db, user["id"])
    if cats:
        cat_lines = []
        over: list[str] = []
        within_any = False
        for c in cats:
            row = dict(c)
            spent_cat = await repo.category_expense_sum(db, user["id"], row["category"], days=30)
            limit = float(row.get("limit_amount") or 0)
            cat_lines.append(
                f"{row['category']}: {format_money(spent_cat)} / {format_money(limit)}"
            )
            if limit > 0:
                if spent_cat > limit * 1.05:
                    over.append(row["category"])
                elif spent_cat > 0:
                    within_any = True
        text += "\nКатегории (за ~месяц):\n" + "\n".join(cat_lines)
        # короткая фраза‑резюме по лимитам
        if over:
            cats_over = ", ".join(over)
            text += f"\nЗа последнее время траты чуть выше ориентиров в категориях: {cats_over}."
        elif within_any:
            text += "\nСейчас ты в целом вписываешься в лимиты по категориям."
    return text


@router.message(Command("bills"))
async def bills_reminder(message: types.Message, db) -> None:
    user = await repo.get_user_by_telegram_id(db, message.from_user.id)
    total = 0.0
    if user:
        total = await repo.monthly_expense_sum(db, user["id"])
    await message.answer(
        "Не забудь оплатить ЖКХ/интернет/мобильную связь раз в месяц. "
        "Можно добавить счёт в разделе «Деньги» → «Счета», и я напомню заранее.\n"
        f"Сейчас расходы за месяц: {total:.0f}",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("budget_set"))
async def budget_set(message: types.Message, db) -> None:
    user = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not user:
        await message.answer(register_text())
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Пример: введи число — сколько хочешь лимит на месяц (₽).")
        return
    try:
        limit = float(parts[1])
        if limit < 0:
            raise ValueError
    except Exception:
        await message.answer("Лимит должен быть неотрицательным числом.")
        return
    await repo.upsert_budget(db, user["id"], limit)
    await message.answer(f"Лимит на месяц установлен: {limit:.0f}")


@router.message(Command("budget_cat"))
async def budget_cat(message: types.Message, db) -> None:
    user = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not user:
        await message.answer(gentle_error("Нужно пройти /start, чтобы сохранить лимит"))
        return
    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Пример: введи «еда 5000» или «транспорт 3000».")
        return
    category = parts[1]
    try:
        limit = float(parts[2])
        if limit < 0:
            raise ValueError
    except Exception:
        await message.answer("Лимит должен быть неотрицательным числом.")
        return
    await repo.upsert_budget_category(db, user["id"], category, limit)
    await message.answer(f"Лимит по категории '{category}' установлен: {limit:.0f}")


@router.callback_query(lambda c: c.data and c.data.startswith("tone:"))
async def tone_set(callback: types.CallbackQuery, db) -> None:
    _, tone = callback.data.split(":")
    user = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    if not user:
        await callback.answer(register_text(), show_alert=True)
        return
    await repo.upsert_wellness(db, user["id"], tone=tone)
    await callback.answer("Тон обновлён")
    await callback.message.edit_text(f"Тон установлен: {tone}")


@router.message(Command("tone"))
async def tone_select(message: types.Message, db) -> None:
    user = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not user:
        await message.answer(register_text())
        return
    await message.answer(
        "Выбери стиль общения:",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(text="Мягкий", callback_data="tone:soft"),
                    types.InlineKeyboardButton(text="Нейтральный", callback_data="tone:neutral"),
                    types.InlineKeyboardButton(text="Подпинывающий", callback_data="tone:pushy"),
                ]
            ]
        ),
    )
@router.message(lambda m: m.text and "деньг" in m.text.lower())
async def money_menu_entry(message: types.Message, state: FSMContext, db) -> None:
    await _ensure_user(db, message.from_user.id, message.from_user.full_name)
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="➕ Записать трату", callback_data="money:spent")],
            [types.InlineKeyboardButton(text="📊 Отчёт за неделю", callback_data="money:report")],
            [types.InlineKeyboardButton(text="🎯 Лимиты", callback_data="money:cat")],
            [types.InlineKeyboardButton(text="⏳ До зарплаты", callback_data="money:payday")],
            [types.InlineKeyboardButton(text="📅 Счета", callback_data="money:bills")],
            [types.InlineKeyboardButton(text="💡 Советы", callback_data="money:tips")],
        ]
    )
    await message.answer(
        "Финансы — это не страшно. Я помогу следить за расходами, чтобы деньги не "
        "исчезали в неизвестность.\n\nВыбери действие:",
        reply_markup=kb,
    )


@router.callback_query(lambda c: c.data and c.data.startswith("money:"))
async def money_callbacks(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await _ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    action = callback.data.split(":")[1]
    if action == "spent":
        await state.set_state(SpendState.amount)
        await callback.message.answer("Сколько ушло? Напиши сумму, или сразу сумму и категорию (например «500 еда»).")
    elif action == "report":
        user = await _ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        text = await _compose_spent_week(db, user)
        now_utc = datetime.datetime.utcnow()
        local_today = local_date_str(now_utc, user["timezone"])
        payday_line = await payday_summary(db, user, local_today)
        if payday_line:
            text += f"\n\n{payday_line}"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text="➕ Записать трату", callback_data="money:spent")],
                [types.InlineKeyboardButton(text="🎯 Лимиты", callback_data="money:cat")],
                [types.InlineKeyboardButton(text="📅 Счета", callback_data="money:bills")],
            ]
        )
        await callback.message.answer(text, reply_markup=kb)
    elif action == "cat":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text="Еда", callback_data="limit:cat:еда")],
                [types.InlineKeyboardButton(text="Транспорт", callback_data="limit:cat:транспорт")],
                [types.InlineKeyboardButton(text="Жильё", callback_data="limit:cat:жильё")],
                [types.InlineKeyboardButton(text="Развлечения", callback_data="limit:cat:развлечения")],
                [types.InlineKeyboardButton(text="Другое", callback_data="limit:cat:другое")],
            ]
        )
        await callback.message.answer("Выбери категорию лимита:", reply_markup=kb)
    elif action == "bills":
        await bills_menu(callback.message, state, db)
    elif action == "payday":
        user = await _ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
        summary = await payday_summary(db, dict(user), today)
        if summary:
            await callback.message.answer(summary, reply_markup=main_menu_keyboard())
        await state.set_state(SpendState.payday_day)
        await callback.message.answer(
            "Какого числа обычно приходит зарплата? Введи число 1–31. "
            "Сразу после этого спрошу бюджет на еду/быт до следующей выплаты."
        )
    elif action == "tips":
        tips_text = (
            "Мини-советы по деньгам:\n"
            "• Подписки: раз в месяц смотри выписку и отменяй лишнее.\n"
            "• Кредиты/рассрочки: избегай, если нет подушки — проценты съедают бюджет.\n"
            "• Подушка: откладывай хотя бы 5–10% дохода на отдельный счёт/копилку.\n"
            "• План: сначала обязательные траты, потом хотелки.\n"
            "• Сделай лимит по категориям — кнопка «Лимиты» рядом."
        )
        await callback.message.answer(tips_text, reply_markup=main_menu_keyboard())
    await callback.answer()


# Свободный ввод для трат (натуральные команды)
@router.message(lambda m: m.text and any(ch.isdigit() for ch in m.text))
async def money_free_parse(message: types.Message, db, state: FSMContext) -> None:
    if await state.get_state():
        return
    parsed = parse_command(message.text)
    if not parsed or parsed.type != "expense":
        return
    user = await _ensure_user(db, message.from_user.id, message.from_user.full_name)
    amount = parsed.payload.get("amount")
    category = parsed.payload.get("category", "другое")
    if amount is None:
        return
    await repo.add_expense(db, user["id"], amount, category)
    await message.answer(
        f"Записала трату: {amount:.0f} ₽, категория {category}.",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("limit:cat:"))
async def limit_cat(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    _, _, cat = callback.data.split(":")
    await _ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await state.update_data(limit_category=cat)
    await state.set_state(SpendState.category)
    await callback.message.answer(f"Лимит для {cat}: введи сумму в месяц (₽).", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.message(SpendState.amount)
async def spend_amount(message: types.Message, state: FSMContext, db) -> None:
    text = message.text.strip()
    parts = text.split(maxsplit=1)
    try:
        amount = float(parts[0])
    except Exception:
        await message.answer("Введи сумму числом. Пример: 1000 или 1000 еда")
        return
    if len(parts) > 1:
        category = parts[1]
        user = await _ensure_user(db, message.from_user.id, message.from_user.full_name)
        await repo.add_expense(db, user["id"], amount, category)
        await state.clear()
        await message.answer(f"Записала: {amount:.0f} ({category})", reply_markup=main_menu_keyboard())
    else:
        await state.update_data(amount=amount)
        await state.set_state(SpendState.category)
        await message.answer("На что потратил? Пример: еда, транспорт, вкусвилл.")


@router.message(SpendState.category)
async def spend_category(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    amount = data.get("amount")
    category = message.text.strip()
    user = await _ensure_user(db, message.from_user.id, message.from_user.full_name)
    if amount is None:
        # This is limit flow
        try:
            limit = float(category.replace(",", "."))
        except Exception:
            from utils import texts

            await message.answer(
                texts.error("лимит должен быть числом, например 15000"),
            )
            return
        limit_cat_name = data.get("limit_category", "другое")
        await repo.upsert_budget_category(db, user["id"], limit_cat_name, limit)
        await state.clear()
        await message.answer(f"Лимит для {limit_cat_name} — {limit:.0f} ₽/мес.", reply_markup=main_menu_keyboard())
        return
    await repo.add_expense(db, user["id"], float(amount), category)
    await state.clear()
    await message.answer(f"Записала: {amount:.0f} ({category})", reply_markup=main_menu_keyboard())


@router.message(SpendState.payday_day)
async def payday_day_set(message: types.Message, state: FSMContext, db) -> None:
    try:
        day = int(message.text.strip())
        if day < 1 or day > 31:
            raise ValueError
    except Exception:
        from utils import texts

        await message.answer(
            texts.error("день должен быть от 1 до 31. Попробуй ещё раз."),
        )
        return
    await state.update_data(payday_day=day)
    await state.set_state(SpendState.payday_budget)
    await message.answer("Какой бюджет на еду/быт до следующей зарплаты? Введи сумму в ₽.")


@router.message(SpendState.payday_budget)
async def payday_budget_set(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    day = data.get("payday_day", 1)
    try:
        budget = float(message.text.strip().replace(",", "."))
        if budget < 0:
            raise ValueError
    except Exception:
        from utils import texts

        await message.answer(
            texts.error("нужно неотрицательное число. Введи сумму в ₽."),
        )
        return
    user = await _ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.upsert_budget(db, user["id"], monthly_limit=budget, payday_day=day, food_budget=budget)
    await state.clear()
    await message.answer(
        f"Записала: день зарплаты {day}, бюджет {budget:.0f} ₽. "
        "В /today покажу остаток и безопасный дневной лимит.",
        reply_markup=main_menu_keyboard(),
    )


# Счета
async def _render_bills(db, user_id: int) -> str:
    bills = await repo.list_bills(db, user_id)
    today = datetime.date.today()
    current_month = today.strftime("%Y-%m")
    if not bills:
        return "Счета: список пуст. Добавь первый платёж."
    lines = []
    for b in bills:
        row = row_to_dict(b)
        paid = "✅" if row.get("last_paid_month") == current_month else "⏳"
        # вычислим ближайшую дату оплаты
        day = int(row.get("day_of_month", 1) or 1)
        year = today.year
        month = today.month
        if today.day > day:
            # следующий месяц
            month += 1
            if month > 12:
                month = 1
                year += 1
        due_date = datetime.date(year, month, day)
        due_text = due_date.strftime("%d.%m.%Y")
        lines.append(f"{paid} {row.get('title')}: ~{row.get('amount',0):.0f} ₽, до {due_text}")
    return "Счета:\n" + "\n".join(lines)


async def bills_menu(message: types.Message, state: FSMContext, db) -> None:
    user = await _ensure_user(db, message.from_user.id, message.from_user.full_name)
    await state.clear()
    text = await _render_bills(db, user["id"])
    kb_rows = []
    bills = await repo.list_bills(db, user["id"])
    for b in bills:
        row = row_to_dict(b)
        kb_rows.append(
            [
                types.InlineKeyboardButton(
                    text=f"Оплачено {row['title']}",
                    callback_data=f"bill:pay:{row['id']}",
                )
            ]
        )
    kb_rows.append([types.InlineKeyboardButton(text="➕ Добавить счёт", callback_data="bill:add")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await message.answer(text, reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("bill:add"))
async def bill_add(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    user = await _ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await state.set_state(SpendState.bill_title)
    await callback.message.answer("Как называется платёж? Например: ЖКХ, Интернет, Мобила.", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.message(SpendState.bill_title)
async def bill_set_title(message: types.Message, state: FSMContext, db) -> None:
    await state.update_data(bill_title=message.text.strip())
    await state.set_state(SpendState.bill_amount)
    await message.answer("Примерная сумма (₽)?", reply_markup=main_menu_keyboard())


@router.message(SpendState.bill_amount)
async def bill_set_amount(message: types.Message, state: FSMContext, db) -> None:
    try:
        amount = float(message.text.strip().replace(",", "."))
    except Exception:
        await message.answer("Сумма должна быть числом. Попробуй ещё раз.")
        return
    await state.update_data(bill_amount=amount)
    await state.set_state(SpendState.bill_day)
    await message.answer("В какой день месяца платить? (1–28)", reply_markup=main_menu_keyboard())


@router.message(SpendState.bill_day)
async def bill_set_day(message: types.Message, state: FSMContext, db) -> None:
    try:
        day = int(message.text.strip())
        if day < 1 or day > 28:
            raise ValueError
    except Exception:
        await message.answer("День должен быть от 1 до 28, чтобы не промахиваться с месяцами.")
        return
    data = await state.get_data()
    title = data.get("bill_title", "Счёт")
    amount = float(data.get("bill_amount", 0))
    user = await _ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.upsert_bill(db, user["id"], title, amount, day)
    await state.clear()
    await message.answer(
        f"Добавила счёт {title}: ~{amount:.0f} ₽, день {day} каждого месяца.",
        reply_markup=main_menu_keyboard(),
    )
    # сразу показать актуальный список счетов
    await bills_menu(message, state, db)


@router.callback_query(lambda c: c.data and c.data.startswith("bill:pay:"))
async def bill_pay(callback: types.CallbackQuery, db) -> None:
    _, _, bill_id = callback.data.split(":")
    bill_id = int(bill_id)
    user = await _ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    current_month = datetime.date.today().strftime("%Y-%m")
    await repo.mark_bill_paid(db, user["id"], bill_id, current_month)
    text = await _render_bills(db, user["id"])
    await callback.message.edit_text(text, reply_markup=None)
    await callback.message.answer("Отметила счёт как оплаченный.", reply_markup=main_menu_keyboard())
    await callback.answer()
