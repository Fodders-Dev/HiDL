"""База спортивного тренера и поведенческой психологии привычек HiDL (чистый Python, без aiogram)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PLAYBOOK_PATH = Path(__file__).resolve().parents[1] / "data" / "knowledge" / "coach_playbook.json"
_PLAYBOOK_CACHE: List[Dict[str, Any]] = []

TOPIC_ORDER: Tuple[Tuple[str, str], ...] = (
    ("nutrition", "🥗 Питание и белок"),
    ("visceral_neat", "🔥 Висц. жир и шаги"),
    ("scales_water", "⚖ Весы и вода"),
    ("training_med", "🏋️ Силовые (MED)"),
    ("anti_procrastination", "🧠 Анти-распиздяй"),
)


def load_coach_playbook() -> List[Dict[str, Any]]:
    """Загрузить карточки научно-практической базы тренера с кэшем в памяти."""
    global _PLAYBOOK_CACHE
    if _PLAYBOOK_CACHE:
        return _PLAYBOOK_CACHE
    try:
        raw = json.loads(PLAYBOOK_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            _PLAYBOOK_CACHE = [dict(x) for x in raw if isinstance(x, dict)]
    except Exception:
        _PLAYBOOK_CACHE = []
    return _PLAYBOOK_CACHE


def list_playbook_topics() -> List[Tuple[str, str, int]]:
    """Вернуть список (topic_key, label, cards_count) для навигации в базе тренера."""
    cards = load_coach_playbook()
    counts: Dict[str, int] = {}
    for c in cards:
        t = str(c.get("topic") or "")
        if t:
            counts[t] = counts.get(t, 0) + 1
    result: List[Tuple[str, str, int]] = []
    seen = set()
    for t_key, label in TOPIC_ORDER:
        if counts.get(t_key, 0) > 0:
            result.append((t_key, label, counts[t_key]))
            seen.add(t_key)
    for c in cards:
        t_key = str(c.get("topic") or "")
        if t_key and t_key not in seen:
            result.append((t_key, str(c.get("topic_label") or t_key), counts.get(t_key, 1)))
            seen.add(t_key)
    return result


def get_topic_cards(topic: str) -> List[Dict[str, Any]]:
    """Вернуть все карточки выбранной темы."""
    clean = str(topic or "").strip()
    return [c for c in load_coach_playbook() if str(c.get("topic") or "") == clean]


def get_coach_card(card_id: str) -> Optional[Dict[str, Any]]:
    """Найти карточку по её id."""
    clean = str(card_id or "").strip()
    for c in load_coach_playbook():
        if str(c.get("id") or "") == clean:
            return c
    return None


def _num_or_none(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def extract_coach_signals(ctx: Dict[str, Any], user_text: str = "") -> List[str]:
    """Выделить активные сигналы тела, питания, тренировок и поведения из снимка контекста."""
    signals: List[str] = []

    def add(sig: str) -> None:
        if sig and sig not in signals:
            signals.append(sig)

    txt = str(user_text or "").strip().lower().replace("ё", "е")
    if any(k in txt for k in ("лень", "прокрастин", "забил", "сорвал", "отлож", "не могу начать", "распиз")):
        add("procrastination")
    if any(k in txt for k in ("переел", "объел", "сорвался", "сладк", "пицц", "бургер", "доставк")):
        add("overeat")
    if any(k in txt for k in ("висц", "живот", "пуз", "бока", "жир")):
        add("visceral_high")
    if any(k in txt for k in ("вод", "отек", "плюс", "привес", "взвес", "весы", "плато", "стоит")):
        add("water_spike")
        add("plateau")
    if any(k in txt for k in ("белок", "протеин", "мышц", "тофу", "чечевиц")):
        add("protein_low")
    if any(k in txt for k in ("трен", "спорт", "сил", "гантел", "отжим", "присед", "шаг")):
        add("beginner_med")
    if any(k in txt for k in ("сон", "устал", "не выспал", "разбит")):
        add("sleep_debt")

    trend = ctx.get("trend") or ctx.get("body_trend") or {}
    latest = ctx.get("latest") or ctx.get("latest_weight") or (trend.get("latest") if isinstance(trend, dict) else None) or {}
    first = trend.get("first") if isinstance(trend, dict) else None

    visc = _num_or_none(latest.get("visceral_fat"))
    if visc is not None and visc >= 13.0:
        add("visceral_high")

    w_delta = _num_or_none(trend.get("weight_delta"))
    f_delta = _num_or_none(trend.get("fat_delta"))
    m_delta = _num_or_none(trend.get("muscle_delta"))
    water_now = _num_or_none(latest.get("water_pct"))
    water_prev = _num_or_none(first.get("water_pct")) if isinstance(first, dict) else None
    water_delta = (water_now - water_prev) if (water_now is not None and water_prev is not None) else None

    if f_delta is not None and f_delta <= -0.3 and (w_delta is None or w_delta >= -0.4):
        if m_delta is None or m_delta >= -0.2:
            add("recomp")

    if w_delta is not None and w_delta > 0.15:
        if (water_delta is not None and water_delta >= 0.3) or (f_delta is not None and f_delta <= 0.1):
            add("water_spike")

    if w_delta is not None and -0.2 <= w_delta <= 0.2 and int(trend.get("count") or 0) >= 2:
        add("plateau")

    wk = ctx.get("week_workouts") or {}
    wk_count = int(wk.get("count") or 0)
    if wk_count == 0:
        add("zero_workouts")
        add("beginner_med")

    n_adv = ctx.get("nutrition_advice") or {}
    tgt = n_adv.get("targets") if isinstance(n_adv, dict) else {}
    eat = n_adv.get("eaten") if isinstance(n_adv, dict) else {}
    tgt_p = float((tgt or {}).get("protein_g") or 0)
    eat_p = float((eat or {}).get("protein_g") or 0)
    eat_k = float((eat or {}).get("kcal") or 0)
    tgt_k = float((tgt or {}).get("kcal") or 0)
    if tgt_p > 0 and eat_k > 0 and eat_p < tgt_p * 0.65:
        add("protein_low")
    if tgt_k > 0 and eat_k > tgt_k + 250:
        add("overeat")

    meal_profile = str(ctx.get("meal_profile") or "").strip().lower()
    if meal_profile == "vegan":
        add("vegan_protein")

    patterns = set(ctx.get("patterns") or [])
    k_state = ctx.get("karma_state") or {}
    if "late_night_drift" in patterns or int(k_state.get("sleep_debt") or 0) >= 2:
        add("sleep_debt")
    if "frequent_snoozer" in patterns or int(k_state.get("tasks_debt") or 0) >= 2:
        add("procrastination")

    if bool(ctx.get("missed_weigh_in")):
        add("missed_weigh_in")

    goal = str(ctx.get("weight_goal") or "").strip().lower()
    if goal in {"loss", "recomp"}:
        add(goal)
    act = str(ctx.get("activity_level") or "").strip().lower()
    if act == "sedentary":
        add("sedentary")

    return signals


def select_coach_principles(
    ctx: Dict[str, Any],
    user_text: str = "",
    *,
    limit: int = 2,
) -> List[Dict[str, Any]]:
    """Подобрать 1–N самых релевантных карточек коуча под живой контекст и запрос."""
    cards = load_coach_playbook()
    if not cards or limit <= 0:
        return []
    signals = extract_coach_signals(ctx, user_text)
    sig_priority = {sig: max(1, len(signals) - idx) for idx, sig in enumerate(signals)}

    scored: List[Tuple[int, int, Dict[str, Any]]] = []
    for idx, card in enumerate(cards):
        triggers = [str(t) for t in (card.get("triggers") or [])]
        score = 0
        for trig in triggers:
            if trig in sig_priority:
                score += sig_priority[trig] * 10 + 5
        scored.append((score, -idx, card))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored[:limit]]


def format_coach_knowledge_for_llm(cards: List[Dict[str, Any]]) -> str:
    """Компактная выжимка принципов для системного/пользовательского промпта LLM."""
    if not cards:
        return ""
    lines = ["=== Научно-практическая база тренера HiDL (опирайся на неё при совете) ==="]
    for card in cards:
        lines.append(
            f"- {card.get('title')} ({card.get('source')}): {card.get('summary')} "
            f"Правило: {card.get('action_rule')}"
        )
    return "\n".join(lines)


def build_body_action_plan(
    *,
    latest: Optional[Dict[str, Any]],
    trend: Optional[Dict[str, Any]],
    week_workouts: Optional[Dict[str, Any]],
    nutrition_advice: Optional[Dict[str, Any]],
    user: Dict[str, Any],
    meal_profile: str = "",
) -> Dict[str, Any]:
    """Сформировать конкретный разбор и действие на сегодня по замерам тела и активности."""
    coach_mode = str(user.get("coach_mode") or "balanced").strip().lower()
    goal = str(user.get("weight_goal") or "loss").strip().lower()
    if goal == "mindful":
        return {
            "headline": "Осознанный ритм",
            "diagnosis": "Ты смотришь на самочувствие без жёсткой гонки за цифрами.",
            "action_today": "Сделай 15–20 минут спокойной прогулки или мягкую разминку и выпей воды.",
            "principle_title": "Правило 3 минут",
            "principle_source": "BJ Fogg — Tiny Habits",
            "suggest_lazy_workout": True,
        }

    ctx = {
        "latest": latest or {},
        "trend": trend or {},
        "week_workouts": week_workouts or {},
        "nutrition_advice": nutrition_advice or {},
        "meal_profile": meal_profile,
        "weight_goal": goal,
        "activity_level": user.get("activity_level"),
    }
    cards = select_coach_principles(ctx, limit=1)
    card = cards[0] if cards else None

    visc = _num_or_none((latest or {}).get("visceral_fat"))
    wk_count = int((week_workouts or {}).get("count") or 0)
    w_d = _num_or_none((trend or {}).get("weight_delta"))
    f_d = _num_or_none((trend or {}).get("fat_delta"))
    tgt = (nutrition_advice or {}).get("targets") or {}
    eat = (nutrition_advice or {}).get("eaten") or {}
    rem_p = int(round(float(tgt.get("protein_g") or 0) - float(eat.get("protein_g") or 0)))

    diag_parts: List[str] = []
    if visc is not None and visc >= 13.0:
        diag_parts.append(f"Висц. жир {visc:g} (повышенный — отлично сгорает от шагов и дефицита).")
    elif f_d is not None and f_d <= -0.3 and (w_d is None or w_d >= -0.4):
        diag_parts.append("Идёт чистая рекомпозиция: жир падает при удержании мышц.")

    if wk_count == 0:
        if coach_mode == "strict":
            diag_parts.append("На этой неделе 0 тренировок — мышцы в дефиците без стимула не удержатся.")
        else:
            diag_parts.append("На этой неделе пока без тренировок — самое время для мягкого старта.")

    if not diag_parts:
        diag_parts.append(str((card or {}).get("summary") or "Ровный ритм замеров и умеренный дефицит работают лучше рывков.").split(".")[0] + ".")

    if wk_count == 0:
        action = (
            "Сегодня: 3-мин микро-старт («🫠 Лень начинать») или 15 мин ходьбы после еды."
            if coach_mode != "strict"
            else "Прямо сейчас: 3-мин старт («🫠 Лень начинать») + минимум 7500 шагов за день!"
        )
    elif rem_p >= 30 and float(eat.get("kcal") or 0) > 0:
        action = f"Докинь сегодня ещё ~{rem_p} г белка (тофу/бобовые/протеин), чтобы защитить мышцы."
    else:
        action = str((card or {}).get("action_rule") or "Держи норму КБЖУ и 7–8 тыс. шагов сегодня.")

    tone_line = ""
    if card:
        tone_line = str(card.get("strict_line") if coach_mode == "strict" else card.get("gentle_line") or "")

    return {
        "headline": str((card or {}).get("title") or "Фокус тренера"),
        "diagnosis": " ".join(diag_parts).strip(),
        "action_today": f"{tone_line} {action}".strip() if tone_line and coach_mode == "strict" else action,
        "principle_title": str((card or {}).get("title") or ""),
        "principle_source": str((card or {}).get("source") or ""),
        "suggest_lazy_workout": wk_count == 0,
    }
