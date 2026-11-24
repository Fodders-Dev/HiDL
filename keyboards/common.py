from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from utils.profile import is_student_mode


def main_menu_keyboard(user: dict | None = None) -> ReplyKeyboardMarkup:
    if is_student_mode(user):
        rows = [
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📚 Учёба / Desk")],
            [KeyboardButton(text="🔔 Напоминания"), KeyboardButton(text="☕ Кафе фокуса")],
            [KeyboardButton(text="😴 Сон и пауза"), KeyboardButton(text="⚙ Настройки")],
        ]
    else:
        rows = [
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="🍽 Еда и покупки")],
            [KeyboardButton(text="🌙 Сон и карма"), KeyboardButton(text="🗓 Расписание")],
            [KeyboardButton(text="📓 Дневник"), KeyboardButton(text="⚙ Ещё")],
        ]
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


def more_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🏠 Наш дом", callback_data="hh:hub"),
                InlineKeyboardButton(text="🧹 Дом", callback_data="more:home"),
            ],
            [
                InlineKeyboardButton(text="💰 Деньги", callback_data="more:money"),
                InlineKeyboardButton(text="🏋️ Спорт", callback_data="more:sport"),
            ],
            [
                InlineKeyboardButton(text="☕ Кафе фокуса", callback_data="more:focus"),
                InlineKeyboardButton(text="🤒 Болею / ЧП (24ч)", callback_data="pulse:sick:prompt:more"),
            ],
            [
                InlineKeyboardButton(text="⚙ Настройки", callback_data="settings:main"),
                InlineKeyboardButton(text="🆘 Помощь сейчас", callback_data="help:menu"),
            ],
        ]
    )


def food_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🍳 Что приготовить сейчас?", callback_data="kitchen:quick_pick")],
            [
                InlineKeyboardButton(text="🛒 Не хватает 1–2 прод.", callback_data="kitchen:almost_ready:0"),
                InlineKeyboardButton(text="✨ ИИ-шеф", callback_data="kitchen:ai_chef"),
            ],
            [
                InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
                InlineKeyboardButton(text="📅 Меню на 3 дня", callback_data="kitchen:meal_plan"),
            ],
            [
                InlineKeyboardButton(text="❄️ Холодильник", callback_data="kitchen:fridge"),
                InlineKeyboardButton(text="🛒 Покупки", callback_data="kitchen:shoplist"),
            ],
            [
                InlineKeyboardButton(text="📥 Импорт корзины", callback_data="kitchen:cart_import"),
                InlineKeyboardButton(text="📥 Импорт рецепта", callback_data="kitchen:recipe_import"),
            ],
            [InlineKeyboardButton(text="📖 Рецепты", callback_data="kitchen:recipes")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="main:menu")],
        ]
    )


def money_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Записать трату", callback_data="money:spent")],
            [InlineKeyboardButton(text="🏠 Общие траты дома", callback_data="money:household")],
            [InlineKeyboardButton(text="📊 Отчёт за неделю", callback_data="money:report")],
            [InlineKeyboardButton(text="🎯 Лимиты", callback_data="money:cat")],
            [InlineKeyboardButton(text="⏳ До зарплаты", callback_data="money:payday")],
            [InlineKeyboardButton(text="📅 Счета", callback_data="money:bills")],
            [InlineKeyboardButton(text="💡 Советы", callback_data="money:tips")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="main:menu")],
        ]
    )


def home_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Наш дом", callback_data="hh:hub")],
            [InlineKeyboardButton(text="🧽 Уборка сейчас", callback_data="home:now")],
            [InlineKeyboardButton(text="⚡ Быстрые сценарии", callback_data="home:quickmenu")],
            [InlineKeyboardButton(text="📅 План на неделю", callback_data="home:week")],
            [InlineKeyboardButton(text="📋 Все дела по дому", callback_data="home:all")],
            [InlineKeyboardButton(text="🧴 Бытовая химия", callback_data="home:supplies")],
            [InlineKeyboardButton(text="🧴 Запахи дома и стирка", callback_data="home:smell")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="main:menu")],
        ]
    )


def movement_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🫠 Лень начинать (микро-старт 3–5 мин)", callback_data="fit:lazy")],
            [InlineKeyboardButton(text="🏋️ Тренировка под мой дом", callback_data="fit:workout_menu")],
            [InlineKeyboardButton(text="⚖ Умные весы и параметры тела", callback_data="fit:body")],
            [InlineKeyboardButton(text="🧰 Мой инвентарь дома", callback_data="fit:gear")],
            [
                InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
                InlineKeyboardButton(text="🎯 Цель и коуч", callback_data="fit:coach_menu"),
            ],
            [InlineKeyboardButton(text="📊 Прогресс за неделю", callback_data="fit:stats")],
            [
                InlineKeyboardButton(text="🚶 Прогулка", callback_data="move:short"),
                InlineKeyboardButton(text="⏱ Фокус 20/10", callback_data="move:focus"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="main:menu")],
        ]
    )


def settings_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Щадящий режим", callback_data="set:gentle")],
            [InlineKeyboardButton(text="Тон общения", callback_data="set:tone")],
            [InlineKeyboardButton(text="Вода/Еда/Фокус", callback_data="set:wellness")],
            [InlineKeyboardButton(text="Профиль питания", callback_data="set:profile")],
            [InlineKeyboardButton(text="Часовой пояс/подъём/отбой", callback_data="set:settings")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="main:menu")],
        ]
    )


def knowledge_keyboard(category: str, items: list) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=item["title"], callback_data=f"article:{item['id']}"
            )
        ]
        for item in items
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
