import datetime

from aiogram import Router, types
from aiogram.filters import Command

from keyboards.common import main_menu_keyboard

router = Router()

ZONES = [
    ("Кухня", ["Плита/рабочая поверхность", "Раковина и стол", "Холодильник — быстрая проверка"]),
    ("Ванная/санузел", ["Раковина/зеркало", "Унитаз/сиденье", "Пол/коврик"]),
    ("Спальня/кровать", ["Постель/проветривание", "Поверхности от пыли", "Быстрый пол"]),
    ("Вход/прихожая", ["Мусор/пакеты", "Обувь/коврик", "Пыль/пол"]),
    ("Хаос-угол", ["Собрать мусор", "Разобрать одну стопку вещей", "Протереть поверхность"]),
]


@router.message(Command("zones"))
async def zones(message: types.Message) -> None:
    week_num = datetime.date.today().isocalendar()[1]
    idx = (week_num - 1) % len(ZONES)
    zone_name, tasks = ZONES[idx]
    tasks_text = "\n".join(f"• {t}" for t in tasks)
    await message.answer(
        f"Зона недели: {zone_name}\nСделай хотя бы 1–2 пункта:\n{tasks_text}",
        reply_markup=main_menu_keyboard(),
    )
