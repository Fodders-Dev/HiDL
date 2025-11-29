import asyncio
import datetime
import os
from typing import Any, Dict, List

import aiosqlite
from db.knowledge_seed import VEGAN_TAG, VEGETARIAN_TAG


def _sqlite_path(database_url: str) -> str:
    """Extract SQLite file path from URL-like string."""
    if database_url.startswith("sqlite:///"):
        return database_url.replace("sqlite:///", "", 1)
    return database_url


async def connect(database_url: str) -> aiosqlite.Connection:
    """Open SQLite connection with row factory."""
    path = _sqlite_path(database_url)
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON;")
    return conn


async def init_db(conn: aiosqlite.Connection) -> None:
    """Create tables and seed minimal data."""
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            name TEXT NOT NULL,
            timezone TEXT NOT NULL,
            wake_up_time TEXT NOT NULL,
            sleep_time TEXT NOT NULL,
            strictness TEXT DEFAULT 'neutral',
            goals TEXT DEFAULT '',
            pause_until TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS routines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            routine_key TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            default_time TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS routine_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            routine_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (routine_id) REFERENCES routines(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS user_routines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            routine_id INTEGER NOT NULL,
            reminder_time TEXT NOT NULL,
            last_sent_date TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (routine_id) REFERENCES routines(id) ON DELETE CASCADE,
            UNIQUE(user_id, routine_id)
        );

        CREATE TABLE IF NOT EXISTS user_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            routine_id INTEGER NOT NULL,
            routine_date TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (routine_id) REFERENCES routines(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS knowledge_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            steps TEXT NOT NULL,
            created_at TEXT NOT NULL,
            tags TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS custom_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            reminder_time TEXT NOT NULL,
            frequency_days INTEGER DEFAULT 1,
            target_weekday INTEGER,
            last_sent_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS custom_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reminder_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reminder_date TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (reminder_id) REFERENCES custom_reminders(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS wellness_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            water_enabled INTEGER DEFAULT 0,
            meal_enabled INTEGER DEFAULT 0,
            focus_mode INTEGER DEFAULT 0,
            water_last_key TEXT DEFAULT '',
            meal_last_key TEXT DEFAULT '',
            focus_work INTEGER DEFAULT 20,
            focus_rest INTEGER DEFAULT 10,
            water_times TEXT DEFAULT '11:00,16:00',
            meal_times TEXT DEFAULT '13:00,19:00',
            tone TEXT DEFAULT 'neutral',
            meal_profile TEXT DEFAULT 'omnivore',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id)
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            monthly_limit REAL DEFAULT 0,
            payday_day INTEGER DEFAULT 1,
            food_budget REAL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id)
        );

        CREATE TABLE IF NOT EXISTS budget_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            limit_amount REAL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, category)
        );

        CREATE TABLE IF NOT EXISTS weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            weight REAL NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            amount REAL DEFAULT 0,
            day_of_month INTEGER DEFAULT 1,
            last_paid_month TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, title)
        );

        CREATE TABLE IF NOT EXISTS regular_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            frequency_days INTEGER NOT NULL,
            last_done_date TEXT,
            next_due_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )
    await ensure_columns(conn)
    await seed_routines(conn)
    await seed_knowledge(conn)
    await conn.commit()


