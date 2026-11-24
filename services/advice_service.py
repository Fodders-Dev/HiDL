"""Ситуативные советы HiDL под реальный контекст дня и режим кармы."""

from __future__ import annotations

import datetime
import html
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from llm_client import LLMClient, client as default_llm_client
from services import coach_knowledge, context_builder

ADVICE_PATH = Path(__file__).resolve().parents[1] / "data" / "knowledge" / "situational_advice.json"
_CACHE: List[Dict[str, Any]] = []


def load_situational_advice() -> List[Dict[str, Any]]:
    """Загрузить карточки ситуативных советов с кэшем в памяти."""
    global _CACHE
    if _CACHE:
        return _CACHE
    try:
        data = json.loads(ADVICE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            _CACHE = [dict(x) for x in data if isinstance(x, dict)]
    except Exception:
        _CACHE = []
    return _CACHE


def detect_situations(snapshot: Dict[str, Any], user_text: str = "") -> List[str]:
    """Определить приоритетные жизненные ситуации по тексту запроса и снимку дня."""
    ranked: List[str] = []
    txt = (user_text or "").strip().lower()

    def add(key: str) -> None:
        if key and key not in ranked:
            ranked.append(key)

    # 1. Явные сигналы из сообщения пользователя
    if any(k in txt for k in ("не могу уснуть", "бессон", "залип", "не сплю", "спать")):
        add("late_night")
        add("sleep_debt")
    if any(k in txt for k in ("проснуть", "не могу встать", "будильник", "сонный")):
        add("cant_wake_up")
    if any(k in txt for k in ("не ел", "голод", "хочу есть", "поесть", "завтрак", "обед", "ужин")):
        add("skipped_meal")
    if any(k in txt for k in ("готовить", "готовка", "нечего есть", "холодильник пуст")):
        add("no_energy_cook")
        add("empty_fridge")
    if any(k in txt for k in ("много дел", "завал", "не успеваю", "перегруж", "хвост")):
        add("overwhelmed_tasks")
    if any(k in txt for k in ("лень", "прокрастин", "не могу начать", "откладыва", "туплю")):
        add("procrastination")
    if any(k in txt for k in ("трево", "паник", "стресс", "тяжело", "выгор", "устал", "нет сил")):
        add("anxious")
        add("no_energy_cook")
    if any(k in txt for k in ("бардак", "грязно", "уборк", "посуд")):
        add("messy_home")
    if any(k in txt for k in ("денег", "трат", "бюджет", "доставк")):
        add("budget_stress")

    # 2. Живые сигналы из снимка дня пользователя
    hour = int(snapshot.get("local_hour") or 12)
    mins_to_sleep = int(snapshot.get("minutes_to_sleep") or 300)
    state = snapshot.get("karma_state") or {}
    score = int(snapshot.get("karma_score") if snapshot.get("karma_score") is not None else 70)
    slot_st = snapshot.get("slot_status") or {}
    patterns = set(snapshot.get("patterns") or [])
    pantry_count = len(snapshot.get("pantry_items") or [])
    pending_count = len(snapshot.get("pending_tasks") or [])

    if mins_to_sleep <= 0 or hour >= 23 or hour < 4 or snapshot.get("sleep_confirmed"):
        add("late_night")
    if int(state.get("sleep_debt") or 0) >= 2 or "late_night_drift" in patterns:
        add("sleep_debt")
    if 0 <= mins_to_sleep <= 75:
        add("evening_winddown")
    if 5 <= hour <= 9:
        if slot_st.get("wake") != "done":
            add("cant_wake_up")
        add("morning_start")
    if (hour >= 14 and slot_st.get("lunch") not in {"done"}) or int(state.get("food_debt") or 0) >= 2:
        add("skipped_meal")
    if pantry_count <= 1:
        add("empty_fridge")
    if pending_count >= 4:
        add("overwhelmed_tasks")
    if score < 25:
        add("strict_recovery")
        add("low_karma")
    elif score < 50:
        add("low_karma")
    elif score >= 80:
        add("high_streak")
    if 14 <= hour <= 17:
        add("afternoon_slump")
    if hour >= 18:
        add("no_energy_cook")
        add("evening_winddown")

    # Гарантированный fallback по времени суток
    if not ranked:
        if hour < 12:
            add("morning_start")
        elif hour < 18:
            add("afternoon_slump")
        else:
            add("evening_winddown")
    return ranked


def pick_situational_advice(
    snapshot: Dict[str, Any],
    user_text: str = "",
    *,
    exclude_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """Подобрать карточку совета под приоритетную ситуацию и текущий режим кармы."""
    cards = load_situational_advice()
    if not cards:
        return {
            "id": "fallback",
            "situation": "general",
            "title": "Один спокойный шаг",
            "advice": "Не нужно решать всё сразу — начни с воды и одного понятного действия на 5 минут.",
            "micro_step": "Выпей стакан воды и закрой один мелкий пункт на сегодня.",
            "selected_tone_text": "Двигайся спокойно и шаг за шагом.",
        }

    excluded = exclude_ids or set()
    situations = detect_situations(snapshot, user_text)
    mode_code = str((snapshot.get("karma_mode") or {}).get("code") or "care")
    tone_key = "firm" if mode_code in {"strict", "alarm"} else "warm"

    chosen: Optional[Dict[str, Any]] = None
    for sit in situations:
        matching = [c for c in cards if c.get("situation") == sit and c.get("id") not in excluded]
        if matching:
            chosen = matching[0]
            break
    if chosen is None:
        fresh = [c for c in cards if c.get("id") not in excluded]
        chosen = fresh[0] if fresh else cards[0]

    out = dict(chosen)
    variants = out.get("tone_variants") or {}
    out["selected_tone_text"] = str(variants.get(tone_key) or variants.get("warm") or "")
    out["tone_variant_key"] = tone_key
    return out


def format_advice_card_html(snapshot: Dict[str, Any], card: Dict[str, Any]) -> str:
    """Собрать структурированный HTML-совет с привязкой к текущему дню."""
    mode = snapshot.get("karma_mode") or {"emoji": "🙂", "label": "Спокойная забота"}
    score = int(snapshot.get("karma_score") or 70)
    pending_count = len(snapshot.get("pending_tasks") or [])
    mins_left = int(snapshot.get("minutes_to_sleep") or 0)
    time_line = f"{snapshot.get('local_time')} · до отбоя " + (
        f"{mins_left} мин" if mins_left >= 0 else f"уже +{abs(mins_left)} мин сверх графика"
    )

    lines = [
        f"💡 <b>{html.escape(str(card.get('title') or 'Совет сейчас'))}</b>",
        f"<i>{mode['emoji']} Карма {score}/100 ({html.escape(str(mode.get('label')))}) · {html.escape(time_line)} · задач: {pending_count}</i>",
        "",
        html.escape(str(card.get("selected_tone_text") or "")),
        html.escape(str(card.get("advice") or "")),
        "",
        f"🎯 <b>Микро-шаг прямо сейчас:</b> {html.escape(str(card.get('micro_step') or ''))}",
    ]
    return "\n".join(line for line in lines if line is not None).strip()


def _llm_text_to_safe_html(text: str) -> str:
    """Конвертировать ответ LLM (с возможным Markdown) в безопасный Telegram HTML."""
    escaped = html.escape(str(text or "").strip())
    lines: List[str] = []
    for raw_line in escaped.splitlines():
        line = re.sub(r"^#{1,3}\s+(.+)$", r"<b>\1</b>", raw_line)
        line = re.sub(r"^\*\s+", "• ", line)
        lines.append(line)
    out = "\n".join(lines)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"__(.+?)__", r"<b>\1</b>", out)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    return out


async def build_situational_advice_reply(
    db,
    user: Dict[str, Any],
    user_text: str = "",
    *,
    reply_context: str = "",
    llm_client_instance: Optional[LLMClient] = None,
    exclude_ids: Optional[Set[str]] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Сформировать живой ситуативный совет (LLM с контекстом дня или чёткая карточка)."""
    snapshot = await context_builder.build_user_day_snapshot(db, user, now_utc=now_utc)
    card = pick_situational_advice(snapshot, user_text, exclude_ids=exclude_ids)

    clean_req = (user_text or "").strip()
    is_rich_user_question = bool(
        clean_req
        and len(clean_req) >= 8
        and clean_req.lower() not in {"/advice", "совет", "дай совет", "ещё совет", "more"}
    )
    client = llm_client_instance or default_llm_client
    if is_rich_user_question and client.is_configured():
        ctx_block = context_builder.format_context_for_llm(snapshot)
        principles = coach_knowledge.select_coach_principles(snapshot, clean_req, limit=2)
        coach_block = coach_knowledge.format_coach_knowledge_for_llm(principles)
        sys_prompt = (
            "Ты — карманная помощница и коуч HiDL. Сначала дай прямой и понятный ответ на вопрос пользователя "
            "(простым языком, без канцелярита и без Markdown-разметки ** и ##; 3–5 коротких предложений + 1 практичный вывод). "
            "Если пользователь спрашивает про КБЖУ, дефицит, поддержание (TDEE), вес или жир — объясняй на реальных цифрах из снимка дня (ориентир КБЖУ vs TDEE/BMR). "
            "Опирайся ТОЛЬКО на факты из снимка дня и научно-практические принципы тренера ниже; строго держи тон режима кармы и коуча. Не выдумывай несуществующие события или рецепты."
        )
        reply_ctx_clean = str(reply_context or "").strip()[:400]
        usr_prompt = (
            f"{ctx_block}\n\n"
            + (f"{coach_block}\n\n" if coach_block else "")
            + (f"Сообщение, на которое отвечает пользователь:\n{reply_ctx_clean}\n\n" if reply_ctx_clean else "")
            + f"Ориентир-карточка: {card.get('title')} — {card.get('micro_step')}\n\n"
            f"Запрос пользователя:\n{clean_req}"
        )
        res = await client.complete_text(sys_prompt, usr_prompt, temperature=0.35, max_tokens=380)
        if res.ok and res.text.strip():
            mode = snapshot["karma_mode"]
            header = f"💡 <b>Совет HiDL</b> ({mode['emoji']} {snapshot['karma_score']}/100)\n\n"
            return header + _llm_text_to_safe_html(res.text), {"card": card, "snapshot": snapshot, "used_llm": True}

    return format_advice_card_html(snapshot, card), {"card": card, "snapshot": snapshot, "used_llm": False}
