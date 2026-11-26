"""Официальный онлайн-поиск КБЖУ брендовых продуктов и блюд из кафе/ресторанов.

Источники (без скрапинга и без капч):
0. Личные сохранённые этикетки пользователя (`user_food_products`).
1. Кэш в SQLite (`nutrition_lookup_cache`, 30 дней).
2. Open Food Facts (`https://search.openfoodfacts.org/search` и API v2 по штрихкоду).
3. Официальный OpenAI Responses Web Search (`client.search_web_json`) для меню кафе/доставок/ресторанов.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from urllib.parse import quote_plus

import aiohttp

from db import repositories as repo
from llm_client import LLMClient, client as default_llm_client
from utils.logger import log_error

OFF_SEARCH_URL = "https://search.openfoodfacts.org/search"
OFF_PRODUCT_V2_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
_USER_AGENT = "HiDLBot/1.0 (+https://hidl.fodders.ru)"

_WEB_BRANDS_OR_VENUES: Tuple[str, ...] = (
    "теремок",
    "теремк",
    "вкусно и точка",
    "макдак",
    "макдональдс",
    "бургер кинг",
    "burger king",
    "додо",
    "dodo",
    "шоколадниц",
    "surf coffee",
    "серф кофе",
    "cofix",
    "кофикс",
    "вкусвилл",
    "vkusvill",
    "яндекс лавк",
    "из лавки",
    "самокат",
    "азбука вкуса",
    "азбуке вкуса",
    "азбуки вкуса",
    "ростикс",
    "rostics",
    "kfc",
    "кфс",
    "крошка картошка",
    "крошке картошке",
    "stars coffee",
    "starbucks",
    "one price coffee",
    "drinkit",
    "дринкит",
    "му-му",
    "тануки",
    "якитория",
    "папа джонс",
    "papa johns",
    "доминос",
    "dominos",
    "брусника",
    "бруснике",
    "андерсон",
    "кофемания",
    "кофемании",
    "subway",
    "сабвей",
    "буше",
    "цех 85",
    "цех85",
    "nemoloko",
    "немолоко",
    "alpro",
    "альпро",
    "ritter",
    "риттер",
    "bombbar",
    "бомббар",
    "proteinrex",
    "chikalab",
    "hi!",
    "greenwise",
    "гринвайз",
    "epica",
    "эпика",
    "экспонента",
    "exponenta",
    "активиа",
    "snickers",
    "сникерс",
    "twix",
    "bounty",
    "oreo",
    "кока-кола",
    "coca-cola",
    "pepsi",
    "пепси",
    "red bull",
    "ред булл",
)

_CAFE_OR_VENUE_RE = re.compile(
    r"(?iu)(?:\bв\s+(?:кафе|ресторане|кофейн\w*|столовой|доставк\w*|пекарн\w*|мак\w*)|"
    r"\bиз\s+(?:доставки|ресторана|кафе|лавки|вкусвилла|самоката|меню)|"
    r"\b(?:биг\s+спешиал|биг\s+хит|воппер|додстер|стартер|пепперони|флэт\s+уайт|"
    r"бабл\s+ти|матча|раф|ролл\s+филадельфи\w*|том\s+ям|фо\s+бо|поке|шаурм\w*|шаверм\w*)\b)"
)

_CAFE_QUERY_HINT_RE = re.compile(
    r"(?iu)(?:\b(?:теремок|теремк\w*|вкусно и точка|макдак\w*|макдональдс\w*|бургер кинг\w*|додо|шоколадниц\w*|"
    r"surf coffee|серф\w*|cofix|кофикс\w*|ростикс\w*|kfc|кфс|крошк\w*\s+картошк\w*|stars coffee|starbucks|"
    r"one price\w*|drinkit|дринкит\w*|му-му|тануки|якитори\w*|папа джонс|доминос|брусник\w*|кофемани\w*|буше|цех\s*85|цех85)\b|"
    r"\bв\s+(?:кафе|ресторане|кофейн\w*|столовой)\b|\bиз\s+(?:меню|ресторана|кафе|доставки)\b)"
)

_LEADING_ACTION_RE = re.compile(
    r"(?iu)^(?:я\s+|только\s+что\s+|сегодня\s+|сейчас\s+|утром\s+|днём\s+|вечером\s+)*"
    r"(?:съел[аи]?|выпил[аи]?|поел[аи]?|доел[аи]?|перекусил[аи]?|позавтракал[аи]?|"
    r"пообедал[аи]?|поужинал[аи]?|заказал[аи]?|взял[аи]?|думаю\s+(?:съесть|взять|заказать|выпить)|"
    r"хочу\s+(?:съесть|взять|заказать|выпить)|планирую\s+(?:съесть|на\s+ужин|на\s+обед)|"
    r"можно\s+(?:ли\s+)?(?:мне\s+)?(?:съесть|выпить)?|стоит\s+(?:ли\s+)?(?:съесть|заказать|брать)?)\s+"
)

_TRAILING_QTY_RE = re.compile(
    r"(?iu)\b(?:\d+(?:[.,]\d+)?\s*(?:кг|kg|г|гр|g|мл|ml|л|l|шт|порц[а-я]*|куск[а-я]*|батончик[а-я]*|стакан[а-я]*)|"
    r"полпорц[а-я]*|половин[ауы]|две|два|три|четыре)\b"
)

_BARCODE_RE = re.compile(r"\b(\d{8,14})\b")


def normalize_food_query(text: str) -> str:
    """Нормализовать запрос продукта/блюда для ключа кэша и поиска."""
    clean = str(text or "").strip().casefold().replace("ё", "е")
    clean = re.sub(r"[?!.,;:\"'«»()]+", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = _LEADING_ACTION_RE.sub("", clean).strip()
    clean = _TRAILING_QTY_RE.sub(" ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or re.sub(r"\s+", " ", str(text or "").strip().casefold().replace("ё", "е"))


def needs_web_nutrition_lookup(text: str) -> bool:
    """Проверить, стоит ли искать КБЖУ продукта/блюда через сеть (бренды, кафе, штрихкод)."""
    low = str(text or "").strip().casefold().replace("ё", "е")
    if not low:
        return False
    if _BARCODE_RE.search(low):
        return True
    if any(brand in low for brand in _WEB_BRANDS_OR_VENUES):
        return True
    if _CAFE_OR_VENUE_RE.search(low):
        return True
    return False


def _is_cafe_or_menu_query(text: str) -> bool:
    low = str(text or "").strip().casefold().replace("ё", "е")
    return bool(_CAFE_QUERY_HINT_RE.search(low))


_MASS_VOL_UNIT = r"(?:кг|kg|грамм(?:ов|а)?|гр|г|g|миллилитр(?:ов|а)?|мл|ml|литр(?:ов|а)?|л|l)"
_MASS_VOL_RE = re.compile(
    rf"(?iu)(?:\b(\d+(?:[.,]\d+)?)\s*({_MASS_VOL_UNIT})\b|\b({_MASS_VOL_UNIT})\s*(\d+(?:[.,]\d+)?)\b)"
)
_SMALL_PORTION_ADDIN_FOLLOW = (
    r"(?!\s+(?:ложк\w*|ст\.?\s*л\.?|ч\.?\s*л\.?|кубик\w*|дольк\w*|щепот\w*|ломтик\w*|пакетик\w*|кусоч\w*\s+сахар\w*|капл\w*)\b)"
)


def extract_requested_grams_and_mult(
    raw_text: str, default_serving_g: float = 0.0
) -> Tuple[Optional[float], float]:
    """Извлечь явные граммы/мл или множитель порции (например '2 шт', 'полпорции', '250 мл')."""
    low = str(raw_text or "").strip().casefold().replace("ё", "е")
    # Не путаем числа в названиях вроде "Цех 85" или штрихкоды. Поддерживаем '300 грамм' и 'мл 300'.
    mass_matches = list(_MASS_VOL_RE.finditer(low))
    if len(mass_matches) >= 2:
        # Несколько разных граммовок в одном комбо ("мл 300 ... 300 грамм") не должны масштабировать всё блюдо.
        return None, 1.0
    if len(mass_matches) == 1:
        m_mass = mass_matches[0]
        val_s = m_mass.group(1) or m_mass.group(4) or "0"
        unit = (m_mass.group(2) or m_mass.group(3) or "").lower()
        val = float(val_s.replace(",", "."))
        is_kilo_or_liter = unit in {"кг", "kg", "л", "l"} or unit.startswith("литр")
        grams = val * 1000.0 if is_kilo_or_liter else val
        grams = max(1.0, min(5000.0, grams))
        mult = (grams / default_serving_g) if default_serving_g > 0 else 1.0
        return round(grams, 1), round(mult, 3)

    if any(w in low for w in ("полпорц", "половина", "половинк", "0.5 порц", "0,5 порц")):
        return None, 0.5
    if any(w in low for w in ("двойн", "x2")):
        return None, 2.0

    m_cnt = re.search(
        r"\b(\d+(?:[.,]\d+)?)\s*(?:шт|порц|куск|блин|ролл|стакан|кружк|батончик|пачк|сырник|бургер)",
        low,
    )
    if m_cnt:
        cnt = max(0.25, min(10.0, float(m_cnt.group(1).replace(",", "."))))
        return None, cnt

    if re.search(rf"(?iu)\b(?:два|две|пару|парочку)\b{_SMALL_PORTION_ADDIN_FOLLOW}", low):
        return None, 2.0
    if re.search(rf"(?iu)\bтри\b{_SMALL_PORTION_ADDIN_FOLLOW}", low):
        return None, 3.0
    return None, 1.0


def scale_lookup_record(record: Dict[str, Any], raw_text: str) -> Optional[Dict[str, Any]]:
    """Пересчитать найденную запись КБЖУ (из кэша, OFF, этикетки или веб-поиска) на запрошенную порцию."""
    if not isinstance(record, dict):
        return None
    per_100 = record.get("per_100g") if isinstance(record.get("per_100g"), dict) else {}
    has_p100 = "kcal" in per_100 and per_100.get("kcal") is not None
    p100_k = float(per_100.get("kcal") or 0)
    p100_p = float(per_100.get("protein_g") or 0)
    p100_f = float(per_100.get("fat_g") or 0)
    p100_c = float(per_100.get("carbs_g") or 0)

    serving_g = float(record.get("serving_g") or 0)
    has_srv = record.get("kcal_serving") is not None
    kcal_srv = float(record.get("kcal_serving") or 0)
    p_srv = float(record.get("protein_g") or 0)
    f_srv = float(record.get("fat_g") or 0)
    c_srv = float(record.get("carbs_g") or 0)

    if record.get("_fresh_web_for_query") and has_srv and kcal_srv >= 0:
        explicit_grams, mult = None, 1.0
    else:
        explicit_grams, mult = extract_requested_grams_and_mult(raw_text, default_serving_g=serving_g)

    if explicit_grams is not None and has_p100:
        factor = explicit_grams / 100.0
        kcal = p100_k * factor
        prot = p100_p * factor
        fat = p100_f * factor
        carbs = p100_c * factor
        portion_note = f"{int(round(explicit_grams))} г"
    elif explicit_grams is not None and has_srv and serving_g > 0:
        factor = explicit_grams / serving_g
        kcal = kcal_srv * factor
        prot = p_srv * factor
        fat = f_srv * factor
        carbs = c_srv * factor
        portion_note = f"{int(round(explicit_grams))} г"
    elif has_srv:
        kcal = kcal_srv * mult
        prot = p_srv * mult
        fat = f_srv * mult
        carbs = c_srv * mult
        portion_note = (
            f"{mult:g} порц."
            if mult != 1.0
            else (f"~{int(round(serving_g))} г" if serving_g > 0 else "1 порция")
        )
    elif has_p100:
        base_g = serving_g if serving_g > 0 else 100.0
        factor = (base_g / 100.0) * mult
        kcal = p100_k * factor
        prot = p100_p * factor
        fat = p100_f * factor
        carbs = p100_c * factor
        portion_note = f"~{int(round(base_g * mult))} г"
    else:
        return None

    if not (0.0 <= kcal <= 4500.0):
        return None
    title = str(record.get("title") or "").strip()
    if mult != 1.0 and explicit_grams is None and title and f"{mult:g}" not in title:
        title = f"{title} ({mult:g} шт/порц.)"
    elif explicit_grams is not None and title and str(int(round(explicit_grams))) not in title:
        title = f"{title} ({int(round(explicit_grams))} г)"

    return {
        "title": title or "Продукт из базы",
        "kcal": int(round(kcal)),
        "protein_g": round(max(0.0, prot), 1),
        "fat_g": round(max(0.0, fat), 1),
        "carbs_g": round(max(0.0, carbs), 1),
        "per_100g": per_100 if p100_k > 0 else {},
        "serving_g": explicit_grams or serving_g or 100.0,
        "portion_note": portion_note,
        "lookup_source": str(record.get("source") or "web_search"),
        "lookup_url": str(record.get("source_url") or ""),
    }


def _parse_serving_grams(raw: Any) -> float:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:g|г|ml|мл)\b", str(raw or "").lower())
    if not m:
        return 0.0
    try:
        val = float(m.group(1).replace(",", "."))
        return val if 1.0 <= val <= 2000.0 else 0.0
    except Exception:
        return 0.0


def _extract_off_record(hit: Dict[str, Any], code: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(hit, dict):
        return None
    nutr = hit.get("nutriments") or {}
    if not isinstance(nutr, dict):
        return None
    raw_kcal_100 = nutr.get("energy-kcal_100g")
    if raw_kcal_100 is None:
        raw_kcal_100 = nutr.get("energy-kcal")
    if raw_kcal_100 is None:
        return None
    try:
        kcal_100 = float(raw_kcal_100)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= kcal_100 <= 950.0):
        return None

    def _f(key: str) -> float:
        try:
            return round(max(0.0, float(nutr.get(key) or 0.0)), 1)
        except (TypeError, ValueError):
            return 0.0

    title = str(hit.get("product_name_ru") or hit.get("product_name") or "").strip()
    brand = str(hit.get("brands") or "").split(",")[0].strip()
    if not title:
        return None
    full_title = f"{title} ({brand})" if brand and brand.lower() not in title.lower() else title
    serving_g = _parse_serving_grams(hit.get("serving_size"))
    per_100 = {
        "kcal": round(kcal_100, 1),
        "protein_g": _f("proteins_100g"),
        "fat_g": _f("fat_100g"),
        "carbs_g": _f("carbohydrates_100g"),
    }
    p_code = str(hit.get("code") or code or "").strip()
    src_url = f"https://world.openfoodfacts.org/product/{p_code}" if p_code else "https://world.openfoodfacts.org"
    kcal_srv = round(per_100["kcal"] * serving_g / 100.0, 1) if serving_g > 0 else 0.0
    return {
        "title": full_title,
        "brand": brand,
        "source": "openfoodfacts",
        "source_url": src_url,
        "per_100g": per_100,
        "serving_g": serving_g,
        "kcal_serving": kcal_srv,
        "protein_g": round(per_100["protein_g"] * serving_g / 100.0, 1) if serving_g > 0 else 0.0,
        "fat_g": round(per_100["fat_g"] * serving_g / 100.0, 1) if serving_g > 0 else 0.0,
        "carbs_g": round(per_100["carbs_g"] * serving_g / 100.0, 1) if serving_g > 0 else 0.0,
    }


async def search_openfoodfacts(
    query: str,
    *,
    timeout_sec: float = 4.5,
    session: Optional[aiohttp.ClientSession] = None,
) -> Optional[Dict[str, Any]]:
    """Поиск магазинного продукта или штрихкода через официальный API Open Food Facts."""
    clean = normalize_food_query(query)
    if not clean:
        return None

    m_code = _BARCODE_RE.search(str(query or ""))
    headers = {"User-Agent": _USER_AGENT}
    timeout = aiohttp.ClientTimeout(total=max(1.5, float(timeout_sec)))
    own_session = session is None
    sess = session or aiohttp.ClientSession(timeout=timeout)
    try:
        if m_code:
            code = m_code.group(1)
            async with sess.get(OFF_PRODUCT_V2_URL.format(code=code), headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    rec = _extract_off_record((data or {}).get("product") or {}, code=code)
                    if rec:
                        return rec

        q_tokens = {w for w in clean.split() if len(w) >= 3}
        url = f"{OFF_SEARCH_URL}?q={quote_plus(clean)}&page_size=6"
        async with sess.get(url, headers=headers) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json(content_type=None)
        hits = (payload or {}).get("hits") or []
        best_rec = None
        best_score = -1
        for hit in hits:
            rec = _extract_off_record(hit)
            if not rec:
                continue
            name_low = rec["title"].casefold().replace("ё", "е")
            name_tokens = {w for w in re.findall(r"\w+", name_low) if len(w) >= 3}
            score = len(q_tokens & name_tokens) * 10
            if rec.get("serving_g", 0) > 0:
                score += 2
            # Для кириллических запросов отдаём приоритет кириллическим или точным совпадениям бренда
            if any("а" <= ch <= "я" for ch in clean) and any("а" <= ch <= "я" for ch in name_low):
                score += 5
            if score > best_score and (score >= 10 or len(hits) == 1):
                best_score = score
                best_rec = rec
        return best_rec
    except Exception as exc:  # noqa: BLE001
        log_error(f"[nutrition_web] openfoodfacts error: {exc.__class__.__name__}")
        return None
    finally:
        if own_session:
            await sess.close()


_WEB_NUTRITION_SYSTEM = (
    "Ты — справочник пищевой ценности HiDL. Найди в интернете официальное КБЖУ для указанного "
    "блюда из ресторана/кафе/доставки или магазинного бренда (сначала смотри официальное меню, "
    "состав на сайте бренда, затем проверенные русскоязычные базы вроде health-diet.ru / calorizator / fatsecret).\n"
    "Ответь СТРОГО валидным JSON-объектом вида:\n"
    "{\"found\": bool, \"title\": str, \"source_name\": str, \"source_url\": str, "
    "\"serving_g\": float, \"kcal_serving\": float, \"protein_g\": float, \"fat_g\": float, \"carbs_g\": float, "
    "\"per_100g\": {\"kcal\": float, \"protein_g\": float, \"fat_g\": float, \"carbs_g\": float}}.\n"
    "Если данных в поиске нет — поставь \"found\": false. Не выдумывай точные цифры наугад."
)


async def search_web_nutrition(
    query: str,
    *,
    llm_client_instance: Optional[LLMClient] = None,
) -> Optional[Dict[str, Any]]:
    """Найти КБЖУ блюда из меню кафе/доставки или продукта через официальный OpenAI web_search."""
    client = llm_client_instance or default_llm_client
    clean = normalize_food_query(query)
    if not clean or not client.is_configured():
        return None
    res = await client.search_web_json(
        _WEB_NUTRITION_SYSTEM,
        f"Найди реальное КБЖУ и стандартный вес порции для: {query.strip()}",
        max_tokens=1400,
        timeout_sec=25.0,
    )
    if not res.ok or not isinstance(res.data, dict):
        return None
    data = res.data
    if not data.get("found", True):
        return None

    def _f(v: Any) -> float:
        try:
            return round(max(0.0, float(v or 0.0)), 1)
        except (TypeError, ValueError):
            return 0.0

    per_100_raw = data.get("per_100g") if isinstance(data.get("per_100g"), dict) else {}
    has_p100 = per_100_raw.get("kcal") is not None
    per_100 = {
        "kcal": _f(per_100_raw.get("kcal")),
        "protein_g": _f(per_100_raw.get("protein_g")),
        "fat_g": _f(per_100_raw.get("fat_g")),
        "carbs_g": _f(per_100_raw.get("carbs_g")),
    }
    serving_g = _f(data.get("serving_g"))
    has_srv = data.get("kcal_serving") is not None
    kcal_srv = _f(data.get("kcal_serving"))
    if not ((has_srv and 0.0 <= kcal_srv <= 4000.0) or (has_p100 and 0.0 <= per_100["kcal"] <= 950.0)):
        return None

    citations = data.get("_citations") if isinstance(data.get("_citations"), list) else []
    src_url = str(data.get("source_url") or (citations[0] if citations else "")).strip()
    return {
        "title": str(data.get("title") or clean).strip(),
        "brand": str(data.get("source_name") or "").strip(),
        "source": "web_search",
        "source_url": src_url,
        "per_100g": per_100,
        "serving_g": serving_g,
        "kcal_serving": kcal_srv,
        "protein_g": _f(data.get("protein_g")),
        "fat_g": _f(data.get("fat_g")),
        "carbs_g": _f(data.get("carbs_g")),
    }


SearchFn = Callable[[str], Awaitable[Optional[Dict[str, Any]]]]


async def lookup_food_nutrition(
    db,
    user_id: Optional[int],
    query: str,
    *,
    allow_network: bool = True,
    llm_client_instance: Optional[LLMClient] = None,
    off_fn: Optional[SearchFn] = None,
    web_fn: Optional[SearchFn] = None,
) -> Optional[Dict[str, Any]]:
    """Многоуровневый поиск КБЖУ: личные этикетки -> кэш SQLite -> OpenFoodFacts -> OpenAI Web Search."""
    clean_key = normalize_food_query(query)
    if not clean_key:
        return None

    # Tier 0: сохранённые личные этикетки пользователя
    if db is not None and user_id is not None:
        user_hit = await repo.find_user_food_product(db, int(user_id), query)
        if user_hit:
            return user_hit

    # Tier 1: локальный 30-дневный кэш
    if db is not None:
        cached = await repo.get_nutrition_cache(db, clean_key)
        if cached:
            return cached

    if not allow_network:
        return None

    found: Optional[Dict[str, Any]] = None
    is_cafe = _is_cafe_or_menu_query(query)
    client = llm_client_instance or default_llm_client

    # Tier 2: Open Food Facts для штрихкодов и магазинных брендов (или если не чисто ресторан)
    if not is_cafe or _BARCODE_RE.search(str(query or "")):
        if off_fn is not None:
            found = await off_fn(query)
        else:
            found = await search_openfoodfacts(query)

    # Tier 3: OpenAI official Web Search (меню ресторанов/кафе или если OFF ничего не нашёл)
    if found is None and (web_fn is not None or client.is_configured()):
        if web_fn is not None:
            found = await web_fn(query)
        else:
            found = await search_web_nutrition(query, llm_client_instance=client)
        if isinstance(found, dict):
            found = {**found, "_fresh_web_for_query": True}

    is_bloated_combo_cache = bool(
        found
        and (
            len(clean_key) > 95
            or ("+" in str(found.get("title") or "") and float(found.get("serving_g") or 0) >= 450)
        )
    )
    if found and db is not None and not is_bloated_combo_cache:
        await repo.upsert_nutrition_cache(
            db,
            clean_key,
            title=str(found.get("title") or clean_key),
            source=str(found.get("source") or "web_search"),
            source_url=str(found.get("source_url") or ""),
            per_100g=found.get("per_100g") if isinstance(found.get("per_100g"), dict) else {},
            serving_g=float(found.get("serving_g") or 0.0),
            kcal_serving=float(found.get("kcal_serving") or 0.0),
            protein_g=float(found.get("protein_g") or 0.0),
            fat_g=float(found.get("fat_g") or 0.0),
            carbs_g=float(found.get("carbs_g") or 0.0),
        )
    return found
