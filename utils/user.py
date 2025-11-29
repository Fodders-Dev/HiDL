from db import repositories as repo


async def ensure_user(db, telegram_id: int, full_name: str = "Друг"):
    """Return existing user or create a default one with base routines."""
    user = await repo.get_user_by_telegram_id(db, telegram_id)
    if user:
        return dict(user)
    name = full_name or "Друг"
    user_id = await repo.create_user(db, telegram_id, name, "UTC", "08:00", "23:00")
    await repo.ensure_user_routines(db, user_id)
    created = await repo.get_user(db, user_id)
    return dict(created) if created else {"id": user_id, "name": name, "timezone": "UTC"}
