"""Гибридный разбор свободных текстовых и расшифрованных голосовых сообщений.

LLM отвечает только за понимание и извлечение структурных кандидатов.
Временные вычисления, защита от ложных обязательств и fallback без сети
выполняются детерминированным кодом Python.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from llm_client import LLMClient, client as default_llm_client
from utils.time import format_date_display, parse_hhmm, tzinfo_from_string

VALID_KINDS = {
    "task",
    "reminder",
    "calendar_event",
    "shopping",
    "shopping_remove",
    "expense",
    "diary",
    "idea",
    "done",
    "pantry_add",
    "pantry_remove",
    "pulse_log",
    "rollover_tasks",
    "shift_bedtime",
    "shift_wake",
    "task_delete",
    "reminder_delete",
    "meal_log",
    "weight_log",
    "workout_log",
}
VALID_INTENTS = {"capture", "query", "chat", "support"}
VALID_QUERY_TYPES = {
    "none",
    "today",
    "calendar",
    "shopping",
    "food",
    "diary",
    "karma",
    "pantry",
    "advice",
    "meal_check",
    "kbju",
}

WEEKDAY_STEMS = {
    "понедель": 0,
    "вторник": 1,
    "сред": 2,
    "четвер": 3,
    "пятниц": 4,
    "суббот": 5,
    "воскрес": 6,
}

SHORT_WEEKDAYS = {
    "пн": 0,
    "вт": 1,
    "ср": 2,
    "чт": 3,
    "пт": 4,
    "сб": 5,
    "вс": 6,
}

_IDEA_GUARDRAIL_RE = re.compile(
    r"(?iu)(?:\bкогда-?нибудь\b|\bхочу\s+когда-?нибудь\b|\bмечтаю\b|"
    r"\bпридумал[аи]?\s+иде\w*|\bиде[яюией]\b|\bзадумк\w*|\bбыло\s+бы\s+круто\b|"
    r"\bможно\s+было\s+бы\b|\bхотелось\s+бы\s+как-?нибудь\b)"
)

_LONG_HORIZON_IDEA_RE = re.compile(
    r"(?iu)(?:\bдо\s+конца\s+(?:этого\s+)?года\b|\bв\s+этом\s+году\b|"
    r"\bв\s+ближайшие\s+месяц\w*\b|\bв\s+этом\s+месяце\b|\bкак-?нибудь\b|\bна\s+будущее\b)"
)

_IMMEDIATE_TASK_TIME_RE = re.compile(
    r"(?iu)(?:\bсегодня\b|\bзавтра\b|\bпослезавтра\b|\bутром\b|\bвечером\b|"
    r"\bчерез\s+\d+\s*(?:мин|час)\w*|\b\d{1,2}:\d{2}\b)"
)

_DIARY_FEELING_RE = re.compile(
    r"(?iu)(?:\bустал[аи]?\b|\bвыжат[аы]?\b|\bвыгорел[аи]?\b|\bтревож\w*|\bтревог\w*|"
    r"\bне\s+могу\s+заснуть\b|\bбессонниц\w*|\bплохо\s+спал[аи]?\b|\bтяжело\b|"
    r"\bнет\s+сил\b|\bмало\s+сил\b|\bгрустн\w*|\bраздраж\w*|\bпережива\w*|"
    r"\bголова\s+болит\b|\bболит\b|\bнастроени\w*|\bчувствую\b)"
)

_EXPLICIT_OBLIGATION_RE = re.compile(
    r"(?iu)(?:\bнапомн\w*|\bнадо\b|\bнужно\b|\bне\s+забыть\b|\bзапланируй\b|\bдобавь\s+задач\w*)"
)

_STATUS_UPDATE_TASK_GUARD_RE = re.compile(
    r"(?iu)(?:^сейчас\s+(?:на\s+\S+\s+)?(?:еду|поеду|победу|иду|пойду|выхожу|работаю|сижу|занимаюсь)\b|"
    r"\bсейчас\b.*\b(?:еду|поеду|победу|иду|пойду|выхожу)\b|"
    r"\bработаю\b.*\b(?:за\s+компом|параллельно|сейчас)\b|"
    r"\bуже\s+(?:еду|иду|работаю|сижу)\b)"
)

_REMINDER_TRIGGER_RE = re.compile(
    r"(?iu)(?:\bнапомн\w*|\bне\s+дать\s+забыть\b|\bпоставь\s+напоминан\w*|\bнапоминалк\w*)"
)

_SHOPPING_REMOVE_RE = re.compile(
    r"(?iu)(?:\b(?:убер[иь]|удал[иь]|убрать|удалить|вычеркн[иь]|вычеркнуть)\b.*\b(?:из\s+(?:списка\s+)?покупок|покупк\w*)\b|"
    r"\bнаш(?:ёл|елся|лась|лось|лись)\b.*\b(?:убер[иь]|удал[иь]|не\s+надо|не\s+нужно|из\s+(?:списка\s+)?покупок)\b|"
    r"\bне\s+(?:надо|нужно)\s+(?:покупать|брать)\b)"
)

_EXPENSE_TRIGGER_RE = re.compile(
    r"(?iu)(?:\b(?:потратил[аи]?|ушло|вышло|отдал[аи]?|заплатил[аи]?)\s+\d+|"
    r"\bкупил[аи]?\s+.+?\s+за\s+\d+|"
    r"\b\d+(?:[.,]\d+)?\s*(?:руб\w*|₽)\b)"
)

_SHOPPING_TRIGGER_RE = re.compile(
    r"(?iu)(?:\bзакончил(?:ся|ась|ись|ось)\b|\bкончил(?:ся|ась|ись|ось)\b|"
    r"\bнадо\s+купить\b|\bнужно\s+купить\b|\bдокупить\b|\bв\s+список\s+покупок\b|"
    r"\bдобавь\s+в\s+покупки\b|\bкупить\b)"
)

_TASK_TRIGGER_RE = re.compile(
    r"(?iu)(?:\bнадо\b|\bнужно\b|\bнеобходимо\b|\bне\s+забыть\b|\bпредстоит\b|"
    r"\bзаняться\b|\bдобавь\s+в\s+план\b|\bсделать\b)"
)

_TASK_ACTION_START_RE = re.compile(
    r"(?iu)^(?:позвонить|написать|сделать|забрать|оплатить|убрать|помыть|записаться|отправить|сходить|проверить)\b"
)

_SUPPORT_TRIGGER_RE = re.compile(
    r"(?iu)(?:\bтревож\w*|\bтревог\w*|\bпаник\w*|\bне\s+могу\s+заснуть\b|\bбессонниц\w*|"
    r"\bплохо\s+сп(?:ал[аи]?|лю)\b|\bнет\s+сил\b|\bмало\s+сил\b|\bвыжат[аы]?\b|"
    r"\bвыгорел[аи]?\b|\bтяжело\b|\bгрустн\w*|\bпережива\w*|\bбоюсь\b|\bстрашно\b|\bодиноко\b)"
)

_DONE_TRIGGER_RE = re.compile(
    r"(?iu)(?:\bуже\s+(?:сделал|купил|оплатил|помыл|убрал|приготовил|позвонил|отправил)[аи]?\b|"
    r"^(?:сделал|выполнил|закрыл|доделал|помыл|убрал|оплатил|приготовил|вынес|позвонил|"
    r"отправил|сходил|проверил|купил|поел|поспал|встал)[аи]?\b)"
)

_VAGUE_WHEN_RE = re.compile(
    r"(?iu)(?:\bпотом\b|\bпозже\b|\bкак-?нибудь\b|\bкогда-?нибудь\b|\bскоро\b|"
    r"\bна\s+днях\b|\bна\s+следующей\s+неделе\b|\bна\s+этой\s+неделе\b|\bна\s+неделе\b|"
    r"\bв\s+следующ\w+\s+(?:понедель|вторник|сред|четвер|пятниц|суббот|воскрес)\w*\b)"
)

_RELATIVE_DATE_MARKER_RE = re.compile(
    r"(?iu)(?:\bсегодня\b|\bзавтра\b|\bпослезавтра\b|\bчерез\s+\d+\s*(?:дн|день|дня|дней|суток)\b|"
    r"\bпонедель|\bвторник|\bсред|\bчетвер|\bпятниц|\bсуббот|\bвоскрес|"
    r"\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\b|\b\d{4}-\d{2}-\d{2}\b)"
)

_SECONDARY_SPLIT_RE = re.compile(
    r",\s*(?=(?:а\s+ещё|и\s+ещё|ещё|дома|надо|нужно|напомн\w*|сегодня|вчера|кстати|"
    r"купить|докупить|закончил\w*|кончил\w*|придумал\w*|потратил\w*)\b)",
    re.IGNORECASE,
)

_AND_ACTION_SPLIT_RE = re.compile(
    r"\s+и\s+(?=(?:позвонить|написать|сделать|забрать|оплатить|купить|убрать|помыть|"
    r"напомн\w*|записаться|отправить|потратил\w*)\b)",
    re.IGNORECASE,
)

_INBOX_SYSTEM_PROMPT = """Ты — умный роутер и модуль разбора входящих сообщений личной помощницы HiDL.
Определи намерение (intent), при необходимости тип запроса (query_type) и короткий ответ (reply), а также разложи факты/дела в items.

Допустимые intent:
- "capture": пользователь фиксирует дела, напоминания, покупки, траты, мысли, идеи или выполненное.
- "query": пользователь спрашивает о своих данных ("что на сегодня", "что купить", "что приготовить", "что я писал про ...").
- "chat": приветствие, благодарность, короткое подтверждение ("привет", "спасибо", "ок").
- "support": эмоциональная тяжесть, тревога, бессонница, выгорание без явной задачи/напоминания.

Допустимые query_type (для intent="query", иначе "none"):
- "today": планы, дела, напоминания на сегодня.
- "calendar": события в календаре / расписание / встречи на сегодня и ближайшие дни.
- "shopping": список покупок / что купить.
- "food": что приготовить / поесть / на ужин, обед, завтрак.
- "diary": поиск в записях дневника/входящих (в query_term укажи ключевую тему, например "папа").
- "karma": уровень кармы, долги по сферам, статус режима.
- "pantry": что сейчас есть в холодильнике/кладовке дома.
- "advice": запрос совета или объяснения по текущей ситуации, питанию, дефициту/профициту калорий, набору/сбросу веса, весам, белку и тренировкам ("что мне сейчас делать", "дай совет", "2210 ккал это надо есть или нельзя больше?", "если переем, буду толстеть?", "почему вес стоит?", "что такое висцеральный жир?").
- "meal_check": пользователь только ДУМАЕТ / ПЛАНИРУЕТ / СПРАШИВАЕТ, стоит ли съесть или выпить конкретное блюдо/напиток ("думаю съесть мороженое", "хочу заказать шаурму, норм?", "можно мне сейчас латте с круассаном?"). В "query_term" верни чистое название блюда/напитка (НЕ создавай meal_log!).
- "kbju": ТОЛЬКО короткий запрос показать сводку/остаток калорий и БЖУ за сегодня ("сколько калорий осталось", "что мне добрать по белку", "мой кбжу", "покажи кбжу"). Если человек спрашивает ЗАЧЕМ/ПОЧЕМУ/МОЖНО ЛИ больше/буду ли толстеть — это "advice", а НЕ "kbju"!

