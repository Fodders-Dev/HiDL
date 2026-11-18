# Технический аудит состояния HiDL

Дата аудита: 2026-11-19 (актуальная ветка `main`).  
Цель: зафиксировать фактическое состояние модулей кода и документации без допущений.

---

## 1. Работает

- **Ядро приложения и точка входа**: `main.py` → `hidl/app.py` (`create_app`), `aiogram` v3 (`Dispatcher`, FSM в памяти), `SQLite` через `aiosqlite` (`db/database.py`, `db/repositories.py`), `APScheduler` (`scheduler/reminder.py`).
- **Онбординг и профиль**: `handlers/start.py` (`/start`, имя, пол/грамматические окончания, часовой пояс, время подъёма и отбоя, `/help`), `handlers/settings.py`.
- **Дашборд `/today` и план дня**: `utils/today.py`, `handlers/day_plan.py` (`/plan_tomorrow`, утренние/вечерние списки `day_plans` / `day_plan_items`, очки и стрик).
- **Рутины, фокус и сон**:
  - `handlers/routines.py`, `handlers/routine_items.py`, `handlers/routine_steps.py`;
  - `handlers/focus_cafe.py` (`/cafe`, серединные чек-ины, страйки и кулдаун);
  - `handlers/sleep_mode.py` (`/sleep`, окно сна, плавный сдвиг в `ReminderScheduler._tick_sleep_mode`).
- **Дом, финансы, кухня и поддержка**:
  - `handlers/home_tasks.py`, `handlers/home_supplies.py`, `handlers/zones.py`;
  - `handlers/finance.py` (траты, лимиты по категориям, отчёт за неделю, счета `bills`);
  - `handlers/kitchen.py`, `handlers/pantry.py` (холодильник `pantry_items`, книга рецептов `recipes_core.json`, список покупок `shopping_list`, учёт профиля питания `omnivore`/`vegetarian`/`vegan`);
  - `handlers/meds.py`, `handlers/movement.py`, `handlers/affirmations.py`, `handlers/ask_mom.py`, `handlers/help_now.py`.
- **Голосовой и текстовый inbox (добавлено в Sprint 9)**:
  - `services/stt.py` — загрузка voice из Telegram и транскрибация через настраиваемый STT (`custom`, `groq`, `openai`, `qwen`) с runtime failover;
  - `llm_client.py` — настраиваемый OpenAI/OpenAI-compatible/DeepSeek/Qwen клиент с прокси, ретраями и failover + JSON-парсер;
  - `services/inbox_parser.py` и `services/inbox_service.py` — сохранение сырого сообщения до вызова STT/LLM, идемпотентность по `(user_id, chat_id, telegram_message_id)`, разбор смешанных фраз (`task`, `reminder`, `shopping`, `diary`, `idea`, `done`), вычисление дат по таймзоне пользователя, guardrails от превращения идей/чувств в задачи, уточнение неоднозначных дат/времени, fallback без модели;
  - `handlers/inbox.py` — приём `F.voice` и свободного текста, короткий ответ с кнопками правки/отмены (`✏️ Исправить`, `↩️ Отменить всё`, переклассификация, переименование, удаление), команда `/diary` с вкладками, поиском, экспортом в JSON и удалением личных записей.
- **Автотесты и симулятор**: `tests/smoke.py`, `tests/test_inbox_pipeline.py`, `tools/batch_test.py` (28 сценариев в `tools/sim_scenarios.py`).

---

## 2. Реализовано частично