async def ensure_columns(conn: aiosqlite.Connection) -> None:
    """Ensure optional columns exist for backward compatibility."""
    users_info = await conn.execute_fetchall("PRAGMA table_info(users);")
    user_cols = {row["name"] for row in users_info}
    if "pause_until" not in user_cols:
        await conn.execute("ALTER TABLE users ADD COLUMN pause_until TEXT;")

    articles_info = await conn.execute_fetchall("PRAGMA table_info(knowledge_articles);")
    article_cols = {row["name"] for row in articles_info}
    if "tags" not in article_cols:
        await conn.execute("ALTER TABLE knowledge_articles ADD COLUMN tags TEXT DEFAULT '';")

    reminders_info = await conn.execute_fetchall("PRAGMA table_info(custom_reminders);")
    reminders_cols = {row["name"] for row in reminders_info}
    if reminders_info:
        if "target_weekday" not in reminders_cols:
            try:
                await conn.execute("ALTER TABLE custom_reminders ADD COLUMN target_weekday INTEGER;")
            except Exception:
                pass

    wellness_info = await conn.execute_fetchall("PRAGMA table_info(wellness_settings);")
    wellness_cols = {row["name"] for row in wellness_info}
    if wellness_info:
        if "water_last_key" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN water_last_key TEXT DEFAULT '';")
        if "meal_last_key" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN meal_last_key TEXT DEFAULT '';")
        if "focus_work" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN focus_work INTEGER DEFAULT 20;")
        if "focus_rest" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN focus_rest INTEGER DEFAULT 10;")
        if "tone" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN tone TEXT DEFAULT 'neutral';")
        if "water_times" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN water_times TEXT DEFAULT '11:00,16:00';")
        if "meal_times" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN meal_times TEXT DEFAULT '13:00,19:00';")
        if "meal_profile" not in wellness_cols:
            await conn.execute("ALTER TABLE wellness_settings ADD COLUMN meal_profile TEXT DEFAULT 'omnivore';")

    weights_info = await conn.execute_fetchall("PRAGMA table_info(weights);")
    if not weights_info:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                weight REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )

    bills_info = await conn.execute_fetchall("PRAGMA table_info(bills);")
    if not bills_info:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                amount REAL DEFAULT 0,
                day_of_month INTEGER DEFAULT 1,
                last_paid_month TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, title)
            );
            """
        )
    users_info2 = await conn.execute_fetchall("PRAGMA table_info(users);")
    user_cols2 = {row["name"] for row in users_info2}
    if "points_total" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN points_total INTEGER DEFAULT 0;")
    if "points_month" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN points_month INTEGER DEFAULT 0;")
    if "last_points_reset" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN last_points_reset TEXT DEFAULT '';")
    if "height_cm" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN height_cm REAL DEFAULT 0;")
    if "weight_goal" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN weight_goal TEXT DEFAULT '';")
    if "weight_target" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN weight_target REAL DEFAULT 0;")
    if "adhd_mode" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN adhd_mode INTEGER DEFAULT 0;")
    if "last_weight_prompt" not in user_cols2:
        await conn.execute("ALTER TABLE users ADD COLUMN last_weight_prompt TEXT DEFAULT '';")
    for col in ["last_care_dentist", "last_care_vision", "last_care_firstaid", "last_care_brush"]:
        if col not in user_cols2:
            await conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT '';")
    points_info = await conn.execute_fetchall("PRAGMA table_info(points_log);")
    if not points_info:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS points_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                points INTEGER NOT NULL,
                local_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )
    reg_info = await conn.execute_fetchall("PRAGMA table_info(regular_tasks);")
    if not reg_info:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS regular_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                frequency_days INTEGER NOT NULL,
                last_done_date TEXT,
                next_due_date TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
            """
        )
    budgets_info = await conn.execute_fetchall("PRAGMA table_info(budgets);")
    budget_cols = {row["name"] for row in budgets_info}
    if "payday_day" not in budget_cols:
        try:
            await conn.execute("ALTER TABLE budgets ADD COLUMN payday_day INTEGER DEFAULT 1;")
        except Exception:
            pass
    if "food_budget" not in budget_cols:
        try:
            await conn.execute("ALTER TABLE budgets ADD COLUMN food_budget REAL DEFAULT 0;")
        except Exception:
            pass


async def seed_routines(conn: aiosqlite.Connection) -> None:
    """Insert or extend basic routine templates."""
    routines_data: List[Dict[str, Any]] = [
        {
            "routine_key": "morning",
            "title": "Утро",
            "default_time": "07:30",
            "items": [
                "Умыться и почистить зубы",
                "Заправить кровать и открыть окно",
                "Выпить воды",
                "Позавтракать чем угодно, не кофе",
                "Проверь, что есть чистые вещи на день",
            ],
        },
        {
            "routine_key": "day",
            "title": "День",
            "default_time": "13:00",
            "items": [
                "Пообедать без фастфуда",
                "Выпить воды",
                "Выйти на улицу хотя бы на 15 минут",
                "Разобрать посуду/кружки со стола",
            ],
        },
        {
            "routine_key": "evening",
            "title": "Вечер",
            "default_time": "21:30",
            "items": [
                "Помыть посуду",
                "Сложить вещи по местам",
                "Короткий душ и гигиена",
                "Подготовить одежду на завтра",
                "Проветрить комнату перед сном",
            ],
        },
    ]

    existing_routines = await conn.execute_fetchall(
        "SELECT id, routine_key FROM routines;"
    )
    routine_map = {row["routine_key"]: row["id"] for row in existing_routines}

    for routine in routines_data:
        if routine["routine_key"] in routine_map:
            routine_id = routine_map[routine["routine_key"]]
        else:
            cursor = await conn.execute(
                "INSERT INTO routines (routine_key, title, default_time) VALUES (?, ?, ?)",
                (routine["routine_key"], routine["title"], routine["default_time"]),
            )
            routine_id = cursor.lastrowid

        existing_items = await conn.execute_fetchall(
            "SELECT title, sort_order FROM routine_items WHERE routine_id = ?",
            (routine_id,),
        )
        existing_titles = {row["title"] for row in existing_items}
        next_order = max([row["sort_order"] for row in existing_items], default=0) + 1
        for title in routine["items"]:
            if title in existing_titles:
                continue
            await conn.execute(
                "INSERT INTO routine_items (routine_id, title, sort_order) VALUES (?, ?, ?)",
                (routine_id, title, next_order),
            )
            next_order += 1

    await conn.commit()