Допустимые типы элементов (kind):
- "task": конкретное дело/задача на сегодня, завтра или ближайшие дни с явным намерением сделать (но НЕ просто рассказ о текущем статусе вроде «сейчас еду на занятия», «работаю за компом» — такие статус-апдейты пиши в "diary"!).
- "reminder": поручение напомнить к дате/времени ("напомни выпить таблетку").
- "calendar_event": встреча/событие для записи в Google Calendar ("запиши в календарь завтра в 15:00 стоматолог на час", "поставь встречу..."). Передай "date_hint", "time_hint", "duration_minutes" (по умолчанию 60) и "location".
- "shopping": что нужно купить или что закончилось дома.
- "shopping_remove": убрать товар из списка покупок ("гречка нашлась, убери из покупок" -> title="Гречка").
- "pantry_add": купленный или имеющийся дома продукт в холодильник ("купил тофу" -> title="Тофу"). Не используй для будущих покупок ("надо купить") или трат с суммой.
- "pantry_remove": явная просьба убрать/вычеркнуть/выкинуть продукт из холодильника ("убери молоко из холодильника" -> title="Молоко"). Для съеденных блюд используй meal_log!
- "meal_log": пользователь сообщает, что КОНКРЕТНО съел, выпил или поел на приём пищи ("поел суп", "съел овсянку с бананом", "выпил капучино на овсяном в Surf Coffee", "на обед был борщ", "пообедал борщом", "позавтракал сырниками"). В title укажи полное описание еды с граммами/местом.
- "weight_log": замеры веса или умных весов ("взвесился 84.8, жир 22.1, мышцы 61").
- "workout_log": выполненная тренировка/активность ("сделал 15 минут с гантелями", "пробежал 20 мин", "позанимался на турнике").
- "pulse_log": только голая отметка Пульса БЕЗ конкретного блюда/названия еды (category: "wake"|"lunch"|"dinner"|"water"|"teeth"|"bedtime", например "я поел", "уже пообедал", "выпил воды", "почистил зубы", "ложусь спать"). Если сказано "съел овсянку и выпил воды" -> верни ОДНОВРЕМЕННО meal_log ("овсянка") и pulse_log (category="water").
- "rollover_tasks": перенести незавершённые дела/хвосты дня на завтра ("перенеси хвосты на завтра").
- "shift_bedtime": перенести время отбоя сегодня на час HH:MM (time_hint="HH:MM", например "ложусь в 00:30").
- "shift_wake": изменить время подъёма/будильника ("завтра подъёмы будут в 6:30", "подъём в 7:00", time_hint="HH:MM") или отметить день без будильника ("завтра без будильника").
- "task_delete": удалить/убрать конкретную задачу из плана дня ("удали задачу ...", "убери задачу ...").
- "reminder_delete": удалить/отменить существующее напоминание ("удали напоминание про ...", "отмени напоминание ...").
- "expense": денежная трата ("потратил 450 на кофе" -> amount=450, category="кофе", title="Кофе").
- "diary": мысли, переживания, усталость, самочувствие, события дня без поручения.
- "idea": задумки, мечты и долгосрочные дела без срока на сегодня ("хочу когда-нибудь...", "до конца года починить ванну", "в этом месяце...", "как-нибудь разобрать балкон").
- "done": то, что пользователь уже сделал или просит отметить/закрыть выполненную задачу ("закрой задачу ...", "отметь задачу ...", "я уже купил билеты").

