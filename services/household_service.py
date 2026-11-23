"""Сервис уведомлений и кооперации внутри общего дома (household)."""

from __future__ import annotations

import datetime
from typing import Any, Dict, Optional

from db import repositories as repo
from services import karma_service
from utils.time import is_in_time_window, local_date_str


def is_partner_quiet_or_paused(partner: Dict[str, Any], now_utc: datetime.datetime) -> bool:
    """Проверить, стоит ли сейчас воздержаться от уведомления партнёра."""
    if int(partner.get("quiet_mode") or 0):
        return True
    if partner.get("sleep_confirmed_at"):
        return True
    if karma_service.is_sick_freeze_active(partner, now_utc):
        return True
    tz = str(partner.get("timezone") or "UTC")
    local_date = local_date_str(now_utc, tz)
    pause_until = str(partner.get("pause_until") or "").strip()
    if pause_until and local_date <= pause_until:
        return True
    sleep_hhmm = str(partner.get("sleep_target_sleep") or partner.get("sleep_time") or "").strip()
    wake_hhmm = str(partner.get("sleep_target_wake") or partner.get("wake_up_time") or "").strip()
    if sleep_hhmm and wake_hhmm and is_in_time_window(now_utc, tz, sleep_hhmm, wake_hhmm):
        return True
    return False


async def notify_household_partners(
    bot,
    db,
    actor_user: Dict[str, Any],
    text: str,
    *,
    now_utc: Optional[datetime.datetime] = None,
) -> int:
    """Бережно отправить уведомление остальным участникам дома (учитывая тихие часы и ЧП)."""
    if bot is None or not text:
        return 0
    actor_id = int(actor_user["id"])
    household_id = await repo.get_or_create_household(db, actor_id)
    household = await repo.get_household(db, household_id)
    if not household:
        return 0
    settings = household.get("settings") or {}
    if not settings.get("notify_partner", True):
        return 0

    now = now_utc or datetime.datetime.utcnow()
    members = await repo.list_household_members(db, household_id)
    sent = 0
    for member in members:
        if int(member["id"]) == actor_id:
            continue
        tg_id = member.get("telegram_id")
        if not tg_id:
            continue
        if is_partner_quiet_or_paused(member, now):
            continue
        try:
            await bot.send_message(int(tg_id), text, parse_mode="HTML")
            sent += 1
        except Exception:
            continue
    return sent
