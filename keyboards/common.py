from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="🍽 Еда")],
            [KeyboardButton(text="💰 Деньги"), KeyboardButton(text="🧹 Дом")],
            [KeyboardButton(text="🏋️ Спорт"), KeyboardButton(text="🛒 Покупки")],
            [KeyboardButton(text="🤱 Спросить маму"), KeyboardButton(text="💬 Поговорить")],
            [KeyboardButton(text="⚙ Настройки"), KeyboardButton(text="☕ Поддержать")],
        ],
        resize_keyboard=True,
    )


def food_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❄️ Мой холодильник", callback_data="kitchen:fridge")],
            [InlineKeyboardButton(text="📖 Книга рецептов", callback_data="kitchen:recipes")],
            [InlineKeyboardButton(text="🛒 Список покупок", callback_data="kitchen:shoplist")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="main:menu")],
        ]
    )


def money_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Записать трату", callback_data="money:spent")],
            [InlineKeyboardButton(text="Отчёт за неделю", callback_data="money:report")],
            [InlineKeyboardButton(text="Лимиты", callback_data="money:cat")],
            [InlineKeyboardButton(text="Счета", callback_data="money:bills")],
            [InlineKeyboardButton(text="Советы", callback_data="money:tips")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="main:menu")],
        ]
    )


def home_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
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
            [InlineKeyboardButton(text="Разминка 5 мин", callback_data="move:warmup")],
            [InlineKeyboardButton(text="Дом тренировка 10-15", callback_data="move:home10")],
            [InlineKeyboardButton(text="Дом тренировка 20 мин", callback_data="move:home20")],
            [InlineKeyboardButton(text="Короткая прогулка", callback_data="move:short")],
            [InlineKeyboardButton(text="Прогулка подлиннее", callback_data="move:long")],
            [InlineKeyboardButton(text="⚖ Вес/цели", callback_data="move:weight")],
            [InlineKeyboardButton(text="Фокус 20/10", callback_data="move:focus")],
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