async def seed_knowledge(conn: aiosqlite.Connection) -> None:
    """Insert or extend knowledge base entries."""
    now = datetime.datetime.utcnow().isoformat()
    articles = [
        {
            "category": "Кухня",
            "title": "Быстрый завтрак за 7 минут",
            "content": "Если совсем нет сил, сделай базовый завтрак, чтобы не жить на кофе.",
            "steps": "\n".join(
                [
                    "Поставь чайник или вскипяти воду.",
                    "Две опции: овсянка быстрого приготовления или яичница.",
                    "Овсянка: залей кипятком, добавь банан/яблоко/орехи, щепотку соли.",
                    "Яичница: сковородка, немного масла, два яйца, щепотка соли. Пока готовится — нарежь хлеб/овощи.",
                    "Выпей стакан воды перед едой.",
                ]
            ),
            "tags": f"{VEGETARIAN_TAG},{VEGAN_TAG}",
        },
        {
            "category": "Кухня",
            "title": "Ужин из трёх ингредиентов",
            "content": "Простой ужин без заморочек: основа + овощ + вкусное сверху.",
            "steps": "\n".join(
                [
                    "Выбери основу: макароны, рис или кус-кус — ставь вариться по инструкции.",
                    "Овощ: нарежь что есть (заморозка ок): лук/морковь/перец/стручковая фасоль. Быстро обжарь на масле 5–7 минут.",
                    "Вкусное сверху: тунец из банки, яйцо, сыр или фасоль из банки. Добавь к овощам.",
                    "Соединяй основу с овощами, посоли/поперчи. Если есть — добавь соевый соус или ложку сметаны.",
                    "Съешь тёплым и выпей воды. Остатки — в контейнер в холодильник.",
                ]
            ),
            "tags": f"{VEGETARIAN_TAG}",
        },
        {
            "category": "Кухня",
            "title": "Как хранить продукты, чтобы не тухло",
            "content": "Мини-гайд по холодильнику и сухим полкам.",
            "steps": "\n".join(
                [
                    "Молочку, мясо, рыбу держи на средней/нижней полке, не на дверце.",
                    "Овощи-фрукты — в ящики. Томаты и бананы лучше снаружи, не в холодильнике.",
                    "Хлеб — в пакете/контейнере, срок 2–3 дня. Остальное в морозилку порезанным.",
                    "Готовую еду — в контейнеры, подписывай дату. Если сомневаешься — выбрось.",
                    "Раз в неделю проверяй: вытереть подтёки, убрать старое, составить список докупок.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Стирка",
            "title": "Постирать тёмные вещи",
            "content": "Базовый сценарий для чёрных футболок, носков и джинс.",
            "steps": "\n".join(
                [
                    "Отсортируй: только тёмные вещи без белого. Проверяй карманы.",
                    "Загрузи барабан не больше чем на 2/3.",
                    "Порошок/гель: 1 мерный колпак или по инструкции, не пересыпай.",
                    "Режим: 'Хлопок' или 'Повседневный', температура 30–40°, отжим 800–1000.",
                    "После стирки сразу развесь, не держи в барабане.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Стирка",
            "title": "Постирать постельное",
            "content": "Чистое постельное = лучше спать и меньше пыли.",
            "steps": "\n".join(
                [
                    "Отсортируй: только постельное бельё и наволочки, без одежды.",
                    "Застегни пододеяльник/молнии, выверни наволочки.",
                    "Режим: 'Хлопок' или 'Постельное', температура 40–60°, отжим 800–1000.",
                    "Порошок/гель: по инструкции, не пересыпай. Кондиционер — по желанию.",
                    "После стирки сразу развесь, встряхни, суши до конца, потом сложи.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Стирка",
            "title": "Если вещи пахнут затхлостью",
            "content": "Как спасти вещи, если после стирки неприятный запах.",
            "steps": "\n".join(
                [
                    "Перестирай при 40–60° с 50–100 мл уксуса или спец-средством от запаха.",
                    "Не перегружай барабан и не пересыпай порошок.",
                    "Сразу развесь после стирки, не оставляй в машинке.",
                    "Проверь фильтр и резинку стиралки — там копится вода и грязь.",
                    "Дай машинке просохнуть с открытой дверцей и лотком.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Уборка",
            "title": "Быстрая уборка за 15 минут",
            "content": "Минимальный порядок без героизма.",
            "steps": "\n".join(
                [
                    "Запусти таймер на 15 минут.",
                    "Собери мусор и вынеси пакет.",
                    "Убери посуду в раковину/посудомойку, быстро сполосни тарелки.",
                    "Протри стол/рабочую поверхность влажной тряпкой.",
                    "Пройдись влажной салфеткой по раковине и крану.",
                    "Если успеваешь — быстро пройтись пылесосом по проходам.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Уборка",
            "title": "Чистая ванная без напряга",
            "content": "Быстрый цикл ухода за санузлом, чтобы не зарастал.",
            "steps": "\n".join(
                [
                    "Побрызгай средство для ванны/раковины/унитаза, дай постоять пару минут.",
                    "Пока ждёшь — убери лишние вещи, вытри зеркало сухой салфеткой.",
                    "Пройди губкой/тряпкой по раковине, крану, поверхности. Смывай водой.",
                    "Йорш + средство в унитаз, потом смой. Протри сиденье и кнопку сверху влажной салфеткой.",
                    "Вымой/замени тряпку, проветри. Добавь рулон бумаги и чистое полотенце, если надо.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Уборка",
            "title": "Разбор завалов за 10 минут",
            "content": "Когда всё валяется, но сил нет.",
            "steps": "\n".join(
                [
                    "Запусти таймер на 10 минут и возьми пакет для мусора.",
                    "Сначала мусор и очевидное: бутылки, упаковки, бумажки.",
                    "Собери грязную одежду в один пакет/корзину, не раскладывай сейчас.",
                    "Сложи чистое в одну стопку на кровати/столе, потом разнесёшь.",
                    "Заверши: протри стол/рабочую поверхность влажной салфеткой, открой окно.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Кухня",
            "title": "Быстрый обед в контейнер",
            "content": "Собери обед за 15 минут и убери в контейнер.",
            "steps": "\n".join(
                [
                    "Основа: макароны/рис/гречка — доведи до готовности.",
                    "Белок: тунец из банки, фасоль, яйцо или кусок курицы — добавь к основе.",
                    "Овощи: свежие или заморозка — обжарь 5–7 минут, посоли/перчи.",
                    "Соус: соевый, сметана, масло или йогурт. Перемешай всё вместе.",
                    "Разложи по контейнерам, остуди и в холодильник.",
                ]
            ),
            "tags": f"{VEGETARIAN_TAG},{VEGAN_TAG}",
        },
        {
            "category": "Уборка",
            "title": "Проверка холодильника за 10 минут",
            "content": "Раз в неделю пробегись по холодильнику, чтобы не воняло и не портилось.",
            "steps": "\n".join(
                [
                    "Возьми пакет для мусора, влажную тряпку и средство для кухни.",
                    "Выгрузи дверь и полки по очереди, выбрасывай явный мусор/старьё.",
                    "Протри полки/ящики влажной тряпкой, подсуши.",
                    "Верни продукты обратно, сгруппируй: готовое/молочка/соусы/овощи.",
                    "Составь короткий список докупок по итогам.",
                ]
            ),
            "tags": "",
        },
        {
            "category": "Уборка",
            "title": "Подготовка постели раз в неделю",
            "content": "Мини-ритуал, чтобы постель была свежей.",
            "steps": "\n".join(
                [
                    "Сними постельное, встряхни матрас и проветри комнату 10 минут.",
                    "Пока стирается — пройдись пылесосом по матрасу/основанию.",
                    "Надень чистое бельё, поставь чистое полотенце рядом.",
                    "Если есть запах — положи открытую соду/уголь на тумбочку на пару часов.",
                ]
            ),
            "tags": "",
        },
    ]

    existing_articles = await conn.execute_fetchall(
        "SELECT title FROM knowledge_articles;"
    )
    existing_titles = {row["title"] for row in existing_articles}

    for article in articles:
        if article["title"] in existing_titles:
            continue
        await conn.execute(
            """
            INSERT INTO knowledge_articles (category, title, content, steps, created_at, tags)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                article["category"],
                article["title"],
                article["content"],
                article["steps"],
                now,
                article.get("tags", ""),
            ),
        )

    await conn.commit()


async def main_init(database_url: str) -> None:
    """Helper to run initialization standalone."""
    conn = await connect(database_url)
    async with conn:
        await init_db(conn)


if __name__ == "__main__":
    db_url = os.getenv("DATABASE_URL", "sqlite:///hidl.db")
    asyncio.run(main_init(db_url))
