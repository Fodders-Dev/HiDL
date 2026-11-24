import datetime
import html
import os

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from services import household_service
from utils import texts
from utils.sender import safe_edit
from utils.time import is_valid_timezone, parse_hhmm
from utils.user import ensure_user

router = Router()


class SettingsState(StatesGroup):
    timezone = State()
    wake = State()
    sleep = State()
    goals = State()
    routine_time = State()
    expiry = State()
    household_join = State()
    affirm_custom_time = State()


ROUTINE_GENITIVE = {
    "morning": "утра",
    "day": "дня",
    "evening": "вечера",
}


def _cancel_settings_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура отмены текстового ввода в настройках."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="settings:main")]
        ]
    )


async def _render_household_screen(db, user: dict) -> tuple[str, InlineKeyboardMarkup]:
    hid = await repo.get_or_create_household(db, int(user["id"]))
    house = await repo.get_household(db, hid)
    members = await repo.list_household_members(db, hid)
    code = str((house or {}).get("invite_code") or f"H{user['id']}")
    cfg = (house or {}).get("settings") or {}
    bot_username = os.getenv("BOT_USERNAME", "HiDL_bot").strip().lstrip("@") or "HiDL_bot"
    deep_link = f"https://t.me/{bot_username}?start=house_{code}"

    member_names = ", ".join(html.escape(str(m.get("name") or f"User {m['id']}")) for m in members)
    lines = [
        "🏠 <b>Наш дом</b>",
        f"👥 Участники ({len(members)}): {member_names or 'только ты'}",
        f"🔑 Код приглашения: <code>{html.escape(code)}</code>",
        f"🔗 Ссылка для партнёра:\n<code>{html.escape(deep_link)}</code>",
        "",
        "Что делите вместе (холодильник и общий список покупок — всегда общие):",
    ]
    rec_on = bool(cfg.get("share_recipes", True))
    bills_on = bool(cfg.get("share_bills", True))
    tasks_on = bool(cfg.get("share_home_tasks", True))
    sup_on = bool(cfg.get("share_supplies", True))
    notif_on = bool(cfg.get("notify_partner", True))
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅' if rec_on else '⭕️'} Общие рецепты и меню",
            callback_data="settings:household_toggle:share_recipes",
        )],
        [InlineKeyboardButton(
            text=f"{'✅' if bills_on else '⭕️'} Общие счета",
            callback_data="settings:household_toggle:share_bills",
        )],
        [InlineKeyboardButton(
            text=f"{'✅' if tasks_on else '⭕️'} Общие дела по дому",
            callback_data="settings:household_toggle:share_home_tasks",
        )],
        [InlineKeyboardButton(
            text=f"{'✅' if sup_on else '⭕️'} Общая бытовая химия",
            callback_data="settings:household_toggle:share_supplies",
        )],
        [InlineKeyboardButton(
            text=f"{'✅' if notif_on else '⭕️'} Уведомления дома",
            callback_data="settings:household_toggle:notify_partner",
        )],
        [InlineKeyboardButton(text="🏠 Открыть хаб «Наш дом»", callback_data="hh:hub")],
        [
            InlineKeyboardButton(text="🔄 Новый код", callback_data="settings:household_regen"),
            InlineKeyboardButton(text="🔗 Ввести код", callback_data="settings:household_join"),
        ],
    ]
    if len(members) > 1:
        rows.append([InlineKeyboardButton(text="🚪 Выйти из дома", callback_data="settings:household_leave")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:main")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _handle_settings_cancel(message: types.Message, state: FSMContext, db) -> bool:
    """Сбросить FSM настроек, если пользователь написал отмену/назад."""
    if (message.text or "").strip().lower() in {"отмена", "назад"}:
        await state.clear()
        user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
        await message.answer("Отменено.", reply_markup=main_menu_keyboard(user))
        return True
    return False


def _settings_main_text(user) -> str:
    return (
        "Твои текущие настройки:\n"
        f"• Имя: {user['name']}\n"
        f"• Часовой пояс: {user['timezone']}\n"
        f"• Подъём: {user['wake_up_time']} / Отбой: {user['sleep_time']}\n"
        f"• Цель/приоритет: {user['goals'] or 'Нет цели пока'}\n\n"
        "Что поменяем? Выбери ниже."
    )


def settings_keyboard(user: dict | None = None) -> InlineKeyboardMarkup:
    """
    Компактное меню настроек.

    Группируем схожие пункты, чтобы не было длинной простыни кнопок.
    Детальные действия (подъём/отбой, время рутин, цели) открываются
    во вложенных меню.
    """
    focus_icon = "✅" if bool((user or {}).get("adhd_mode")) else "❌"
    student = (user or {}).get("profile_mode", "adult") == "student"
    rows = [
            [InlineKeyboardButton(text=f"🎓 Режим: {'Ученик' if student else 'Взрослый'}", callback_data=f"settings:profile_mode:{'adult' if student else 'student'}")],
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="settings:profile")],
            [InlineKeyboardButton(text="🔔 Уведомления", callback_data="settings:notifications")],
            [InlineKeyboardButton(text="😴 Режим сна", callback_data="settings:sleep_mode")],
            [InlineKeyboardButton(text="Время и режим дня", callback_data="settings:time_menu")],
            [InlineKeyboardButton(text="Рутины (шаги и время)", callback_data="settings:routines_menu")],
            [InlineKeyboardButton(text=f"Щадящий фокус {focus_icon}", callback_data="settings:adhd")],
    ]
    if not student:
        rows.extend([
            [InlineKeyboardButton(text="Срок «скоро истечёт»", callback_data="settings:expiry")],
            [InlineKeyboardButton(text="Общий дом", callback_data="settings:household")],
        ])
    rows.extend([
            [InlineKeyboardButton(text="📚 Fodder's Desk", callback_data="desk:main")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="main:menu")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_notifications_menu(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    wellness = await repo.get_wellness(db, user["id"])
    w = dict(wellness) if wellness else {}

    meal_enabled = w.get("meal_enabled", 1)
    water_enabled = w.get("water_enabled", 0)
    affirm_enabled = w.get("affirm_enabled", 0)
    quiet_enabled = user.get("quiet_mode", 0)

    meal_icon = "✅" if meal_enabled else "❌"
    water_icon = "✅" if water_enabled else "❌"
    affirm_icon = "✅" if affirm_enabled else "❌"
    quiet_icon = "✅" if quiet_enabled else "❌"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            *([] if user.get("profile_mode", "adult") == "student" else [
                [InlineKeyboardButton(text=f"🍽 Еда {meal_icon}", callback_data="settings:notify:meal")]
            ]),
            [InlineKeyboardButton(text=f"💧 Вода {water_icon}", callback_data="settings:notify:water")],
            [InlineKeyboardButton(text=f"🌟 Аффирмации {affirm_icon}", callback_data="settings:notify:affirm_menu")],
            [InlineKeyboardButton(text=f"🔕 Тихий режим {quiet_icon}", callback_data="settings:quiet")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:main")],
        ]
    )
    await safe_edit(
        callback.message,
        "🔔 Настройки уведомлений\n\n"
        "Включай и выключай напоминания по категориям:",
        reply_markup=kb,
    )


def _affirm_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    """Собрать клавиатуру выбора режима аффирмаций с подсветкой текущего выбора и кнопкой Назад."""
    def label(mode: str, text: str) -> str:
        return f"✅ {text}" if current_mode == mode else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label("off", "Выкл"), callback_data="settings:affirm:set:off"),
                InlineKeyboardButton(text=label("morning", "Утром"), callback_data="settings:affirm:set:morning"),
            ],
            [
                InlineKeyboardButton(text=label("evening", "Вечером"), callback_data="settings:affirm:set:evening"),
                InlineKeyboardButton(text=label("both", "Утром и вечером"), callback_data="settings:affirm:set:both"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:profile_menu")],
        ]
    )


