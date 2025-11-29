from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from utils.mom_tips import pick_tip, find_tip_by_tag
from keyboards.common import main_menu_keyboard

router = Router()


class AskMomLaundry(StatesGroup):
    item = State()
    dirt = State()


class AskMomCook(StatesGroup):
    ingredients = State()
    profile = State()


def ask_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤯 Не знаю, с чего начать", callback_data="ask:start:panic")],
            [InlineKeyboardButton(text="🧺 Стирка и одежда", callback_data="ask:start:laundry")],
            [InlineKeyboardButton(text="🍳 Кухня и готовка", callback_data="ask:start:cook")],
            [InlineKeyboardButton(text="🧹 Уборка и порядок", callback_data="ask:start:cleaning")],
            [InlineKeyboardButton(text="💰 Деньги и счета", callback_data="ask:start:money")],
            [InlineKeyboardButton(text="🏠 Квартира/переезд", callback_data="ask:start:home")],
            [InlineKeyboardButton(text="❤️‍🩹 Здоровье/энергия", callback_data="ask:start:health")],
            [InlineKeyboardButton(text="✍️ Свой вопрос", callback_data="ask:start:free")],
        ]
    )


async def start_cook_flow(message: types.Message, state: FSMContext) -> None:
    await state.set_state(AskMomCook.profile)
    await message.answer(
        "Выбери профиль питания:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Обычный", callback_data="cookprof:omnivore"),
                    InlineKeyboardButton(text="Вегетарианец", callback_data="cookprof:vegetarian"),
                    InlineKeyboardButton(text="Веган", callback_data="cookprof:vegan"),
                ]
            ]
        ),
    )


@router.message(Command("ask_mom"))
@router.message(lambda m: m.text and "спроси" in m.text.lower())
async def ask_mom_entry(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    # если пользователь задал вопрос текстом сразу после команды
    text = message.text.replace("/ask_mom", "").strip()
    if text:
        tip = find_tip_by_tag(text)
        if tip:
            lines = [f"{tip.get('title','Совет')}:"]
            lines += [f"• {b}" for b in tip.get("body", [])]
            await message.answer("\n".join(lines), reply_markup=ask_menu_keyboard())
            return
    await message.answer(
        "Напиши, в чём проблема, или выбери тему. Я отвечу по‑маминому: коротко и без шейминга.",
        reply_markup=ask_menu_keyboard(),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("ask:start"))
async def ask_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    _, _, kind = callback.data.split(":")
    if kind == "panic":
        await state.clear()
        await callback.message.answer(
            "Давай выберем одно простое:\n"
            "1) Вынеси мусор.\n"
            "2) Попей воды/перекусить что-то простое.\n"
            "3) Умойся/почисти зубы.\n"
            "Сделай любой пункт — это уже победа. Потом вернись за следующим.",
            reply_markup=main_menu_keyboard(),
        )
    elif kind == "laundry":
        await state.set_state(AskMomLaundry.item)
        await callback.message.answer(
            "Что стираем?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Тёмные повседневные", callback_data="ask:laundry:item:dark")],
                    [InlineKeyboardButton(text="Светлые/белые", callback_data="ask:laundry:item:light")],
                    [InlineKeyboardButton(text="Постельное", callback_data="ask:laundry:item:bed")],
                    [InlineKeyboardButton(text="Полотенца", callback_data="ask:laundry:item:towel")],
                ]
            ),
        )
    elif kind == "cleaning":
        await state.clear()
        await send_tip(callback.message, "cleaning")
    elif kind == "cook":
        await start_cook_flow(callback.message, state)
    elif kind == "odor":
        await state.clear()
        await callback.message.answer(
            "Запахи: выбери проблему.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Стиралка/бельё", callback_data="ask:odor:wash")],
                    [InlineKeyboardButton(text="Кухня/раковина/холод", callback_data="ask:odor:kitchen")],
                    [InlineKeyboardButton(text="Ванная/туалет/сливы", callback_data="ask:odor:bath")],
                    [InlineKeyboardButton(text="Общий запах в комнате", callback_data="ask:odor:room")],
                ]
            ),
        )
    elif kind == "money":
        await state.clear()
        await send_tip(callback.message, "money")
    elif kind == "home":
        await state.clear()
        await send_tip(callback.message, "home")
    elif kind == "health":
        await state.clear()
        await send_tip(callback.message, "health")
    elif kind == "free":
        await state.clear()
        await callback.message.answer("Напиши свой вопрос текстом. Я отвечу по ситуации.", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("ask:odor:"))
