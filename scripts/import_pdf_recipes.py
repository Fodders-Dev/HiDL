"""CLI для импорта личных рецептов из JSON или PDF-книг в таблицу user_recipes HiDL."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import get_settings
from db import repositories as repo
from db.database import connect, init_db
from llm_client import client as llm_client

_PDF_SYSTEM_PROMPT = (
    "Ты аккуратно извлекаешь кулинарные рецепты из текста книги в строгий JSON без выдумок. "
    "Верни объект: {\"recipes\": [{\"title\": str, \"desc\": str, \"time_minutes\": int, "
    "\"base_servings\": int, \"tags\": [str], \"ingredients\": [{\"name\": str, \"qty\": float, \"unit\": str}], "
    "\"steps\": [str]}]}. Теги выбирай только из: vegan, vegetarian, omnivore, breakfast, lunch, dinner, snack, soup, salad, fast, healthy, comfort_food."
)


def _deterministic_pdf_key(source_label: str, title: str) -> str:
    digest = hashlib.sha1(f"{source_label}::{title.casefold()}".encode("utf-8")).hexdigest()
    return f"pdf_{digest[:10]}"


def _extract_pdf_pages(pdf_path: Path) -> List[str]:
    try:
        import pypdf  # type: ignore

        reader = pypdf.PdfReader(str(pdf_path))
        return [(page.extract_text() or "").strip() for page in reader.pages]
    except ImportError:
        pass
    try:
        import fitz  # type: ignore

        doc = fitz.open(str(pdf_path))
        return [page.get_text("text").strip() for page in doc]
    except ImportError as exc:
        raise RuntimeError(
            "Для чтения PDF установите `pypdf` или `pymupdf`, либо передайте уже извлечённый JSON через --json."
        ) from exc


def _normalize_imported_recipe(raw: Dict[str, Any], default_diet: str) -> Dict[str, Any]:
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ValueError("Пустое название рецепта.")
    tags = [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()]
    if default_diet in {"vegan", "vegetarian", "omnivore"} and default_diet not in tags:
        tags.append(default_diet)
    if default_diet == "vegan" and "vegetarian" not in tags:
        tags.append("vegetarian")
    ings: List[Dict[str, Any]] = []
    for ing in raw.get("ingredients") or []:
        if isinstance(ing, dict) and str(ing.get("name") or "").strip():
            ings.append(
                {
                    "name": str(ing["name"]).strip(),
                    "qty": float(ing.get("qty") or 1.0),
                    "unit": str(ing.get("unit") or "шт").strip() or "шт",
                }
            )
    steps = [str(s).strip() for s in (raw.get("steps") or []) if str(s).strip()]
    return {
        "title": title,
        "desc": str(raw.get("desc") or "").strip(),
        "tags": tags,
        "base_servings": max(1, int(raw.get("base_servings") or 2)),
        "time_minutes": max(1, int(raw.get("time_minutes") or 25)),
        "ingredients": ings,
        "steps": steps,
    }


async def _parse_pdf_to_recipes(pdf_path: Path, default_diet: str) -> List[Dict[str, Any]]:
    if not llm_client.is_configured():
        raise RuntimeError("LLM не настроен — задайте ключи в окружении или передайте --json.")
    pages = [p for p in _extract_pdf_pages(pdf_path) if p]
    chunk_size = 4
    collected: List[Dict[str, Any]] = []
    seen_titles: set[str] = set()
    for idx in range(0, len(pages), chunk_size):
        chunk_text = "\n\n---\n\n".join(pages[idx : idx + chunk_size])[:12000]
        res = await llm_client.complete_json(
            _PDF_SYSTEM_PROMPT,
            f"Диета книги по умолчанию: {default_diet}.\n\nТекст страниц книги:\n{chunk_text}",
            temperature=0.1,
            max_tokens=2200,
        )
        if not res.ok or not isinstance(res.data, dict):
            continue
        for raw in res.data.get("recipes") or []:
            if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
                continue
            norm = _normalize_imported_recipe(raw, default_diet)
            tk = norm["title"].casefold()
            if tk in seen_titles:
                continue
            seen_titles.add(tk)
            collected.append(norm)
    return collected


def _load_json_recipes(json_path: Path, default_diet: str) -> List[Dict[str, Any]]:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    raw_list = payload.get("recipes") if isinstance(payload, dict) else payload
    if not isinstance(raw_list, list):
        raise ValueError("JSON должен содержать список рецептов или объект {'recipes': [...]}.")
    out: List[Dict[str, Any]] = []
    for raw in raw_list:
        if isinstance(raw, dict) and str(raw.get("title") or "").strip():
            out.append(_normalize_imported_recipe(raw, default_diet))
    return out


async def run_import(args: argparse.Namespace) -> int:
    source_path = Path(args.json or args.pdf).resolve()
    if not source_path.exists():
        print(f"Файл не найден: {source_path}", file=sys.stderr)
        return 1
    source_label = args.source_label or source_path.stem

    if args.json:
        recipes = _load_json_recipes(source_path, args.diet)
    else:
        recipes = await _parse_pdf_to_recipes(source_path, args.diet)

    print(f"Извлечено рецептов из «{source_label}»: {len(recipes)}")
    if args.dry_run:
        for r in recipes[:10]:
            print(f"  - {r['title']} ({r['time_minutes']} мин, {len(r['ingredients'])} ингр.)")
        return 0

    db = await connect(args.db)
    try:
        await init_db(db)
        user = await repo.get_user_by_telegram_id(db, args.user_tg)
        if not user:
            print(f"Пользователь с telegram_id={args.user_tg} не найден в БД {args.db}", file=sys.stderr)
            return 1
        uid = int(user["id"])
        for rec in recipes:
            rkey = _deterministic_pdf_key(source_label, rec["title"])
            await repo.upsert_user_recipe(
                db,
                uid,
                recipe_key=rkey,
                title=rec["title"],
                desc=rec["desc"],
                tags=rec["tags"],
                base_servings=rec["base_servings"],
                time_minutes=rec["time_minutes"],
                ingredients=rec["ingredients"],
                steps=rec["steps"],
                source="pdf_book",
                source_label=source_label,
            )
        print(f"Сохранено в user_recipes для user_id={uid} (tg={args.user_tg}): {len(recipes)} шт.")
    finally:
        await db.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Импорт рецептов из JSON или PDF-книг в HiDL user_recipes.")
    src_group = parser.add_mutually_exclusive_group(required=True)
    src_group.add_argument("--json", help="Путь к JSON со списком рецептов")
    src_group.add_argument("--pdf", help="Путь к PDF-книге с рецептами")
    parser.add_argument("--user-tg", type=int, required=True, help="Telegram ID владельца")
    parser.add_argument("--db", default=get_settings(require_bot_token=False).database_url, help="Путь к SQLite БД HiDL")
    parser.add_argument("--source-label", default="", help="Название книги для source_label")
    parser.add_argument("--diet", choices=["vegan", "vegetarian", "omnivore"], default="vegan")
    parser.add_argument("--dry-run", action="store_true", help="Показать извлечённые рецепты без записи в БД")
    args = parser.parse_args()
    return asyncio.run(run_import(args))


if __name__ == "__main__":
    raise SystemExit(main())
