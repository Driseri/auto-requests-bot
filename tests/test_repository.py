from __future__ import annotations

import json

import pytest
import aiosqlite

from app.models import (
    ApplicationStatus,
    BulkBatchLocationState,
    BulkRegistrationState,
    BulkReservationState,
    FieldName,
    Step,
)
from app.repository import DraftRepository


@pytest.mark.asyncio
async def test_drafts_for_different_users_do_not_mix(tmp_path):
    repository = DraftRepository(str(tmp_path / "drafts.db"))
    await repository.init()

    await repository.get_or_create(1)
    await repository.get_or_create(2)
    await repository.save_answer(1, FieldName.INTENT.value, "intent.one")
    await repository.save_answer(2, FieldName.INTENT.value, "intent.two")

    first = await repository.get_by_user_id(1)
    second = await repository.get_by_user_id(2)

    assert first is not None
    assert second is not None
    assert first.intent == "intent.one"
    assert second.intent == "intent.two"


@pytest.mark.asyncio
async def test_sqlite_state_persists_between_repository_instances(tmp_path):
    db_path = str(tmp_path / "persistent.db")
    first_repository = DraftRepository(db_path)
    await first_repository.init()
    await first_repository.get_or_create(3)
    await first_repository.save_answer(3, FieldName.INTENT.value, "intent.persisted")
    await first_repository.set_step(3, Step.SCRIPTWRITER)

    second_repository = DraftRepository(db_path)
    await second_repository.init()
    draft = await second_repository.get_by_user_id(3)

    assert draft is not None
    assert draft.intent == "intent.persisted"
    assert draft.current_step == Step.SCRIPTWRITER


@pytest.mark.asyncio
async def test_application_events_schema_and_indexes_are_created(tmp_path):
    db_path = str(tmp_path / "events_schema.db")
    repository = DraftRepository(db_path)
    await repository.init()

    async with aiosqlite.connect(db_path) as db:
        table_rows = await (
            await db.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name = 'application_events'
                """
            )
        ).fetchall()
        index_rows = await (
            await db.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'index' AND tbl_name = 'application_events'
                """
            )
        ).fetchall()

    assert table_rows == [("application_events",)]
    assert {
        "idx_application_events_application",
        "idx_application_events_type_time",
        "idx_application_events_user_time",
    } <= {row[0] for row in index_rows}


@pytest.mark.asyncio
async def test_record_application_event_stores_fields_without_application_id(tmp_path):
    repository = DraftRepository(str(tmp_path / "events_record.db"))
    await repository.init()

    await repository.record_application_event(
        event_type="draft_started",
        telegram_user_id=100,
        event_at="2026-07-06T10:00:00+00:00",
        old_value="old",
        new_value="new",
        metadata={"ключ": "значение", "count": 2},
    )

    events = await repository.list_application_events(event_type="draft_started")

    assert len(events) == 1
    event = events[0]
    assert event.application_id is None
    assert event.telegram_user_id == 100
    assert event.event_type == "draft_started"
    assert event.event_at == "2026-07-06T10:00:00+00:00"
    assert event.old_value == "old"
    assert event.new_value == "new"
    assert event.metadata_json is not None
    assert "\\u" not in event.metadata_json
    assert json.loads(event.metadata_json) == {"ключ": "значение", "count": 2}
    assert event.created_at


@pytest.mark.asyncio
async def test_list_application_events_filters_limits_and_sorts(tmp_path):
    repository = DraftRepository(str(tmp_path / "events_list.db"))
    await repository.init()

    await repository.record_application_event(
        application_id="APP-1",
        telegram_user_id=100,
        event_type="status_changed",
        event_at="2026-07-06T10:00:00+00:00",
        new_value="first",
    )
    await repository.record_application_event(
        application_id="APP-2",
        telegram_user_id=101,
        event_type="status_changed",
        event_at="2026-07-06T11:00:00+00:00",
        new_value="other-application",
    )
    await repository.record_application_event(
        application_id="APP-1",
        telegram_user_id=100,
        event_type="final_answer_added",
        event_at="2026-07-06T12:00:00+00:00",
        new_value="other-type",
    )
    await repository.record_application_event(
        application_id="APP-1",
        telegram_user_id=100,
        event_type="status_changed",
        event_at="2026-07-06T13:00:00+00:00",
        new_value="latest",
    )
    await repository.record_application_event(
        application_id="APP-1",
        telegram_user_id=100,
        event_type="status_changed",
        event_at="2026-07-06T13:00:00+00:00",
        new_value="latest-by-id",
    )

    by_application = await repository.list_application_events(application_id="APP-1")
    by_type = await repository.list_application_events(event_type="final_answer_added")
    limited = await repository.list_application_events(
        application_id="APP-1",
        event_type="status_changed",
        limit=2,
    )

    assert {event.application_id for event in by_application} == {"APP-1"}
    assert [event.event_type for event in by_type] == ["final_answer_added"]
    assert [event.new_value for event in limited] == ["latest-by-id", "latest"]


