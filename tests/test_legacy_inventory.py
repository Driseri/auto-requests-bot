from __future__ import annotations

import sqlite3

import pytest

from app.legacy_inventory import build_legacy_inventory
from app.repository import DraftRepository, LATEST_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_inventory_reports_legacy_blockers_without_writing_database(tmp_path):
    database = tmp_path / "inventory.db"
    repository = DraftRepository(str(database))
    await repository.init()

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO bulk_batches (
                batch_id, telegram_user_id, spreadsheet_id, direction,
                sheet_name, sheet_id, start_row, data_start_row,
                reserved_rows, registration_state, registered_count,
                created_at, updated_at
            ) VALUES (
                'BATCH-TEST', 1, 'sheet', 'ФЛ', 'Массовый ввод',
                10, 2, 4, 10, 'REGISTERED', 1,
                '2026-07-27T00:00:00+00:00',
                '2026-07-27T00:00:00+00:00'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO submitted_applications (
                application_id, telegram_user_id, spreadsheet_id, sheet_id,
                sheet_name, last_known_status, application_type, batch_id,
                last_seen_row_number, polling_state, created_at, updated_at
            ) VALUES (
                'ABC12345', 1, 'sheet', 10, 'Массовый ввод', 'Новая',
                'Массовая', 'BATCH-TEST', 4, 'ACTIVE',
                '2026-07-27T00:00:00+00:00',
                '2026-07-27T00:00:00+00:00'
            )
            """
        )
        connection.commit()

    report = build_legacy_inventory(str(database))

    assert report["database"]["user_version"] == LATEST_SCHEMA_VERSION
    assert report["database"]["integrity_check"] == "ok"
    assert report["legacy"]["bulk_applications"]["count"] == 1
    assert report["legacy"]["bulk_batches"]["count"] == 1
    assert report["legacy"]["bulk_applications"]["items"][0]["application_id"] == "ABC12345"
    assert report["blockers"] == [
        "active_legacy_bulk_applications",
        "legacy_bulk_batches_present",
    ]

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM submitted_applications WHERE application_id = 'ABC12345'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM bulk_batches WHERE batch_id = 'BATCH-TEST'"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_repository_records_versioned_schema_migrations(tmp_path):
    database = tmp_path / "migrations.db"
    repository = DraftRepository(str(database))
    await repository.init()
    await repository.init()

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        rows = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert rows == [
        (1, "llm_completeness_check"),
        (2, "versioned_migration_framework"),
    ]


@pytest.mark.asyncio
async def test_repository_rejects_newer_schema_before_bootstrap(tmp_path):
    database = tmp_path / "future.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 999")

    repository = DraftRepository(str(database))
    with pytest.raises(RuntimeError, match="database=999 application=2"):
        await repository.init()

    with sqlite3.connect(database) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    assert tables == []