- **Реальные подключения LLM и STT**: код клиента и конфигурация через `.env` готовы и покрыты mock-тестами HTTP-транспорта; для живого запуска в Telegram требуется заполнить реальные переменные окружения (`BOT_TOKEN`, ключи/endpoint/model LLM и STT) в локальном `.env`.
- **Личное расписание (`handlers/schedule.py`)**: локальные таблицы `schedule_blocks` и `schedule_events` + расчёт свободных окон работают, но ручной ввод через текстовый формат и интеграция с внешним календарём отсутствуют.
- **Питание и готовка из запасов**: в `handlers/kitchen.py` есть холодильник, рецепты и список покупок; сопоставление рецептов с текущими остатками работает по отдельным рецептам, а бытовой ввод («полпачки», «банка») поддержан на входе в inbox, но ещё не интегрирован в полноценное планирование блюд на семью/неделю и подготовку корзины магазина.
- **Режим `/talk`**: переведён на честное описание свободного голосового/текстового inbox без фиктивного чат-сценария, полноценный диалоговый режим памяти поверх дневника пока не реализован.

---

## 3. Только описано (в коде отсутствует)

- **Интеграция с Google Calendar** (OAuth/Service Account/ICS-чтение, буферы на подготовку и дорогу, исключение нерабочих блоков).
- **Ежедневный цикл сопровождения «утро → чек-ины дня → вечерний отчёт»** с фиксацией фактического времени подъёма, пропуском проверок во время календарных событий и ограничением повторов.
- **Настраиваемая система правил ответственности и штрафов в коде** (opt-in правила, лимитированные списания, запрет штрафов за отсутствие ответа/усталость).
- **MCP-сервер** для внешнего клиента с авторизацией и раздельными правами доступа к дневнику.
- **OCR чеков** (`receipt_photos` в БД существует как заглушка таблицы без обработчика).
- **Подготовка корзины доставки продуктов** (отдельно от оплаты).

---

## 4. Исправлено в ходе аудита и требует внимания

### Исправлено в коде:
1. **Жадные подстрочные фильтры меню и финансов**:
   - `handlers/menu.py`, `handlers/knowledge.py`, `handlers/donate.py`, `handlers/focus_cafe.py`, `handlers/ask_mom.py`, `handlers/wellness.py`, `handlers/schedule.py` переведены на точное сопоставление через `utils/menu_match.py` (`match_menu_label`) и `StateFilter(None)` где нужно — обычные фразы со словами «сегодня», «дома», «еда», «поговорить» больше не перехватываются кнопками меню.
   - `handlers/finance.py`: `money_free_parse` получил `StateFilter(None)` и фильтр `looks_like_expense(...)` — числовой ввод в FSM (например, количество продуктов в `PantryAddState.amount`) и обычные предложения с цифрами больше не проглатываются и не записываются как траты.
2. **Ошибки типов и идентификаторов в существующих хендлерах**:
   - `handlers/schedule.py`: `parse_hhmm()` возвращает `datetime.time`, из-за чего `_to_min` падал с `AttributeError` — добавлена нормализация `datetime.time | str`.
   - `handlers/custom_reminders.py`: исправлена отсутствующая запятая между строками клавиатуры в `rem:edit_wd`; `rem:add` теперь берёт `callback.from_user.id` вместо `callback.message.from_user.id` (бота); `update_custom_reminder_time/freq` теперь передают внутренний `users.id` вместо `telegram_id`.
   - `handlers/sleep_mode.py`: `_render_sleep_menu` при вызове из callback использует `callback.from_user.id` вместо ID сообщения бота.
   - `scheduler/reminder.py`: в `_tick_weekly_finance` определена локальная переменная `local_date` перед вызовом `_safe_send_message`.
   - `handlers/custom_reminders.py` / `handlers/natural.py`: многосоставные и неоднозначные напоминания направляются в единый `inbox`, а не в legacy-парсер.

### Требует внимания в следующих итерациях:
- Ручная проверка на реальном Telegram-боте с живыми ключами LLM/STT (голосовые сообщения, тайминги, ответы в реальном чате).
- Постепенный отказ от `datetime.datetime.utcnow()` (Python 3.14 `DeprecationWarning` во многих legacy-модулях).
- Проверка `cleaning_v2` в общем прогоне `tools/batch_test.py` (в shared in-memory БД `cleaning_quick` оставляет активную сессию уборки для того же `tester_id`).
- Реализация следующих приоритетов: Google Calendar (только чтение) → утренне-вечерний цикл → правила ответственности → питание/запасы → MCP.
