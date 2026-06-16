from __future__ import annotations

import json

import pytest
import aiosqlite

from app.models import ApplicationStatus, BulkRegistrationState, FieldName, Step
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