@router.message(Command("settings"))
async def settings_entry(message: types.Message, state: FSMContext, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await state.clear()
    await message.answer(_settings_main_text(user), reply_markup=settings_keyboard(user))


@router.callback_query(
    lambda c: c.data
    and c.data.startswith("settings:")
    and not c.data.startswith("settings:mealprof:set:")
    and not c.data.startswith("settings:affirm:set:")
)
async def settings_select(callback: types.CallbackQuery, state: FSMContext, db, skip_answer: bool = False) -> None:
    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer()
        return
    action = parts[1]
    await state.clear()
    if action == "profile_mode" and len(parts) == 3 and parts[2] in {"adult", "student"}:
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        await repo.update_user_profile_mode(db, user["id"], parts[2])
        user = dict(await repo.get_user(db, user["id"]))
        await safe_edit(callback.message, _settings_main_text(user), reply_markup=settings_keyboard(user))
        await callback.message.answer("Профиль обновлён.", reply_markup=main_menu_keyboard(user))
        await callback.answer("Готово")
        return
    # Возврат в главное меню настроек
    if action == "main":
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        await safe_edit(callback.message, _settings_main_text(user), reply_markup=settings_keyboard(user))
        if not skip_answer:
            await callback.answer()
        return
    # Вложенное меню «Время и режим дня».
    if action == "time_menu":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Часовой пояс", callback_data="settings:tz"),
                ],
                [
                    InlineKeyboardButton(text="Подъём", callback_data="settings:wake"),
                    InlineKeyboardButton(text="Отбой", callback_data="settings:sleep"),
                ],
                [
                    InlineKeyboardButton(text="Время: утро", callback_data="settings:rt:morning"),
                    InlineKeyboardButton(text="Время: день", callback_data="settings:rt:day"),
                    InlineKeyboardButton(text="Время: вечер", callback_data="settings:rt:evening"),
                ],
                [
                    InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:main"),
                ],
            ]
        )
        await safe_edit(
            callback.message,
            "Время и режим дня:\n"
            "• Часовой пояс — для правильных дат и времени.\n"
            "• Подъём/Отбой — для планов и щадящего режима.\n"
            "• Время рутин — во сколько приходят Утро/День/Вечер.",
            reply_markup=kb,
        )
    # Вложенное меню «Рутины».
    elif action == "routines_menu":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Шаги утро/день/вечер", callback_data="settings:rsteps"),
                ],
                [
                    InlineKeyboardButton(text="Время: утро", callback_data="settings:rt:morning"),
                    InlineKeyboardButton(text="Время: день", callback_data="settings:rt:day"),
                    InlineKeyboardButton(text="Время: вечер", callback_data="settings:rt:evening"),
                ],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:main")],
            ]
        )
        await safe_edit(
            callback.message,
            "Рутины:\n"
            "Можно настроить шаги и время для утренней, дневной и вечерней рутин.",
            reply_markup=kb,
        )
    elif action == "rsteps":
        # Показываем меню выбора рутины для редактирования шагов (тот же поток, что /routine_steps).
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Утро", callback_data="rstep:routine:morning"),
                    InlineKeyboardButton(text="День", callback_data="rstep:routine:day"),
                    InlineKeyboardButton(text="Вечер", callback_data="rstep:routine:evening"),
                ],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:routines_menu")],
            ]
        )
        await safe_edit(
            callback.message,
            "Выбери рутину, чтобы включить/выключить шаги, переименовать или добавить новые.",
            reply_markup=kb,
        )
    # Вложенное меню «Питание и аффирмации».
    elif action == "profile_menu":
        # Legacy redirect or keep as separate if needed, but we are moving to settings:profile
        # For now, let's redirect to main profile
        await settings_select(
            callback.model_copy(update={"data": "settings:profile"}),
            state,
            db,
            skip_answer=skip_answer,
        )
        return

    # Вложенное меню «Мой профиль»
    elif action == "profile":
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        wellness = await repo.get_wellness(db, user["id"])

        gender = user.get("gender", "neutral")
        g_label = {
            "male": "👨 Мужчина",
            "female": "👩 Женщина",
            "neutral": "🙂 Не указан",
        }.get(gender, "🙂 Не указан")

        diet = (wellness or {}).get("meal_profile", "omnivore")
        d_label = {
            "omnivore": "🥩 Обычный",
            "vegetarian": "🥗 Вегетарианец",
            "vegan": "🌱 Веган",
        }.get(diet, diet)

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"Пол: {g_label}", callback_data="settings:gender")],
                *([] if user.get("profile_mode", "adult") == "student" else [
                    [InlineKeyboardButton(text=f"Питание: {d_label}", callback_data="settings:mealprof")]
                ]),
                [InlineKeyboardButton(text="Цель/Приоритет", callback_data="settings:goals")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:main")],
            ]
        )
        await safe_edit(
            callback.message,
            "👤 <b>Мой профиль</b>\n\n"
            "Здесь хранится информация о тебе для персонализации бота.",
            reply_markup=kb,
        )
    # Старые действия — оставляем для совместимости и прямых переходов.
    elif action == "tz":
        await state.set_state(SettingsState.timezone)
        await callback.message.answer(
            "Сколько у тебя сейчас времени? Напиши в формате HH:MM (я сама посчитаю смещение).\n"
            "Если хочешь задать вручную — пришли таймзону вроде Europe/Moscow или UTC+3.",
            reply_markup=_cancel_settings_keyboard(),
        )
    elif action == "wake":
        await state.set_state(SettingsState.wake)
        await callback.message.answer(
            "Новый подъём? Формат HH:MM, например 07:30.",
            reply_markup=_cancel_settings_keyboard(),
        )
    elif action == "sleep":
        await state.set_state(SettingsState.sleep)
        await callback.message.answer(
            "Новый отбой? Формат HH:MM, например 23:30.",
            reply_markup=_cancel_settings_keyboard(),
        )
    elif action == "goals":
        await state.set_state(SettingsState.goals)
        await callback.message.answer(
            "Коротко опиши приоритет или цель (одно сообщение).",
            reply_markup=_cancel_settings_keyboard(),
        )
    elif action == "household":
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        text, kb = await _render_household_screen(db, user)
        await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    elif action == "household_create":
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        text, kb = await _render_household_screen(db, user)
        await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    elif action == "household_regen":
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        hid = await repo.get_or_create_household(db, user["id"])
        await repo.regenerate_household_invite_code(db, hid)
        text, kb = await _render_household_screen(db, user)
        await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
        await callback.answer("Обновила код приглашения 🔄")
        return
    elif action == "household_toggle" and len(parts) >= 3:
        key = parts[2]
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        hid = await repo.get_or_create_household(db, user["id"])
        house = await repo.get_household(db, hid)
        cur_settings = (house or {}).get("settings") or {}
        if key == "share_recipes":
            new_val = not bool(cur_settings.get("share_recipes", True))
            await repo.update_household_settings(db, hid, share_recipes=new_val, share_meal_plan=new_val)
        elif key in {"share_bills", "notify_partner", "share_home_tasks", "share_supplies"}:
            new_val = not bool(cur_settings.get(key, True))
            await repo.update_household_settings(db, hid, **{key: new_val})
        text, kb = await _render_household_screen(db, user)
        await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
        await callback.answer("Сохранено")
        return
    elif action == "household_leave":
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        await repo.leave_household(db, user["id"])
        text, kb = await _render_household_screen(db, user)
        await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
        await callback.answer("Ты вышел(-а) в свой личный дом.")
        return
    elif action == "household_join":
        await state.set_state(SettingsState.household_join)
        await callback.message.answer(
            "Пришли код дома, который дал тебе партнёр. Я попробую подключить тебя к тому же пространству.",
            reply_markup=_cancel_settings_keyboard(),
        )

    # --- Выбор пола ---
    elif action == "gender":
        # Если это установка пола
        if len(parts) >= 4 and parts[2] == "set":
            gender = parts[3]
            if gender in {"male", "female", "neutral"}:
                user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
                await repo.update_user_gender(db, user["id"], gender)
                await callback.answer("Сохранено")
        
        # Отображение меню (обновленного или первичного)
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        current_gender = user.get("gender", "neutral")
        
        def g_label(g: str, text: str) -> str:
            return f"✅ {text}" if current_gender == g else text
            
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=g_label("female", "👩 Женщина"), callback_data="settings:gender:set:female"),
                    InlineKeyboardButton(text=g_label("male", "👨 Мужчина"), callback_data="settings:gender:set:male"),
                ],
                [
                    InlineKeyboardButton(text=g_label("neutral", "🙂 Не указывать"), callback_data="settings:gender:set:neutral"),
                ],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:profile")],
            ]
        )
        
        try:
            await callback.message.edit_text(
                "👤 Выбери пол для персонализации сообщений.\n\n"
                "Это влияет на окончания слов: «ты поел» / «ты поела».",
                reply_markup=kb
            )
        except:
            # Если текст не изменился (пользователь нажал на тот же пол), aiogram может кинуть ошибку
            pass
            
        if not skip_answer and not (len(parts) >= 4 and parts[2] == "set"):
            # Если мы просто открыли меню, answer нужен, чтобы убрать часики
            await callback.answer()
        return
    # --- Уведомления ---
    elif action == "notifications":
        await _render_notifications_menu(callback, db)
    elif action == "sleep_mode":
        from handlers import sleep_mode as sleep_handler

        await sleep_handler._render_sleep_menu(  # noqa: SLF001
            callback.message,
            db,
            origin="settings",
            tg_user_id=callback.from_user.id,
            tg_full_name=callback.from_user.full_name,
        )
        if not skip_answer:
            await callback.answer()
        return
    elif action == "quiet":
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        current = user.get("quiet_mode", 0)
        new_val = 0 if current else 1
        await repo.set_quiet_mode(db, user["id"], new_val)
        await callback.answer("Тихий режим: " + ("вкл" if new_val else "выкл"))
        await _render_notifications_menu(callback, db)
        return
    elif action == "affirm_cat_menu":
        await settings_select(
            callback.model_copy(update={"data": "settings:notify:affirm_cat_menu"}),
            state,
            db,
            skip_answer=skip_answer,
        )
        return
    elif action == "notify" and len(parts) >= 3:
        notify_type = parts[2]
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        wellness = await repo.get_wellness(db, user["id"])
        w = dict(wellness) if wellness else {}

        if notify_type == "meal":
            new_val = 0 if w.get("meal_enabled", 1) else 1
            await repo.upsert_wellness(db, user["id"], meal_enabled=new_val)
            await callback.answer("Еда: " + ("вкл" if new_val else "выкл"))
        elif notify_type == "water":
            new_val = 0 if w.get("water_enabled", 0) else 1
            await repo.upsert_wellness(db, user["id"], water_enabled=new_val)
            await callback.answer("Вода: " + ("вкл" if new_val else "выкл"))
        elif notify_type == "affirm_menu":
            affirm_enabled = w.get("affirm_enabled", 0)
            affirm_hours_raw = w.get("affirm_hours", "[9]")
            try:
                import json
                affirm_hours = json.loads(affirm_hours_raw) if affirm_hours_raw else [9]
            except:
                affirm_hours = [9]
            affirm_hours.sort()
            
            # Helper to check active preset
            def is_preset(target):
                return affirm_hours == sorted(target)
                
            presets = {
                "morning": [9],
                "evening": [21],
                "both": [9, 21],
                "allday": [9, 13, 17, 21]
            }
            
            # Find active mode
            active_mode = "custom"
            for mode, hours in presets.items():
                if is_preset(hours):
                    active_mode = mode
                    break
            
            def btn(label, mode):
                check = "✅ " if active_mode == mode else ""
                return InlineKeyboardButton(text=f"{check}{label}", callback_data=f"settings:affirm:set_sched:{mode}")

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text=f"{'✅ Аффирмации включены' if affirm_enabled else '❌ Аффирмации выключены'}",
                        callback_data="settings:affirm_toggle"
                    )],
                    [InlineKeyboardButton(text="--- Частота отправки: ---", callback_data="settings:ignore")],
                    [btn("🌤️ Утром (09:00)", "morning")],
                    [btn("🌙 Вечером (21:00)", "evening")],
                    [btn("🌗 Утро и Вечер", "both")],
                    [btn("⚡ Весь день (4 раза)", "allday")],
                    [btn("⚙️ Своё время...", "custom")],
                    [InlineKeyboardButton(text="--- Настройки: ---", callback_data="settings:ignore")],
                    [InlineKeyboardButton(text="📝 Категории фраз", callback_data="settings:notify:affirm_cat_menu")],
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:notifications")],
                ]
            )
            
            await safe_edit(
                callback.message,
                "🌟 Аффирмации\n\n"
                "Выбери, как часто ты хочешь получать поддержку:",
                reply_markup=kb,
            )
            if not skip_answer:
                await callback.answer()
            return
        elif notify_type == "affirm_cat_menu":
            # Show category picker (logic moved from affirm_menu)
            affirm_enabled = w.get("affirm_enabled", 0)
            categories_raw = w.get("affirm_categories", '["motivation","calm"]')
            try:
                import json
                categories = json.loads(categories_raw) if categories_raw else []
            except:
                categories = ["motivation", "calm"]
            
            cat_labels = {
                "motivation": "💪 Мотивация",
                "calm": "🧘 Спокойствие", 
                "confidence": "🌟 Уверенность",
                "quotes": "📚 Цитаты"
            }
            
            cat_buttons = []
            for cat_key, cat_name in cat_labels.items():
                check = "☑️" if cat_key in categories else "☐"
                cat_buttons.append(
                    InlineKeyboardButton(
                        text=f"{check} {cat_name}",
                        callback_data=f"settings:affirm_cat:{cat_key}"
                    )
                )
            
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    cat_buttons[:2],
                    cat_buttons[2:],
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:notify:affirm_menu")],
                ]
            )
            await safe_edit(
                callback.message,
                "📝 Категории аффирмаций\n\n"
                "Отметь темы, которые тебе интересны:",
                reply_markup=kb,
            )
            if not skip_answer:
                await callback.answer()
            return
        else:
            if not skip_answer:
                await callback.answer()
            return

        # Обновляем меню уведомлений
        await _render_notifications_menu(callback, db)
        return
    elif action == "affirm_toggle":
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        wellness = await repo.get_wellness(db, user["id"])
        w = dict(wellness) if wellness else {}
        new_val = 0 if w.get("affirm_enabled", 0) else 1
        await repo.upsert_wellness(db, user["id"], affirm_enabled=new_val)
        await callback.answer("Аффирмации: " + ("вкл" if new_val else "выкл"))
        await settings_select(
            callback.model_copy(update={"data": "settings:notify:affirm_menu"}),
            state,
            db,
            skip_answer=True,
        )
        return
    elif action == "affirm_cat" and len(parts) >= 3:
        cat = parts[2]
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        wellness = await repo.get_wellness(db, user["id"])
        w = dict(wellness) if wellness else {}
        
        categories_raw = w.get("affirm_categories", '["motivation","calm"]')
        try:
            import json
            categories = json.loads(categories_raw) if categories_raw else []
        except:
            categories = ["motivation", "calm"]
        
        # Toggle category
        if cat in categories:
            categories.remove(cat)
        else:
            categories.append(cat)
        
        import json
        await repo.upsert_wellness(db, user["id"], affirm_categories=json.dumps(categories))
        
        # Обновляем клавиатуру с новыми галочками
        wellness = await repo.get_wellness(db, user["id"])
        w = dict(wellness) if wellness else {}
        affirm_enabled = w.get("affirm_enabled", 0)
        
        cat_labels = {
            "motivation": "💪 Мотивация",
            "calm": "🧘 Спокойствие", 
            "confidence": "🌟 Уверенность",
            "quotes": "📚 Цитаты"
        }
        
        cat_buttons = []
        for cat_key, cat_name in cat_labels.items():
            check = "☑️" if cat_key in categories else "☐"
            cat_buttons.append(
                InlineKeyboardButton(
                    text=f"{check} {cat_name}",
                    callback_data=f"settings:affirm_cat:{cat_key}"
                )
            )
        
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"{'✅ Включены' if affirm_enabled else '❌ Выключены'}",
                    callback_data="settings:affirm_toggle"
                )],
                cat_buttons[:2],  # Мотивация, Спокойствие
                cat_buttons[2:],  # Уверенность, Цитаты
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:notifications")],
            ]
        )
        
        try:
            await callback.message.edit_reply_markup(reply_markup=kb)
        except:
            pass
        
        await callback.answer(f"{cat}: {'добавлено' if cat in categories else 'убрано'}")
        return

    elif action == "affirm" and len(parts) >= 4 and parts[2] == "set_sched":
        mode = parts[3]
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        
        if mode == "custom":
            await state.set_state(SettingsState.affirm_custom_time)
            await callback.message.answer(
                "⚙️ Настройка времени\n\n"
                "Напиши часы, в которые хочешь получать аффирмации (от 0 до 23), через запятую или пробел.\n"
                "Например: <code>9 14 20</code> или <code>10</code>.",
                reply_markup=_cancel_settings_keyboard(),
            )
            if not skip_answer:
                await callback.answer()
            return
            
        presets = {
            "morning": [9],
            "evening": [21],
            "both": [9, 21],
            "allday": [9, 13, 17, 21]
        }
        
        new_hours = presets.get(mode, [9])
        import json
        await repo.upsert_wellness(db, user["id"], affirm_hours=json.dumps(new_hours), affirm_frequency=mode)
        
        # Refresh menu to show checkmark
        new_cb = callback.model_copy(update={"data": "settings:notify:affirm_menu"})
        await settings_select(new_cb, state, db, skip_answer=skip_answer)
        return
    elif action == "expiry":
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        wellness = await repo.get_wellness(db, user["id"])
        current_days = int((wellness or {}).get("expiring_window_days", 3))
        await state.set_state(SettingsState.expiry)
        await callback.message.answer(
            "Через сколько дней до конца срока считать, что продукт «скоро испортится»?\n"
            f"Сейчас: около {current_days} дн.\n"
            "Введи целое число от 1 до 30, например 3 или 5.",
            reply_markup=_cancel_settings_keyboard(),
        )
    elif action == "mealprof":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Обычный", callback_data="settings:mealprof:set:omnivore"),
                    InlineKeyboardButton(text="Вегетарианец", callback_data="settings:mealprof:set:vegetarian"),
                    InlineKeyboardButton(text="Веган", callback_data="settings:mealprof:set:vegan"),
                ],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:profile")]
            ]
        )
        await safe_edit(callback.message, "Выбери профиль питания:", reply_markup=kb)
    elif action == "adhd":
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        enabled = not bool(user.get("adhd_mode"))
        await repo.toggle_adhd(db, user["id"], enabled)
        updated_user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        await safe_edit(
            callback.message,
            _settings_main_text(updated_user),
            reply_markup=settings_keyboard(updated_user),
        )
        await callback.answer("Щадящий фокус: " + ("вкл" if enabled else "выкл"))
        return
    elif action == "rt" and len(parts) >= 3:
        routine_key = parts[2]
        routine_label = ROUTINE_GENITIVE.get(routine_key, routine_key)
        await state.update_data(routine_key=routine_key)
        await state.set_state(SettingsState.routine_time)
        await callback.message.answer(
            f"Новое время для {routine_label} (HH:MM, например 07:30).",
            reply_markup=_cancel_settings_keyboard(),
        )
    if not skip_answer:
        await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("settings:mealprof:set:"))