Строгие правила:
1. Приветствия / "спасибо" / "ок" -> intent="chat", items=[], query_type="none", тёплый короткий reply (1-2 предложения по-русски в женском роде).
2. Вопросы о плане на сегодня / списке покупок / что приготовить / карме / холодильнике / совете / заметках / КБЖУ / примерке еды ("думаю съесть...") -> intent="query", соответствующий query_type (и query_term), items=[] (НЕ создавай пустую запись в дневнике или meal_log!).
3. Тревога / бессонница / усталость без явной задачи или напоминания -> intent="support", практичный и тёплый/деловой reply с учётом «Снимка дня» И один элемент kind="diary". Если сообщение смешанное (intent="capture"), но человек явно жалуется на сильную усталость или выгорание — добавь 1 короткое тёплое предложение поддержки в "reply".
4. Если передан «Снимок дня пользователя» (время, карма, отбой, задачи, холодильник, диета, цель и остаток КБЖУ), строго соблюдай директиву тона и стиля коуча и не выдумывай отсутствующие факты.
5. Чистые мечты, задумки и долгосрочные дела без ближнего срока ("до конца года", "в этом месяце", "когда-нибудь", "как-нибудь") -> только kind="idea", НИКОГДА "task" или "reminder".
6. Сохраняй "source_quote" дословным фрагментом исходного текста. В "model_note" пиши только пометку-гипотезу, не выдумывай факты.
7. Для "shopping", "shopping_remove", "pantry_add", "pantry_remove", "task_delete", "reminder_delete" в "title" пиши чистое название объекта.
8. Для "task", "reminder", "calendar_event", "shift_bedtime" и "shift_wake" передай "date_hint" ("сегодня", "завтра", "послезавтра", "19.11.2026") и "time_hint" ("12:00", "00:30", "в 8", "утром") без вычисления дат.
9. Если дан "Недавний контекст диалога", используй его для коротких продолжений вроде "а ещё?", "другое", "без картошки", "побыстрее". Если в предыдущем ответе обсуждалась еда/рецепты, а пользователь просит "а ещё?" или уточняет пожелание — возвращай intent="query", query_type="food", query_term="more" (или текст фильтра, например "без картошки"), НЕ "diary"!
10. Отвечай строго валидным JSON объектом формата:
{
  "intent": "capture|query|chat|support",
  "reply": "короткий ответ 1-2 предложения (для chat/support или пустого)",
  "query_type": "none|today|calendar|shopping|food|diary|karma|pantry|advice|meal_check|kbju",
  "query_term": "",
  "items": [
    {
      "kind": "task|reminder|calendar_event|shopping|shopping_remove|pantry_add|pantry_remove|meal_log|weight_log|workout_log|pulse_log|rollover_tasks|shift_bedtime|shift_wake|task_delete|reminder_delete|expense|diary|idea|done",
      "title": "...",
      "source_quote": "...",
      "model_note": "",
      "date_hint": "",
      "time_hint": "",
      "duration_minutes": 60,
      "location": "",
      "quantity": 1,
      "unit": "шт",
      "amount": 0,
      "category": "",
      "tags": []
    }
  ]
}"""


@dataclass
class ResolvedSchedule:
    resolved_date: Optional[str]
    resolved_time: Optional[str]
    is_ambiguous: bool
    ambiguity_reason: Optional[str] = None
    candidate_times: list[str] = field(default_factory=list)
    question: Optional[str] = None


@dataclass
class ParsedInboxItem:
    kind: str
    title: str
    source_quote: str
    model_note: str = ""
    tags: list[str] = field(default_factory=list)
    quantity: float = 1.0
    unit: str = "шт"
    schedule: Optional[ResolvedSchedule] = None
    amount: float = 0.0
    category: str = ""
    duration_minutes: int = 60
    location: str = ""


@dataclass
class InboxParseResult:
    items: list[ParsedInboxItem]
    used_llm: bool
    llm_provider: str = ""
    llm_model: str = ""
    llm_raw_json: str = ""
    llm_error: Optional[str] = None
    intent: str = "capture"
    reply_text: str = ""
    query_type: str = "none"
    query_term: str = ""


def compute_one_time_last_sent(
    target_date: Optional[str], today_date: str, freq: int = 9999
) -> Optional[str]:
    """Сместить last_sent назад на freq-delta дней для будущего одноразового напоминания."""
    if not target_date or target_date <= today_date:
        return None
    try:
        due = datetime.date.fromisoformat(target_date)
    except ValueError:
        return None
    return (due - datetime.timedelta(days=freq)).isoformat()


def _to_local_now(now_utc: datetime.datetime, tz_str: str) -> datetime.datetime:
    if now_utc.tzinfo is None:
        utc_dt = now_utc.replace(tzinfo=datetime.timezone.utc)
    else:
        utc_dt = now_utc.astimezone(datetime.timezone.utc)
    return utc_dt.astimezone(tzinfo_from_string(tz_str))


def _extract_relative_delta(src: str) -> Optional[datetime.timedelta]:
    txt = src.lower()
    if re.search(r"\bчерез\s+полчас\w*\b", txt):
        return datetime.timedelta(minutes=30)
    m = re.search(r"через\s+(\d+)\s*(мин\w*|м)\b", txt)
    if m:
        return datetime.timedelta(minutes=int(m.group(1)))
    m = re.search(r"через\s+(\d+)\s*(час\w*|ч)\b", txt)
    if m:
        return datetime.timedelta(hours=int(m.group(1)))
    if re.search(r"\bчерез\s+час\b", txt):
        return datetime.timedelta(hours=1)
    return None


def _extract_date_from_text(src: str, local_today: datetime.date) -> Optional[datetime.date]:
    txt = src.lower()
    if "послезавтра" in txt:
        return local_today + datetime.timedelta(days=2)
    if "завтра" in txt:
        return local_today + datetime.timedelta(days=1)
    if "сегодня" in txt:
        return local_today

    m_days = re.search(r"через\s+(\d+)\s*(?:день|дня|дней|дн|суток)\b", txt)
    if m_days:
        return local_today + datetime.timedelta(days=int(m_days.group(1)))

    m_iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", txt)
    if m_iso:
        try:
            return datetime.date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        except ValueError:
            pass

    m_dmy = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", txt)
    if m_dmy:
        try:
            return datetime.date(int(m_dmy.group(3)), int(m_dmy.group(2)), int(m_dmy.group(1)))
        except ValueError:
            pass

    # DD.MM without year (ignore HH.MM preceded by "в"/"на")
    for m_dm in re.finditer(r"(?<![вк]\s)\b(\d{1,2})\.(\d{1,2})\b(?!\.\d)", txt):
        dd, mm = int(m_dm.group(1)), int(m_dm.group(2))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            try:
                cand = datetime.date(local_today.year, mm, dd)
            except ValueError:
                continue
            if cand < local_today:
                try:
                    cand = datetime.date(local_today.year + 1, mm, dd)
                except ValueError:
                    continue
            return cand

    for stem, wd in WEEKDAY_STEMS.items():
        if re.search(rf"\b{stem}\w*\b", txt):
            delta = (wd - local_today.weekday()) % 7
            if delta == 0:
                delta = 7
            return local_today + datetime.timedelta(days=delta)

    for short_wd, wd in SHORT_WEEKDAYS.items():
        if re.search(rf"\b(?:в|во|на)\s+{short_wd}\b", txt):
            delta = (wd - local_today.weekday()) % 7
            if delta == 0:
                delta = 7
            return local_today + datetime.timedelta(days=delta)

    return None


def _extract_time_from_text(
    src: str,
) -> tuple[Optional[str], list[str], Optional[str]]:
    """Return (resolved_hhmm, ambiguous_candidates, part_of_day)."""
    txt = src.lower()

    # 1. Explicit HH:MM or "в/на/к HH.MM"
    m_hhmm = re.search(r"\b(\d{1,2}):(\d{2})\b", txt)
    if not m_hhmm:
        m_hhmm = re.search(r"\b(?:в|на|к)\s*(\d{1,2})\.(\d{2})\b", txt)
    if m_hhmm:
        hh, mm = int(m_hhmm.group(1)), int(m_hhmm.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}", [], None

    # 2. "12 часов 30" / "8 ч 15"
    m_hm = re.search(r"\b(\d{1,2})\s*(?:час\w*|ч)\s+(\d{1,2})(?:\s*мин\w*)?\b", txt)
    if m_hm:
        hh, mm = int(m_hm.group(1)), int(m_hm.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}", [], None

    # 3. "в 8 утра / в 8 вечера / в 3 дня / в 2 ночи"
    m_qual = re.search(
        r"(?:\b(?:в|на|к)\s*)?(\d{1,2})\s*(?:час\w*\s*)?(утра|дня|вечера|ночи)\b",
        txt,
    )
    if m_qual:
        hh = int(m_qual.group(1))
        qual = m_qual.group(2)
        if 0 <= hh <= 23:
            if qual == "дня" and 1 <= hh <= 5:
                hh += 12
            elif qual == "вечера" and 1 <= hh <= 11:
                hh += 12
            elif qual == "ночи" and hh == 12:
                hh = 0
            return f"{hh:02d}:00", [], None

    if "полдень" in txt:
        return "12:00", [], None
    if "полночь" in txt:
        return "00:00", [], None

    # 4. Bare "в 8" / "на 10" / "к 15" (excluding "в 2 раза" or "через 8")
    m_bare = re.search(
        r"(?<!через\s)\b(?:в|на|к)\s*(\d{1,2})(?:\s*час\w*)?(?!\s*(?:[:.]\d|\d|мин|дн|раз|шт|₽|руб))",
        txt,
    )
    if m_bare:
        hh = int(m_bare.group(1))
        if hh == 0 or 12 <= hh <= 23:
            return f"{hh:02d}:00", [], None
        if 1 <= hh <= 11:
            return None, [f"{hh:02d}:00", f"{hh + 12:02d}:00"], None

    # 5. Standalone hour e.g. when user answers "14:00" or "8" in clarification
    m_only = re.match(r"^\s*(\d{1,2})\s*$", txt)
    if m_only:
        hh = int(m_only.group(1))
        if hh == 0 or 12 <= hh <= 23:
            return f"{hh:02d}:00", [], None
        if 1 <= hh <= 11:
            return None, [f"{hh:02d}:00", f"{hh + 12:02d}:00"], None

    # 6. Part of day without explicit hour
    if re.search(r"\bутр(?:ом|а)\b", txt):
        return None, [], "morning"
    if re.search(r"\bдн(?:ём|ем)\b", txt):
        return None, [], "day"
    if re.search(r"\bвечер(?:ом|а)\b", txt):
        return None, [], "evening"
    if re.search(r"\bночь(?:ю)?\b", txt):
        return None, [], "night"

    return None, [], None


def resolve_temporal_expression(
    date_hint: Optional[str],
    time_hint: Optional[str],
    raw_fragment: str,
    now_utc: datetime.datetime,
    tz_str: str,
) -> ResolvedSchedule:
    """Вычислить дату и время напоминания строго по часовому поясу пользователя."""
    local_now = _to_local_now(now_utc, tz_str)
    local_today = local_now.date()

    raw_lower = (raw_fragment or "").lower()
    combined_for_rel = " ".join(x for x in (raw_fragment or "", date_hint or "", time_hint or "") if x)

    # 1. Relative offset ("через 2 часа", "через 30 минут")
    rel_delta = _extract_relative_delta(combined_for_rel)
    if rel_delta is not None:
        target_dt = local_now + rel_delta
        return ResolvedSchedule(
            resolved_date=target_dt.date().isoformat(),
            resolved_time=target_dt.strftime("%H:%M"),
            is_ambiguous=False,
        )

    # 2. Vague wording check on raw fragment
    has_vague = bool(_VAGUE_WHEN_RE.search(raw_lower))
    has_explicit_cal = bool(
        re.search(r"\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\b|\b\d{4}-\d{2}-\d{2}\b", raw_lower)
    )
    if has_vague and not has_explicit_cal:
        # Preserve time if user wrote "потом в 18:00" -> still vague date
        return ResolvedSchedule(
            resolved_date=None,
            resolved_time=None,
            is_ambiguous=True,
            ambiguity_reason="vague_when",
            candidate_times=[],
            question="Не уверена в дате и времени. Напиши точнее, например: завтра в 12:00.",
        )

    # 3. Date resolution: prefer raw_fragment if it contains any date expression
    date_obj: Optional[datetime.date] = None
    if _RELATIVE_DATE_MARKER_RE.search(raw_lower):
        date_obj = _extract_date_from_text(raw_lower, local_today)
    if date_obj is None and date_hint:
        date_obj = _extract_date_from_text(date_hint, local_today)
    if date_obj is None and raw_lower:
        date_obj = _extract_date_from_text(raw_lower, local_today)

    # 4. Time resolution: check raw_fragment first, then time_hint
    hhmm, ampm_candidates, part_of_day = _extract_time_from_text(raw_lower)
    if hhmm is None and not ampm_candidates and time_hint:
        hhmm, ampm_candidates, hint_part = _extract_time_from_text(time_hint)
        part_of_day = part_of_day or hint_part

    if ampm_candidates:
        resolved_date_str = date_obj.isoformat() if date_obj else None
        date_note = f" на {format_date_display(resolved_date_str)}" if resolved_date_str else ""
        return ResolvedSchedule(
            resolved_date=resolved_date_str,
            resolved_time=None,
            is_ambiguous=True,
            ambiguity_reason="ambiguous_ampm",
            candidate_times=ampm_candidates,
            question=f"Уточни время{date_note}: {ampm_candidates[0]} или {ampm_candidates[1]}?",
        )

    if date_obj is not None and hhmm is None:
        presets = {
            "morning": ["09:00", "11:00"],
            "day": ["13:00", "15:00"],
            "evening": ["19:00", "21:00"],
            "night": ["22:00", "23:00"],
        }.get(part_of_day or "", ["10:00", "14:00", "19:00"])
        date_iso = date_obj.isoformat()
        return ResolvedSchedule(
            resolved_date=date_iso,
            resolved_time=None,
            is_ambiguous=True,
            ambiguity_reason="missing_time",
            candidate_times=presets,
            question=f"Во сколько напомнить {format_date_display(date_iso)}?",
        )

    if date_obj is None and hhmm is not None:
        parsed_t = datetime.datetime.strptime(hhmm, "%H:%M").time()
        target_d = local_today if parsed_t > local_now.time() else local_today + datetime.timedelta(days=1)
        return ResolvedSchedule(
            resolved_date=target_d.isoformat(),
            resolved_time=hhmm,
            is_ambiguous=False,
        )

    if date_obj is None and hhmm is None:
        return ResolvedSchedule(
            resolved_date=None,
            resolved_time=None,
            is_ambiguous=True,
            ambiguity_reason="vague_when",
            candidate_times=[],
            question="На какую дату и время поставить напоминание?",
        )

    return ResolvedSchedule(
        resolved_date=date_obj.isoformat(),
        resolved_time=hhmm,
        is_ambiguous=False,
    )


def _capitalize_first(text: str) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip(" ,.;:-"))
    if not clean:
        return ""
    return clean[0].upper() + clean[1:]


_SHOPPING_UNINFLECTED_U = {"тофу", "рагу", "азу", "тирамису", "кешью", "меню", "барбекю", "фондю"}


def _nominative_shopping_phrase(phrase: str) -> str:
    words = phrase.split()
    out: list[str] = []
    for w in words:
        lw = w.lower()
        if lw in _SHOPPING_UNINFLECTED_U or len(lw) < 4:
            out.append(w)
        elif lw.endswith("ую"):
            out.append(w[:-2] + "ая")
        elif lw.endswith("юю"):
            out.append(w[:-2] + "яя")
        elif lw.endswith("у"):
            out.append(w[:-1] + "а")
        elif re.search(r"[бвгджзклмнпрстфхцчшщ]ю$", lw):
            out.append(w[:-1] + "я")
        else:
            out.append(w)
    return " ".join(out)


def _clean_shopping_title(raw: str) -> str:
    txt = (raw or "").strip()
    had_buy_verb = bool(re.search(r"(?iu)\b(?:купить|докупить|покупать)\b", txt))
    patterns = [
        r"(?iu)^(?:у\s+нас\s+)?(?:дома\s+)?(?:совсем\s+)?(?:уже\s+)?(?:закончил(?:ся|ась|ись|ось)|кончил(?:ся|ась|ись|ось))\s+",
        r"(?iu)^(?:дома\s+)?(?:надо|нужно|пора)\s+(?:бы\s+)?(?:купить|докупить)\s+",
        r"(?iu)^(?:добавь\s+в\s+(?:список\s+)?покупки?\s+)",
        r"(?iu)^(?:купить|докупить)\s+",
        r"(?iu)\b(?:дома|в\s+холодильнике)\b",
    ]
    for pat in patterns:
        txt = re.sub(pat, " ", txt)
    # Strip quantity words at start or end like "2 пачки", "полпачки", "1 банка"
    txt = re.sub(
        r"(?iu)\b(?:\d+(?:[.,]\d+)?\s*(?:пач\w*|уп\w*|банк\w*|шт\w*|кг|г|гр|л|мл)\.?|"
        r"половин\w*\s+упаковк\w*|полпачки|пачк[ауи]|банк[ауи])\b",
        " ",
        txt,
    )
    cleaned = _capitalize_first(txt)
    if cleaned and had_buy_verb:
        cleaned = _capitalize_first(_nominative_shopping_phrase(cleaned))
    return cleaned or _capitalize_first(raw)


def _extract_shopping_qty_unit(raw: str) -> tuple[float, str]:
    txt = (raw or "").lower()
    if re.search(r"полпачк|половин\w*\s+упаковк|пол\s+пачк", txt):
        return 0.5, "уп"
    if re.search(r"полбанк", txt):
        return 0.5, "банк"
    m = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(пач\w*|уп\w*|банк\w*|шт\w*|кг|мл|л|гр|г)\b",
        txt,
    )
    if m:
        qty = float(m.group(1).replace(",", "."))
        u_raw = m.group(2)
        if u_raw.startswith("пач"):
            unit = "пач"
        elif u_raw.startswith("уп"):
            unit = "уп"
        elif u_raw.startswith("банк"):
            unit = "банк"
        elif u_raw in {"г", "гр"}:
            unit = "г"
        elif u_raw in {"кг", "л", "мл"}:
            unit = u_raw
        else:
            unit = "шт"
        return qty, unit
    if re.search(r"\bпачк[ауи]\b", txt):
        return 1.0, "пач"
    if re.search(r"\bбанк[ауи]\b", txt):
        return 1.0, "банк"
    if re.search(r"\bупаковк[ауи]\b", txt):
        return 1.0, "уп"
    return 1.0, "шт"


def _clean_task_title(raw: str) -> str:
    txt = (raw or "").strip()
    patterns = [
        r"(?iu)^(?:а\s+)?(?:ещё\s+)?(?:сегодня\s+)?(?:надо|нужно|необходимо|не\s+забыть|добавь\s+в\s+план)\s+",
        r"(?iu)^(?:надо|нужно)\s+",
    ]
    for pat in patterns:
        txt = re.sub(pat, "", txt).strip()
    return _capitalize_first(txt) or _capitalize_first(raw)


_ROLLOVER_RE = re.compile(
    r"(?iu)(?:\bперенес[ит]\w*\b|\bперекинь\b|\bскинь\b|\bотложи\b|\bсдвин[иь]\b).*"
    r"\b(?:хвост\w*|дела|задач\w*|несделан\w*|незаверш\w*|остатк\w*)\b.*\bна\s+завтра\b"
)

_SHIFT_BEDTIME_RE = re.compile(
    r"(?iu)(?:\b(?:сегодня\s+)?(?:ложусь|пойду\s+спать|уйду\s+спать|отбой)\s+(?:в|на|к)\s*(\d{1,2}\s*[:.]\s*\d{2})|"
    r"\b(?:подвин[иь]|сдвин[иь]|перенес[ит]\w*)\s+отбой\s+(?:на|в|к)\s*(\d{1,2}\s*[:.]\s*\d{2}))"
)

_SHIFT_WAKE_RE = re.compile(
    r"(?iu)(?:\b(?:завтра|сегодня|обычно)?\s*(?:подъ[её]м\w*|будильник\w*|встаю|вставать|проснуться)\s*(?:буд(?:ет|ут))?\s*(?:в|на|к)\s*(\d{1,2}\s*[:.]\s*\d{2})|"
    r"\b(?:перенес[ит]\w*|постав[иь]|сдвин[иь]|поменяй|измени)\s+(?:подъ[её]м\w*|будильник\w*)\s+(?:на|в|к)\s*(\d{1,2}\s*[:.]\s*\d{2})|"
    r"\b(?:завтра|сегодня)\s+без\s+будильник\w*\b|\bсплю\s+без\s+будильник\w*\b)"
)

_TASK_DELETE_RE = re.compile(
    r"(?iu)\b(?:удал[иь]|удалить|убер[иь]|убрать|вычеркн[иь]|отмен[иь])\s+задач[уи]\s+(.+)"
)

_REMINDER_DELETE_RE = re.compile(
    r"(?iu)\b(?:удал[иь]|удалить|убер[иь]|убрать|отмен[иь]|выключ[иь])\s+напоминан\w*(?:\s+(?:про|о|об))?\s+(.+)"
)

_PANTRY_REMOVE_RE = re.compile(
    r"(?iu)(?:^(?:я\s+|мы\s+|уже\s+)?(?:съел[аи]?|доел[аи]?)\s+([а-яёa-z-]+)$|"
    r"(?:убер[иь]|удал[иь]|убрать|удалить|вычеркн[иь]|выкинул[аи]?)\s+(.+?)\s+из\s+(?:холодильник\w*|кладовк\w*))"
)

_WEIGHT_LOG_RE = re.compile(
    r"(?iu)(?:^(?:я\s+|сегодня\s+|утром\s+)?(?:взвесил(?:ся|ась|ись)?|мой\s+вес|вес(?:\s+(?:сейчас|сегодня|стал))?)\s*[:=-]?\s*\d{2,3}(?:[.,]\d+)?\b|"
    r"\b\d{2,3}(?:[.,]\d+)?\s*кг\b.*\bжир\b|\bжир\s+\d+(?:[.,]\d+)?\s*%)"
)

_WORKOUT_LOG_RE = re.compile(
    r"(?iu)(?:^(?:я\s+|уже\s+|сегодня\s+)?(?:сделал[аи]?|пробежал[аи]?|отжал(?:ся|ась|ись)|подтянул(?:ся|ась|ись)|"
    r"позанимал(?:ся|ась|ись)|потренировал(?:ся|ась|ись)|поприседал[аи]?|прош(?:ёл|ла)|проехал[аи]?)\b.*"
    r"(?:\b\d+\s*(?:мин\w*|м|ч|час\w*|км|раз)\b.*\b(?:гантел\w*|турник\w*|резинк\w*|коврик\w*|дорожк\w*|вело\w*|эллипс\w*|тренировк\w*|присед\w*|отжиман\w*|подтягиван\w*|бег|планк\w*)\b|"
    r"\b(?:гантел\w*|турник\w*|резинк\w*|коврик\w*|дорожк\w*|вело\w*|эллипс\w*|тренировк\w*|отжиман\w*|подтягиван\w*|планк\w*)\b)|"
    r"\bтренировк[ауе]\s+(?:сделана|выполнена|готова|закончил\w*)\b)"
)

_MEAL_LOG_EXPLICIT_RE = re.compile(
    r"(?iu)(?:^(?:я\s+|только\s+что\s+|сегодня\s+|утром\s+|днём\s+|вечером\s+|на\s+(?:завтрак|обед|ужин|перекус)\s+)*"
    r"(?:съел[аи]?|доел[аи]?|перекусил[аи]?|позавтракал[аи]?|пообедал[аи]?|поужинал[аи]?|поел[аи]?)\s+(.+)|"
    r"^(?:я\s+|сегодня\s+|утром\s+|только\s+что\s+)*выпил[аи]?\s+(?!вод[ауы]?\b)(.+)|"
    r"^на\s+(?:завтрак|обед|ужин|перекус)\s+(?:был[аои]?|ел[аи]?|съел[аи]?)\s+(.+))"
)

_PANTRY_ADD_RE = re.compile(
    r"(?iu)(?:^(?:я\s+|мы\s+|уже\s+)?(?:купил[аи]?|взял[аи]?|прин[её]с(?:ла|ли)?|докупил[аи]?)\s+(.+)|"
    r"^(?:дома\s+есть|положи\s+в\s+холодильник|добавь\s+в\s+холодильник|в\s+холодильнике\s+есть)\s+(.+))"
)

_PULSE_NEG_RE = re.compile(r"(?iu)\bне\s+(?:поел|пообедал|поужинал|позавтракал|встал|пил|выпил|чистил|убрал|ложусь)")

_PULSE_LOG_RULES = (
    (re.compile(r"(?iu)\b(?:убрал[аи]?\s+(?:телефон|мобил\w*)|ложусь(?:\s+спать)?|иду\s+спать)\b"), "bedtime", "Отбой"),
    (re.compile(r"(?iu)\bпочистил[аи]?\s+зуб\w*\b"), "teeth", "Чистка зубов"),
    (re.compile(r"(?iu)\b(?:выпил[аи]?|попил[аи]?)\s+(?:стакан\s+)?вод\w*\b"), "water", "Стакан воды"),
    (
        re.compile(r"(?iu)^(?:я\s+|уже\s+|сегодня\s+|только\s+что\s+)*поужинал[аи]?(?:\s+(?:дома|хорошо|вкусно|плотно|уже))?$"),
        "dinner",
        "Ужин",
    ),
    (
        re.compile(
            r"(?iu)^(?:я\s+|уже\s+|сегодня\s+|только\s+что\s+)*(?:пообедал[аи]?|поел[аи]?|позавтракал[аи]?)(?:\s+(?:дома|хорошо|вкусно|плотно|уже))?$"
        ),
        "lunch",
        "Приём пищи",
    ),
    (re.compile(r"(?iu)^(?:(?:я|вот|только|сейчас|уже)\s+)*(?:встал[аи]?|проснул(?:ся|ась))\b"), "wake", "Подъём"),
)


def _split_pantry_products(tail: str) -> list[str]:
    clean = re.sub(r"(?iu)\b(?:в\s+холодильник\w*|из\s+холодильник\w*|из\s+кладовк\w*|дома|сегодня|уже)\b", " ", tail or "")
    parts = re.split(r",|\s+и\s+", clean)
    out: list[str] = []
    for p in parts:
        cand = _capitalize_first(_nominative_shopping_phrase(p.strip()))
        if cand and len(cand) >= 2 and cand.lower() not in {"все", "всё", "это"}:
            out.append(cand)
    return out


def _extract_bedtime_hhmm(raw: str) -> Optional[str]:
    m = re.search(r"(\d{1,2})\s*[:.]\s*(\d{2})", raw or "")
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    return f"{hh:02d}:{mm:02d}"


def _clean_idea_title(raw: str) -> str:
    txt = (raw or "").strip()
    txt = re.sub(r"(?iu)^(?:а\s+)?(?:ещё\s+)?(?:придумал[аи]?\s+)", "", txt).strip()
    txt = re.sub(r"(?iu)^(?:хочу\s+когда-?нибудь\s+|когда-?нибудь\s+хочу\s+|когда-?нибудь\s+)", "", txt).strip()
    txt = re.sub(r"(?iu)^идею\b", "Идея", txt)
    return _capitalize_first(txt) or _capitalize_first(raw)


def _clean_shopping_remove_title(raw: str) -> str:
    txt = (raw or "").strip()
    patterns = [
        r"(?iu)\bиз\s+(?:списка\s+)?покупок\b",
        r"(?iu)\bв\s+списке\s+покупок\b",
        r"(?iu)\bне\s+(?:надо|нужно)\s+(?:покупать|брать)\b",
        r"(?iu)\b(?:уже\s+)?наш(?:ёл|елся|лась|лось|лись)\b",
        r"(?iu)\b(?:пожалуйста\s+)?(?:убер[иь]|удал[иь]|убрать|удалить|вычеркн[иь]|вычеркнуть)\b",
        r"(?iu)\b(?:не|нет|кстати|её|его|их|это|уже)\b",
    ]
    for pat in patterns:
        txt = re.sub(pat, " ", txt)
    cleaned = _capitalize_first(txt)
    return cleaned or _capitalize_first(raw)


def _extract_expense_details(raw: str) -> tuple[float, str, str]:
    txt = (raw or "").strip()
    m_buy = re.search(r"(?iu)\bкупил[аи]?\s+(.+?)\s+за\s+(\d+(?:[.,]\d+)?)", txt)
    if m_buy:
        amount = float(m_buy.group(2).replace(",", "."))
        cat_raw = re.sub(r"(?iu)\b(?:уже|сегодня|вчера|там)\b", " ", m_buy.group(1))
        cat = re.sub(r"\s+", " ", cat_raw).strip(" ,.;:-").lower() or "другое"
        return amount, cat, _capitalize_first(cat)

    m_num = re.search(r"(\d+(?:[.,]\d+)?)", txt)
    if not m_num:
        return 0.0, "другое", _capitalize_first(txt)
    amount = float(m_num.group(1).replace(",", "."))

    cat = ""
    m_for = re.search(r"(?iu)\b(?:на|за)\s+([а-яёa-z0-9-]+)", txt)
    if m_for:
        cand = m_for.group(1).strip().lower()
        if cand not in {"руб", "рубля", "рублей", "р"}:
            cat = cand
    if not cat:
        skip = {
            "потратил", "потратила", "потратили", "ушло", "вышло",
            "отдал", "отдала", "заплатил", "заплатила", "купил", "купила",
            "руб", "рубля", "рублей", "р", "на", "за", "в", "сегодня", "вчера",
        }
        for tok in re.findall(r"[а-яёa-z]+", txt.lower()):
            if tok not in skip and len(tok) > 1:
                cat = tok
                break
    cat = cat or "другое"
    return amount, cat, _capitalize_first(cat)


def _clean_reminder_title(raw: str, resolved_time: Optional[str]) -> str:
    txt = (raw or "").strip()
    patterns = [
        r"(?iu)^(?:пожалуйста\s+)?(?:напомни(?:\s+мне)?|поставь\s+напоминание|сделай\s+напоминание)\s*",
        r"(?iu)\b(?:сегодня|завтра|послезавтра|потом|позже|утром|днём|вечером|ночью)\b",
        r"(?iu)\bчерез\s+\d+\s*(?:мин\w*|час\w*|ч|дн\w*|день|дня|дней)\b",
        r"(?iu)\b(?:в|во|на)\s+(?:понедель\w*|вторник\w*|сред\w*|четвер\w*|пятниц\w*|суббот\w*|воскрес\w*)\b",
        r"(?iu)\b(?:в|на|к)\s*\d{1,2}\s*[:.]\s*\d{2}\b",
        r"(?iu)\b(?:в|на|к)\s*\d{1,2}(?:\s*час\w*)?(?:\s*(?:утра|дня|вечера|ночи))?\b",
        r"(?iu)\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\b",
    ]
    for pat in patterns:
        txt = re.sub(pat, " ", txt)
    if resolved_time:
        txt = txt.replace(resolved_time, " ")
    return _capitalize_first(txt) or "Напоминание"


_CALENDAR_EVENT_TRIGGER_RE = re.compile(
    r"(?iu)(?:\b(?:запиш[иь]|добав[иь]|постав[иь]|внес[иь]|занес[иь]|сохран[иь])\s+в\s+(?:google\s+|гугл\s+)?календар\w*|"
    r"\bв\s+(?:google\s+|гугл\s+)?календар\w*\s+(?:на|в|завтра|сегодня|послезавтра|\d)|"
    r"\b(?:постав[иь]|запланируй|назнач[иь])\s+(?:встречу|созвон|событие)\b)"
)

_QUERY_CALENDAR_RE = re.compile(
    r"(?iu)^(?:а\s+)?(?:покажи\s+|подскажи\s+|скажи\s+|глянь\s+)?"
    r"(?:что\s+(?:у\s+меня\s+)?в\s+(?:google\s+|гугл\s+)?календар\w*|"
    r"какие\s+(?:у\s+меня\s+)?(?:встречи|события|планы\s+в\s+календаре)|"
    r"(?:мой\s+)?(?:google\s+|гугл\s+)?календарь|"
    r"(?:мо[её]\s+)?расписание(?:\s+на\s+\S+)?)\b"
)


def _extract_time_range(raw: str) -> Optional[tuple[str, str, int]]:
    m = re.search(
        r"(?iu)(?:с\s*)?(\d{1,2})\s*[:.]\s*(\d{2})\s*(?:-|–|—|\bдо\b)\s*(\d{1,2})\s*[:.]\s*(\d{2})",
        raw or "",
    )
    if not m:
        return None
    h1, m1, h2, m2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    if h1 > 23 or m1 > 59 or h2 > 23 or m2 > 59:
        return None
    s_min = h1 * 60 + m1
    e_min = h2 * 60 + m2
    if e_min <= s_min:
        return None
    return f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}", max(15, min(720, e_min - s_min))


def _extract_duration_minutes(raw: str, default: int = 60) -> int:
    rng = _extract_time_range(raw)
    if rng is not None:
        return rng[2]
    txt = (raw or "").lower()
    if re.search(r"\b(?:на\s+)?полтора\s+часа\b|\b1[.,]5\s*час", txt):
        return 90
    if re.search(r"\b(?:на\s+)?полчаса\b", txt):
        return 30
    m_hr = re.search(r"\bна\s+(\d+(?:[.,]\d+)?)\s*(?:час\w*|ч)\b", txt)
    if m_hr:
        val = float(m_hr.group(1).replace(",", "."))
        return max(15, min(720, int(round(val * 60))))
    if re.search(r"\bна\s+(?:один\s+)?час\b", txt):
        return 60
    m_min = re.search(r"\bна\s+(\d+)\s*(?:мин\w*|м)\b", txt)
    if m_min:
        return max(15, min(720, int(m_min.group(1))))
    return max(15, min(720, int(default or 60)))


def _clean_calendar_event_title(raw: str, resolved_time: Optional[str]) -> str:
    txt = (raw or "").strip()
    patterns = [
        r"(?iu)^(?:пожалуйста\s+)?(?:запиш[иь]|добав[иь]|постав[иь]|внес[иь]|занес[иь]|сохран[иь])(?:\s+мне)?\s+",
        r"(?iu)\bв\s+(?:google\s+|гугл\s+)?календар\w*\b",
        r"(?iu)\b(?:сегодня|завтра|послезавтра|потом|позже|утром|днём|вечером|ночью)\b",
        r"(?iu)\b(?:в|во)\s+(?:понедель\w*|вторник\w*|сред\w*|четвер\w*|пятниц\w*|суббот\w*|воскрес\w*)\b",
        r"(?iu)\b(?:с\s*)?\d{1,2}\s*[:.]\s*\d{2}\s*(?:-|–|—|\bдо\b)\s*\d{1,2}\s*[:.]\s*\d{2}\b",
        r"(?iu)\b(?:в|на|к)\s*\d{1,2}\s*[:.]\s*\d{2}\b",
        r"(?iu)\b(?:в|на|к)\s*\d{1,2}(?:\s*час\w*)?(?:\s*(?:утра|дня|вечера|ночи))?\b",
        r"(?iu)\bна\s+(?:полтора\s+часа|полчаса|(?:один\s+)?час|\d+(?:[.,]\d+)?\s*(?:час\w*|ч|мин\w*|м))\b",
        r"(?iu)\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\b",
    ]
    for pat in patterns:
        txt = re.sub(pat, " ", txt)
    if resolved_time:
        txt = txt.replace(resolved_time, " ")
    return _capitalize_first(txt) or "Событие"


def _split_clauses(raw_text: str) -> list[str]:
    text = (raw_text or "").strip()
    if not text:
        return []
    primary = re.split(r"(?<!\d)\.(?!\d)|[!?\n;]+", text)
    clauses: list[str] = []
    for chunk in primary:
        piece = chunk.strip(" ,")
        if not piece:
            continue
        subparts = _SECONDARY_SPLIT_RE.split(piece)
        for sub in subparts:
            for part in _AND_ACTION_SPLIT_RE.split(sub):
                clean = part.strip(" ,")
                if clean:
                    clauses.append(clean)
    return clauses


def _apply_guardrails(
    item: ParsedInboxItem,
    *,
    now_utc: datetime.datetime,
    tz_str: str,
    date_hint: str = "",
    time_hint: str = "",
) -> ParsedInboxItem:
    quote = (item.source_quote or item.title or "").strip()
    kind = item.kind if item.kind in VALID_KINDS else "diary"

    # Guardrail 1: wishes / future dreams / long-horizon items are NEVER today's tasks or reminders
    if (
        kind not in {"shopping_remove", "expense"}
        and (
            _IDEA_GUARDRAIL_RE.search(quote)
            or (
                kind == "task"
                and _LONG_HORIZON_IDEA_RE.search(quote)
                and not _IMMEDIATE_TASK_TIME_RE.search(quote)
            )
        )
        and not _REMINDER_TRIGGER_RE.search(quote)
    ):
        kind = "idea"
        if not item.title or item.title == quote:
            item.title = _clean_idea_title(quote)

    # Guardrail 2: emotional state or live status narration without explicit obligation -> diary
    elif (
        kind in {"task", "reminder"}
        and _DIARY_FEELING_RE.search(quote)
        and not _EXPLICIT_OBLIGATION_RE.search(quote)
    ):
        kind = "diary"
    elif (
        kind == "task"
        and _STATUS_UPDATE_TASK_GUARD_RE.search(quote)
        and not _EXPLICIT_OBLIGATION_RE.search(quote)
        and not _TASK_TRIGGER_RE.search(quote)
    ):
        kind = "diary"

    # Guardrail 3: clean shopping title & units
    if kind == "shopping":
        qty, unit = _extract_shopping_qty_unit(quote)
        if item.quantity == 1.0 and qty != 1.0:
            item.quantity = qty
        if item.unit == "шт" and unit != "шт":
            item.unit = unit
        if any(w in (item.title or "").lower() for w in ("закончил", "кончил", "дома", "купить")):
            item.title = _clean_shopping_title(item.title or quote)
        else:
            item.title = _capitalize_first(item.title or _clean_shopping_title(quote))

    elif kind == "shopping_remove":
        raw_title = item.title or quote
        if any(w in raw_title.lower() for w in ("убер", "удал", "покуп", "нашл", "нашёл", "надо", "нужно")):
            item.title = _clean_shopping_remove_title(raw_title)
        else:
            item.title = _capitalize_first(raw_title or _clean_shopping_remove_title(quote))

    elif kind == "expense":
        amt, cat, exp_title = _extract_expense_details(quote)
        if item.amount <= 0 and amt > 0:
            item.amount = amt
        item.category = (item.category or cat or "другое").strip().lower()
        if not item.title or item.title.lower() == quote.lower():
            item.title = _capitalize_first(item.category) if item.category != "другое" else exp_title
        else:
            item.title = _capitalize_first(item.title)

    elif kind == "reminder":
        schedule = resolve_temporal_expression(
            date_hint=date_hint,
            time_hint=time_hint,
            raw_fragment=quote,
            now_utc=now_utc,
            tz_str=tz_str,
        )
        item.schedule = schedule
        if not item.title or "напомн" in item.title.lower():
            item.title = _clean_reminder_title(quote, schedule.resolved_time)
        else:
            item.title = _clean_reminder_title(item.title, schedule.resolved_time)
    elif kind == "calendar_event":
        rng = _extract_time_range(quote)
        eff_time_hint = rng[0] if rng else time_hint
        schedule = resolve_temporal_expression(
            date_hint=date_hint,
            time_hint=eff_time_hint,
            raw_fragment=quote,
            now_utc=now_utc,
            tz_str=tz_str,
        )
        item.schedule = schedule
        item.duration_minutes = _extract_duration_minutes(quote, item.duration_minutes or 60)
        raw_t = (item.title or "").strip()
        if not raw_t or "календар" in raw_t.lower() or raw_t.lower() == quote.lower():
            item.title = _clean_calendar_event_title(quote, schedule.resolved_time)
        else:
            item.title = _clean_calendar_event_title(raw_t, schedule.resolved_time)
    elif kind in {"pantry_add", "pantry_remove"}:
        item.title = _capitalize_first(_nominative_shopping_phrase(item.title or quote))
    elif kind == "pulse_log":
        is_bare_pulse = any(pat.search(quote) for pat, _, _ in _PULSE_LOG_RULES)
        if not is_bare_pulse and _MEAL_LOG_EXPLICIT_RE.search(quote):
            kind = "meal_log"
            item.category = ""
            item.title = _capitalize_first(quote)
        else:
            slot = (item.category or "").strip().lower()
            if slot not in {"wake", "lunch", "dinner", "water", "teeth", "bedtime"}:
                slot = "lunch"
                for pat, slot_key, _ in _PULSE_LOG_RULES:
                    if pat.search(quote):
                        slot = slot_key
                        break
            item.category = slot
            item.title = _capitalize_first(item.title or quote)
    elif kind in {"shift_bedtime", "shift_wake"}:
        hhmm = _extract_bedtime_hhmm(time_hint) or _extract_bedtime_hhmm(quote)
        item.schedule = ResolvedSchedule(
            resolved_date=None,
            resolved_time=hhmm,
            is_ambiguous=(hhmm is None),
        )
        if kind == "shift_bedtime":
            item.title = f"Сдвинуть отбой на {hhmm}" if hhmm else _capitalize_first(item.title or quote)
        else:
            if "без будильник" in quote.lower():
                item.category = "no_alarm"
            item.title = f"Подъём в {hhmm}" if hhmm else _capitalize_first(item.title or quote)
    elif kind == "task_delete":
        m_td = _TASK_DELETE_RE.search(quote)
        raw_td = (m_td.group(1) if m_td else item.title or quote).strip(" ,.?!\"'«»")
        item.title = _capitalize_first(raw_td) or _capitalize_first(quote)
    elif kind == "reminder_delete":
        m_rd = _REMINDER_DELETE_RE.search(quote)
        raw_rd = (m_rd.group(1) if m_rd else item.title or quote).strip(" ,.?!\"'«»")
        item.title = _capitalize_first(raw_rd) or _capitalize_first(quote)
    else:
        item.title = _capitalize_first(item.title or quote)

    item.kind = kind
    item.source_quote = quote
    return item


def _dedupe_items(items: list[ParsedInboxItem]) -> list[ParsedInboxItem]:
    seen: set[tuple[str, str]] = set()
    out: list[ParsedInboxItem] = []
    for it in items:
        key = (it.kind, re.sub(r"\s+", " ", (it.title or "").strip().lower()))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def parse_inbox_fallback(
    raw_text: str,
    now_utc: datetime.datetime,
    tz_str: str,
) -> list[ParsedInboxItem]:
    """Детерминированный разбор смешанного сообщения без обращения к LLM."""
    clauses = _split_clauses(raw_text)
    if not clauses:
        return []

    items: list[ParsedInboxItem] = []
    for clause in clauses:
        if _ROLLOVER_RE.search(clause):
            items.append(
                _apply_guardrails(
                    ParsedInboxItem(
                        kind="rollover_tasks",
                        title="Перенести незавершённые дела на завтра",
                        source_quote=clause,
                    ),
                    now_utc=now_utc,
                    tz_str=tz_str,
                )
            )
            continue
        m_shift = _SHIFT_BEDTIME_RE.search(clause)
        if m_shift:
            t_raw = next((g for g in m_shift.groups() if g), "")
            hhmm = _extract_bedtime_hhmm(t_raw)
            items.append(
                _apply_guardrails(
                    ParsedInboxItem(
                        kind="shift_bedtime",
                        title=f"Сдвинуть отбой на {hhmm}" if hhmm else clause,
                        source_quote=clause,
                    ),
                    now_utc=now_utc,
                    tz_str=tz_str,
                    time_hint=hhmm or "",
                )
            )
            continue

        m_wake_shift = _SHIFT_WAKE_RE.search(clause)
        if m_wake_shift:
            t_raw = next((g for g in (m_wake_shift.groups() or ()) if g), "")
            hhmm = _extract_bedtime_hhmm(t_raw) if t_raw else None
            items.append(
                _apply_guardrails(
                    ParsedInboxItem(
                        kind="shift_wake",
                        title=f"Подъём в {hhmm}" if hhmm else clause,
                        source_quote=clause,
                    ),
                    now_utc=now_utc,
                    tz_str=tz_str,
                    time_hint=hhmm or "",
                )
            )
            continue

        m_tdel = _TASK_DELETE_RE.search(clause)
        if m_tdel:
            items.append(
                _apply_guardrails(
                    ParsedInboxItem(
                        kind="task_delete",
                        title=m_tdel.group(1).strip(),
                        source_quote=clause,
                    ),
                    now_utc=now_utc,
                    tz_str=tz_str,
                )
            )
            continue

        m_rdel = _REMINDER_DELETE_RE.search(clause)
        if m_rdel:
            items.append(
                _apply_guardrails(
                    ParsedInboxItem(
                        kind="reminder_delete",
                        title=m_rdel.group(1).strip(),
                        source_quote=clause,
                    ),
                    now_utc=now_utc,
                    tz_str=tz_str,
                )
            )
            continue

        if not _PULSE_NEG_RE.search(clause):
            pulse_hit = None
            for pat, slot_key, slot_label in _PULSE_LOG_RULES:
                if pat.search(clause):
                    pulse_hit = (slot_key, slot_label)
                    break
            if pulse_hit is not None:
                slot_key, slot_label = pulse_hit
                items.append(
                    _apply_guardrails(
                        ParsedInboxItem(
                            kind="pulse_log",
                            title=slot_label,
                            source_quote=clause,
                            category=slot_key,
                        ),
                        now_utc=now_utc,
                        tz_str=tz_str,
                    )
                )
                continue

        if _CALENDAR_EVENT_TRIGGER_RE.search(clause):
            rng = _extract_time_range(clause)
            schedule = resolve_temporal_expression(
                date_hint="",
                time_hint=(rng[0] if rng else ""),
                raw_fragment=clause,
                now_utc=now_utc,
                tz_str=tz_str,
            )
            item = ParsedInboxItem(
                kind="calendar_event",
                title=_clean_calendar_event_title(clause, schedule.resolved_time),
                source_quote=clause,
                schedule=schedule,
                duration_minutes=_extract_duration_minutes(clause, 60),
            )
        elif _REMINDER_TRIGGER_RE.search(clause):
            schedule = resolve_temporal_expression(
                date_hint="",
                time_hint="",
                raw_fragment=clause,
                now_utc=now_utc,
                tz_str=tz_str,
            )
            item = ParsedInboxItem(
                kind="reminder",
                title=_clean_reminder_title(clause, schedule.resolved_time),
                source_quote=clause,
                schedule=schedule,
            )
        elif _IDEA_GUARDRAIL_RE.search(clause):
            item = ParsedInboxItem(
                kind="idea",
                title=_clean_idea_title(clause),
                source_quote=clause,
            )
        elif _SHOPPING_REMOVE_RE.search(clause):
            item = ParsedInboxItem(
                kind="shopping_remove",
                title=_clean_shopping_remove_title(clause),
                source_quote=clause,
            )
        elif _EXPENSE_TRIGGER_RE.search(clause):
            amt, cat, exp_title = _extract_expense_details(clause)
            if amt > 0:
                item = ParsedInboxItem(
                    kind="expense",
                    title=exp_title,
                    source_quote=clause,
                    amount=amt,
                    category=cat,
                )
            else:
                item = ParsedInboxItem(
                    kind="diary",
                    title=_capitalize_first(clause),
                    source_quote=clause,
                )
        elif _WORKOUT_LOG_RE.search(clause):
            item = ParsedInboxItem(
                kind="workout_log",
                title=_capitalize_first(clause),
                source_quote=clause,
            )
        elif _WEIGHT_LOG_RE.search(clause):
            item = ParsedInboxItem(
                kind="weight_log",
                title=_capitalize_first(clause),
                source_quote=clause,
            )
        elif _SHOPPING_TRIGGER_RE.search(clause):
            qty, unit = _extract_shopping_qty_unit(clause)
            item = ParsedInboxItem(
                kind="shopping",
                title=_clean_shopping_title(clause),
                source_quote=clause,
                quantity=qty,
                unit=unit,
            )
        elif _PANTRY_REMOVE_RE.search(clause):
            m_pr = _PANTRY_REMOVE_RE.search(clause)
            tail_pr = next((g for g in m_pr.groups() if g), "") if m_pr else ""
            products = _split_pantry_products(tail_pr)
            if products:
                for prod in products:
                    items.append(
                        _apply_guardrails(
                            ParsedInboxItem(
                                kind="pantry_remove",
                                title=prod,
                                source_quote=clause,
                            ),
                            now_utc=now_utc,
                            tz_str=tz_str,
                        )
                    )
                continue
            item = ParsedInboxItem(
                kind="diary",
                title=_capitalize_first(clause),
                source_quote=clause,
            )
        elif _MEAL_LOG_EXPLICIT_RE.search(clause):
            item = ParsedInboxItem(
                kind="meal_log",
                title=_capitalize_first(clause),
                source_quote=clause,
            )
        elif _PANTRY_ADD_RE.search(clause):
            m_pa = _PANTRY_ADD_RE.search(clause)
            tail_pa = next((g for g in m_pa.groups() if g), "") if m_pa else ""
            products = _split_pantry_products(tail_pa)
            if products:
                for prod in products:
                    items.append(
                        _apply_guardrails(
                            ParsedInboxItem(
                                kind="pantry_add",
                                title=prod,
                                source_quote=clause,
                                quantity=1.0,
                                unit="шт",
                            ),
                            now_utc=now_utc,
                            tz_str=tz_str,
                        )
                    )
                continue
            item = ParsedInboxItem(
                kind="diary",
                title=_capitalize_first(clause),
                source_quote=clause,
            )
        elif _DONE_TRIGGER_RE.search(clause) and not _DIARY_FEELING_RE.search(clause):
            item = ParsedInboxItem(
                kind="done",
                title=_capitalize_first(clause),
                source_quote=clause,
            )
        elif _DIARY_FEELING_RE.search(clause) and not _EXPLICIT_OBLIGATION_RE.search(clause):
            item = ParsedInboxItem(
                kind="diary",
                title=_capitalize_first(clause),
                source_quote=clause,
            )
        elif _TASK_TRIGGER_RE.search(clause) or _TASK_ACTION_START_RE.match(clause):
            if _LONG_HORIZON_IDEA_RE.search(clause) and not _IMMEDIATE_TASK_TIME_RE.search(clause):
                item = ParsedInboxItem(
                    kind="idea",
                    title=_clean_task_title(clause) or _clean_idea_title(clause),
                    source_quote=clause,
                )
            else:
                item = ParsedInboxItem(
                    kind="task",
                    title=_clean_task_title(clause),
                    source_quote=clause,
                )
        else:
            item = ParsedInboxItem(
                kind="diary",
                title=_capitalize_first(clause),
                source_quote=clause,
            )
        items.append(
            _apply_guardrails(item, now_utc=now_utc, tz_str=tz_str)
        )

    return _dedupe_items(items)


_CHAT_REPLY_BY_PHRASE = {
    "привет": "Привет! Я рядом. Напиши или надиктуй, что сделать, купить или записать.",
    "ку": "Привет! Я рядом. Напиши или надиктуй, что сделать, купить или записать.",
    "здарова": "Привет! На связи — напиши или надиктуй, что запишем или разберём.",
    "прив": "Привет! Я рядом. Напиши или надиктуй, что сделать, купить или записать.",
    "хелло": "Привет! На связи — напиши или надиктуй, что сделать или записать.",
    "приветик": "Привет! На связи — напиши или надиктуй, что запишем или разберём.",
    "хай": "Привет! Я рядом — что запишем или подскажем?",
    "здравствуй": "Здравствуй! На связи — напиши или надиктуй, что нужно сделать или сохранить.",
    "здравствуйте": "Здравствуйте! На связи — напиши или надиктуй, что нужно сделать или сохранить.",
    "хэллоу": "Привет! На связи — напиши или надиктуй, что сделать или записать.",
    "хеллоу": "Привет! На связи — напиши или надиктуй, что сделать или записать.",
    "доброе утро": "Доброе утро! Напиши или надиктуй планы, покупки или мысли — я разложу.",
    "с добрым утром": "Доброе утро! Напиши или надиктуй планы, покупки или мысли — я разложу.",
    "добрый день": "Добрый день! Я рядом — что запишем или посмотрим?",
    "добрый вечер": "Добрый вечер! На связи — можно записать дела, покупки или просто выдохнуть.",
    "спокойной ночи": "Спокойной ночи! Если в голове крутятся мысли — просто напиши их, я бережно сохраню.",
    "спасибо": "Пожалуйста! Если понадобится помощь — просто напиши или надиктуй.",
    "спс": "Пожалуйста! Я всегда на связи.",
    "благодарю": "Пожалуйста! Всегда рада помочь.",
    "спасибо большое": "Пожалуйста! Всегда рада помочь.",
    "огромное спасибо": "Пожалуйста! Всегда рада помочь.",
    "большое спасибо": "Пожалуйста! Всегда рада помочь.",
    "ок": "Хорошо! Буду на связи.",
    "окей": "Хорошо! Буду на связи.",
    "оки": "Хорошо! Буду на связи.",
    "ага": "Ага, я на связи!",
    "угу": "Угу, я рядом!",
    "понял": "Хорошо! Если что-то ещё вспомнишь — пиши.",
    "поняла": "Хорошо! Если что-то ещё вспомнишь — пиши.",
    "хорошо": "Хорошо! Буду на связи.",
    "ладно": "Хорошо! Я рядом.",
    "ясно": "Поняла, я на связи.",
}

_QUERY_TODAY_RE = re.compile(
    r"(?iu)^(?:а\s+)?(?:покажи\s+|подскажи\s+|скажи\s+)?"
    r"(?:что\s+(?:у\s+меня\s+)?на\s+сегодня|"
    r"(?:какие\s+|какой\s+)?план(?:ы)?\s+на\s+сегодня|"
    r"что\s+запланировано\s+на\s+сегодня|"
    r"что\s+по\s+плану\s+на\s+сегодня|"
    r"какие\s+дела\s+на\s+сегодня)\b"
)

_QUERY_SHOPPING_RE = re.compile(
    r"(?iu)^(?:а\s+)?(?:покажи\s+|подскажи\s+|скажи\s+)?"
    r"(?:какие\s+(?:у\s+меня\s+)?покупки|"
    r"что\s+(?:мне\s+|нам\s+)?(?:надо\s+|нужно\s+)?купить|"
    r"список\s+покупок|"
    r"что\s+(?:есть\s+)?в\s+покупках)\b"
)

_QUERY_FOOD_RE = re.compile(
    r"(?iu)^(?:а\s+)?(?:покажи\s+|подскажи\s+|скажи\s+)?"
    r"(?:что\s+(?:бы\s+|можно\s+)?(?:приготовить|сготовить|поесть|покушать)|"
    r"что\s+(?:у\s+нас\s+)?на\s+(?:ужин|обед|завтрак))\b"
)

_QUERY_DIARY_RE = re.compile(
    r"(?iu)(?:что\s+я\s+(?:писал[аи]?|говорил[аи]?|записывал[аи]?|отмечал[аи]?)\s+(?:про|о|об)\s+(.+)|"
    r"(?:найди|поищи|покажи)\s+в\s+дневнике(?:\s+(?:про|о|об))?\s*(.*)|"
    r"что\s+было\s+(?:в\s+дневнике\s+)?(?:про|о|об)\s+(.+))"
)

_QUERY_KARMA_RE = re.compile(
    r"(?iu)^(?:а\s+)?(?:покажи\s+|скажи\s+|подскажи\s+)?"
    r"(?:какая\s+(?:у\s+меня\s+)?карма|моя\s+карма|карма|мой\s+режим\s*(?:и\s+карма)?|"
    r"статус\s+карм\w*|сколько\s+(?:у\s+меня\s+)?карм\w*)\b"
)

_QUERY_PANTRY_RE = re.compile(
    r"(?iu)^(?:а\s+)?(?:покажи\s+|скажи\s+|подскажи\s+)?"
    r"(?:что\s+(?:у\s+нас\s+|у\s+меня\s+)?в\s+(?:холодильник\w*|кладовк\w*)|"
    r"(?:покажи\s+)?(?:мой\s+)?холодильник|что\s+(?:у\s+меня\s+)?есть\s+дома(?:\s+из\s+еды)?)\b"
)

_QUERY_ADVICE_RE = re.compile(
    r"(?iu)^(?:а\s+)?(?:дай\s+совет\w*|посоветуй(?:\s+что-?нибудь|\s+что\s+делать)?|"
    r"что\s+мне\s+(?:сейчас\s+)?делать|не\s+знаю,?\s+(?:за\s+что\s+взяться|что\s+делать))\b"
)

_QUERY_KBJU_RE = re.compile(
    r"(?iu)^(?:а\s+)?(?:покажи\s+|подскажи\s+|скажи\s+|глянь\s+)?"
    r"(?:сколько\s+(?:у\s+меня\s+)?(?:осталось\s+)?(?:калорий|ккал|кбжу|белка|жиров|углеводов)(?:\s+осталось|\s+на\s+сегодня|\s+добрать)?|"
    r"(?:что\s+)?(?:мне\s+)?(?:можно\s+)?(?:добрать|поесть)\s+по\s+(?:кбжу|белку|калориям)|"
    r"(?:мой|мои)\s+(?:кбжу|калории)(?:\s+на\s+сегодня)?)\b"
)

_QUERY_MEAL_CHECK_THINK_RE = re.compile(
    r"(?iu)^(?:я\s+|а\s+)?(?:думаю|хочу|собираюсь|планирую)\s+"
    r"(?:сейчас\s+|вечером\s+|на\s+ночь\s+|на\s+ужин\s+|на\s+обед\s+)?"
    r"(?:съесть|выпить|заказать|взять(?:\s+поесть)?)\s+(.+)"
)

_QUERY_MEAL_CHECK_ASK_RE = re.compile(
    r"(?iu)^(?:а\s+)?(?:можно|стоит|норм|нормально|влезет|впишется)(?:\s+ли)?(?:\s+мне)?(?:\s+сейчас|\s+на\s+ночь|\s+на\s+ужин)?\s+"
    r"(?:съесть|выпить|заказать|взять)\s+(.+)"
)


def _extract_meal_check_candidate(raw: str) -> Optional[str]:
    txt = (raw or "").strip()
    if not txt or _IDEA_GUARDRAIL_RE.search(txt):
        return None
    for pat in (_QUERY_MEAL_CHECK_THINK_RE, _QUERY_MEAL_CHECK_ASK_RE):
        m = pat.search(txt)
        if m:
            cand = re.sub(
                r"(?iu)(?:,?\s*(?:как\s+думаешь|стоит\s*(?:ли)?|норм\w*|можно\s*(?:ли)?|влезет\s*(?:ли)?|впишется\s*(?:ли)?|\?)+)+$",
                "",
                m.group(1),
            ).strip(" ,.?!\"'«»")
            if cand and len(cand) >= 2:
                return cand
    return None

_MORE_FOLLOWUP_RE = re.compile(
    r"(?iu)^(?:а\s+ещ[её]|ещ[её](?:\s+вариант\w*|\s+совет\w*)?|что\s+ещ[её]|"
    r"други[ех]\s+вариант\w*|другое|другой\s+совет|дальше|а\s+что\s+ещ[её]|"
    r"ещ[её]\s+что-?нибудь|ещ[её]\s+блюд\w*|покажи\s+ещ[её])$"
)

_FOOD_FILTER_FOLLOWUP_RE = re.compile(
    r"(?iu)^(?:без\s+\S+|побыстрее|быстрее|до\s+\d+\s*мин\w*)$"
)


def _latest_assistant_query_type(recent_context: Optional[list[dict[str, Any]]]) -> Optional[str]:
    if not recent_context:
        return None
    for turn in reversed(recent_context):
        if turn.get("is_reply_context"):
            continue
        if turn.get("role") == "assistant":
            meta = turn.get("meta") if isinstance(turn.get("meta"), dict) else {}
            qt = str(meta.get("query_type") or "").strip().lower()
            if qt in {"food", "shopping", "today", "calendar", "diary", "karma", "pantry", "advice"}:
                return qt
            return None
    return None


def _format_recent_context(recent_context: Optional[list[dict[str, Any]]]) -> str:
    if not recent_context:
        return ""
    lines = ["Недавний контекст диалога:"]
    for turn in recent_context[-4:]:
        if turn.get("is_reply_context"):
            role = "Сообщение HiDL, на которое отвечает пользователь"
            max_len = 400
        else:
            role = "Пользователь" if turn.get("role") == "user" else "HiDL"
            max_len = 180
        txt = re.sub(r"\s+", " ", str(turn.get("text") or "").strip())
        if not txt:
            continue
        if len(txt) > max_len:
            txt = txt[: max_len - 3] + "..."
        lines.append(f"- {role}: {txt}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _match_deterministic_chat(clean_text: str) -> Optional[InboxParseResult]:
    """Быстрый разбор коротких приветствий и благодарностей без записи в дневник."""
    raw = (clean_text or "").strip()
    if not raw or len(raw) > 45:
        return None
    norm = re.sub(r"[^\w\s]+", " ", raw.lower()).strip()
    norm = re.sub(r"\s+", " ", norm)
    reply = _CHAT_REPLY_BY_PHRASE.get(norm)
    if reply is None:
        return None
    return InboxParseResult(
        items=[],
        used_llm=False,
        intent="chat",
        reply_text=reply,
        query_type="none",
    )


def _try_deterministic_intent(
    clean_text: str,
    recent_context: Optional[list[dict[str, Any]]] = None,
) -> Optional[InboxParseResult]:
    """Детерминированный роутинг приветствий, продолжений диалога и прямых вопросов."""
    chat_res = _match_deterministic_chat(clean_text)
    if chat_res is not None:
        return chat_res

    raw = (clean_text or "").strip()
    if not raw or len(_split_clauses(raw)) > 1:
        return None

    stripped_punct = re.sub(r"[?!.,;:\s]+$", "", raw).strip()
    prev_qt = _latest_assistant_query_type(recent_context)
    if prev_qt and _MORE_FOLLOWUP_RE.fullmatch(stripped_punct):
        return InboxParseResult(
            items=[],
            used_llm=False,
            intent="query",
            query_type=prev_qt,
            query_term="more",
        )
    if prev_qt == "food" and _FOOD_FILTER_FOLLOWUP_RE.fullmatch(stripped_punct):
        return InboxParseResult(
            items=[],
            used_llm=False,
            intent="query",
            query_type="food",
            query_term=stripped_punct,
        )

    if _QUERY_CALENDAR_RE.search(raw):
        return InboxParseResult(items=[], used_llm=False, intent="query", query_type="calendar")
    if _QUERY_TODAY_RE.search(raw):
        return InboxParseResult(items=[], used_llm=False, intent="query", query_type="today")
    if _QUERY_KARMA_RE.search(raw):
        return InboxParseResult(items=[], used_llm=False, intent="query", query_type="karma")
    if _QUERY_PANTRY_RE.search(raw):
        return InboxParseResult(items=[], used_llm=False, intent="query", query_type="pantry")
    meal_cand = _extract_meal_check_candidate(raw)
    if meal_cand:
        return InboxParseResult(items=[], used_llm=False, intent="query", query_type="meal_check", query_term=meal_cand)
    if _QUERY_KBJU_RE.search(raw):
        return InboxParseResult(items=[], used_llm=False, intent="query", query_type="kbju")
    if _QUERY_ADVICE_RE.search(raw):
        return InboxParseResult(items=[], used_llm=False, intent="query", query_type="advice", query_term=raw)
    if _QUERY_SHOPPING_RE.search(raw):
        return InboxParseResult(items=[], used_llm=False, intent="query", query_type="shopping")
    if _QUERY_FOOD_RE.search(raw):
        return InboxParseResult(items=[], used_llm=False, intent="query", query_type="food")
    m_diary = _QUERY_DIARY_RE.search(raw)
    if m_diary:
        term = next((g for g in m_diary.groups() if g and g.strip()), "") or ""
        term = re.sub(r"[?!.,;:\"'«»]+", " ", term).strip()
        term = re.sub(r"\s+", " ", term)
        return InboxParseResult(
            items=[],
            used_llm=False,
            intent="query",
            query_type="diary",
            query_term=term,
        )
    return None


def parse_inbox_offline(
    clean_text: str,
    now_utc: datetime.datetime,
    tz_str: str,
    recent_context: Optional[list[dict[str, Any]]] = None,
) -> InboxParseResult:
    """Офлайн-разбор с поддержкой chat/query/support/capture без обращения к LLM."""
    det = _try_deterministic_intent(clean_text, recent_context=recent_context)
    if det is not None:
        return det

    items = parse_inbox_fallback(clean_text, now_utc, tz_str)
    if (
        items
        and all(it.kind == "diary" for it in items)
        and _SUPPORT_TRIGGER_RE.search(clean_text)
        and not _EXPLICIT_OBLIGATION_RE.search(clean_text)
    ):
        return InboxParseResult(
            items=items,
            used_llm=False,
            intent="support",
            reply_text=(
                "Я рядом. Не обязательно справляться со всем сразу — сделай один спокойный вдох "
                "и дай себе немного бережности. Мысль я аккуратно сохранила."
            ),
        )
    return InboxParseResult(items=items, used_llm=False, intent="capture")


_KIND_ALIASES = {
    "задача": "task",
    "дело": "task",
    "напоминание": "reminder",
    "календарь": "calendar_event",
    "в календарь": "calendar_event",
    "событие": "calendar_event",
    "встреча": "calendar_event",
    "google calendar": "calendar_event",
    "покупка": "shopping",
    "покупки": "shopping",
    "список покупок": "shopping",
    "убрать из покупок": "shopping_remove",
    "удалить из покупок": "shopping_remove",
    "shopping_del": "shopping_remove",
    "в холодильник": "pantry_add",
    "холодильник": "pantry_add",
    "кладовка": "pantry_add",
    "убрать из холодильника": "pantry_remove",
    "съел": "meal_log",
    "еда": "meal_log",
    "кбжу": "meal_log",
    "приём пищи": "meal_log",
    "вес": "weight_log",
    "весы": "weight_log",
    "взвешивание": "weight_log",
    "тренировка": "workout_log",
    "спорт": "workout_log",
    "пульс": "pulse_log",
    "режим": "pulse_log",
    "перенос хвостов": "rollover_tasks",
    "перенести хвосты": "rollover_tasks",
    "сдвиг отбоя": "shift_bedtime",
    "перенести отбой": "shift_bedtime",
    "трата": "expense",
    "расход": "expense",
    "дневник": "diary",
    "мысль": "diary",
    "заметка": "diary",
    "идея": "idea",
    "сделано": "done",
    "выполнено": "done",
}

_INTENT_ALIASES = {
    "capture": "capture",
    "запись": "capture",
    "сохранение": "capture",
    "save": "capture",
    "query": "query",
    "вопрос": "query",
    "поиск": "query",
    "запрос": "query",
    "search": "query",
    "chat": "chat",
    "чат": "chat",
    "приветствие": "chat",
    "разговор": "chat",
    "smalltalk": "chat",
    "support": "support",
    "поддержка": "support",
    "помощь": "support",
}

_QUERY_TYPE_ALIASES = {
    "none": "none",
    "": "none",
    "нет": "none",
    "null": "none",
    "today": "today",
    "сегодня": "today",
    "план": "today",
    "план на сегодня": "today",
    "дела": "today",
    "calendar": "calendar",
    "календарь": "calendar",
    "расписание": "calendar",
    "встречи": "calendar",
    "события": "calendar",
    "shopping": "shopping",
    "покупки": "shopping",
    "список покупок": "shopping",
    "купить": "shopping",
    "food": "food",
    "еда": "food",
    "ужин": "food",
    "обед": "food",
    "завтрак": "food",
    "рецепт": "food",
    "рецепты": "food",
    "готовка": "food",
    "diary": "diary",
    "дневник": "diary",
    "записи": "diary",
    "заметки": "diary",
    "karma": "karma",
    "карма": "karma",
    "режим": "karma",
    "pantry": "pantry",
    "холодильник": "pantry",
    "кладовка": "pantry",
    "продукты": "pantry",
    "advice": "advice",
    "совет": "advice",
    "что делать": "advice",
    "meal_check": "meal_check",
    "примерка": "meal_check",
    "стоит ли есть": "meal_check",
    "kbju": "kbju",
    "кбжу": "kbju",
    "калории": "kbju",
}


def _normalize_intent(raw_intent: Any) -> str:
    val = str(raw_intent or "capture").strip().lower()
    mapped = _INTENT_ALIASES.get(val, val)
    return mapped if mapped in VALID_INTENTS else "capture"


def _normalize_query_type(raw_qt: Any) -> str:
    val = str(raw_qt or "none").strip().lower()
    mapped = _QUERY_TYPE_ALIASES.get(val, val)
    return mapped if mapped in VALID_QUERY_TYPES else "none"


def validate_and_normalize_llm_items(
    raw_items: Any,
    *,
    raw_text: str,
    now_utc: datetime.datetime,
    tz_str: str,
) -> list[ParsedInboxItem]:
    if not isinstance(raw_items, list):
        return []
    out: list[ParsedInboxItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "diary").strip().lower()
        kind = _KIND_ALIASES.get(kind, kind)
        if kind not in VALID_KINDS:
            kind = "diary"
        source_quote = str(entry.get("source_quote") or entry.get("title") or raw_text).strip()
        title = str(entry.get("title") or source_quote).strip()
        model_note = str(entry.get("model_note") or "").strip()
        date_hint = str(entry.get("date_hint") or "").strip()
        time_hint = str(entry.get("time_hint") or "").strip()

        raw_tags = entry.get("tags")
        tags = (
            [str(t).strip() for t in raw_tags if str(t).strip()][:6]
            if isinstance(raw_tags, list)
            else []
        )

        try:
            qty = float(entry.get("quantity") or 1.0)
            if qty <= 0:
                qty = 1.0
        except (TypeError, ValueError):
            qty = 1.0
        unit = str(entry.get("unit") or "шт").strip() or "шт"

        try:
            amount = float(str(entry.get("amount") or 0).replace(",", "."))
            if amount < 0:
                amount = 0.0
        except (TypeError, ValueError):
            amount = 0.0
        category = str(entry.get("category") or "").strip()
        try:
            dur_min = max(15, min(720, int(entry.get("duration_minutes") or 60)))
        except (TypeError, ValueError):
            dur_min = 60
        location = str(entry.get("location") or "").strip()

        item = ParsedInboxItem(
            kind=kind,
            title=title,
            source_quote=source_quote,
            model_note=model_note,
            tags=tags,
            quantity=qty,
            unit=unit,
            amount=amount,
            category=category,
            duration_minutes=dur_min,
            location=location,
        )
        out.append(
            _apply_guardrails(
                item,
                now_utc=now_utc,
                tz_str=tz_str,
                date_hint=date_hint,
                time_hint=time_hint,
            )
        )
    return _dedupe_items(out)


async def analyze_inbox_text(
    raw_text: str,
    now_utc: datetime.datetime,
    tz_str: str,
    llm_client_instance: Optional[LLMClient] = None,
    recent_context: Optional[list[dict[str, Any]]] = None,
    user_context_block: str = "",
) -> InboxParseResult:
    """Разобрать сообщение через настроенную LLM с отказоустойчивым fallback."""
    clean_text = (raw_text or "").strip()
    if not clean_text:
        return InboxParseResult(items=[], used_llm=False)

    # Короткие приветствия и контекстные «А ещё?» обрабатываем мгновенно и без мусора в дневнике.
    chat_hit = _match_deterministic_chat(clean_text)
    if chat_hit is not None:
        return chat_hit

    stripped_punct = re.sub(r"[?!.,;:\s]+$", "", clean_text).strip()
    prev_qt = _latest_assistant_query_type(recent_context)
    if prev_qt and _MORE_FOLLOWUP_RE.fullmatch(stripped_punct):
        return InboxParseResult(
            items=[],
            used_llm=False,
            intent="query",
            query_type=prev_qt,
            query_term="more",
        )
    if prev_qt == "food" and _FOOD_FILTER_FOLLOWUP_RE.fullmatch(stripped_punct):
        return InboxParseResult(
            items=[],
            used_llm=False,
            intent="query",
            query_type="food",
            query_term=stripped_punct,
        )

    client = llm_client_instance or default_llm_client
    if client.is_configured():
        local_now = _to_local_now(now_utc, tz_str)
        ctx_block = _format_recent_context(recent_context)
        parts = [f"Локальное время пользователя: {local_now.strftime('%d.%m.%Y %H:%M')} ({tz_str})."]
        if user_context_block and user_context_block.strip():
            parts.append(user_context_block.strip())
        if ctx_block:
            parts.append(ctx_block)
        parts.append(f"Сообщение:\n{clean_text}")
        user_prompt = "\n\n".join(parts)
        res = await client.complete_json(_INBOX_SYSTEM_PROMPT, user_prompt, temperature=0.1)
        if res.ok and isinstance(res.data, dict):
            intent = _normalize_intent(res.data.get("intent"))
            reply = str(res.data.get("reply") or "").strip()
            query_type = _normalize_query_type(res.data.get("query_type"))
            query_term = str(res.data.get("query_term") or "").strip()
            items = validate_and_normalize_llm_items(
                res.data.get("items"),
                raw_text=clean_text,
                now_utc=now_utc,
                tz_str=tz_str,
            )
            if query_type != "none" and intent == "capture" and not items:
                intent = "query"
            # Защита: вопрос "думаю/хочу съесть ..." никогда не должен молча логироваться как meal_log
            if (
                len(_split_clauses(clean_text)) <= 1
                and (_check_cand := _extract_meal_check_candidate(clean_text)) is not None
                and all(it.kind in {"meal_log", "pantry_remove", "diary"} for it in items)
            ):
                intent = "query"
                query_type = "meal_check"
                query_term = _check_cand
                items = []
            if intent != "capture" or items or reply or query_type != "none":
                return InboxParseResult(
                    items=items,
                    used_llm=True,
                    llm_provider=res.provider,
                    llm_model=res.model,
                    llm_raw_json=res.text,
                    llm_error=None,
                    intent=intent,
                    reply_text=reply,
                    query_type=query_type,
                    query_term=query_term,
                )
            llm_error = "empty_items"
        else:
            llm_error = res.error or "llm_failed"

        offline = parse_inbox_offline(clean_text, now_utc, tz_str, recent_context=recent_context)
        offline.llm_provider = res.provider
        offline.llm_model = res.model
        offline.llm_raw_json = res.text
        offline.llm_error = llm_error
        return offline

    return parse_inbox_offline(clean_text, now_utc, tz_str, recent_context=recent_context)
