from __future__ import annotations

import json
import sqlite3

import pytest

from app.legacy_cleanup import (
    LegacyCleanupError,
    build_cleanup_plan,
    execute_cleanup,
)
from app.repository import DraftRepository


async def _database_with_legacy_data(tmp_path):
    database = tmp_path / "cleanup.db"
    await DraftRepository(str(database)).init()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO bulk_batches (
                batch_id, telegram_user_id, sheet_name, sheet_id, start_row,
                data_start_row, reserved_rows, registration_state,
                registered_count, created_at, updated_at
            ) VALUES ('BATCH-1', 1, 'Mass', 1, 2, 4, 10, 'REGISTERED', 1, 't', 't')
            """
        )
        connection.execute(
            """
            INSERT INTO bulk_creation_requests (
                idempotency_key, telegram_user_id, direction, state, batch_id,
                started_at, created_at, updated_at
            ) VALUES ('request-1', 1, 'FL', 'CREATED', 'BATCH-1', 't', 't', 't')
            """
        )
        connection.execute(
            """
            INSERT INTO submitted_applications (
                application_id, telegram_user_id, sheet_name, last_known_status,
                application_type, batch_id, last_seen_row_number, polling_state,
                created_at, updated_at
            ) VALUES ('LEGACY01', 1, 'Mass', 'New', 'Bulk', 'BATCH-1', 4, 'ACTIVE', 't', 't')
            """
        )
        connection.execute(
            """
            INSERT INTO application_events (
                application_id, telegram_user_id, event_type,
                event_at, metadata_json, created_at
            ) VALUES ('LEGACY01', 1, 'application_created', 't', '{}', 't')
            """
        )
        connection.execute(
            """
            INSERT INTO notification_outbox (
                event_id, dedupe_key, telegram_user_id, event_type, snapshot_json,
                html, chunk_index, chunk_count, state, created_at, updated_at
            ) VALUES (
                'notice-1', 'notice-1', 1, 'application-status', ?, 'x',
                0, 1, 'FAILED', 't', 't'
            )
            """,
            (json.dumps({"application_id": "LEGACY01"}),),
        )
        connection.commit()
    return database


@pytest.mark.asyncio
async def test_cleanup_dry_run_is_read_only_and_execute_preserves_events(tmp_path):
    database = await _database_with_legacy_data(tmp_path)

    plan = build_cleanup_plan(str(database))
    assert plan["counts"]["submitted_applications"] == 1
    assert plan["counts"]["application_events_preserved"] == 1
    assert plan["google_sheets_changed"] is False

    result = execute_cleanup(str(database), plan["confirmation_token"])
    assert result["deleted"]["submitted_applications"] == 1
    assert result["deleted"]["bulk_batches"] == 1
    assert result["deleted"]["bulk_creation_requests"] == 1
    assert result["deleted"]["notification_outbox"] == 1

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM submitted_applications"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM application_events"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_cleanup_rejects_stale_confirmation_token(tmp_path):
    database = await _database_with_legacy_data(tmp_path)
    plan = build_cleanup_plan(str(database))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE bulk_creation_requests SET idempotency_key = 'request-2'"
        )
        connection.commit()

    with pytest.raises(LegacyCleanupError, match="confirmation token"):
        execute_cleanup(str(database), plan["confirmation_token"])


@pytest.mark.asyncio
async def test_cleanup_blocks_mixed_pending_notification(tmp_path):
    database = await _database_with_legacy_data(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE notification_outbox
            SET snapshot_json = ?
            WHERE event_id = 'notice-1'
            """,
            (
                json.dumps(
                    {
                        "applications": [
                            {"application_id": "LEGACY01"},
                            {"application_id": "CURRENT1"},
                        ]
                    }
                ),
            ),
        )
        connection.commit()

    plan = build_cleanup_plan(str(database))
    assert plan["blockers"] == ["mixed_notification_outbox:notice-1"]
    with pytest.raises(LegacyCleanupError, match="cleanup is blocked"):
        execute_cleanup(str(database), plan["confirmation_token"])
