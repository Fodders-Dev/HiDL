# HiDL MCP-сервер для ChatGPT (Streamable HTTP + OAuth 2.1)

Удалённый MCP-сервер `hidl-mcp` позволяет внешнему клиенту (включая ChatGPT Custom Connectors / Apps) читать данные HiDL и записывать задачи, напоминания, покупки, продукты, расходы и заметки в ту же SQLite-БД (в режиме WAL) без запуска второго Telegram polling.

---

## 1. Архитектура

- **Точка входа:** [`mcp_main.py`](../mcp_main.py) запускает `aiohttp.web`-приложение из [`mcp_server/app.py`](../mcp_server/app.py) на `127.0.0.1:8765` (без импорта `aiogram`).
- **Общая БД (WAL):** оба сервиса (`hidl.service` и `hidl-mcp.service`) открывают один файл SQLite с `PRAGMA journal_mode=WAL` и `busy_timeout=5000`.
- **HTTPS reverse proxy:** перед `127.0.0.1:8765` ставится Caddy ([`deploy/caddy/Caddyfile.example`](../deploy/caddy/Caddyfile.example)), обеспечивающий TLS-терминацию и проброс SSE (`flush_interval -1`). Если UDP 443 занят на сервере, используется `servers { protocols h1 h2 }`.

---

## 2. Авторизация и приватность

- **OAuth 2.1 + PKCE (`S256`) + Dynamic Client Registration (RFC 7591):**
  - `GET /.well-known/oauth-protected-resource` (RFC 9728)
  - `GET /.well-known/oauth-authorization-server` и `GET /.well-known/openid-configuration` (RFC 8414)
  - `POST /oauth/register` (выдача `client_id` и при необходимости `client_secret`)
  - `GET/POST /oauth/authorize` (форма подтверждения доступа с проверкой пароля `MCP_ACCESS_PASSWORD` и CSRF-подписью)
  - `POST /oauth/token` (`authorization_code` с проверкой `S256` PKCE и `refresh_token` с ротацией)
  - Коды и токены хранятся в SQLite только в виде SHA-256 хешей (`mcp_oauth_clients`, `mcp_oauth_codes`, `mcp_oauth_tokens`).
- **Опциональный статический токен (`MCP_BEARER_TOKEN`):** подходит для CLI/скриптов/MCP Inspector в заголовке `Authorization: Bearer ...`.
- **Изоляция владельца:** `MCP_OWNER_TELEGRAM_ID` жёстко задаёт единственного пользователя БД, к данным которого имеют доступ инструменты. Чужие пользователи в `users` недоступны.
- **Разделение прав:**
  - `MCP_ALLOW_DIARY=0` полностью скрывает и блокирует чтение/поиск/запись дневника (`hidl_get_diary`, `hidl_search_diary`, `hidl_add_diary_entry`, ресурс `hidl://diary/recent`, а также дневниковые объекты в `search`/`fetch`).
  - `MCP_READONLY=1` переводит сервер в режим только чтения (скрывает и блокирует все инструменты записи).

---

## 3. Доступные инструменты MCP (`tools/list`)

### Чтение (`readOnlyHint=true`)
- `hidl_get_today` — рутины, задачи плана дня, напоминания, дела по дому, сроки продуктов (и последние записи дневника, если `MCP_ALLOW_DIARY=1`).
- `hidl_list_tasks` — задачи плана дня на дату и активные напоминания.
- `hidl_list_shopping` — общий и личный списки покупок.
- `hidl_get_kitchen` — холодильник, профиль питания и рецепты (масштабируемые под `servings`).
- `hidl_get_finance` — траты за период, сумма за месяц и лимит.
- `hidl_get_diary` / `hidl_search_diary` — чтение и поиск по основам слов *(только при `MCP_ALLOW_DIARY=1`)*.
- `search` / `fetch` — универсальный поиск и загрузка карточек объектов для коннектора ChatGPT.

### Запись (`MCP_READONLY=0`)
- `hidl_add_task`, `hidl_set_task_done`
- `hidl_add_reminder`
- `hidl_manage_shopping` (`add`, `remove`, `mark_bought`)
- `hidl_add_pantry_item`
- `hidl_add_expense`
- `hidl_add_diary_entry` *(только при `MCP_ALLOW_DIARY=1`; сохраняет `source="mcp_chatgpt"`)*

---

## 4. Подключение в ChatGPT

1. Направьте DNS A-запись поддомена (например, `mcp.example.com`) на IP сервера и примените [`deploy/caddy/Caddyfile.example`](../deploy/caddy/Caddyfile.example).
2. Заполните в env-файле сервиса `MCP_PUBLIC_BASE_URL`, `MCP_OWNER_TELEGRAM_ID`, `MCP_ACCESS_PASSWORD` и запустите `hidl-mcp.service` ([`deploy/systemd/hidl-mcp.service`](../deploy/systemd/hidl-mcp.service)).
3. В ChatGPT → **Settings / Connectors / Apps → Add custom MCP server** укажите URL `https://mcp.example.com/mcp`, выберите тип авторизации **OAuth** и подтвердите доступ паролем `MCP_ACCESS_PASSWORD`.
