# Релиз HiDL на VPS (`rodnya`) — инструкция для Codex

Этот документ описывает единый продакшн-запуск HiDL под `systemd` в режиме одного Telegram polling-процесса.  
Секреты в репозиторий не коммитятся и в логи не выводятся.

---

## Целевая схема на сервере `rodnya` (<VPS_IP>)

- Код и окружение: `/opt/hidl/app` и `/opt/hidl/.venv`
- Рабочая SQLite БД (отдельно от кода): `/var/lib/hidl/hidl.db` (бэкапы: `/var/lib/hidl/backups/`)
- Конфигурация (секреты): `/etc/hidl/hidl.env` (`root:hidl`, права `0640`)
- Systemd-сервис: `/etc/systemd/system/hidl.service` (пользователь `hidl:hidl`)
- Основная LLM: OpenAI (`https://api.openai.com/v1/chat/completions`, `OPENAI_MODEL=gpt-6-luna`, `reasoning_effort=none`)
- Резервная LLM: DeepSeek (`https://api.deepseek.com`, `DEEPSEEK_MODEL=deepseek-v4-flash`, включается автоматически при наличии `DEEPSEEK_API_KEY`)

---

## 1. Pre-flight проверка на `rodnya` (один polling)

Убедиться, что на сервере нет старых процессов HiDL и webhook у бота снят:

```bash
ssh rodnya "ps aux | grep -iE 'hidl|main\.py' | grep -v grep || true"
ssh rodnya "systemctl list-units --type=service --all | grep -i hidl || true"
```

Проверить `getWebhookInfo` (без вывода токена в терминал), что `url == ""` — webhook отсутствует.

---

## 2. Подготовка директорий и пользователя `hidl`

```bash
ssh rodnya "id -u hidl >/dev/null 2>&1 || useradd --system --home /var/lib/hidl --shell /usr/sbin/nologin hidl"
ssh rodnya "mkdir -p /opt/hidl/app /var/lib/hidl/backups /etc/hidl && chown -R hidl:hidl /var/lib/hidl && chmod 750 /var/lib/hidl /etc/hidl"
```

---

## 3. Доставка кода и установка зависимостей

Залить/склонировать актуальный релизный коммит ветки `main` в `/opt/hidl/app` (без локальных `.env` и `*.db`):

```bash
ssh rodnya "python3 -m venv /opt/hidl/.venv && /opt/hidl/.venv/bin/pip install --upgrade pip && /opt/hidl/.venv/bin/pip install -r /opt/hidl/app/requirements.txt"
ssh rodnya "chown -R root:hidl /opt/hidl && chmod -R g+rX /opt/hidl"
```

---

## 4. Перенос рабочей SQLite БД (отдельно от кода)

Локально проверенная рабочая БД — `hidl.db` (бэкап-копия: `backups/hidl_2026-11-19_pre_release.db`).  
Скопировать проверенную копию на сервер и сохранить её также в `/var/lib/hidl/backups/`:

```bash
scp backups/hidl_2026-11-19_pre_release.db rodnya:/var/lib/hidl/backups/hidl_pre_release.db
ssh rodnya "cp /var/lib/hidl/backups/hidl_pre_release.db /var/lib/hidl/hidl.db && chown -R hidl:hidl /var/lib/hidl && chmod 640 /var/lib/hidl/hidl.db"
```

При первом запуске `init_db` автоматически применит `CREATE TABLE IF NOT EXISTS` для новых таблиц (`inbox_messages`, `journal_entries`, `inbox_actions`) в режиме WAL без потери существующих данных.

---

## 5. Создание `/etc/hidl/hidl.env` и pre-start health check

Создать `/etc/hidl/hidl.env` по шаблону `deploy/systemd/hidl.env.example` (вписать `BOT_TOKEN`, `OPENAI_API_KEY`, при наличии `DEEPSEEK_API_KEY` и/или STT-ключ; права `chown root:hidl /etc/hidl/hidl.env && chmod 640 /etc/hidl/hidl.env`).

Перед запуском сервиса выполнить проверку (без печати секретов):

```bash
ssh rodnya "set -a && . /etc/hidl/hidl.env && set +a && /opt/hidl/.venv/bin/python /opt/hidl/app/deploy/scripts/check_health.py --db /var/lib/hidl/hidl.db --allow-pre-migration --check-env --probe-llm"
```

Ожидается: `DB_CHECK: ... integrity=ok`, `LLM_PROBE: ok=True provider=openai model=gpt-6-luna`.

---

## 6. Установка и запуск `systemd`-сервиса

```bash
ssh rodnya "cp /opt/hidl/app/deploy/systemd/hidl.service /etc/systemd/system/hidl.service && systemctl daemon-reload && systemctl enable --now hidl.service"
ssh rodnya "systemctl status hidl.service --no-pager -n 20"
ssh rodnya "journalctl -u hidl.service -n 30 --no-pager"
```

Проверить в логах отсутствие `TelegramConflictError` (подтверждает единственный polling) и строку `HiDL starting: db=/var/lib/hidl/hidl.db llm=openai:gpt-6-luna ...`.

---

## 7. Приёмка релиза (выполняется после запуска)

1. **`/start`**: бот отвечает в Telegram, логи `journalctl -u hidl.service` без ошибок.
2. **Живой текстовый ответ (LLM)**: отправить смешанное сообщение (например: дело + покупка + мысль) → бот разбирает через `gpt-6-luna`, коротко отвечает и предлагает кнопки `✏️ Исправить` / `↩️ Отменить всё`.
3. **Сохранность данных после рестарта**:
   - выполнить `ssh rodnya "systemctl restart hidl.service"`;
   - проверить `check_health.py --db /var/lib/hidl/hidl.db` (`Sprint 9 tables present`, `integrity=ok`);
   - в Telegram выполнить `/diary` и `/today` — все записи, созданные до рестарта, на месте.
4. **Голос (отдельно)**:
   - если `STT_PROVIDER=auto` и настроен OpenAI/Groq/Qwen ключ — отправить короткое голосовое сообщение и проверить расшифровку + сохранение;
   - если голос пока отложен — выставить `STT_PROVIDER=off` в `/etc/hidl/hidl.env` и перезапустить сервис.