async def settings_meal_profile(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    parts = callback.data.split(":")
    profile = parts[3] if len(parts) > 3 else "omnivore"
    if profile not in {"omnivore", "vegetarian", "vegan"}:
        await callback.answer("Не поняла профиль.", show_alert=True)
        return
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.upsert_wellness(db, user["id"], meal_profile=profile)
    await callback.answer("Сохранено")
    await settings_select(
        callback.model_copy(update={"data": "settings:profile"}),
        state,
        db,
        skip_answer=True,
    )


@router.callback_query(lambda c: c.data and c.data.startswith("settings:affirm:set:"))
async def settings_affirm_mode(callback: types.CallbackQuery, db) -> None:
    _, _, _, mode = callback.data.split(":")
    if mode not in {"off", "morning", "evening", "both"}:
        await callback.answer()
        return
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.upsert_wellness(db, user["id"], affirm_mode=mode)
    labels = {
        "off": "выключены",
        "morning": "только утром",
        "evening": "только вечером",
        "both": "утром и вечером",
    }
    wellness = await repo.get_wellness(db, user["id"])
    current = (wellness or {}).get("affirm_mode", mode)
    await safe_edit(
        callback.message,
        "Могу иногда подкидывать короткую фразу поддержки.\n"
        f"Режим: {labels[current]}. Если станет слишком много — можно вернуть «Выкл».",
        reply_markup=_affirm_keyboard(current),
    )
    await callback.answer("Сохранено")


@router.message(SettingsState.timezone, lambda m: m.text and not m.text.startswith("/"))
async def settings_timezone(message: types.Message, state: FSMContext, db) -> None:
    if await _handle_settings_cancel(message, state, db):
        return
    tz = message.text.strip()
    computed_tz = None
    if not is_valid_timezone(tz):
        # попробуем трактовать как текущее локальное время пользователя
        hhmm = parse_hhmm(tz)
        if hhmm:
            try:
                now_utc = datetime.datetime.utcnow()
                hh, mm = hhmm.hour, hhmm.minute
                today = now_utc.date()
                local_dt = datetime.datetime.combine(today, datetime.time(hour=hh, minute=mm))
                utc_dt = datetime.datetime.combine(today, datetime.time(hour=now_utc.hour, minute=now_utc.minute))
                offset_minutes = int((local_dt - utc_dt).total_seconds() // 60)
                # нормализуем в диапазон -720..+720
                if offset_minutes > 720:
                    offset_minutes -= 1440
                if offset_minutes < -720:
                    offset_minutes += 1440
                sign = "+" if offset_minutes >= 0 else "-"
                hrs = abs(offset_minutes) // 60
                mins = abs(offset_minutes) % 60
                computed_tz = f"UTC{sign}{hrs}"
                if mins:
                    computed_tz += f":{mins:02d}"
                tz = computed_tz
            except Exception:
                pass
        if not computed_tz:
            await message.answer(
                texts.error(
                    "не совсем поняла. Можно прислать текущее время (HH:MM) или таймзону (Europe/Moscow, UTC+3)."
                ),
                reply_markup=_cancel_settings_keyboard(),
            )
            return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.update_user_timezone(db, user["id"], tz)
    await state.clear()
    await message.answer(
        f"Часовой пояс обновлён на {tz}.", reply_markup=main_menu_keyboard(user)
    )


@router.message(SettingsState.wake, lambda m: m.text and not m.text.startswith("/"))
async def settings_wake(message: types.Message, state: FSMContext, db) -> None:
    if await _handle_settings_cancel(message, state, db):
        return
    time_value = parse_hhmm(message.text.strip())
    if not time_value:
        await message.answer(
            texts.error("не распознала время. Напиши, например, 07:30."),
            reply_markup=_cancel_settings_keyboard(),
        )
        return
    hhmm = time_value.strftime("%H:%M")
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.update_user_wake(db, user["id"], hhmm)
    await state.clear()
    await message.answer(
        f"Время подъёма обновлено: {hhmm}.",
        reply_markup=main_menu_keyboard(user),
    )


@router.message(SettingsState.sleep, lambda m: m.text and not m.text.startswith("/"))
async def settings_sleep(message: types.Message, state: FSMContext, db) -> None:
    if await _handle_settings_cancel(message, state, db):
        return
    time_value = parse_hhmm(message.text.strip())
    if not time_value:
        await message.answer(
            texts.error("не распознала время. Напиши, например, 23:30."),
            reply_markup=_cancel_settings_keyboard(),
        )
        return
    hhmm = time_value.strftime("%H:%M")
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.update_user_sleep(db, user["id"], hhmm)
    await state.clear()
    await message.answer(
        f"Время отбоя обновлено: {hhmm}.",
        reply_markup=main_menu_keyboard(user),
    )


@router.message(SettingsState.goals, lambda m: m.text and not m.text.startswith("/"))
async def settings_goals(message: types.Message, state: FSMContext, db) -> None:
    if await _handle_settings_cancel(message, state, db):
        return
    text = message.text.strip()
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.update_user_goals(db, user["id"], text)
    await state.clear()
    await message.answer(
        "Цель обновлена. Я буду учитывать это в подсказках.",
        reply_markup=main_menu_keyboard(user),
    )


@router.message(SettingsState.expiry, lambda m: m.text and not m.text.startswith("/"))
async def settings_expiry(message: types.Message, state: FSMContext, db) -> None:
    if await _handle_settings_cancel(message, state, db):
        return
    raw = message.text.strip()
    try:
        days = int(raw)
        if days < 1 or days > 30:
            raise ValueError
    except Exception:
        await message.answer(
            texts.error("нужно целое число дней от 1 до 30, например 3 или 5."),
            reply_markup=_cancel_settings_keyboard(),
        )
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.upsert_wellness(db, user["id"], expiring_window_days=days)
    await state.clear()
    await message.answer(
        f"Хорошо, буду считать, что продукт «скоро испортится», если до конца срока осталось ≤ {days} дн.",
        reply_markup=main_menu_keyboard(user),
    )


@router.message(SettingsState.routine_time, lambda m: m.text and not m.text.startswith("/"))
async def settings_routine_time(message: types.Message, state: FSMContext, db) -> None:
    if await _handle_settings_cancel(message, state, db):
        return
    time_value = parse_hhmm(message.text.strip())
    if not time_value:
        await message.answer(
            texts.error("не распознала время. Напиши, например, 07:30."),
            reply_markup=_cancel_settings_keyboard(),
        )
        return
    hhmm = time_value.strftime("%H:%M")
    data = await state.get_data()
    routine_key = data.get("routine_key")
    if not routine_key:
        await message.answer("Не понял, какую рутину менять. Выбери снова /settings.")
        await state.clear()
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.update_user_routine_time(db, user["id"], routine_key, hhmm)
    await state.clear()
    routine_label = ROUTINE_GENITIVE.get(routine_key, routine_key)
    await message.answer(
        f"Время напоминания для {routine_label} обновлено: {hhmm}.",
        reply_markup=main_menu_keyboard(user),
    )


@router.message(SettingsState.household_join, lambda m: m.text and not m.text.startswith("/"))
async def settings_household_join(message: types.Message, state: FSMContext, db) -> None:
    if await _handle_settings_cancel(message, state, db):
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    code = (message.text or "").strip()
    if not code:
        await message.answer(
            "Код пустой. Пришли, пожалуйста, код, который тебе дал партнёр (буквы и цифры).",
            reply_markup=_cancel_settings_keyboard(),
        )
        return
    from db import repositories as repo_mod

    household = await repo_mod.get_household_by_code(db, code)
    if not household:
        await message.answer(
            "Я не нашла дом с таким кодом. Проверь, не перепутались ли буквы/цифры, или попроси партнёра показать код ещё раз.",
            reply_markup=main_menu_keyboard(user),
        )
        await state.clear()
        return
    await repo_mod.set_user_household(db, user["id"], household["id"])
    await state.clear()
    actor_name = html.escape(str(user.get("name") or "Партнёр"))
    await household_service.notify_household_partners(
        getattr(message, "bot", None),
        db,
        dict(user),
        f"🏠 <b>{actor_name}</b> присоединился(-ась) к общему дому по коду.",
    )
    await message.answer(
        "Подключила тебя к общему дому. Теперь холодильник, список покупок, общие рецепты, меню и общие счета синхронизированы.",
        reply_markup=main_menu_keyboard(user),
    )


@router.message(SettingsState.affirm_custom_time, lambda m: m.text and not m.text.startswith("/"))
async def settings_affirm_custom_time(message: types.Message, state: FSMContext, db) -> None:
    if await _handle_settings_cancel(message, state, db):
        return
    text = (message.text or "").replace(",", " ").replace(";", " ")
    parts = text.split()
    hours = []
    try:
        seen = set()
        for p in parts:
            h = int(p)
            if not (0 <= h <= 23):
                raise ValueError
            if h not in seen:
                hours.append(h)
                seen.add(h)
        if not hours:
            raise ValueError
        hours.sort()
    except ValueError:
        await message.answer(
            texts.error("введи корректные часы от 0 до 23, например 9 14 20"),
            reply_markup=_cancel_settings_keyboard(),
        )
        return

    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    import json
    await repo.upsert_wellness(db, user["id"], affirm_hours=json.dumps(hours), affirm_frequency="custom")

    await state.clear()
    await message.answer(
        f"✅ Принято! Аффирмации будут приходить в эти часы: {', '.join(map(str, hours))}.",
        reply_markup=main_menu_keyboard(user)
    )
