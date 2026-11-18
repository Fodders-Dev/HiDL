# Голосовые (voice) — интеграция, конфигурация и надёжность

## Реализованный пайплайн
1. Входящее голосовое сообщение (`F.voice` в `handlers/inbox.py`) передаётся в `services/inbox_service.capture_and_process_voice`.
2. **Сначала сохранение, потом внешние вызовы**: запись создаётся в `inbox_messages` (с `telegram_file_id`, `duration_sec`, `status="received"` и уникальным ключом `(user_id, chat_id, telegram_message_id)`). Повторная доставка того же апдейта Telegram не вызывает повторно STT/LLM и не создаёт дубликатов.
3. **Расшифровка (STT)** в `services/stt.py`:
   - Скачивает аудиофайл через Telegram Bot API (`download_telegram_voice`).
   - Пробует настроенные провайдеры (`STT_PROVIDER=auto` → `custom` → `groq` → `openai` → `qwen`) с автоматическим runtime failover.
   - При сбое STT сырая запись остаётся в `inbox_messages` (`status="failed_stt"`), пользователю отправляется короткое сообщение с кнопкой «🔁 Повторить расшифровку».
4. **Сохранение расшифровки и разбор**:
   - Расшифрованный текст сразу пишется в `inbox_messages.raw_text` (`status="transcribed"`) до обращения к LLM.
   - Далее `services/inbox_parser.analyze_inbox_text` извлекает задачи, напоминания, покупки, дневниковые записи, идеи и выполненные действия; при сбое LLM включается детерминированный fallback без потери текста.
5. **Короткий ответ и правки**:
   - Пользователь видит расшифровку (сокращённо), список записанного, уточнение по неоднозначным датам/времени и кнопки «✏️ Исправить» / «↩️ Отменить всё».

## Переменные окружения (`.env`)
- `STT_PROVIDER`: `auto` | `custom` | `groq` | `openai` | `qwen`
- `GROQ_API_KEY`, `STT_GROQ_MODEL` (по умолчанию `whisper-large-v3-turbo`)
- `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_PROXY_URL`, `STT_OPENAI_MODEL` (по умолчанию `whisper-1`)
- `QWEN_API_KEY`, `QWEN_BASE_URL`, `STT_QWEN_MODEL` (по умолчанию `qwen3-asr-flash`)
- `STT_API_KEY`, `STT_BASE_URL`, `STT_MODEL`, `STT_PROXY_URL` — произвольный OpenAI-compatible endpoint
- `STT_LANGUAGE` (по умолчанию `ru`), `STT_TIMEOUT_SEC` (по умолчанию `30`), `STT_MAX_DURATION_SEC` (по умолчанию `180`)

## Ограничения
- Длительность голосового ограничена `STT_MAX_DURATION_SEC` (по умолчанию 180 сек).
- Для работы в живом боте необходим хотя бы один настроенный STT-ключ в `.env`; без него голосовые сохраняются как `failed_stt` с честным уведомлением.
