"""WAL-safe online SQLite backup, integrity verification, and migration dry-run tool."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db.database import connect, init_db

REQUIRED_SPRINT9_TABLES = ("inbox_messages", "journal_entries", "inbox_actions")


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_signature(db_path: Path) -> tuple[str, dict[str, int], dict[str, str]]:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
            ).fetchall()
        ]
        counts: dict[str, int] = {}
        hashes: dict[str, str] = {}
        for t in tables:
            h = hashlib.sha256()
            cnt = 0
            for row in conn.execute(f'SELECT * FROM "{t}";'):
                cnt += 1
                h.update(repr(row).encode("utf-8", errors="replace"))
            counts[t] = cnt
            hashes[t] = h.hexdigest()[:16]
        return str(integrity), counts, hashes
    finally:
        conn.close()


def create_verified_backup(source: Path, dest: Path) -> dict:
    """Create online SQLite backup and verify schema/row counts/hashes match source."""
    if not source.exists():
        raise FileNotFoundError(f"Source database does not exist: {source}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    src_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(str(dest))
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    src_integrity, src_counts, src_hashes = _snapshot_signature(source)
    dst_integrity, dst_counts, dst_hashes = _snapshot_signature(dest)

    if src_integrity.lower() != "ok":
        raise RuntimeError(f"Source integrity_check failed: {src_integrity}")
    if dst_integrity.lower() != "ok":
        raise RuntimeError(f"Backup integrity_check failed: {dst_integrity}")
    if src_counts != dst_counts or src_hashes != dst_hashes:
        raise RuntimeError("Backup content mismatch against source database")

    return {
        "source": str(source),
        "dest": str(dest),
        "size_bytes": dest.stat().st_size,
        "sha256": _file_sha256(dest),
        "integrity": dst_integrity,
        "tables_matched": True,
        "table_counts": dst_counts,
    }


async def _run_migration_probe(temp_db: Path) -> tuple[str, dict[str, int]]:
    conn = await connect(str(temp_db))
    try:
        await init_db(conn)
    finally:
        await conn.close()
    return _snapshot_signature(temp_db)[0], _snapshot_signature(temp_db)[1]


def verify_migration_on_copy(backup_path: Path, expected_counts: dict[str, int]) -> dict:
    """Copy backup to temp dir, run init_db() migration, verify integrity and preserved row counts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_db = Path(tmpdir) / backup_path.name
        shutil.copy2(backup_path, tmp_db)
        integrity, after_counts = asyncio.run(_run_migration_probe(tmp_db))

        if integrity.lower() != "ok":
            raise RuntimeError(f"Migrated copy integrity_check failed: {integrity}")

        for req_table in REQUIRED_SPRINT9_TABLES:
            if req_table not in after_counts:
                raise RuntimeError(f"Missing expected Sprint 9 table after migration: {req_table}")

        for tbl, old_cnt in expected_counts.items():
            new_cnt = after_counts.get(tbl)
            if new_cnt is None or new_cnt < old_cnt:
                raise RuntimeError(
                    f"Migration check failed for table {tbl}: before={old_cnt}, after={new_cnt}"
                )

        return {
            "migrate_integrity": integrity,
            "new_tables_present": True,
            "preserved_tables": len(expected_counts),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create and verify a SQLite backup for HiDL.")
    parser.add_argument("--source", required=True, help="Path to source SQLite database.")
    parser.add_argument("--dest", required=True, help="Path to destination backup SQLite file.")
    parser.add_argument(
        "--check-migrate",
        action="store_true",
        help="Run init_db() migration dry-run on a temporary copy of the backup.",
    )
    args = parser.parse_args(argv)

    result = create_verified_backup(Path(args.source), Path(args.dest))
    print(
        f"BACKUP_OK dest={result['dest']} size={result['size_bytes']} "
        f"sha256={result['sha256'][:16]} integrity={result['integrity']} "
        f"tables_matched={result['tables_matched']}"
    )
    print("NONZERO_COUNTS:", {k: v for k, v in result["table_counts"].items() if v > 0})

    if args.check_migrate:
        mig = verify_migration_on_copy(Path(args.dest), result["table_counts"])
        print(
            f"MIGRATE_OK integrity={mig['migrate_integrity']} "
            f"new_tables_present={mig['new_tables_present']} "
            f"preserved_tables={mig['preserved_tables']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