async def ask_odor(callback: types.CallbackQuery) -> None:
    kind = callback.data.split(":")[2]
    if kind == "wash":
        text = (
            "Запах в стиралке — коротко:\n"
            "• Протри резинку дверцы (там вода и грязь).\n"
            "• Пустая стирка 60° с 50–100 мл уксуса/спецсредства.\n"
            "• Почисти фильтр снизу.\n"
            "• Держи дверцу и лоток приоткрытыми, не пересыпай порошок."
        )
    elif kind == "kitchen":
        text = (
            "Запах на кухне/в раковине:\n"
            "• Вымой посуду, убери остатки еды.\n"
            "• Пролей слив: кипяток + 1–2 ст.л. соды, через 5 мин уксус, потом снова кипяток.\n"
            "• Протри/замени тряпки и губки.\n"
            "• Вынеси мусор, протри ведро.\n"
            "• Проветри 5–10 минут."
        )
    elif kind == "room":
        text = (
            "Запах в комнате:\n"
            "• Проветривание 5–10 минут (если можно — сквозняк).\n"
            "• Мусор вынести, ведро протереть.\n"
            "• Встряхни плед/одежду, убери в стирку то, что пахнет.\n"
            "• Пройди пылесосом/влажной тряпкой по проходам.\n"
            "• Если сыро — дай высохнуть: приоткрытые окна или тёплый обогрев на чуть-чуть."
        )
    else:
        text = (
            "Запах в ванной/туалете:\n"
            "• Ершик с чистящим средством по унитазу (и под ободком).\n"
            "• Слив: кипяток + сода + уксус, потом снова кипяток.\n"
            "• Пол/коврик протереть и высушить.\n"
            "• Проверить вентиляцию: листок должен притягиваться.\n"
            "• Оставь дверь приоткрытой для проветривания."
        )
    await callback.message.answer(text, reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("cookprof:"))
async def cook_profile(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    _, profile = callback.data.split(":")
    await state.update_data(profile=profile)
    from db import repositories as repo
    from utils.user import ensure_user

    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.upsert_wellness(db, user["id"], meal_profile=profile)
    await state.set_state(AskMomCook.ingredients)
    await callback.message.answer(
        "Напиши, что у тебя есть (через запятую). Пример: макароны, помидор, яйца.\n"
        "Если лень перечислять — отправь любое слово, пришлю базовую шпаргалку с временем варки/жарки.",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer()


def cook_suggestion(ingredients_text: str, profile: str = "omnivore") -> str:
    ing = ingredients_text.lower()
    have = set([w.strip() for w in ing.replace(",", " ").split() if w.strip()])

    def has_any(words):
        return any(w in ing for w in words)

    # Быстрая шпаргалка, если почти ничего не названо
    if len(have) <= 1:
        protein = (
            "фасоль/нут/тофу"
            if profile == "vegan"
            else "яйца/творог/сыр/бобовые"
            if profile == "vegetarian"
            else "яйцо/тунец/фасоль/сыр/тофу"
        )
        return (
            "Базовый конструктор:\n"
            "1) Основа: макароны/рис/гречка/картошка. Доведи до готовности.\n"
            "2) Овощи: обжарь всё, что есть (лук/морковь/перец/заморозка).\n"
            f"3) Белок: {protein} — добавь к овощам.\n"
            "4) Соедини с основой, соль/перец. Соусы: соевый, сметана или масло.\n\n"
            "Шпаргалка по времени:\n"
            "• Макароны: кипящая вода, соль, 8–12 мин, помешивай.\n"
            "• Рис: промыть, вода 1:2, кипятить 5 мин, затем 10–15 мин на малом огне.\n"
            "• Гречка: вода 1:2, после закипания 15–20 мин на малом огне.\n"
            "• Картошка: кубиками жарить 10–15 мин; варить 15–20 мин.\n"
            "• Яйца: всмятку 5–6 мин, вкрутую 8–10 мин.\n"
            "• Пельмени: в кипящую воду, после всплытия 7–10 мин.\n"
            "• Жарка без прилипания: разогрей сковородку, чуть масла, не перегружай, помешивай.\n"
        )

    if has_any(["макарон", "паста"]) and has_any(["томат", "помидор"]):
        return "Паста с томатом: обжарь лук/чеснок, добавь томаты, соль/перец, провари 10 минут. Смешай с макаронами. Добавь белок по профилю."
    if has_any(["рис"]) and has_any(["яйц", "куриц", "тофу", "фасоль", "нут"]):
        return "Рис+белок: обжарь лук/морковь, добавь белок (яйца/курица/тофу/фасоль), потом готовый рис, соевый соус. 10–12 минут."
    if has_any(["картош", "картоф"]):
        return "Картошка: нарежь, обжарь 10–15 мин. Добавь лук/морковь и белок (яйца/тофу/фасоль/курица). Соль, перец, зелень."

    return "Собери набор из основы+овощей+белка. Если сомневаешься — напиши подробнее, подскажу конкретнее."


async def send_tip(message: types.Message, category: str, tip_id: str | None = None) -> None:
    if tip_id:
        tip = find_tip_by_tag(tip_id) or pick_tip(category)
    else:
        tip = pick_tip(category)
    if not tip:
        await message.answer("Пока нет готового совета на эту тему. Спроси текстом — отвечу по ситуации.", reply_markup=ask_menu_keyboard())
        return
    lines = [f"{tip.get('title','Совет')}:"]
    for b in tip.get("body", []):
        lines.append(f"• {b}")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Ещё совет", callback_data=f"ask:tip:{category}")],
            [InlineKeyboardButton(text="Сделать напоминанием", callback_data=f"ask:tiprem:{category}:{tip.get('id','')}")],
            [InlineKeyboardButton(text="Назад к темам", callback_data="ask:back")],
        ]
    )
    await message.answer("\n".join(lines), reply_markup=kb)


