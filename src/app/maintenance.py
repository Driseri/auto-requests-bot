from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys

REQUIRED_TABLES = {
    "drafts",
    "user_settings",
    "submitted_applications",
    "bulk_batches",
}


def create_backup(
    source_path: str,
    backup_directory: str,
    *,
    retention_days: int = 7,
    now: datetime | None = None,
) -> Path:
    source = Path(source_path)
    destination_directory = Path(backup_directory)
    destination_directory.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    destination = destination_directory / f"app-{timestamp}.db"
    with sqlite3.connect(source, timeout=10) as source_db:
        source_db.execute("PRAGMA busy_timeout=10000")
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)
    verify_database(destination)
    cleanup_old_backups(
        destination_directory,
        retention_days=retention_days,
        now=now,
    )
    return destination


def restore_backup(backup_path: str, destination_path: str) -> Path:
    backup = Path(backup_path)
    destination = Path(destination_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    verify_database(backup)
    with sqlite3.connect(backup, timeout=10) as source_db:
        with sqlite3.connect(destination, timeout=10) as destination_db:
            source_db.backup(destination_db)
    verify_database(destination)
    return destination


def verify_database(path: str | Path) -> None:
    with sqlite3.connect(path, timeout=10) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"SQLite integrity_check failed for {path}: {integrity}")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    missing = REQUIRED_TABLES - tables
    if missing:
        raise RuntimeError(
            f"SQLite backup {path} is missing required tables: {sorted(missing)}"
        )


def cleanup_old_backups(
    backup_directory: str | Path,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> None:
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    for backup in Path(backup_directory).glob("app-*.db"):
        modified_at = datetime.fromtimestamp(backup.stat().st_mtime, timezone.utc)
        if modified_at < cutoff:
            backup.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite backup and restore utility")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--source", default="/data/app.db")
    backup_parser.add_argument("--directory", default="/backups")
    backup_parser.add_argument("--retention-days", type=int, default=7)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("path")

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("backup")
    restore_parser.add_argument("destination")

    args = parser.parse_args()
    if args.command == "backup":
        print(
            create_backup(
                args.source,
                args.directory,
                retention_days=args.retention_days,
            )
        )
    elif args.command == "verify":
        verify_database(args.path)
        print("ok")
    else:
        print(restore_backup(args.backup, args.destination))
    return 0


if __name__ == "__main__":
    sys.exit(main())