@pytest.mark.asyncio
async def test_complete_submission_records_application_submitted_event(tmp_path):
    repository = DraftRepository(str(tmp_path / "events_submission.db"))
    await repository.init()
    draft = await repository.get_or_create(100)

    await repository.complete_submission(
        100,
        application_id=draft.application_id or "APP-SUBMITTED",
        spreadsheet_id="spreadsheet",
        sheet_id=0,
        sheet_name="01.07",
        row_number=5,
        last_known_status=ApplicationStatus.NEW.value,
        direction="FL",
        answer_type="regular",
        application_type="single",
        change_type="ADD",
        is_urgent=False,
        submitted_at="2026-07-06T12:00:00+00:00",
    )

    events = await repository.list_application_events(
        application_id=draft.application_id,
        event_type="application_submitted",
    )

    assert len(events) == 1
    assert events[0].telegram_user_id == 100
    assert events[0].event_at == "2026-07-06T12:00:00+00:00"
    metadata = json.loads(events[0].metadata_json or "{}")
    assert metadata["sheet_id"] == 0
    assert metadata["row_number"] == 5
    assert metadata["direction"] == "FL"


@pytest.mark.asyncio
async def test_index_submitted_application_records_application_indexed_event(tmp_path):
    repository = DraftRepository(str(tmp_path / "events_indexed.db"))
    await repository.init()

    await repository.index_submitted_application(
        application_id="APP-INDEXED",
        telegram_user_id=100,
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        sheet_name="01.07",
        last_known_status=ApplicationStatus.NEW.value,
        direction="FL",
        answer_type="regular",
        application_type="single",
        change_type="EDIT",
        is_urgent=True,
        last_seen_row_number=9,
        submitted_at="2026-07-06T12:30:00+00:00",
    )

    events = await repository.list_application_events(
        application_id="APP-INDEXED",
        event_type="application_indexed",
    )

    assert len(events) == 1
    assert events[0].event_at == "2026-07-06T12:30:00+00:00"
    metadata = json.loads(events[0].metadata_json or "{}")
    assert metadata["row_number"] == 9
    assert metadata["change_type"] == "EDIT"
    assert metadata["is_urgent"] is True


