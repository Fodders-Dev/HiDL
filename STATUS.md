# HIDL_bot — Статус

- **Ветка:** `main`
- **Последний продуктовый коммит:** `3b7fcd0` (2026-11-26) — Fix 2x calorie scaling, album photo previews, meal slots, status tasks, DB locking & shutdown
- **Недавно сделано:**
  - `docs/hidl-object-map.md` (27.11): OOUX-карта (15 сущностей / 57 таблиц), матрица `CRUD x Каналы x Роли`, Breadboard (включая FigJam-доску), Delete-first реестр и мини-контракт для «Умной адаптации рецепта под холодильник».
  - `AGENTS.md` (27.11): закреплён 3-шаговый процесс `OOUX -> Breadboard -> Код`.
  - `3b7fcd0` (26.11): фиксы 2× калорий, альбомов фото, `meal_slot`, статус-задач, `busy_timeout=30000`, scheduler jitter и graceful shutdown.
- **Проверка:** `python -m tests.smoke`
- **Следующие шаги:**
  1. Утвердить с пользователем `docs/hidl-object-map.md` + мини-контракт в разделе 5.
  2. Реализовать умную адаптацию рецептов под холодильник (`✨ Заменить по холодильнику`) и гигиену «Меню на 3 дня».
- **Блокеры:** нет.
