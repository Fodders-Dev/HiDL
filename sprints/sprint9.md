# Sprint 9 — Аудит, голосовой/текстовый inbox, настраиваемая LLM и дневник

**Цель:** провести честный аудит кодовой базы, починить критические сбои маршрутизации и реализовать первый сквозной сценарий: **голосовое или свободное текстовое сообщение → расшифровка → безопасное сохранение → разбор без ложных обязательств → короткий ответ → исправление или отмена**.

---

## Выполненные задачи

### 1. Аудит и исправления маршрутизации / багов
- [x] Зафиксирован актуальный технический аудит в `docs/audit_current_state.md` (разделение: работает / частично / только описано / исправлено).
- [x] Внедрён `utils/menu_match.py`: точное сопоставление кнопок меню (`match_menu_label`), фильтрация быстрых трат (`looks_like_expense`) и простых односоставных напоминаний (`is_single_clause_reminder`).
- [x] Устранены жадные фильтры в `handlers/menu.py`, `handlers/knowledge.py`, `handlers/finance.py`, `handlers/donate.py`, `handlers/focus_cafe.py`, `handlers/ask_mom.py`, `handlers/wellness.py`, `handlers/schedule.py`.
- [x] Добавлен `StateFilter(None)` в `handlers/finance.py` (`money_free_parse`) и `handlers/natural.py`, благодаря чему числовой ввод в FSM (например, количество продуктов в холодильнике) больше не перехватывается.
- [x] Исправлен баг `parse_hhmm` (`datetime.time` vs `str`) в `handlers/schedule.py`.
- [x] Исправлены `rem:add`, синтаксическая запятая в `rem:edit_wd` и передача `user_id` в `handlers/custom_reminders.py`.
- [x] Исправлено определение пользователя по `callback.from_user.id` в `handlers/sleep_mode.py`.
- [x] Определена `local_date` в `ReminderScheduler._tick_weekly_finance` (`scheduler/reminder.py`).

### 2. Конфигурируемые LLM и STT
- [x] `config.py` и `.env.example`: добавлены настройки LLM (`openai`, `openai_compatible`, `deepseek`, `qwen`, прокси, failover) и STT (`auto`, `custom`, `groq`, `openai`, `qwen`) без жёсткого вшивания или угадывания ID моделей.
- [x] `llm_client.py`: реальный асинхронный OpenAI-совместимый клиент с ретраями (429/5xx/сеть), запретом ретраев на 401/timeout, failover и ремонтом JSON.
- [x] `services/stt.py`: загрузка voice из Telegram и транскрибация с runtime failover между провайдерами.

### 3. Сквозной Voice/Text Inbox и `/diary`
- [x] Новые таблицы в `db/database.py` и методы в `db/repositories.py`: `inbox_messages`, `journal_entries`, `inbox_actions`.
- [x] Идемпотентное сохранение сырого сообщения до обращения к STT/LLM.
- [x] `services/inbox_parser.py`: гибридный LLM + детерминированный fallback-разбор, вычисление дат по таймзоне пользователя, guardrails против превращения мыслей/идей в задачи.
- [x] `services/inbox_service.py` и `handlers/inbox.py`: приём `F.voice` и свободного текста, уточнение неоднозначного времени/даты кнопками, исправление (переклассификация, переименование, удаление) и полная отмена (`undo`).
- [x] Команда `/diary`: вкладки, поиск по записям, раздельное хранение слов пользователя и заметок модели, экспорт в JSON и двухшаговое удаление личных данных.

### 4. Подготовка релиза под `systemd` (Codex Handoff)
- [x] `db/database.py` и `main.py`: включён `PRAGMA journal_mode=WAL` / `busy_timeout=5000` для файловой БД и корректный graceful shutdown планировщика, БД и бот-сессии.
- [x] `config.py`, `services/inbox_parser.py`: таймаут 15 сек, лимит 1200 токенов, авто-резерв DeepSeek при наличии `DEEPSEEK_API_KEY`, режим `STT_PROVIDER=off`, нормализация русских названий типов (`покупка`, `идея`, `задача`, `дневник`, `напоминание`, `сделано`).
- [x] `deploy/systemd/hidl.service`, `deploy/systemd/hidl.env.example`, `deploy/scripts/check_health.py`, `scripts/backup_sqlite.py`, `docs/release_codex_handoff.md`: шаблон одного polling-инстанса в `/opt/hidl` с отдельной БД `/var/lib/hidl/hidl.db`, preflight-проверками и онлайн-бэкапом.
- [x] С сервера `rodnya` подтверждены живые ответы `POST /chat/completions` (`gpt-6-luna`, `reasoning_effort=none`, JSON-режим), резервного DeepSeek (`deepseek-v4-flash`) и чистое состояние Telegram webhook.

### 5. Проверки
- [x] `python -m tests.smoke` — `[SMOKE] OK`.
- [x] `python -m unittest discover -s tests -p "test_*.py" -v` — 12/12 тестов (`test_inbox_pipeline.py` + `test_release_readiness.py`).
- [x] `python tools/batch_test.py all` — 28/28 сценариев симулятора (предупреждения в `food_management` и `kitchen_testing` устранены).
- [x] `python scripts/backup_sqlite.py --source hidl.db --dest backups/hidl_2026-11-19_pre_release.db --check-migrate` — бэкап и миграция на временной копии прошли без потерь.

---

## Next (следующие итерации)
1. **Google Calendar (только чтение)**: чтение событий, учёт подготовки/дороги/отдыха, фильтрация служебных блоков, чтобы не считать промежутки между делами полностью свободными.
2. **Утренний старт и ежедневное сопровождение**: кнопка/фраза «встал» с учётом указанного времени («час назад»), посильный план из 1–2 дел, проверки еды/действий, вечерний короткий отчёт, уважение тихих часов и отсутствие ложных выводов при молчании.
3. **Правила ответственности и баллы**: настраиваемая строгость, предсказуемый код подсчёта баллов/штрафов только по явно принятым обязательствам, запрет штрафов за усталость/откровенность/отсутствие ответа.
4. **Питание на двоих и запасы**: учёт бытовых единиц, подбор выполнимых блюд из запасов без автоматического списания при предложении рецепта, подготовка списка покупок.
5. **MCP-сервер**: авторизованный доступ к тем же данным и правилам (с отдельной настройкой доступа к личному дневнику).