@pytest.mark.asyncio
async def test_not_found_and_deletion_paths_record_application_events(tmp_path):
    repository = DraftRepository(str(tmp_path / "events_problems.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="APP-PROBLEM",
        telegram_user_id=100,
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        sheet_name="01.07",
        last_known_status=ApplicationStatus.NEW.value,
        last_seen_row_number=10,
    )

    await repository.mark_submitted_application_not_found(
        "APP-PROBLEM",
        threshold=2,
        recheck_seconds=3600,
    )
    await repository.record_application_deletion_error(
        "APP-PROBLEM",
        "section lock is busy",
    )
    await repository.complete_application_deletion(
        application_id="APP-PROBLEM",
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        deleted_row_number=10,
    )

    events = await repository.list_application_events(application_id="APP-PROBLEM")
    event_by_type = {event.event_type: event for event in events}

    assert event_by_type["application_not_found"].new_value == "1"
    not_found_metadata = json.loads(
        event_by_type["application_not_found"].metadata_json or "{}"
    )
    assert not_found_metadata["threshold"] == 2
    assert not_found_metadata["polling_state"] == "ACTIVE"
    assert event_by_type["application_deletion_error"].new_value == "section lock is busy"
    deletion_metadata = json.loads(
        event_by_type["application_deleted"].metadata_json or "{}"
    )
    assert deletion_metadata["deleted_row_number"] == 10
    assert await repository.get_submitted_application("APP-PROBLEM") is None


@pytest.mark.asyncio
async def test_notification_event_records_application_events_atomically(tmp_path):
    repository = DraftRepository(str(tmp_path / "events_notification.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="APP-NOTIFY",
        telegram_user_id=100,
        sheet_name="01.07",
        last_known_status=ApplicationStatus.NEW.value,
    )
    update = {
        "application_id": "APP-NOTIFY",
        "spreadsheet_id": "spreadsheet",
        "sheet_id": 123,
        "sheet_name": "01.07",
        "last_known_status": ApplicationStatus.ACCEPTED.value,
        "last_seen_row_number": 11,
    }
    event = {
        "event_type": "status_changed",
        "application_id": "APP-NOTIFY",
        "telegram_user_id": 100,
        "old_value": ApplicationStatus.NEW.value,
        "new_value": ApplicationStatus.ACCEPTED.value,
    }

    inserted = await repository.enqueue_notification_event(
        telegram_user_id=100,
        event_type="application-status",
        dedupe_key="application-status:APP-NOTIFY",
        snapshot_json="{}",
        chunks=["status"],
        application_updates=[update],
        application_events=[event],
    )
    duplicated = await repository.enqueue_notification_event(
        telegram_user_id=100,
        event_type="application-status",
        dedupe_key="application-status:APP-NOTIFY",
        snapshot_json="{}",
        chunks=["status"],
        application_updates=[update],
        application_events=[event],
    )

    events = await repository.list_application_events(application_id="APP-NOTIFY")
    tracked = await repository.get_submitted_application("APP-NOTIFY")

    assert inserted is True
    assert duplicated is False
    assert len(events) == 1
    assert events[0].event_type == "status_changed"
    assert tracked is not None
    assert tracked.last_known_status == ApplicationStatus.ACCEPTED.value


@pytest.mark.asyncio
async def test_update_application_tracking_batch_records_application_events(tmp_path):
    repository = DraftRepository(str(tmp_path / "events_tracking_batch.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="APP-BATCH-EVENT",
        telegram_user_id=100,
        sheet_name="01.07",
        last_known_status=ApplicationStatus.NEW.value,
    )

    await repository.update_application_tracking_batch(
        [
            {
                "application_id": "APP-BATCH-EVENT",
                "spreadsheet_id": "spreadsheet",
                "sheet_id": 123,
                "sheet_name": "01.07",
                "last_known_status": ApplicationStatus.ACCEPTED.value,
                "last_seen_row_number": 11,
            }
        ],
        application_events=[
            {
                "event_type": "status_changed",
                "application_id": "APP-BATCH-EVENT",
                "telegram_user_id": 100,
                "old_value": ApplicationStatus.NEW.value,
                "new_value": ApplicationStatus.ACCEPTED.value,
            }
        ],
    )

    events = await repository.list_application_events(
        application_id="APP-BATCH-EVENT",
        event_type="status_changed",
    )

    assert len(events) == 1
    assert events[0].old_value == ApplicationStatus.NEW.value
    assert events[0].new_value == ApplicationStatus.ACCEPTED.value


@pytest.mark.asyncio
async def test_bulk_reservation_migration_and_row_shift(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_reservations.db"))
    await repository.init()
    await repository.create_bulk_reservation(
        reservation_id="RES-1",
        idempotency_key="key-1",
        telegram_user_id=10,
    )
    await repository.complete_bulk_reservation_creation(
        "RES-1",
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        sheet_name="29.06 (1)",
        start_row=20,
        end_row=24,
        insert_url="https://example.test",
    )
    await repository.save_submitted_application(
        application_id="APP-BELOW",
        telegram_user_id=10,
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        sheet_name="29.06 (1)",
        last_known_status=ApplicationStatus.NEW.value,
        last_seen_row_number=30,
    )
    await repository.save_submitted_application(
        application_id="APP-ABOVE",
        telegram_user_id=10,
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        sheet_name="29.06 (1)",
        last_known_status=ApplicationStatus.NEW.value,
        last_seen_row_number=10,
    )

    await repository.shift_rows_after_insert(
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        from_row=15,
        delta=5,
    )

    reservation = await repository.get_bulk_reservation("RES-1")
    below = await repository.get_submitted_application("APP-BELOW")
    above = await repository.get_submitted_application("APP-ABOVE")
    assert reservation is not None
    assert reservation.state == BulkReservationState.CREATED.value
    assert reservation.start_row == 25
    assert reservation.end_row == 29
    assert below is not None
    assert below.last_seen_row_number == 35
    assert above is not None
    assert above.last_seen_row_number == 10


@pytest.mark.asyncio
async def test_failed_bulk_reservation_is_not_active(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_failed_not_active.db"))
    await repository.init()
    await repository.create_bulk_reservation(
        reservation_id="RES-FAILED",
        idempotency_key="key-failed",
        telegram_user_id=10,
    )
    await repository.fail_bulk_reservation("RES-FAILED", error="google failed")

    assert await repository.get_active_bulk_reservation(10) is None


@pytest.mark.asyncio
async def test_complete_bulk_reservation_creation_and_shift_is_atomic(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_creation_atomic.db"))
    await repository.init()
    await repository.create_bulk_reservation(
        reservation_id="RES-1",
        idempotency_key="key-1",
        telegram_user_id=10,
    )
    await repository.update_bulk_reservation_step(
        "RES-1",
        state=BulkReservationState.AWAITING_CONFIRMATION,
        direction="ФЛ",
        target_kind="rollout",
        change_type="ADD",
        requested_count=3,
    )
    await repository.save_submitted_application(
        application_id="APP-BELOW",
        telegram_user_id=10,
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        sheet_name="29.06 (1)",
        last_known_status=ApplicationStatus.NEW.value,
        last_seen_row_number=30,
    )

    await repository.complete_bulk_reservation_creation_and_shift(
        "RES-1",
        spreadsheet_id="spreadsheet",
        sheet_id=123,
        sheet_name="29.06 (1)",
        start_row=20,
        end_row=22,
        insert_url="https://example.test",
        shifted_rows=3,
    )

    reservation = await repository.get_bulk_reservation("RES-1")
    below = await repository.get_submitted_application("APP-BELOW")
    assert reservation is not None
    assert reservation.state == BulkReservationState.CREATED.value
    assert reservation.start_row == 20
    assert reservation.end_row == 22
    assert below is not None
    assert below.last_seen_row_number == 33


@pytest.mark.asyncio
async def test_bulk_section_lock_blocks_other_owner_until_released(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_section_lock.db"))
    await repository.init()

    first = await repository.acquire_bulk_section_lock(
        lock_key="spreadsheet:sheet:rollout:add",
        owner="RES-1",
        ttl_seconds=600,
    )
    second = await repository.acquire_bulk_section_lock(
        lock_key="spreadsheet:sheet:rollout:add",
        owner="RES-2",
        ttl_seconds=600,
    )
    await repository.release_bulk_section_lock(
        lock_key="spreadsheet:sheet:rollout:add",
        owner="RES-1",
    )
    third = await repository.acquire_bulk_section_lock(
        lock_key="spreadsheet:sheet:rollout:add",
        owner="RES-2",
        ttl_seconds=600,
    )

    assert first is True
    assert second is False
    assert third is True


@pytest.mark.asyncio
async def test_repository_migrates_old_database_without_formatting_column(tmp_path):
    db_path = str(tmp_path / "old.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE drafts (
                telegram_user_id INTEGER PRIMARY KEY,
                current_step TEXT NOT NULL,
                intent TEXT,
                scriptwriter TEXT,
                reason TEXT,
                raw_change_description TEXT,
                formatted_change_description TEXT,
                source_text TEXT,
                priority TEXT,
                llm_check_status TEXT NOT NULL DEFAULT 'not_checked',
                llm_score REAL,
                clarification_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            INSERT INTO drafts (
                telegram_user_id, current_step, source_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (5, Step.SOURCE_TEXT.value, "old source", "2026-05-18T13:00:00+00:00", "2026-05-18T13:00:00+00:00"),
        )
        await db.commit()

    repository = DraftRepository(db_path)
    await repository.init()
    draft = await repository.get_by_user_id(5)

    assert draft is not None
    assert draft.source_text == "old source"
    assert draft.source_text_formatting_json is None


@pytest.mark.asyncio
async def test_repository_migrates_old_database_without_application_id(tmp_path):
    db_path = str(tmp_path / "old_no_application_id.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE drafts (
                telegram_user_id INTEGER PRIMARY KEY,
                current_step TEXT NOT NULL,
                intent TEXT,
                scriptwriter TEXT,
                reason TEXT,
                raw_change_description TEXT,
                formatted_change_description TEXT,
                source_text TEXT,
                source_text_formatting_json TEXT,
                priority TEXT,
                llm_check_status TEXT NOT NULL DEFAULT 'not_checked',
                llm_score REAL,
                clarification_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            INSERT INTO drafts (
                telegram_user_id, current_step, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                6,
                Step.INTENT.value,
                "2026-05-18T13:00:00+00:00",
                "2026-05-18T13:00:00+00:00",
            ),
        )
        await db.commit()

    repository = DraftRepository(db_path)
    await repository.init()
    draft = await repository.get_by_user_id(6)
    updated = await repository.ensure_application_id(6)

    assert draft is not None
    assert draft.application_id is None
    assert updated.application_id is not None
    assert len(updated.application_id) == 8
    assert updated.application_id == updated.application_id.upper()


@pytest.mark.asyncio
async def test_repository_replaces_old_llm_rewrite_only_once_for_active_draft(tmp_path):
    db_path = str(tmp_path / "old_llm_rewrite.db")
    repository = DraftRepository(db_path)
    await repository.init()
    await repository.get_or_create(61)
    await repository.save_answer(61, "raw_change_description", "Исходный текст сценариста")
    await repository.save_llm_result(
        61,
        formatted_change_description="Переформулированный деловой текст",
        llm_check_status="complete",
        llm_score=0.95,
    )
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA user_version = 0")
        await db.commit()

    await repository.init()
    migrated = await repository.get_by_user_id(61)
    assert migrated is not None
    assert migrated.formatted_change_description == "Исходный текст сценариста"
    assert migrated.llm_score is None

    await repository.save_llm_result(
        61,
        formatted_change_description=(
            "Исходный текст сценариста\n\nУточнение сценариста: Новый ответ"
        ),
        llm_check_status="complete",
        llm_score=None,
        clarification_count=1,
    )
    await repository.init()
    after_restart = await repository.get_by_user_id(61)
    assert after_restart is not None
    assert after_restart.formatted_change_description.endswith("Новый ответ")


@pytest.mark.asyncio
async def test_new_draft_gets_application_id(tmp_path):
    repository = DraftRepository(str(tmp_path / "drafts.db"))
    await repository.init()

    draft = await repository.get_or_create(7)

    assert draft.application_id is not None
    assert len(draft.application_id) == 8
    assert draft.application_id == draft.application_id.upper()


@pytest.mark.asyncio
async def test_user_settings_do_not_mix_between_users(tmp_path):
    repository = DraftRepository(str(tmp_path / "settings.db"))
    await repository.init()

    await repository.save_user_setting(1, "default_intent", "intent.one")
    await repository.save_user_setting(2, "default_intent", "intent.two")
    await repository.save_user_setting(1, "default_scriptwriter", "Writer One")
    await repository.clear_user_setting(2, "default_intent")

    first = await repository.get_user_settings(1)
    second = await repository.get_user_settings(2)

    assert first.default_intent == "intent.one"
    assert first.default_scriptwriter == "Writer One"
    assert second.default_intent is None
    assert second.default_scriptwriter is None


@pytest.mark.asyncio
async def test_active_message_coordinates_are_saved_and_cleared_together(tmp_path):
    repository = DraftRepository(str(tmp_path / "active_message.db"))
    await repository.init()

    saved = await repository.set_active_message(1, chat_id=100, message_id=200)
    cleared = await repository.clear_active_message(1)

    assert saved.active_chat_id == 100
    assert saved.active_message_id == 200
    assert cleared.active_chat_id is None
    assert cleared.active_message_id is None


@pytest.mark.asyncio
async def test_repository_migrates_old_user_settings_for_active_message(tmp_path):
    db_path = str(tmp_path / "old_user_settings.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE user_settings (
                telegram_user_id INTEGER PRIMARY KEY,
                default_direction TEXT,
                default_intent TEXT,
                default_scriptwriter TEXT,
                pending_action TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            INSERT INTO user_settings (
                telegram_user_id, created_at, updated_at
            ) VALUES (1, '2026-06-01T10:00:00+00:00', '2026-06-01T10:00:00+00:00')
            """
        )
        await db.commit()

    repository = DraftRepository(db_path)
    await repository.init()
    settings = await repository.get_user_settings(1)

    assert settings.active_chat_id is None
    assert settings.active_message_id is None


@pytest.mark.asyncio
async def test_submitted_applications_are_saved_listed_and_updated(tmp_path):
    repository = DraftRepository(str(tmp_path / "submitted.db"))
    await repository.init()

    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        sheet_name="Высокий",
        last_known_status=ApplicationStatus.NEW.value,
        submitted_at="2026-06-15T10:30:00+00:00",
    )
    await repository.save_submitted_application(
        application_id="B1C2D3E4",
        telegram_user_id=200,
        sheet_name="Низкий",
        last_known_status=ApplicationStatus.NEW.value,
    )

    await repository.update_submitted_application_status(
        "A1B2C3D4",
        sheet_name="Высокий",
        last_known_status=ApplicationStatus.ACCEPTED.value,
        last_seen_row_number=5,
        last_seen_editor="редактор 1",
        last_seen_editor_comment="Можно использовать",
    )

    first = await repository.get_submitted_application("A1B2C3D4")
    tracked = await repository.list_submitted_applications()

    assert first is not None
    assert first.telegram_user_id == 100
    assert first.last_known_status == ApplicationStatus.ACCEPTED.value
    assert first.last_seen_row_number == 5
    assert first.last_seen_editor == "редактор 1"
    assert first.last_seen_editor_comment == "Можно использовать"
    assert first.last_seen_scriptwriter_response is None
    assert first.pending_editor_comment is None
    assert first.pending_editor_comment_seen_count == 0
    assert first.pending_scriptwriter_response is None
    assert first.pending_scriptwriter_response_seen_count == 0
    assert first.submitted_at == "2026-06-15T10:30:00+00:00"
    assert first.polling_state == "ACTIVE"
    assert first.not_found_count == 0
    assert {item.application_id for item in tracked} == {"A1B2C3D4", "B1C2D3E4"}


@pytest.mark.asyncio
async def test_submitted_application_not_found_is_deferred_and_reset(tmp_path):
    repository = DraftRepository(str(tmp_path / "submitted_not_found.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        sheet_name="Высокий",
        last_known_status=ApplicationStatus.NEW.value,
    )

    await repository.mark_submitted_application_not_found(
        "A1B2C3D4",
        threshold=2,
        recheck_seconds=3600,
    )
    first = await repository.get_submitted_application("A1B2C3D4")
    assert first is not None
    assert first.polling_state == "ACTIVE"
    assert first.not_found_count == 1
    assert first.next_status_check_at is None

    await repository.mark_submitted_application_not_found(
        "A1B2C3D4",
        threshold=2,
        recheck_seconds=3600,
    )
    deferred = await repository.get_submitted_application("A1B2C3D4")
    listed = await repository.list_submitted_applications()
    listed_with_deferred = await repository.list_submitted_applications(
        include_deferred=True
    )

    assert deferred is not None
    assert deferred.polling_state == "NOT_FOUND"
    assert deferred.not_found_count == 2
    assert deferred.next_status_check_at is not None
    assert listed == []
    assert [item.application_id for item in listed_with_deferred] == ["A1B2C3D4"]

    await repository.update_submitted_application_status(
        "A1B2C3D4",
        sheet_name="Высокий",
        last_known_status=ApplicationStatus.ACCEPTED.value,
        last_seen_row_number=5,
    )
    reset = await repository.get_submitted_application("A1B2C3D4")
    listed_after_reset = await repository.list_submitted_applications()

    assert reset is not None
    assert reset.polling_state == "ACTIVE"
    assert reset.not_found_count == 0
    assert reset.next_status_check_at is None
    assert [item.application_id for item in listed_after_reset] == ["A1B2C3D4"]


@pytest.mark.asyncio
async def test_repository_migration_adds_submitted_applications_table(tmp_path):
    db_path = str(tmp_path / "old_without_submitted.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE drafts (
                telegram_user_id INTEGER PRIMARY KEY,
                current_step TEXT NOT NULL,
                application_id TEXT,
                intent TEXT,
                scriptwriter TEXT,
                reason TEXT,
                raw_change_description TEXT,
                formatted_change_description TEXT,
                source_text TEXT,
                source_text_formatting_json TEXT,
                priority TEXT,
                llm_check_status TEXT NOT NULL DEFAULT 'not_checked',
                llm_score REAL,
                clarification_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.commit()

    repository = DraftRepository(db_path)
    await repository.init()

    assert await repository.list_submitted_applications() == []
    async with aiosqlite.connect(db_path) as db:
        draft_columns = {
            row[1] for row in await (await db.execute("PRAGMA table_info(drafts)")).fetchall()
        }
        tracking_columns = {
            row[1]
            for row in await (
                await db.execute("PRAGMA table_info(submitted_applications)")
            ).fetchall()
        }
    assert {
        "chip_text_before",
        "chip_text_before_formatting_json",
        "chip_text",
        "chip_text_formatting_json",
        "chip_text_after",
        "chip_text_after_formatting_json",
    } <= draft_columns
    assert "change_type" in tracking_columns


@pytest.mark.asyncio
async def test_repository_restores_bulk_batch_and_child_coordinates(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk-location.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id="sheet-1",
        direction="FL",
        sheet_name="Old name",
        sheet_id=300,
        start_row=10,
        data_start_row=12,
        reserved_rows=100,
        data_end_row=16,
    )
    await repository.create_bulk_creation_request(
        idempotency_key="request-1",
        telegram_user_id=100,
        batch_id="BATCH-ABC12345",
    )
    await repository.complete_bulk_creation(
        "request-1",
        insert_url="https://docs.google.com/old-range",
    )
    for application_id, row_number in (("A1B2C3D4", 12), ("B1C2D3E4", 16)):
        await repository.save_submitted_application(
            application_id=application_id,
            telegram_user_id=100,
            spreadsheet_id="sheet-1",
            sheet_id=300,
            sheet_name="Old name",
            last_known_status=ApplicationStatus.NEW.value,
            batch_id="BATCH-ABC12345",
            last_seen_row_number=row_number,
        )
        await repository.mark_submitted_application_not_found(
            application_id,
            threshold=1,
            recheck_seconds=3600,
        )
    await repository.record_bulk_batch_location_problem(
        "BATCH-ABC12345",
        state=BulkBatchLocationState.MISSING.value,
        error="missing",
        recheck_seconds=3600,
    )

    restored = await repository.restore_bulk_batch_location(
        "BATCH-ABC12345",
        spreadsheet_id="sheet-1",
        sheet_name="Renamed",
        sheet_id=300,
        start_row=30,
    )

    assert restored is not None
    assert restored.start_row == 30
    assert restored.data_start_row == 32
    assert restored.data_end_row == 36
    assert restored.location_state == BulkBatchLocationState.KNOWN.value
    tracked = await repository.list_submitted_applications(include_deferred=True)
    assert [item.last_seen_row_number for item in tracked] == [32, 36]
    assert all(item.sheet_name == "Renamed" for item in tracked)
    assert all(item.not_found_count == 0 for item in tracked)
    assert all(item.polling_state == "ACTIVE" for item in tracked)
    creation_request = await repository.get_bulk_creation_request("request-1")
    assert creation_request is not None
    assert creation_request.insert_url == (
        "https://docs.google.com/spreadsheets/d/sheet-1/edit#gid=300&range=A32:G32"
    )


@pytest.mark.asyncio
async def test_repository_migrates_bulk_location_columns(tmp_path):
    db_path = str(tmp_path / "old-bulk.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE bulk_batches (
                batch_id TEXT PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL,
                sheet_name TEXT NOT NULL,
                sheet_id INTEGER NOT NULL,
                start_row INTEGER NOT NULL,
                data_start_row INTEGER NOT NULL,
                reserved_rows INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.commit()

    repository = DraftRepository(db_path)
    await repository.init()

    async with aiosqlite.connect(db_path) as db:
        columns = {
            row[1]
            for row in await (await db.execute("PRAGMA table_info(bulk_batches)")).fetchall()
        }
    assert {
        "location_state",
        "location_miss_count",
        "last_location_search_at",
        "next_location_search_at",
        "last_location_error",
    } <= columns


@pytest.mark.asyncio
async def test_repository_migrates_submitted_at_column(tmp_path):
    db_path = str(tmp_path / "old_submitted.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE submitted_applications (
                application_id TEXT PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL,
                spreadsheet_id TEXT,
                sheet_id INTEGER,
                sheet_name TEXT NOT NULL,
                last_known_status TEXT NOT NULL,
                direction TEXT,
                answer_type TEXT,
                application_type TEXT,
                is_urgent INTEGER,
                batch_id TEXT,
                last_seen_row_number INTEGER,
                last_seen_editor TEXT,
                last_seen_editor_comment TEXT,
                last_seen_final_answer TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            INSERT INTO submitted_applications (
                application_id, telegram_user_id, sheet_name, last_known_status,
                created_at, updated_at
            ) VALUES (
                'A1B2C3D4', 100, '01.06', 'Новая',
                '2026-06-01T10:00:00+00:00', '2026-06-01T10:00:00+00:00'
            )
            """
        )
        await db.commit()

    repository = DraftRepository(db_path)
    await repository.init()
    tracked = await repository.get_submitted_application("A1B2C3D4")

    assert tracked is not None
    assert tracked.submitted_at is None


@pytest.mark.asyncio
async def test_repository_migrates_existing_bulk_batches_to_status_schema_v1(tmp_path):
    db_path = str(tmp_path / "old_bulk_batches.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE bulk_batches (
                batch_id TEXT PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL,
                spreadsheet_id TEXT,
                direction TEXT,
                sheet_name TEXT NOT NULL,
                sheet_id INTEGER NOT NULL,
                start_row INTEGER NOT NULL,
                data_start_row INTEGER NOT NULL,
                reserved_rows INTEGER NOT NULL,
                batch_status TEXT,
                last_known_batch_status TEXT,
                last_seen_final_answers_digest_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            INSERT INTO bulk_batches (
                batch_id, telegram_user_id, spreadsheet_id, direction, sheet_name,
                sheet_id, start_row, data_start_row, reserved_rows, batch_status,
                last_known_batch_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "BATCH-OLD",
                100,
                "spreadsheet",
                "ФЛ",
                "Массовый ввод",
                10,
                1,
                3,
                5,
                "Нужны пояснения",
                "Новая пачка",
                "2026-06-01T10:00:00+00:00",
                "2026-06-01T10:00:00+00:00",
            ),
        )
        await db.commit()

    repository = DraftRepository(db_path)
    await repository.init()
    batch = await repository.get_bulk_batch("BATCH-OLD")

    assert batch is not None
    assert batch.status_schema_version == 1
    assert batch.batch_status == "Нужны пояснения"
    assert batch.data_end_row == 7
    assert batch.registration_state == BulkRegistrationState.DRAFT.value
    assert batch.registered_count == 0


@pytest.mark.asyncio
async def test_notification_outbox_claims_chunks_in_insert_order(tmp_path):
    repository = DraftRepository(str(tmp_path / "outbox_order.db"))
    await repository.init()
    await repository.enqueue_notification_event(
        telegram_user_id=100,
        event_type="status",
        dedupe_key="event-one",
        snapshot_json="{}",
        chunks=["first", "second"],
    )

    first = await repository.claim_next_notification(stale_after_seconds=300)
    assert first is not None
    assert first.html == "first"
    assert await repository.claim_next_notification(stale_after_seconds=300) is None

    await repository.complete_notification(first.event_id, telegram_message_id=10)
    second = await repository.claim_next_notification(stale_after_seconds=300)
    assert second is not None
    assert second.html == "second"


@pytest.mark.asyncio
async def test_notification_outbox_moves_to_failed_after_max_attempts(tmp_path):
    repository = DraftRepository(str(tmp_path / "outbox_failed.db"))
    await repository.init()
    await repository.enqueue_notification_event(
        telegram_user_id=100,
        event_type="status",
        dedupe_key="event-failed",
        snapshot_json="{}",
        chunks=["message"],
    )
    item = await repository.claim_next_notification(stale_after_seconds=300)
    assert item is not None

    for _ in range(10):
        await repository.fail_notification(
            item.event_id,
            error="telegram unavailable",
            max_attempts=10,
            retry_base_seconds=30,
        )

    saved = (await repository.list_notification_outbox())[0]
    assert saved.state == "FAILED"
    assert saved.attempts == 10


@pytest.mark.asyncio
async def test_dashboard_outbox_coalesces_latest_projection_and_retries(tmp_path):
    repository = DraftRepository(str(tmp_path / "dashboard_outbox.db"))
    await repository.init()
    await repository.upsert_dashboard_projection(
        entity_type="APPLICATION",
        entity_id="A1B2C3D4",
        snapshot={"row": ["A1B2C3D4", "", "", "", "", "", "", "", "Новая"]},
    )
    await repository.upsert_dashboard_projection(
        entity_type="APPLICATION",
        entity_id="A1B2C3D4",
        snapshot={"row": ["A1B2C3D4", "", "", "", "", "", "", "", "В работе"]},
    )

    saved = await repository.list_dashboard_outbox()
    assert len(saved) == 1
    assert json.loads(saved[0].snapshot_json)["row"][8] == "В работе"

    claimed = await repository.claim_dashboard_projections(
        stale_after_seconds=300
    )
    assert len(claimed) == 1
    await repository.fail_dashboard_projections(
        claimed,
        error="dashboard unavailable",
        retry_base_seconds=60,
        retry_max_seconds=3600,
    )

    failed = (await repository.list_dashboard_outbox())[0]
    assert failed.state == "PENDING"
    assert failed.attempts == 1
    assert failed.last_error == "dashboard unavailable"
