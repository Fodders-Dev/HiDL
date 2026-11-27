# HiDL — OOUX Object Map, Breadboard & Design Contracts

> Единый источник правды по сущностям, связям, экранам, каналам входа и правилам интеграции фич (процесс **OOUX -> Breadboard -> Код**).
>
> **Интерактивная схема в FigJam:** [HiDL Object and Screen Breadboard](https://www.figma.com/board/FZwitiGcxWfvKUiqwsv8Ch?utm_source=other&utm_content=edit_in_figjam&oai_id=&request_id=16b958c7-c264-49c4-8bab-e962425581f6)

---

## 1. OOUX / ORCA Map (Сущности, таблицы и связи)

В основе HiDL — 15 пользовательских сущностей поверх 57 таблиц SQLite ([db/database.py](../db/database.py)).

| # | Сущность | Таблицы SQLite | Область владения (`scope` / права) | Ключевые связи (`Relationships`) | Модули-владельцы |
|---|---|---|---|---|---|
| 1 | **Пользователь и профиль** | `users`, `wellness_settings`, `user_profile_memory` | Personal (`user_id`, удаляется полностью) | Задаёт режим (`adult` / `student`), КБЖУ-цель, `meal_profile`, таймзону, сон | `handlers/start.py`, `handlers/settings.py`, `middlewares/ensure_user.py` |
| 2 | **План дня (Задачи)** | `day_plans`, `day_plan_items`, `inbox_actions` | Personal (`user_id`) | Переносится (`sync_overdue`), откладывается в **Идеи**, синхронизируется с **Сегодня** | `handlers/day_plan.py`, `services/inbox_service.py`, `utils/today.py` |
| 3 | **Идея / Бэклог / Дневник** | `journal_entries`, `inbox_messages`, `inbox_actions` | Personal (`user_id`) | Извлекается из свободного чата/голоса; `idea` <-> `day_plan_items` | `handlers/inbox.py`, `services/inbox_parser.py`, `services/inbox_service.py` |
| 4 | **Холодильник (Кладовка)** | `pantry_items`, `receipt_photos` | Shared by `household_id` (или `user_id`) | Питает **Рецепт** и **Меню**, списывается из **КБЖУ**, пополняется из **Покупок** / чеков | `handlers/kitchen.py`, `services/kitchen_ai.py`, `services/inbox_service.py` |
| 5 | **Список покупок** | `shopping_list` | `household` / `personal` | Заполняется из рецептов («не хватает»), чата и авто-режима; убранное может уйти в кладовку | `handlers/kitchen.py`, `services/kitchen_ai.py` |
| 6 | **Рецепт** | `user_recipes` + встроенная база (`research_data.py` / сиды) | Personal / Household (`share_recipes`) | Читает `pantry_items`, при «Приготовил» создаёт `meal_logs`, списывает кладовку, закрывает **Пульс** | `handlers/kitchen.py`, `services/kitchen_ai.py`, `handlers/nutrition.py` |
| 7 | **Меню на 3 дня** | `meal_plans` | Personal / Household | Ссылается на `recipe_id` и `meal_slot` (`breakfast`/`lunch`/`dinner`), открывается на верху `🍽 Еда и покупки` | `handlers/kitchen.py`, `services/kitchen_ai.py` |
| 8 | **Дневник КБЖУ и этикетки** | `meal_logs`, `pending_meal_clarifications`, `user_food_products`, `nutrition_lookup_cache` | Strictly Personal (`user_id`) | Создаётся из текста/голоса/фото/рецепта; 1-тап списывает `pantry_items` и закрывает пищевой **Пульс** | `handlers/nutrition.py`, `handlers/inbox.py`, `services/fitness_nutrition.py`, `services/nutrition_web.py` |
| 9 | **Пульс дня и Карма** | `pulse_checkins`, `user_karma_state`, `karma_events` | Personal (`user_id`) | Слоты `wake`/`lunch`/`dinner`/`water`/`bedtime` закрываются кнопками, едой или сном | `handlers/pulse.py`, `services/pulse_service.py`, `services/karma_service.py` |
| 10 | **Напоминания (Разовые / Лекарства)** | `custom_reminders`, `custom_tasks`, `meds`, `med_logs` | Personal (`user_id`) | Показываются в `/today`, управляются из чата и планировщика | `handlers/custom_reminders.py`, `handlers/meds.py`, `scheduler/reminder.py` |
| 11 | **Расписание и Календарь** | `schedule_blocks`, `schedule_events`, `calendar_events`, `user_calendar_connections`, `gcal_oauth_states` | Personal (`user_id`) | Двусторонний sync с Google Calendar, алерты, блок на **Сегодня** | `handlers/schedule.py`, `services/calendar_service.py` |
| 12 | **Тело, Весы и Спорт** | `weights`, `workout_logs`, `focus_sessions` | Strictly Personal (`user_id`) | Влияют на суточную норму КБЖУ (`calculate_daily_targets`), закрывают тренировочные цели | `handlers/movement.py`, `handlers/focus_cafe.py`, `services/fitness_nutrition.py` |
| 13 | **Наш дом и Регулярные дела** | `households`, `household_requests`, `household_dinner_polls`, `regular_tasks`, `cleaning_sessions`, `supplies`, `routines`, `user_routines`, `routine_items`, `routine_steps` | Household (`household_id`, настройки в `households.settings`) | Партнёрские просьбы, голосование за ужин, зоны уборки, бытовая химия, рутины | `handlers/household.py`, `handlers/home_tasks.py`, `handlers/home_supplies.py`, `handlers/routines.py` |
| 14 | **Финансы и Счета** | `budgets`, `budget_categories`, `expenses`, `bills`, `points_log` | Personal / Household (`share_bills`) | Траты из чата или кнопок, напоминания по счетам на **Сегодня** | `handlers/finance.py`, `handlers/stats.py` |
| 15 | **Интеграции (Desk / MCP)** | `desk_links`, `desk_link_tokens`, `desk_events`, `desk_outbox`, `mcp_*` | Personal (`user_id`) | Учёба/дэдлайны в режиме `student`, внешние ИИ-клиенты через OAuth/Bearer | `handlers/desk.py`, `handlers/mcp_connect.py`, `mcp_server/app.py` |

### Инварианты архитектуры
1. **Приватность здоровья и дневника:** `meal_logs`, `weights`, `workout_logs`, `journal_entries`, `user_karma_state` строго личные (`user_id`), никогда не утекают партнёру по дому.
2. **Единая кухня (`adult` vs `student`):** в режиме `student` скрыты бытовые/кухонные/финансовые разделы (`EnsureUserMiddleware._student_blocked`), вместо них выводится `📚 Учёба / Desk`.
3. **Связка `Холодильник <-> Рецепт <-> Меню <-> КБЖУ`:** рецепт и план на день обязаны учитывать реальные остатки `pantry_items`, честно показывать недостающее и после готовки списывать реальные продукты.

---

## 2. Матрица жизненного цикла (`Сущности x CRUD x Каналы x Роли`)

| Сущность | Создать | Открыть | Изменить / Адаптировать | Закрыть / Списать / Отложить | Поделиться (`household`) | Удалить | Каналы входа | Режим `student` | Подтверждённые дыры в UI/логике |
|---|---|---|---|---|---|---|---|---|---|
| **План дня (`day_plan_items`)** | Да (кнопка `+`, текст, голос, словесные даты «до 5 декабря») | Да (`/today`, `dplan:list`) | **Нет Rename в UI** (только важность/дата) | Да (Done, `⏳ На потом` в бэклог) | Нет (личное) | Да (`dplan:del`) | Кнопки, чат, голос, MCP | Разрешено | Поддержка русских месяцев (`до 5 декабря` -> `05.12`) добавлена; остаётся Rename в UI |
| **Идея / Дневник (`journal_entries`)** | Да (текст, голос) | Да (`/diary`, `diary:ideas`) | Через `inbox:item` частично | Да («В план сегодня») | Нет (личное) | Да (`diary:del`, Undo) | Чат, голос, кнопка, MCP | Разрешено | Нет прямого редактирования текста идеи в UI |
| **Холодильник (`pantry_items`)** | Да (кнопка, bulk, чек, голос, 3–4 скрина корзины и авто-вход из чата) | Да (`kitchen:fridge`, `/pantry`, чистый формат без `3e+02 г`) | Частично (`Nemoloko овсяный ↔ Овсяное молоко` склеиваются) | Да (`pantry_remove`, 1-тап в КБЖУ, `🗑 Кончилось:` для хвостов) | Да (общий по `household_id`) | Да (`kitchen:fridge_del`) | Кнопки, чат, голос, фото/альбом корзины, MCP | Скрыто | Закрыты 3e+02 г, дубль овсяного молока, мультискрин-корзина и 1-тап добивка хвостов банок; остаётся inline-edit срока годности |
| **Покупки (`shopping_list`)** | Да (кнопка, чат, «не хватает в рецепте») | Да (`kitchen:shoplist`) | Отметка «куплено» (`toggle`) | Да (`shop_finish` -> перенос в кладовку) | Да (`household` / `personal`) | Да (`shop_del`, чат `shopping_remove`) | Кнопки, чат, голос, MCP | Скрыто | Проверить удаление устаревших ручных команд `/shoplist` (`guides.py`) |
| **Рецепт (`user_recipes`)** | Да (ИИ-шеф save, импорт PDF/текста) | Да (`kitchen:recipes`, `cook_view`: `(дома: ...)` + `⏳ Подготовка заранее`) | Адаптация под холодильник (`kitchen:adapt`); **GAP: нет прямого Edit в UI** | Да (`Приготовил (N порц.) + 1 в КБЖУ` / `Только списать N`, снос дублей) | Да (`share_recipes`) | **GAP: нет Delete в UI** | Кнопки, PDF/текст, активный контекст рецепта в чате, MCP | Скрыто | Закрыты дубли кнопок готовки, путаница с сухой/консерв. фасолью, бейдж замачивания и роутинг вопросов о рецепте в `advice` |
| **Меню на 3 дня (`meal_plans`)** | Да (`kitchen:meal_plan` авто-сборка с приоритетом «всё дома + истекает» на День 0) | Да (`Еда и покупки` сверху + `Меню на 3 дня`, `is_stale`, `⚠️ нет N прод.`, `⏳ заранее`) | **GAP: нельзя поменять 1 слот** | Да (`status = cooked`) | Да (`household_id`) | **GAP: нет сброса отдельного дня** | Кнопки | Скрыто | Закрыт GAP по устаревшим планам, замачиванию заранее и приоритету холодильника на День 0; остаётся ручная замена/сброс слота |
| **КБЖУ (`meal_logs`)** | Да (текст, голос, фото, рецепт, MCP, 1-тап кнопка партнёру при `servings >= 2`) | Да (`/kbju`, `kbju:today`) | Да (`kbju:refine`, `kitchen:cook_edit_log` UPDATE без дубля) | Да (автокоммит через 10 мин, 1-тап списание кладовки) | Никогда (личная запись, партнёр подтверждает сам 1-м тапом) | Да (`kbju:del`, Undo) | Текст, голос, фото, альбом, кнопки, пуш партнёра, MCP | Скрыто | — |
| **Личные этикетки (`user_food_products`)** | Авто-сохранение с фото/текста | **GAP: нет экрана просмотра** | Перезапись по ключу | Переиспользуется при разборе | Никогда | **GAP: нет удаления в UI** | Фото, текст `на 100 г` | Скрыто | Пользователь не видит и не может удалить ошибочно запомненную этикетку |
| **Пульс и Карма** | Авто-создание по расписанию | Да (`🌙 Сон и карма`, `/karma`) | Настройка времени/паузы | Да (кнопки, еда, сон, ЧП 24ч, `breakfast` ≠ `wake`) | Нет | Пауза / сброс сном | Кнопки, пуши, чат | Еда-слоты скрыты | — |
| **Напоминания (`custom_reminders`)** | Да (`/add_reminder`, чат, голос) | Да (`/reminders`, `/today`) | Да (`customedit:`) | Да (`done`, `later`) | Нет | Да (`customdel:`) | Кнопки, чат, голос | Разрешено | — |
| **Календарь (`schedule_events`)** | Да (`/schedule`, чат -> GCal) | Да (`🗓 Расписание`, `/today`) | Частично | Событие проходит / перенос | Нет | Да (`cal:del`) | Кнопки, чат, голос, GCal, MCP | Разрешено | — |
| **Весы и Спорт (`weights`, `workout_logs`)** | Да (текст, фото Xiaomi, кнопки) | Да (`⚖ Тело и весы`, `🏋️ Спорт`) | Да (merge 2-го скрина весов) | Да (тренировки в карму + бонус КБЖУ) | Никогда | Базовое удаление/undo для inbox | Кнопки, чат, фото/файл, MCP | Весы/спорт по профилю | — |
| **Наш дом / Регулярки / Химия** | Да (`🏠 Наш дом`, `🧹 Дом`) | Да (`hh:hub`, `more:home`) | Да | Да (просьба принять/отложить/готово; уборка) | Да (`household_id`) | Да | Кнопки, пуши, чат | Скрыто | `zones.py` и `meds.py` оторваны от основного меню |
| **Финансы (`expenses`, `bills`)** | Да (кнопка, чат `потратил ...`) | Да (`more:money`, `/finance`) | Лимиты категорий | Да (оплата счёта) | Счета `share_bills` | Да (`money:history`) | Кнопки, чат, голос, MCP | Скрыто | Legacy `stats.py` дублирует отчётность |

---

## 3. Breadboard (Карта экранов и переходов)

### Точки входа (`ReplyKeyboard` + свободный чат)

| Точка входа | Целевой экран / модуль | Основные контролы внутри | Куда ведёт `Назад` | Владелец |
|---|---|---|---|---|
| `📅 Сегодня` / `/today` | Дашборд дня (`render_today`) | Рутины ✅/⏭, напоминания, фокус, быстрый чекин | `main:menu` | `handlers/menu.py`, `utils/today.py` |
| `🍽 Еда и покупки` / `/kitchen` | Умная кухня (`build_kitchen_home_payload`) | Текущее блюдо дня, `Что приготовить`, `Не хватает 1-2`, `ИИ-шеф`, `Мой КБЖУ`, `Меню на 3 дня`, `Холодильник`, `Покупки`, `Рецепты` | `kitchen:main` -> `main:menu` | `handlers/kitchen.py` |
| `🌙 Сон и карма` / `/pulse` | Пульс и карма | Статус слотов, долг/стрик, режим сна, перенос отбоя | `main:menu` | `handlers/pulse.py`, `handlers/sleep_mode.py` |
| `🗓 Расписание` / `/schedule` | Расписание и GCal | События сегодня/недели, подключение Google Calendar | `main:menu` | `handlers/schedule.py` |
| `📓 Дневник` / `/diary` | Дневник и Идеи | Записи дневника, список Идей, перенос идеи в План дня | `main:menu` | `handlers/inbox.py`, `handlers/day_plan.py` |
| `⚙ Ещё` | `more_menu_keyboard()` | `Наш дом`, `Дом`, `План на день`, `Напоминания`, `Деньги`, `Спорт`, `Кафе фокуса`, `Болею/ЧП`, `Настройки`, `Фидбек`, `Помощь сейчас` | `main:menu` | `keyboards/common.py`, `handlers/menu.py` |
| `Свободный чат` (текст / голос / фото / файл) | Единый Inbox-роутер | Классификация (`task`, `idea`, `diary`, `meal_log`, `pantry_*`, `shopping*`, `reminder`, `calendar_event`, `expense`, `weight_log`, `workout_log`, `query`), альбомы `kbju:confirm:<pid>`, `Исправить`, `Отменить всё` | — | `handlers/inbox.py`, `handlers/natural.py`, `services/inbox_service.py` |
| `Student Menu` (`is_student_mode`) | Учёба / Desk + фокус + сон | `Сегодня`, `Учёба / Desk`, `Напоминания`, `Кафе фокуса`, `Сон и пауза`, `Настройки` | `main:menu` | `keyboards/common.py`, `middlewares/ensure_user.py` |

### Полная карта всего HiDL (все 15 сущностей, разделы и Delete-first)

```mermaid
flowchart LR
    subgraph entries ["1. Точки входа и каналы"]
        replyAdult["Главное меню (Adult)"]
        replyStudent["Меню Ученика (student)"]
        freeChat["Свободный чат (Текст / Голос / Фото / Альбом)"]
        schedJob["Планировщик (scheduler / напоминания)"]
        extDesk["#15 Интеграции (Desk / MCP API)"]
    end

    subgraph profileCore ["2. Ядро профиля и дня (Personal)"]
        profile["#1 Профиль, цели КБЖУ, диета, сон, adult/student"]
        todayDash["Дашборд Сегодня (/today)"]
        dayPlan["#2 План дня (задачи + даты «до 5 декабря»)"]
        diaryBacklog["#3 Дневник и Идеи (бэклог)"]
        reminders["#10 Напоминания и лекарства"]
        calEvents["#11 Расписание и Google Calendar"]
    end

    subgraph kitchenLoop ["3. Кухня и Закупки (Shared + Personal)"]
        kitchenHome["Умная кухня (/kitchen)"]
        cartImport["Мультискрин-корзина (3–4 скрина ВкусВилла)"]
        fridge["#4 Общий холодильник (household, чистый формат)"]
        shopList["#5 Список покупок (household/personal)"]
        mealPlan["#7 Меню на 3 дня (⏳ замачивание + ⚠️ нет N прод.)"]
        recipeCard["#6 Карточка рецепта (адаптация, (дома: ...), ⏳ подготовка)"]
        postCook["Экран «Приготовлено» (хвосты банок + правка порции)"]
    end

    subgraph healthPulse ["4. Тело, КБЖУ и Ритм (Strictly Personal)"]
        pulseKarma["#9 Пульс дня, Сон и Карма"]
        kbjuDiary["#8 Дневник КБЖУ и личные этикетки"]
        bodyScale["#12 Тело, Весы Xiaomi и Спорт"]
        focusCafe["#12 Кафе фокуса (таймер)"]
    end

    subgraph houseFinance ["5. Общий дом и Финансы"]
        ourHome["#13 Наш дом (просьбы, ужин-опрос, пуши партнёру)"]
        homeChores["#13 Уборка, регулярки, химия и рутины"]
        finance["#14 Финансы, бюджет и счета"]
    end

    subgraph deleteLegacy ["6. Delete-First: Дубли на снос"]
        delGuides["СНОСИМ: guides.py / food:* / pantry.py"]
        delSet["СНОСИМ: settings_menu_keyboard / set:*"]
        delFocus["СНОСИМ: фокус в 3 модулях"]
        delPause["СНОСИМ: пауза/ЧП в 3 модулях"]
        delOrphans["СНОСИМ/СВОДИМ: talk / zones / stats / meds"]
        delCookBtns["СНОСИМ: дубли кнопок на карточке рецепта"]
    end

    replyAdult --> todayDash & kitchenHome & pulseKarma & calEvents & diaryBacklog & ourHome & homeChores & finance & bodyScale & focusCafe
    replyStudent --> todayDash & extDesk & focusCafe & pulseKarma & profile
    profile -.->|"Права, диета, цели КБЖУ, сон"| replyAdult & replyStudent & kitchenHome & kbjuDiary & pulseKarma & bodyScale

    freeChat --> dayPlan & diaryBacklog & reminders & calEvents & kbjuDiary & bodyScale & finance & cartImport & shopList
    schedJob --> todayDash & pulseKarma & reminders & calEvents & homeChores & extDesk
    extDesk <-->|"Синк учёбы и MCP-действий"| dayPlan & kbjuDiary & fridge & pulseKarma

    todayDash --> dayPlan & homeChores & reminders & calEvents & focusCafe
    dayPlan <-->|"На потом / В план сегодня"| diaryBacklog

    kitchenHome --> mealPlan & recipeCard & fridge & shopList & kbjuDiary & cartImport
    cartImport -->|"В холодильник + авто-вычёркивание"| fridge & shopList
    cartImport -.->|"Опц. трата"| finance
    cartImport -.->|"Пополнили склад"| ourHome
    fridge --> mealPlan & recipeCard
    mealPlan --> recipeCard
    recipeCard -.->|"Контекст вопросов по готовке"| freeChat
    recipeCard --> postCook
    postCook -->|"Списание N порц. + 🗑 Кончилось"| fridge
    postCook -->|"Личная порция + правка"| kbjuDiary
    postCook -->|"Если N >= 2: 1-тап пуш партнёру"| ourHome
    ourHome -.->|"1 тап согласия"| kbjuDiary
    kbjuDiary -.->|"1-тап списание"| fridge
    kbjuDiary -->|"Закрывает слот еды"| pulseKarma
    bodyScale -->|"Нормы КБЖУ и цели"| kbjuDiary
    bodyScale -->|"Тренировки"| pulseKarma
    homeChores -->|"Карма быта"| pulseKarma
    homeChores -.->|"Хватает химии?"| shopList
    ourHome -->|"Ужин-опрос / Просьбы"| mealPlan & homeChores

    delGuides -.->|"Свести в"| kitchenHome
    delSet -.->|"Свести в"| profile
    delFocus -.->|"Свести в"| focusCafe
    delPause -.->|"Свести в"| pulseKarma
    delOrphans -.->|"В дом / финансы / напоминания"| homeChores
    delCookBtns -.->|"Оставить единый путь на"| recipeCard
```

---

## 4. Delete-First реестр (что унифицировать и удалить при следующих релизах)

1. **`handlers/guides.py` + `handlers/menu.py` (`food:*`) + `handlers/pantry.py`:** старые статичные меню (`/meal_plan`, `/recipes_fast`, `/shoplist`, `/plate`, `/cook_from_expiring`) и обёртка `food:pantry` дублируют живую `handlers/kitchen.py`. При касании раздела — вырезать статичные дубли и свести команды к `kitchen.py`.
2. **`keyboards/common.py:settings_menu_keyboard()` + `handlers/menu.py` (`set:*`):** дубликат `handlers/settings.py` (`settings:*`). Оставить единый экран `settings.py`.
3. **Фокус-таймер в 3 местах:** `handlers/focus_cafe.py`, `handlers/movement.py` (`move:focus`), `handlers/wellness.py` (`/focus`). Оставить один вход (`focus_cafe.py`) и проксировать из кнопок туда.
4. **Пауза и щадящий режим в 3 местах:** `handlers/pause.py`, `handlers/wellness.py` (`/gentle`, `/resume`), `handlers/settings.py`. Свести в единый модуль.
5. **Оторванные / заглушечные модули:** `handlers/talk.py` (16 строк, заглушка — поглотить через `/help` и `inbox`), `handlers/zones.py` (28 строк — перенести внутрь `home_tasks.py`), `handlers/meds.py` (нет кнопки ни в `more_menu_keyboard`, ни в `settings` — либо привязать в меню, либо убрать из видимых команд), `handlers/stats.py` (`points_log` — наследие до Кармы).
6. **Дубли действий на карточке рецепта (`handlers/kitchen.py`):** убрать с карточки `🔥 Приготовил (+3 кармы)` (`kitchen:cook_fast` / `kitchen:cook_fast_ad`), нижний ряд голых `🍽 ½ / 1 / 2 порц.` (`kbju:log_recipe`) и legacy `✅ Проверить продукты` (`kitchen:cook_check` / `kitchen:cook_done`). Оставить 1 главный путь (`🍳 Приготовил (N порц.) + мне 1 порц. в КБЖУ`) и 1 явную альтернативу (`❄️ Только списать N порц.`), а правку своей съеденной порции перенести на экран после готовки через обновление `meal_logs` без дублей.

---

## 5. Мини-контракт следующей фичи: «Умная адаптация рецепта под холодильник + свежесть меню»

> **Статус:** **Реализовано (2026-11-27)** — `services/kitchen_ai.py` (`adapt_recipe_to_pantry`, приоритет Дня 0 в `generate_meal_plan`, `is_stale` / `missing_count` в `get_meal_plan_view`, `recipe_override` в `commit_cooked_recipe`), `handlers/kitchen.py` (`kitchen:adapt`, `kitchen:adapt_apply`, `kitchen:cook_and_log_ad`, `kitchen:cook_fast_ad`), тесты в `tests/test_recipe_pantry_adapt.py`.

### Цель и инварианты
1. **Честность наличия:** при показе любого рецепта (`cook_view`, `ai_view`, превью меню) всегда явно отображаются два блока: `✅ Есть дома (X/Y)` и `⚠️ Не хватает (N): ...`. Бот никогда не делает вид, что недостающий продукт есть дома.
2. **Инвариант `План <-> Рецепт`:** генератор «Меню на 3 дня» не должен брать в приоритет рецепты с 3+ отсутствующими продуктами (напр. миндаль или овсяные сливки), когда в базе/холодильнике есть полноценные совпадения. Старые слоты прошлых генераций (> 2 дней назад или когда база холодильника сильно изменилась) помечаются как устаревшие с 1-тап кнопкой пересборки.
3. **Единое место в UI:** вся адаптация живёт внутри карточки рецепта (`handlers/kitchen.py`) — без создания новых веток меню.

### Поток пользователя (`Умная замена ингредиентов`)
1. Если в рецепте есть база (`coverage >= 60%` и не хватает `1..3` второстепенных/заменяемых ингредиентов), в карточке появляется кнопка **`✨ Заменить по холодильнику`**.
2. При нажатии ИИ (с офлайн-fallback по словарю эквивалентов) анализирует реальный `pantry_items` пользователя (с приоритетом на скоропортящиеся `expires_at`):
   - предлагает для каждого отсутствующего ингредиента конкретное действие: **пропустить** (посыпка/гарнир вроде миндаля или вяленых томатов) или **заменить** на продукт из холодильника (петрушка -> укроп/шпинат; овсяные сливки -> кокосовое или овсяное молоко);
   - пересчитывает адаптированные шаги, список продуктов и обновлённое КБЖУ.
3. Показывает превью замен и 3 кнопки:
   - **`✅ Норм, готовлю так`** — сохраняет адаптированный вариант в сессии приготовления и перерисовывает карточку рецепта с обновлённым составом (по «Приготовил» списываются реальные заменители из кладовки и пишется честное КБЖУ в `meal_logs`);
   - **`🛒 Лучше докупить недостающее`** — добавляет исходные отсутствующие продукты в `shopping_list`;
   - **`⬅️ Вернуть оригинал`**.

### Матрица состояний (5 обязательных состояний)

| Состояние | Ожидаемое поведение UI и данных |
|---|---|
| **1. Ideal (0 не хватает)** | Карточка показывает `✅ Всё есть дома`, кнопка адаптации не показывается (не шумит) |
| **2. Partial (`1..3` не хватает, база есть)** | Показаны `⚠️ Не хватает: ...` и кнопка `✨ Заменить по холодильнику`; после нажатия — конкретные замены/пропуски и пересчёт КБЖУ |
| **3. Loading (вызов ИИ)** | Временный индикатор `callback.answer("Подбираю замены по холодильнику…")` + обновление сообщения без дублей |
| **4. Empty / Offline (пустой холодильник, нечего заменять или ошибка LLM)** | Офлайн-таблица типовых замен (зелень<->зелень, молоко<->сливки, крупы/бобовые, орехи-посыпки -> пропуск); если заменить нечем или критично мало продуктов (`coverage < 60%`) — честное сообщение и кнопка `🛒 Докупить недостающее` |
| **5. Edge cases (`student`, старое меню, повторное нажатие, откат)** | В режиме `student` раздел скрыт; при отсутствии заменённого продукта к моменту «Приготовил» списание идёт безопасно без отрицательных остатков; «Вернуть оригинал» восстанавливает исходные ингредиенты |

### Rabbit Holes & Non-Goals
- **Rabbit Holes:** не перезаписывать глобальный `user_recipes` при разовой замене (чтобы не сломать оригинальный рецепт пользователя); аккуратно учитывать граммовки при замене «сливки 150 мл -> кокосовое молоко 150 мл» для пересчёта КБЖУ и списания.
- **Non-Goals:** не редактируем произвольные поля рецепта руками в этом же PR (это отдельная задача полного CRUD для `user_recipes` по матрице из раздела 2).

---

## 6. Мини-контракт: «Опыт готовки v2 + Мультискрин-корзина + Даты задач и Пульс»

> **Статус:** **Реализовано (2026-11-27)** — `utils/pantry.py`, `db/repositories.py`, `data/knowledge/recipes_core.json`, `services/kitchen_ai.py`, `handlers/kitchen.py`, `handlers/inbox.py`, `services/inbox_parser.py`, `services/inbox_service.py`; тесты в `tests/test_cooking_v2_and_cart.py`.

### 8 инвариантов
1. **Разделение «готовлю N порц.» и «съел в КБЖУ» + правка без дубля:** на карточке рецепта переключатель порций явно подписан `Спишется с холодильника: на N порц.`. Главная кнопка `🍳 Приготовил (N порц.) + мне 1 порц. в КБЖУ` списывает продукты за `N` порций и создаёт ровно 1 запись `meal_logs` (на 1 порцию); кнопка `❄️ Только списать N порц.` только списывает продукты и закрывает слот меню без записи КБЖУ. На экране «Приготовлено» кнопки `✏️ Я съел ½ порц.` / `✏️ 2 порц.` выполняют `UPDATE` уже созданной записи `meal_logs` (никогда второй `INSERT`).
2. **Совместная еда с партнёром (consent-first):** при `servings >= 2` уведомление партнёру (`notify_household_partners`) уходит с кнопками `🍽 Я тоже поел(а) — 1 порц.` · `🍽 ½ порц.` · `Не ела` и снапшотом КБЖУ/названия блюда (включая адаптированный состав). Партнёр подтверждает сам в 1 тап → создаётся его личный `meal_log` и закрывается его пищевой слот Пульса.
3. **Добивка «хвостов» банок/упаковок:** на экране «Приготовлено» для списанных позиций с ненулевым остатком (`remaining > 0`), где осталось `<= 35%` исходного объёма или `<= 150 г/мл`, показываются компактные 1-тап кнопки `🗑 Кончилось: <Продукт> (100 г)` (до 3 шт., без длинных суффиксов для мобильного экрана), удаляющие остаток из `pantry_items` в 1 клик.
4. **Ясность ингредиентов и замачивание заранее:** в `data/knowledge/recipes_core.json` у быстрых блюд с бобовыми/кукурузой явно указано `(консервированная)`; запрещено матчить `сухую` фасоль/нут/горох из холодильника в консервированные или быстрые (`<= 25 мин`) рецепты; в карточке при расхождении названия показывается подсказка `(дома: ...)`; для рецептов с подготовкой (`замочить`, `на ночь`, `мариновать`, `разморозить`) в карточке и в «Меню на 3 дня» показывается бейдж `⏳ Подготовка заранее`.
5. **Вопросы по рецепту в чате:** при открытии карточки (`view_recipe` / `adapt_apply_cb`) активный рецепт сохраняется в диалоговом контексте; кулинарные вопросы в общем чате (*«какая фасоль?», «надо замачивать?», «сколько варить?»*) роутятся в `query_type="advice"` с контекстом активного рецепта и холодильника, а не в `query_type="food"`.
6. **Мультискрин-корзина (3–4 скрина ВкусВилла/Перекрёстка):** `CartImportState` принимает серию скриншотов альбомом (`media_group_id` буфер ~1.5 с) и докидывание фото в `waiting_input` / `confirm`, объединяет позиции в единое превью с дедупликацией (итоговая сумма чека `total_amount` не суммируется повторно); в общем чате в этот же мультискрин-импорт сразу уходят фото/альбомы с подписью (`корзина`, `вкусвилл`, `перекрёсток`, `самокат`, `чек`, `закупка`) **или без подписи**, если это альбом `3+` фото либо Vision определил экран корзины/списка покупок вместо тарелки/этикетки (1–2 фото тарелки/этикетки КБЖУ продолжают работать как раньше).
7. **Чистый холодильник и Пульс:** убрать научную нотацию (`3e+02 г` -> `300 г`) в `utils/pantry.py`; канонически объединять `Напиток овсяный Nemoloko` и `Овсяное молоко` (строго исключая `йогурт`, `десерт`, `сливки`, и убрать ложное `nemoloko -> йогурт` в бустере); добавить `"breakfast"` в допустимые слоты `pulse_log`, не плодить `pulse_log` при наличии `meal_log` на ту же еду, и запретить слову «завтрак» срабатывать как `wake` (подъём).
8. **Словесные даты задач и напоминаний (`до 5 декабря`):** поддержать русские названия месяцев (`5 декабря`, `до 5 декабря`, `15 января`...) и `через N дней` в `resolve_temporal_expression` (`services/inbox_parser.py`) и `_TASK_DATE_HINT_RE` (`services/inbox_service.py`), чтобы задача сохранялась на `2026-12-05` и явно отображала дату в ответе (`✅ Задача (на 05.12): ...`), не проваливаясь молча на сегодня.

### Breadboard (единый замкнутый контур + Delete-first)

```mermaid
flowchart LR
    subgraph entry ["Точки входа (Закупка и План)"]
        entryCart["Еда и покупки -> Импорт корзины"]
        entryChatCart["Свободный чат: 3+ скрина ВкусВилла или подпись корзина/чек"]
        mealPlan["Меню на 3 дня (⚠️ нет N прод. + ⏳ Подготовка заранее)"]
    end

    subgraph cartLoop ["Мультискрин-корзина"]
        cartBuf["Буфер серии 1.5с + докидывание фото"]
        cartMerge["Единое превью без дублей (Nemoloko <-> Овсяное молоко)"]
    end

    subgraph houseHub ["Общий дом (Household)"]
        sharedFridge["Общий холодильник (формат без 3e+02 г)"]
        shopList["Список покупок (авто-вычёркивание)"]
        financeLog["Финансы (опц. 1-тап Трата)"]
        partnerFridgePush["Пуш партнёру: холодильник пополнен"]
    end

    subgraph cookCore ["Карточка рецепта + Delete-first"]
        deleteOld["СНОСИМ дубли: 🔥 Приготовил (+3), нижний ряд 🍽 ½/1/2 порц., ✅ Проверить продукты"]
        recipeCard["Карточка: ⏳ Подготовка заранее | (дома: ...) без сухой фасоли | ✨ Заменить | N порц. на склад | 🍳 Приготовил (N) + 1 в КБЖУ | ❄️ Только списать N"]
    end

    subgraph chatCtx ["Контекст в свободном чате"]
        chatQ["Вопрос: «какая фасоль / замачивать?»"]
        adviceAns["advice с контекстом открытого рецепта и холодильника"]
    end

    subgraph postLoop ["После готовки (Личное и Партнёр)"]
        postCook["Экран «Приготовлено»"]
        myKbju["Личный КБЖУ + Пульс (breakfast ≠ wake, правка ✏️ ½/2 через UPDATE)"]
        partnerPush["Если N >= 2: пуш партнёру (🍽 1 порц. | ½ порц. | Не ела)"]
        partnerKbju["Личный КБЖУ + Пульс партнёра"]
    end

    entryCart & entryChatCart --> cartBuf --> cartMerge
    cartMerge -->|"🧊 Всё в холодильник"| sharedFridge
    cartMerge -->|"Вычеркнуть"| shopList
    cartMerge -.->|"Если есть сумма"| financeLog
    cartMerge -.-> partnerFridgePush

    sharedFridge -->|"Наличие + сроки"| mealPlan
    sharedFridge -->|"Подсказка и фильтр бобовых"| recipeCard
    mealPlan -->|"Открыть блюдо дня"| recipeCard
    deleteOld -.->|"Убираем дубли с"| recipeCard

    recipeCard -.->|"Запоминает активный рецепт"| chatQ
    sharedFridge -.-> chatQ
    chatQ -->|"вместо «Что приготовить»"| adviceAns

    recipeCard -->|"🍳 или ❄️"| postCook
    postCook -->|"Списание N порц."| sharedFridge
    postCook -->|"🗑 Кончилось: Продукт (100 г)"| sharedFridge
    postCook -->|"1 порц. или правка"| myKbju
    postCook -->|"Совместная еда"| partnerPush -->|"1 тап согласия"| partnerKbju
```

### Матрица состояний (5 обязательных состояний)

| Состояние | Ожидаемое поведение UI и данных |
|---|---|
| **1. Ideal** | 2 порции на карточке -> 1 тап `🍳 Приготовил (2 порц.) + мне 1 порц. в КБЖУ` -> списано на двоих, у тебя ровно 1 порция в `meal_logs`, партнёр получает пуш с кнопками, остаток фасоли `100 г` убирается в 1 тап кнопкой `🗑 Кончилось: Фасоль (100 г)`. |
| **2. Loading / Album** | При 3–4 скринах корзины альбом собирается через буфер ~1.5 с, показывает «Разобрала 3 скрина ВкусВилла...», дедуплицирует позиции и позволяет докинуть ещё фото до нажатия `🧊 Всё в холодильник`. |
| **3. Empty / Offline** | Если фото корзины без Vision/LLM — честная подсказка вставить список строками; если рецепт не требует замачивания и нет хвостов — лишние бейджи и `🗑`-кнопки не рисуются. |
| **4. Error / Idempotency** | 1 размытый скрин из 4 не роняет весь альбом (остальные добавляются + честно пишется про 1 нераспознанный); повторный тап партнёра по пушу не создаёт дубль `meal_log`; правка `✏️ ½ порц.` обновляет существующий `meal_log`. |
| **5. Edge cases** | `student`-режим скрывает КБЖУ/корзину; `Йогурт овсяный Nemoloko` НЕ сливается с `Овсяным молоком`; `сухая фасоль` не матчится в 12-минутном тако; текст `завтрак: ...` в 11:30 не переписывает `wake` («встал в 11:30»); задача `до 5 декабря` (если месяц уже прошёл — берёт следующий год) попадает на `2026-12-05`. |

### Rabbit Holes & Non-Goals
- **Rabbit Holes:** при слиянии `total_amount` из нескольких скринов одной корзины (где итого виден на каждом скрине) нельзя суммировать одинаковый итог 3 раза — сохраняем максимальный/последний валидный `total_amount`; при `UPDATE` порции в `meal_logs` пересчитывать КБЖУ от базовой 1 порции, а не накапливать округление.
- **Non-Goals:** в этом релизе не строим отдельный экран редактирования/удаления личных этикеток `user_food_products` и ручной CRUD `user_recipes` — это следующий цикл.