@router.message(AskMomCook.ingredients)
async def ask_cook(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    profile = data.get("profile", "omnivore")
    suggestion = cook_suggestion(message.text, profile=profile)
    await state.clear()
    await message.answer(suggestion, reply_markup=main_menu_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("ask:tip:"))
async def ask_tip_more(callback: types.CallbackQuery) -> None:
    _, _, category = callback.data.split(":")
    await send_tip(callback.message, category)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data == "ask:back")
async def ask_back(callback: types.CallbackQuery) -> None:
    await callback.message.answer(
        "Выбери тему или задай вопрос текстом. Я отвечу по‑маминому.",
        reply_markup=ask_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("ask:tiprem:"))
async def ask_tip_reminder(callback: types.CallbackQuery, db) -> None:
    _, _, category, tip_id = callback.data.split(":")
    from utils.user import ensure_user
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    title = "Совет по дому"
    tip = find_tip_by_tag(tip_id) or pick_tip(category)
    if tip:
        title = tip.get("title", title)
    # ставим напоминание на завтра 10:00, раз в неделю
    today = datetime.date.today().isoformat()
    rid = await repo.create_custom_reminder(
        db,
        user_id=user["id"],
        title=title,
        reminder_time="10:00",
        frequency_days=7,
    )
    await repo.set_custom_reminder_sent(db, rid, today)  # чтобы первое пришло завтра
    await callback.message.answer(
        f"Сделала напоминание раз в неделю: «{title}» в 10:00. Первый раз придёт завтра.",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer("Добавила напоминание")


@router.callback_query(lambda c: c.data and c.data.startswith("ask:laundry:item"))
async def ask_laundry_item(callback: types.CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state != AskMomLaundry.item.state:
        await callback.answer()
        return
    _, _, _, item = callback.data.split(":")
    await state.update_data(item=item)
    await state.set_state(AskMomLaundry.dirt)
    await callback.message.answer(
        "Насколько грязные?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Немного", callback_data="ask:laundry:dirt:light"),
                    InlineKeyboardButton(text="Сильно", callback_data="ask:laundry:dirt:hard"),
                ]
            ]
        ),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("ask:laundry:dirt"))
async def ask_laundry_dirt(callback: types.CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state != AskMomLaundry.dirt.state:
        await callback.answer()
        return
    _, _, _, dirt = callback.data.split(":")
    data = await state.get_data()
    item = data.get("item", "dark")
    await state.clear()
    modes = {
        "dark": "Синтетика/Деликатная 30–40°, отжим 600–800.",
        "light": "Хлопок/Повседневный 40°, отжим 800–1000. Если вещь нежная — Синтетика 30–40°.",
        "bed": "Хлопок 40–60°, отжим 800–1000. Если боишься линьки — 40°.",
        "towel": "Хлопок 60°, отжим 800–1000. Если новые/яркие — первые разы 40°.",
    }
    dirt_text = "Сильно грязные — можно добавить предствирку или замочить на 20–30 минут." if dirt == "hard" else "Обычная стирка без замачивания."
    load = "Барабан не больше 2/3, застёжки застегнуть, вещи встряхнуть."
    powder = (
        "Лоток: «II» — порошок/гель, «I» — только если есть предствирка, цветок — кондиционер. "
        "Если лотка нет — налей гель в колпачок и положи в барабан."
    )
    symbols = "Если не знаешь, что нажать: выбери Синтетику/Деликатную 30–40°, отжим 600–800 — самый безопасный режим."
    text = (
        "Стираем бережно, без стресса:\n"
        f"Режим: {modes.get(item, modes['dark'])}\n"
        f"{dirt_text}\n"
        f"{load}\n"
        f"{powder}\n"
        f"{symbols}\n"
        "Порошок: 1 мерный колпак или по инструкции, не пересыпай.\n"
        "После стирки: достань сразу, встряхни и развесь. Не держи в барабане."
    )
    await callback.message.answer(text, reply_markup=main_menu_keyboard())
    await callback.answer()
