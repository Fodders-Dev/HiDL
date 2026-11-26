from aiogram import BaseMiddleware

from db import repositories as repo
from llm_client import reset_current_llm_user, set_current_llm_user
from utils.profile import is_student_mode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from utils.menu_match import match_menu_label


STUDENT_BLOCKED_COMMANDS = {
    "/budget", "/pantry", "/home", "/meds", "/ask_mom", "/meal_plan", "/plate",
    "/recipes_fast", "/shoplist", "/clean_schedule", "/cook_from_expiring",
    "/finance_tips", "/paperwork", "/spent", "/spent_week", "/bills", "/finance",
    "/budget_set", "/budget_cat", "/zones", "/supplies", "/fridge", "/mealplan",
    "/kitchen", "/home_plan", "/home_audit", "/home_audit_setup", "/vitamins_info",
}
STUDENT_BLOCKED_CALLBACKS = (
    "home:", "money:", "food:", "kitchen:", "pantry:", "meds:", "ask_mom:",
    "home_supplies:", "zones:", "hweek:", "supplies:", "zone:", "med:", "limit:",
    "meal:", "shop:", "cook:", "ask:", "medtake:", "medskip:", "medsched:",
    "supply:", "hall:", "cl2:", "reg:", "recipe:", "quick:", "care:",
    "bill:", "cookprof:",
)
STUDENT_BLOCKED_LABELS = {
    "дом", "еда", "еда и покупки", "покупки", "деньги", "финансы", "таблетки",
    "холодильник", "мои продукты", "спросить маму", "спроси маму", "план по дому",
    "список покупок",
}


def _student_blocked(event, message) -> bool:
    callback_data = getattr(event, "data", "") or ""
    if callback_data.startswith(STUDENT_BLOCKED_CALLBACKS) or callback_data.startswith((
        "settings:household_kick_ask:", "settings:household_kick_do:",
    )) or callback_data in {
        "settings:expiry", "settings:household", "settings:household_create",
        "settings:household_join", "settings:household_kick_menu",
        "settings:mealprof", "settings:notify:meal",
        "help:mess", "help:food",
    } or callback_data.startswith("settings:mealprof:set:"):
        return True
    value = (getattr(event, "text", None) or getattr(message, "text", None) or "").strip()
    if not value:
        return False
    command = value.split(maxsplit=1)[0].split("@", 1)[0].lower()
    return command in STUDENT_BLOCKED_COMMANDS or match_menu_label(value, STUDENT_BLOCKED_LABELS)


class EnsureUserMiddleware(BaseMiddleware):
    """Ensure user exists only when it's safe.

    Важно: НЕ создаём пользователя в middleware, иначе ломается FSM-регистрация (/start).
    Для большинства сценариев создание делается явно (в /start) или через utils.user.ensure_user в хендлерах.
    """

    def __init__(self, conn):
        super().__init__()
        self.conn = conn

    async def __call__(self, handler, event, data):
        effective_event = getattr(event, "callback_query", None) or getattr(event, "message", None) or event
        from_user = getattr(effective_event, "from_user", None)
        message = getattr(effective_event, "message", None) if getattr(effective_event, "data", None) is not None else effective_event
        text = getattr(effective_event, "text", None) or getattr(message, "text", None)
        if from_user:
            if getattr(from_user, "is_bot", False):
                return None
            tok = set_current_llm_user(int(from_user.id))
            try:
                # /start должен запускать регистрацию как "нового" пользователя.
                if effective_event is not getattr(event, "callback_query", None) and text and text.startswith("/start"):
                    return await handler(event, data)
                # Если юзер уже существует — ничего не делаем (хендлеры сами дергают ensure_user при необходимости).
                # Если юзера нет — тоже ничего не делаем: пусть /start проведёт регистрацию.
                user_row = await repo.get_user_by_telegram_id(self.conn, from_user.id)
                if user_row:
                    user = dict(user_row)
                    data["user"] = user
                    if is_student_mode(user) and _student_blocked(effective_event, message):
                        target = message
                        if target:
                            kb = InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="⚙ Настройки", callback_data="settings:main")
                            ]])
                            await target.answer("Этот раздел скрыт в режиме «Ученик». Его можно вернуть в настройках профиля.", reply_markup=kb)
                        if getattr(effective_event, "data", None) is not None and hasattr(effective_event, "answer"):
                            await effective_event.answer()
                        return None
                    return await handler(event, data)
                return await handler(event, data)
            finally:
                reset_current_llm_user(tok)
        return await handler(event, data)
