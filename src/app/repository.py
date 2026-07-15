from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from app.models import (
    ApplicationEvent,
    BulkCreationRequest,
    BulkCreationState,
    BulkBatch,
    BulkBatchLocationState,
    BulkBatchStatus,
    BulkRegistrationState,
    BulkReservation,
    BulkReservationState,
    DashboardOutboxItem,
    DashboardOutboxState,
    Draft,
    NotificationOutboxItem,
    NotificationOutboxState,
    StatusPollingState,
    Step,
    SubmissionState,
    SubmittedApplication,
    TEXT_FIELDS,
    UserSettings,
    generate_application_id,
    generate_batch_id,
    utc_now_iso,
)


class DraftRepository:
    """Единая точка доступа к черновикам, tracking и массовым заявкам в SQLite."""

    def __init__(self, sqlite_path: str) -> None:
        self.sqlite_path = sqlite_path

    async def init(self) -> None:
        """Подготовить SQLite, выполнить совместимые миграции и создать индексы."""
        db_path = Path(self.sqlite_path)
        if db_path.parent and str(db_path.parent) != ".":
            db_path.parent.mkdir(parents=True, exist_ok=True)

        async with self._connection() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS drafts (
                    telegram_user_id INTEGER PRIMARY KEY,
                    current_step TEXT NOT NULL,
                    application_id TEXT,
                    direction TEXT,
                    answer_type TEXT,
                    is_urgent INTEGER,
                    application_type TEXT,
                    change_type TEXT,
                    author_name TEXT,
                    intent TEXT,
                    scriptwriter TEXT,
                    reason TEXT,
                    raw_change_description TEXT,
                    formatted_change_description TEXT,
                    source_text TEXT,
                    source_text_formatting_json TEXT,
                    chip_text_before TEXT,
                    chip_text_before_formatting_json TEXT,
                    chip_text TEXT,
                    chip_text_formatting_json TEXT,
                    chip_after_text_action TEXT,
                    chip_text_after TEXT,
                    chip_text_after_formatting_json TEXT,
                    priority TEXT,
                    llm_check_status TEXT NOT NULL DEFAULT 'not_checked',
                    llm_score REAL,
                    clarification_count INTEGER NOT NULL DEFAULT 0,
                    submission_state TEXT NOT NULL DEFAULT 'DRAFT',
                    submission_started_at TEXT,
                    submission_spreadsheet_id TEXT,
                    submission_sheet_name TEXT,
                    submission_sheet_id INTEGER,
                    submission_row_number INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await self._ensure_column(db, "source_text_formatting_json", "TEXT")
            await self._ensure_column(db, "chip_text_before", "TEXT")
            await self._ensure_column(db, "chip_text_before_formatting_json", "TEXT")
            await self._ensure_column(db, "chip_text", "TEXT")
            await self._ensure_column(db, "chip_text_formatting_json", "TEXT")
            await self._ensure_column(db, "chip_after_text_action", "TEXT")
            await self._ensure_column(db, "chip_text_after", "TEXT")
            await self._ensure_column(db, "chip_text_after_formatting_json", "TEXT")
            await self._ensure_column(db, "application_id", "TEXT")
            await self._ensure_column(db, "direction", "TEXT")
            await self._ensure_column(db, "answer_type", "TEXT")
            await self._ensure_column(db, "is_urgent", "INTEGER")
            await self._ensure_column(db, "application_type", "TEXT")
            await self._ensure_column(db, "change_type", "TEXT")
            await self._ensure_column(db, "author_name", "TEXT")
            await self._ensure_column(
                db, "submission_state", "TEXT NOT NULL DEFAULT 'DRAFT'"
            )
            await self._ensure_column(db, "submission_started_at", "TEXT")
            await self._ensure_column(db, "submission_spreadsheet_id", "TEXT")
            await self._ensure_column(db, "submission_sheet_name", "TEXT")
            await self._ensure_column(db, "submission_sheet_id", "INTEGER")
            await self._ensure_column(db, "submission_row_number", "INTEGER")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_settings (
                    telegram_user_id INTEGER PRIMARY KEY,
                    default_direction TEXT,
                    default_intent TEXT,
                    default_scriptwriter TEXT,
                    pending_action TEXT,
                    active_chat_id INTEGER,
                    active_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS submitted_applications (
                    application_id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    spreadsheet_id TEXT,
                    sheet_id INTEGER,
                    sheet_name TEXT NOT NULL,
                    last_known_status TEXT NOT NULL,
                    direction TEXT,
                    answer_type TEXT,
                    application_type TEXT,
                    change_type TEXT,
                    is_urgent INTEGER,
                    batch_id TEXT,
                    last_seen_row_number INTEGER,
                    last_seen_editor TEXT,
                    last_seen_editor_comment TEXT,
                    last_seen_final_answer TEXT,
                    last_seen_scriptwriter_response TEXT,
                    pending_editor_comment TEXT,
                    pending_editor_comment_seen_count INTEGER NOT NULL DEFAULT 0,
                    pending_scriptwriter_response TEXT,
                    pending_scriptwriter_response_seen_count INTEGER NOT NULL DEFAULT 0,
                    submitted_at TEXT,
                    polling_state TEXT NOT NULL DEFAULT 'ACTIVE',
                    not_found_count INTEGER NOT NULL DEFAULT 0,
                    last_not_found_at TEXT,
                    next_status_check_at TEXT,
                    deletion_seen_count INTEGER NOT NULL DEFAULT 0,
                    deletion_last_seen_at TEXT,
                    deletion_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await self._ensure_submitted_applications_column(db, "batch_id", "TEXT")
            await self._ensure_submitted_applications_column(db, "spreadsheet_id", "TEXT")
            await self._ensure_submitted_applications_column(db, "sheet_id", "INTEGER")
            await self._ensure_submitted_applications_column(db, "direction", "TEXT")
            await self._ensure_submitted_applications_column(db, "answer_type", "TEXT")
            await self._ensure_submitted_applications_column(db, "application_type", "TEXT")
            await self._ensure_submitted_applications_column(db, "change_type", "TEXT")
            await self._ensure_submitted_applications_column(db, "is_urgent", "INTEGER")
            await self._ensure_submitted_applications_column(db, "last_seen_final_answer", "TEXT")
            await self._ensure_submitted_applications_column(
                db,
                "last_seen_scriptwriter_response",
                "TEXT",
            )
            await self._ensure_submitted_applications_column(db, "pending_editor_comment", "TEXT")
            await self._ensure_submitted_applications_column(
                db,
                "pending_editor_comment_seen_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            await self._ensure_submitted_applications_column(
                db,
                "pending_scriptwriter_response",
                "TEXT",
            )
            await self._ensure_submitted_applications_column(
                db,
                "pending_scriptwriter_response_seen_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            await self._ensure_submitted_applications_column(db, "last_seen_editor", "TEXT")
            await self._ensure_submitted_applications_column(db, "submitted_at", "TEXT")
            await self._ensure_submitted_applications_column(
                db,
                "polling_state",
                "TEXT NOT NULL DEFAULT 'ACTIVE'",
            )
            await self._ensure_submitted_applications_column(
                db,
                "not_found_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            await self._ensure_submitted_applications_column(db, "last_not_found_at", "TEXT")
            await self._ensure_submitted_applications_column(db, "next_status_check_at", "TEXT")
            await self._ensure_submitted_applications_column(
                db,
                "deletion_seen_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            await self._ensure_submitted_applications_column(db, "deletion_last_seen_at", "TEXT")
            await self._ensure_submitted_applications_column(db, "deletion_error", "TEXT")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS bulk_batches (
                    batch_id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    spreadsheet_id TEXT,
                    direction TEXT,
                    sheet_name TEXT NOT NULL,
                    sheet_id INTEGER NOT NULL,
                    start_row INTEGER NOT NULL,
                    data_start_row INTEGER NOT NULL,
                    reserved_rows INTEGER NOT NULL,
                    data_end_row INTEGER,
                    registration_state TEXT NOT NULL DEFAULT 'DRAFT',
                    registration_started_at TEXT,
                    registered_count INTEGER NOT NULL DEFAULT 0,
                    status_schema_version INTEGER NOT NULL DEFAULT 1,
                    batch_status TEXT,
                    last_known_batch_status TEXT,
                    last_seen_final_answers_digest_at TEXT,
                    location_state TEXT NOT NULL DEFAULT 'KNOWN',
                    location_miss_count INTEGER NOT NULL DEFAULT 0,
                    last_location_search_at TEXT,
                    next_location_search_at TEXT,
                    last_location_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    event_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    telegram_user_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    html TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    sending_started_at TEXT,
                    last_error TEXT,
                    telegram_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS bulk_creation_requests (
                    idempotency_key TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    direction TEXT,
                    state TEXT NOT NULL,
                    batch_id TEXT NOT NULL UNIQUE,
                    insert_url TEXT,
                    last_error TEXT,
                    started_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS bulk_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    telegram_user_id INTEGER NOT NULL,
                    direction TEXT,
                    target_kind TEXT,
                    change_type TEXT,
                    requested_count INTEGER,
                    spreadsheet_id TEXT,
                    sheet_id INTEGER,
                    sheet_name TEXT,
                    start_row INTEGER,
                    end_row INTEGER,
                    insert_url TEXT,
                    state TEXT NOT NULL,
                    registered_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    started_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    registered_at TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS bulk_section_locks (
                    lock_key TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS dashboard_outbox (
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    sending_started_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (entity_type, entity_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS application_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id TEXT,
                    telegram_user_id INTEGER,
                    event_type TEXT NOT NULL,
                    event_at TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            await self._ensure_bulk_batches_column(db, "spreadsheet_id", "TEXT")
            await self._ensure_bulk_batches_column(db, "direction", "TEXT")
            await self._ensure_bulk_batches_column(db, "batch_status", "TEXT")
            await self._ensure_bulk_batches_column(db, "last_known_batch_status", "TEXT")
            await self._ensure_bulk_batches_column(db, "last_seen_final_answers_digest_at", "TEXT")
            await self._ensure_bulk_batches_column(
                db,
                "location_state",
                "TEXT NOT NULL DEFAULT 'KNOWN'",
            )
            await self._ensure_bulk_batches_column(
                db,
                "location_miss_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            await self._ensure_bulk_batches_column(db, "last_location_search_at", "TEXT")
            await self._ensure_bulk_batches_column(db, "next_location_search_at", "TEXT")
            await self._ensure_bulk_batches_column(db, "last_location_error", "TEXT")
            await self._ensure_bulk_batches_column(
                db,
                "status_schema_version",
                "INTEGER NOT NULL DEFAULT 1",
            )
            await self._ensure_bulk_batches_column(db, "data_end_row", "INTEGER")
            registration_state_added = await self._ensure_bulk_batches_column(
                db,
                "registration_state",
                "TEXT NOT NULL DEFAULT 'DRAFT'",
            )
            await self._ensure_bulk_batches_column(db, "registration_started_at", "TEXT")
            await self._ensure_bulk_batches_column(
                db,
                "registered_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            await db.execute(
                """
                UPDATE bulk_batches
                SET data_end_row = data_start_row + MAX(reserved_rows, 1) - 1
                WHERE data_end_row IS NULL
                """
            )
            if registration_state_added:
                await db.execute(
                    """
                    UPDATE bulk_batches
                    SET registration_state = 'REGISTERED',
                        registered_count = (
                            SELECT COUNT(*)
                            FROM submitted_applications
                            WHERE submitted_applications.batch_id = bulk_batches.batch_id
                        ),
                        data_end_row = COALESCE(
                            (
                                SELECT MAX(last_seen_row_number)
                                FROM submitted_applications
                                WHERE submitted_applications.batch_id = bulk_batches.batch_id
                            ),
                            data_end_row
                        )
                    WHERE EXISTS (
                        SELECT 1
                        FROM submitted_applications
                        WHERE submitted_applications.batch_id = bulk_batches.batch_id
                    )
                    """
                )
            await self._ensure_user_settings_column(db, "pending_action", "TEXT")
            await self._ensure_user_settings_column(db, "default_direction", "TEXT")
            await self._ensure_user_settings_column(db, "active_chat_id", "INTEGER")
            await self._ensure_user_settings_column(db, "active_message_id", "INTEGER")
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_submitted_applications_batch_id
                ON submitted_applications(batch_id)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_submitted_applications_user_id
                ON submitted_applications(telegram_user_id)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_submitted_applications_sheet
                ON submitted_applications(spreadsheet_id, sheet_name)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bulk_batches_active
                ON bulk_batches(registration_state, last_known_batch_status)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_notification_outbox_delivery
                ON notification_outbox(state, next_attempt_at, created_at)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_notification_outbox_user
                ON notification_outbox(telegram_user_id, created_at, chunk_index)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bulk_creation_user
                ON bulk_creation_requests(telegram_user_id, created_at)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dashboard_outbox_delivery
                ON dashboard_outbox(state, next_attempt_at, updated_at)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_application_events_application
                ON application_events(application_id, event_type, event_at)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_application_events_type_time
                ON application_events(event_type, event_at)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_application_events_user_time
                ON application_events(telegram_user_id, event_at)
                """
            )
            await self._migrate_llm_completeness_check(db)
            await db.commit()

    async def get_or_create(self, telegram_user_id: int) -> Draft:
        """Вернуть черновик пользователя или создать новый с application_id."""
        draft = await self.get_by_user_id(telegram_user_id)
        if draft is not None:
            return draft

        now = utc_now_iso()
        draft = Draft(
            telegram_user_id=telegram_user_id,
            current_step=Step.DIRECTION,
            application_id=generate_application_id(),
            created_at=now,
            updated_at=now,
        )
        async with self._connection() as db:
            await db.execute(
                """
                INSERT INTO drafts (
                    telegram_user_id, current_step, application_id, application_type,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_user_id,
                    Step.DIRECTION.value,
                    draft.application_id,
                    draft.application_type,
                    now,
                    now,
                ),
            )
            await self._record_application_event_in_connection(
                db,
                event_type="draft_started",
                application_id=draft.application_id,
                telegram_user_id=telegram_user_id,
                event_at=now,
                metadata={
                    "current_step": Step.DIRECTION.value,
                    "application_type": draft.application_type,
                },
                created_at=now,
            )
            await db.commit()
        return draft

    async def get_by_user_id(self, telegram_user_id: int) -> Draft | None:
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM drafts WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()

        if row is None:
            return None
        return self._draft_from_row(row)

    async def ensure_application_id(self, telegram_user_id: int) -> Draft:
        draft = await self.get_by_user_id(telegram_user_id)
        if draft is None:
            raise LookupError(f"Draft not found for user {telegram_user_id}")
        if draft.application_id:
            return draft

        await self._update_fields(telegram_user_id, {"application_id": generate_application_id()})
        updated = await self.get_by_user_id(telegram_user_id)
        if updated is None:
            raise LookupError(f"Draft not found for user {telegram_user_id}")
        return updated

    async def save_answer(self, telegram_user_id: int, field: str, value: Any) -> Draft:
        if field not in TEXT_FIELDS and field not in {"priority", "is_urgent"}:
            raise ValueError(f"Unsupported draft field: {field}")

        if field == "is_urgent" and isinstance(value, bool):
            value = 1 if value else 0
        await self._update_fields(telegram_user_id, {field: value})
        draft = await self.get_by_user_id(telegram_user_id)
        if draft is None:
            raise LookupError(f"Draft not found for user {telegram_user_id}")
        return draft

    async def save_llm_result(
        self,
        telegram_user_id: int,
        *,
        formatted_change_description: str | None,
        llm_check_status: str,
        llm_score: float | None,
        raw_change_description: str | None = None,
        clarification_count: int | None = None,
    ) -> Draft:
        values: dict[str, Any] = {
            "formatted_change_description": formatted_change_description,
            "llm_check_status": llm_check_status,
            "llm_score": llm_score,
        }
        if raw_change_description is not None:
            values["raw_change_description"] = raw_change_description
        if clarification_count is not None:
            values["clarification_count"] = clarification_count
        await self._update_fields(telegram_user_id, values)
        draft = await self.get_by_user_id(telegram_user_id)
        if draft is None:
            raise LookupError(f"Draft not found for user {telegram_user_id}")
        return draft

    async def set_step(self, telegram_user_id: int, step: Step) -> Draft:
        await self._update_fields(telegram_user_id, {"current_step": step.value})
        draft = await self.get_by_user_id(telegram_user_id)
        if draft is None:
            raise LookupError(f"Draft not found for user {telegram_user_id}")
        return draft

    async def delete(self, telegram_user_id: int) -> None:
        async with self._connection() as db:
            await db.execute("DELETE FROM drafts WHERE telegram_user_id = ?", (telegram_user_id,))
            await db.commit()

    async def complete(self, telegram_user_id: int) -> Draft:
        return await self.set_step(telegram_user_id, Step.COMPLETED)

    async def begin_submission(
        self,
        telegram_user_id: int,
        *,
        spreadsheet_id: str,
        sheet_name: str,
        stale_after_seconds: int = 600,
    ) -> Draft:
        """Атомарно зафиксировать маршрут и перевести одиночную заявку в PENDING."""
        now = datetime.now(timezone.utc)
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM drafts WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                raise LookupError(f"Draft not found for user {telegram_user_id}")
            current = self._draft_from_row(row)
            if current.submission_state == SubmissionState.SENT.value:
                await db.commit()
                return current
            started = _parse_iso_datetime(current.submission_started_at)
            active_pending = (
                current.submission_state == SubmissionState.PENDING.value
                and started is not None
                and now - started < timedelta(seconds=stale_after_seconds)
            )
            if not active_pending:
                await db.execute(
                    """
                    UPDATE drafts
                    SET submission_state = ?,
                        submission_started_at = ?,
                        submission_spreadsheet_id = COALESCE(submission_spreadsheet_id, ?),
                        submission_sheet_name = COALESCE(submission_sheet_name, ?),
                        updated_at = ?
                    WHERE telegram_user_id = ?
                    """,
                    (
                        SubmissionState.PENDING.value,
                        now.isoformat(),
                        spreadsheet_id,
                        sheet_name,
                        now.isoformat(),
                        telegram_user_id,
                    ),
                )
            await db.commit()
        updated = await self.get_by_user_id(telegram_user_id)
        if updated is None:
            raise LookupError(f"Draft not found for user {telegram_user_id}")
        return updated

    async def fail_submission(self, telegram_user_id: int) -> None:
        await self._update_fields(
            telegram_user_id,
            {
                "submission_state": SubmissionState.FAILED.value,
                "submission_started_at": None,
            },
        )

    async def complete_submission(
        self,
        telegram_user_id: int,
        *,
        application_id: str,
        spreadsheet_id: str | None,
        sheet_id: int | None,
        sheet_name: str,
        row_number: int | None,
        last_known_status: str,
        direction: str | None,
        answer_type: str | None,
        application_type: str | None,
        is_urgent: bool | None,
        change_type: str | None = None,
        submitted_at: str | None = None,
        dashboard_projection: dict[str, Any] | None = None,
        notification_event: dict[str, Any] | None = None,
    ) -> Draft:
        """Одной транзакцией сохранить tracking и отметить черновик отправленным."""
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO submitted_applications (
                    application_id, telegram_user_id, spreadsheet_id, sheet_id, sheet_name,
                    last_known_status, direction, answer_type, application_type, change_type, is_urgent,
                    last_seen_row_number, submitted_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(application_id) DO UPDATE SET
                    spreadsheet_id = excluded.spreadsheet_id,
                    sheet_id = excluded.sheet_id,
                    sheet_name = excluded.sheet_name,
                    change_type = excluded.change_type,
                    submitted_at = COALESCE(
                        submitted_applications.submitted_at,
                        excluded.submitted_at
                    ),
                    last_seen_row_number = COALESCE(
                        excluded.last_seen_row_number,
                        submitted_applications.last_seen_row_number
                    ),
                    polling_state = ?,
                    not_found_count = 0,
                    last_not_found_at = NULL,
                    next_status_check_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    application_id,
                    telegram_user_id,
                    spreadsheet_id,
                    sheet_id,
                    sheet_name,
                    last_known_status,
                    direction,
                    answer_type,
                    application_type,
                    change_type,
                    1 if is_urgent else 0 if is_urgent is not None else None,
                    row_number,
                    submitted_at,
                    now,
                    now,
                    StatusPollingState.ACTIVE.value,
                ),
            )
            await db.execute(
                """
                UPDATE drafts
                SET submission_state = ?,
                    submission_started_at = NULL,
                    submission_spreadsheet_id = ?,
                    submission_sheet_name = ?,
                    submission_sheet_id = ?,
                    submission_row_number = ?,
                    current_step = ?,
                    updated_at = ?
                WHERE telegram_user_id = ?
                """,
                (
                    SubmissionState.SENT.value,
                    spreadsheet_id,
                    sheet_name,
                    sheet_id,
                    row_number,
                    Step.COMPLETED.value,
                    now,
                    telegram_user_id,
                ),
            )
            if dashboard_projection is not None:
                await self._upsert_dashboard_projection_in_connection(
                    db,
                    entity_type="APPLICATION",
                    entity_id=application_id,
                    snapshot=dashboard_projection,
                    now=now,
                )
            if notification_event is not None:
                await self._insert_notification_event_in_connection(
                    db,
                    telegram_user_id=notification_event["telegram_user_id"],
                    event_type=notification_event["event_type"],
                    dedupe_key=notification_event["dedupe_key"],
                    snapshot_json=notification_event["snapshot_json"],
                    chunks=notification_event["chunks"],
                    now=now,
                )
            await self._record_application_event_in_connection(
                db,
                event_type="application_submitted",
                application_id=application_id,
                telegram_user_id=telegram_user_id,
                event_at=submitted_at or now,
                metadata={
                    "spreadsheet_id": spreadsheet_id,
                    "sheet_id": sheet_id,
                    "sheet_name": sheet_name,
                    "row_number": row_number,
                    "direction": direction,
                    "answer_type": answer_type,
                    "application_type": application_type,
                    "change_type": change_type,
                    "is_urgent": is_urgent,
                },
                created_at=now,
            )
            await db.commit()
        draft = await self.get_by_user_id(telegram_user_id)
        if draft is None:
            raise LookupError(f"Draft not found for user {telegram_user_id}")
        return draft

    async def complete_linked_submission(
        self,
        telegram_user_id: int,
        *,
        applications: list[dict[str, Any]],
        row_shifts: tuple[tuple[str, int, int, int], ...] = (),
        dashboard_projections: list[dict[str, Any]] | None = None,
        notification_event: dict[str, Any] | None = None,
    ) -> Draft:
        """Атомарно завершить исходный draft и сохранить две независимые заявки."""
        if len(applications) != 2:
            raise ValueError("Linked CHIPS submission must contain exactly two applications")
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            for spreadsheet_id, sheet_id, from_row, delta in row_shifts:
                await self._shift_rows_after_insert_in_connection(
                    db,
                    spreadsheet_id=spreadsheet_id,
                    sheet_id=sheet_id,
                    from_row=from_row,
                    delta=delta,
                    now=now,
                )
            for item in applications:
                await self._save_submitted_application_in_connection(db, item, now)
                await self._record_application_event_in_connection(
                    db,
                    event_type="application_submitted",
                    application_id=item["application_id"],
                    telegram_user_id=telegram_user_id,
                    event_at=item.get("submitted_at") or now,
                    metadata={
                        "spreadsheet_id": item.get("spreadsheet_id"),
                        "sheet_id": item.get("sheet_id"),
                        "sheet_name": item.get("sheet_name"),
                        "row_number": item.get("last_seen_row_number"),
                        "direction": item.get("direction"),
                        "answer_type": item.get("answer_type"),
                        "application_type": item.get("application_type"),
                        "change_type": item.get("change_type"),
                        "is_urgent": item.get("is_urgent"),
                    },
                    created_at=now,
                )
            primary = applications[0]
            await db.execute(
                """
                UPDATE drafts
                SET submission_state = ?, submission_started_at = NULL,
                    submission_spreadsheet_id = ?, submission_sheet_name = ?,
                    submission_sheet_id = ?, submission_row_number = ?,
                    current_step = ?, updated_at = ?
                WHERE telegram_user_id = ?
                """,
                (
                    SubmissionState.SENT.value,
                    primary.get("spreadsheet_id"),
                    primary.get("sheet_name"),
                    primary.get("sheet_id"),
                    primary.get("last_seen_row_number"),
                    Step.COMPLETED.value,
                    now,
                    telegram_user_id,
                ),
            )
            for projection in dashboard_projections or []:
                await self._upsert_dashboard_projection_in_connection(
                    db,
                    entity_type="APPLICATION",
                    entity_id=projection["entity_id"],
                    snapshot=projection["snapshot"],
                    now=now,
                )
            if notification_event is not None:
                await self._insert_notification_event_in_connection(
                    db,
                    telegram_user_id=notification_event["telegram_user_id"],
                    event_type=notification_event["event_type"],
                    dedupe_key=notification_event["dedupe_key"],
                    snapshot_json=notification_event["snapshot_json"],
                    chunks=notification_event["chunks"],
                    now=now,
                )
            await db.commit()
        draft = await self.get_by_user_id(telegram_user_id)
        if draft is None:
            raise LookupError(f"Draft not found for user {telegram_user_id}")
        return draft

    async def get_user_settings(self, telegram_user_id: int) -> UserSettings:
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM user_settings WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()

        if row is not None:
            return self._settings_from_row(row)

        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute(
                """
                INSERT INTO user_settings (
                    telegram_user_id, created_at, updated_at
                ) VALUES (?, ?, ?)
                """,
                (telegram_user_id, now, now),
            )
            await db.commit()
        return UserSettings(telegram_user_id=telegram_user_id, created_at=now, updated_at=now)

    async def save_user_setting(
        self,
        telegram_user_id: int,
        field: str,
        value: str | None,
    ) -> UserSettings:
        if field not in {
            "default_direction",
            "default_intent",
            "default_scriptwriter",
            "pending_action",
        }:
            raise ValueError(f"Unsupported user setting field: {field}")
        await self.get_user_settings(telegram_user_id)
        await self._update_user_settings(telegram_user_id, {field: value})
        return await self.get_user_settings(telegram_user_id)

    async def clear_user_setting(self, telegram_user_id: int, field: str) -> UserSettings:
        return await self.save_user_setting(telegram_user_id, field, None)

    async def get_pending_settings_action(self, telegram_user_id: int) -> str | None:
        settings = await self.get_user_settings(telegram_user_id)
        return settings.pending_action

    async def set_active_message(
        self,
        telegram_user_id: int,
        *,
        chat_id: int,
        message_id: int,
    ) -> UserSettings:
        """Атомарно назначить единственное активное управляющее сообщение."""
        await self.get_user_settings(telegram_user_id)
        await self._update_user_settings(
            telegram_user_id,
            {
                "active_chat_id": chat_id,
                "active_message_id": message_id,
            },
        )
        return await self.get_user_settings(telegram_user_id)

    async def clear_active_message(self, telegram_user_id: int) -> UserSettings:
        """Атомарно очистить координаты активной inline-клавиатуры."""
        await self.get_user_settings(telegram_user_id)
        await self._update_user_settings(
            telegram_user_id,
            {
                "active_chat_id": None,
                "active_message_id": None,
            },
        )
        return await self.get_user_settings(telegram_user_id)

    async def record_application_event(
        self,
        *,
        event_type: str,
        application_id: str | None = None,
        telegram_user_id: int | None = None,
        event_at: str | None = None,
        old_value: str | None = None,
        new_value: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self._connection() as db:
            await self._record_application_event_in_connection(
                db,
                event_type=event_type,
                application_id=application_id,
                telegram_user_id=telegram_user_id,
                event_at=event_at,
                old_value=old_value,
                new_value=new_value,
                metadata=metadata,
            )
            await db.commit()

    async def list_application_events(
        self,
        *,
        application_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[ApplicationEvent]:
        where_clauses: list[str] = []
        params: list[Any] = []
        if application_id is not None:
            where_clauses.append("application_id = ?")
            params.append(application_id)
        if event_type is not None:
            where_clauses.append("event_type = ?")
            params.append(event_type)
        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)
        params.append(max(0, limit))

        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT *
                FROM application_events
                {where_sql}
                ORDER BY event_at DESC, id DESC
                LIMIT ?
                """,
                params,
            )
            rows = await cursor.fetchall()
        return [self._application_event_from_row(row) for row in rows]

    @staticmethod
    async def _record_application_event_in_connection(
        db: aiosqlite.Connection,
        *,
        event_type: str,
        application_id: str | None = None,
        telegram_user_id: int | None = None,
        event_at: str | None = None,
        old_value: str | None = None,
        new_value: str | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> None:
        now = created_at or utc_now_iso()
        metadata_json = (
            None if metadata is None else json.dumps(metadata, ensure_ascii=False)
        )
        await db.execute(
            """
            INSERT INTO application_events (
                application_id, telegram_user_id, event_type, event_at,
                old_value, new_value, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                application_id,
                telegram_user_id,
                event_type,
                event_at or now,
                old_value,
                new_value,
                metadata_json,
                now,
            ),
        )

    async def save_submitted_application(
        self,
        *,
        application_id: str,
        telegram_user_id: int,
        sheet_name: str,
        last_known_status: str,
        spreadsheet_id: str | None = None,
        sheet_id: int | None = None,
        direction: str | None = None,
        answer_type: str | None = None,
        application_type: str | None = None,
        change_type: str | None = None,
        is_urgent: bool | None = None,
        batch_id: str | None = None,
        last_seen_row_number: int | None = None,
        last_seen_editor: str | None = None,
        last_seen_editor_comment: str | None = None,
        last_seen_final_answer: str | None = None,
        submitted_at: str | None = None,
    ) -> SubmittedApplication:
        now = utc_now_iso()
        item = {
            "application_id": application_id,
            "telegram_user_id": telegram_user_id,
            "spreadsheet_id": spreadsheet_id,
            "sheet_id": sheet_id,
            "sheet_name": sheet_name,
            "last_known_status": last_known_status,
            "direction": direction,
            "answer_type": answer_type,
            "application_type": application_type,
            "change_type": change_type,
            "is_urgent": is_urgent,
            "batch_id": batch_id,
            "last_seen_row_number": last_seen_row_number,
            "last_seen_editor": last_seen_editor,
            "last_seen_editor_comment": last_seen_editor_comment,
            "last_seen_final_answer": last_seen_final_answer,
            "submitted_at": submitted_at,
        }
        async with self._connection() as db:
            previous_location = await self._submitted_location_in_connection(
                db,
                application_id,
            )
            await self._save_submitted_application_in_connection(db, item, now)
            await self._maybe_record_application_indexed_event_in_connection(
                db,
                item=item,
                previous_location=previous_location,
                now=now,
            )
            await db.commit()
        submitted = await self.get_submitted_application(application_id)
        if submitted is None:
            raise LookupError(f"Submitted application not found: {application_id}")
        return submitted

    async def index_submitted_application(
        self,
        *,
        application_id: str,
        telegram_user_id: int,
        sheet_name: str,
        last_known_status: str,
        spreadsheet_id: str | None,
        sheet_id: int | None,
        direction: str | None,
        answer_type: str | None,
        application_type: str | None,
        change_type: str | None,
        is_urgent: bool | None,
        last_seen_row_number: int | None,
        last_seen_editor: str | None = None,
        last_seen_editor_comment: str | None = None,
        last_seen_final_answer: str | None = None,
        submitted_at: str | None = None,
        dashboard_projection: dict[str, Any] | None = None,
    ) -> SubmittedApplication:
        """Атомарно поставить вручную найденную строку в tracking и dashboard outbox."""
        now = utc_now_iso()
        item = {
            "application_id": application_id,
            "telegram_user_id": telegram_user_id,
            "spreadsheet_id": spreadsheet_id,
            "sheet_id": sheet_id,
            "sheet_name": sheet_name,
            "last_known_status": last_known_status,
            "direction": direction,
            "answer_type": answer_type,
            "application_type": application_type,
            "change_type": change_type,
            "is_urgent": is_urgent,
            "batch_id": None,
            "last_seen_row_number": last_seen_row_number,
            "last_seen_editor": last_seen_editor,
            "last_seen_editor_comment": last_seen_editor_comment,
            "last_seen_final_answer": last_seen_final_answer,
            "submitted_at": submitted_at,
        }
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            previous_location = await self._submitted_location_in_connection(
                db,
                application_id,
            )
            await self._save_submitted_application_in_connection(db, item, now)
            if dashboard_projection is not None:
                await self._upsert_dashboard_projection_in_connection(
                    db,
                    entity_type="APPLICATION",
                    entity_id=application_id,
                    snapshot=dashboard_projection,
                    now=now,
                )
            await self._maybe_record_application_indexed_event_in_connection(
                db,
                item=item,
                previous_location=previous_location,
                now=now,
            )
            await db.commit()
        submitted = await self.get_submitted_application(application_id)
        if submitted is None:
            raise LookupError(f"Submitted application not found: {application_id}")
        return submitted

    @staticmethod
    async def _submitted_location_in_connection(
        db: aiosqlite.Connection,
        application_id: str,
    ) -> tuple[str | None, int | None, str | None, int | None] | None:
        cursor = await db.execute(
            """
            SELECT spreadsheet_id, sheet_id, sheet_name, last_seen_row_number
            FROM submitted_applications
            WHERE application_id = ?
            """,
            (application_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return row[0], row[1], row[2], row[3]

    @staticmethod
    def _application_location(
        item: dict[str, Any],
    ) -> tuple[str | None, int | None, str | None, int | None] | None:
        spreadsheet_id = item.get("spreadsheet_id")
        sheet_id = item.get("sheet_id")
        sheet_name = item.get("sheet_name")
        row_number = item.get("last_seen_row_number")
        if not spreadsheet_id or sheet_id is None or not sheet_name or row_number is None:
            return None
        return str(spreadsheet_id), int(sheet_id), str(sheet_name), int(row_number)

    @classmethod
    async def _maybe_record_application_indexed_event_in_connection(
        cls,
        db: aiosqlite.Connection,
        *,
        item: dict[str, Any],
        previous_location: tuple[str | None, int | None, str | None, int | None] | None,
        now: str,
    ) -> None:
        location = cls._application_location(item)
        if location is None or location == previous_location:
            return
        spreadsheet_id, sheet_id, sheet_name, row_number = location
        old_value = None if previous_location is None else json.dumps(previous_location, ensure_ascii=False)
        await cls._record_application_event_in_connection(
            db,
            event_type="application_indexed",
            application_id=item["application_id"],
            telegram_user_id=item["telegram_user_id"],
            event_at=now,
            old_value=old_value,
            new_value=json.dumps(location, ensure_ascii=False),
            metadata={
                "spreadsheet_id": spreadsheet_id,
                "sheet_id": sheet_id,
                "sheet_name": sheet_name,
                "row_number": row_number,
                "direction": item.get("direction"),
                "answer_type": item.get("answer_type"),
                "change_type": item.get("change_type"),
                "is_urgent": item.get("is_urgent"),
            },
            created_at=now,
        )

    @staticmethod
    async def _save_submitted_application_in_connection(
        db: aiosqlite.Connection,
        item: dict[str, Any],
        now: str,
    ) -> None:
        is_urgent = item.get("is_urgent")
        await db.execute(
            """
            INSERT INTO submitted_applications (
                application_id, telegram_user_id, spreadsheet_id, sheet_id, sheet_name,
                last_known_status, direction, answer_type, application_type, change_type, is_urgent,
                batch_id, last_seen_row_number, last_seen_editor,
                last_seen_editor_comment, last_seen_final_answer, submitted_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(application_id) DO UPDATE SET
                telegram_user_id = excluded.telegram_user_id,
                spreadsheet_id = excluded.spreadsheet_id,
                sheet_id = excluded.sheet_id,
                sheet_name = excluded.sheet_name,
                last_known_status = excluded.last_known_status,
                direction = excluded.direction,
                answer_type = excluded.answer_type,
                application_type = excluded.application_type,
                change_type = excluded.change_type,
                is_urgent = excluded.is_urgent,
                batch_id = excluded.batch_id,
                last_seen_row_number = excluded.last_seen_row_number,
                last_seen_editor = excluded.last_seen_editor,
                last_seen_editor_comment = excluded.last_seen_editor_comment,
                last_seen_final_answer = excluded.last_seen_final_answer,
                last_seen_scriptwriter_response = NULL,
                pending_editor_comment = NULL,
                pending_editor_comment_seen_count = 0,
                pending_scriptwriter_response = NULL,
                pending_scriptwriter_response_seen_count = 0,
                submitted_at = COALESCE(
                    submitted_applications.submitted_at,
                    excluded.submitted_at
                ),
                polling_state = ?,
                not_found_count = 0,
                last_not_found_at = NULL,
                next_status_check_at = NULL,
                deletion_seen_count = 0,
                deletion_last_seen_at = NULL,
                deletion_error = NULL,
                updated_at = excluded.updated_at
            """,
            (
                item["application_id"],
                item["telegram_user_id"],
                item.get("spreadsheet_id"),
                item.get("sheet_id"),
                item["sheet_name"],
                item["last_known_status"],
                item.get("direction"),
                item.get("answer_type"),
                item.get("application_type"),
                item.get("change_type"),
                None if is_urgent is None else (1 if is_urgent else 0),
                item.get("batch_id"),
                item.get("last_seen_row_number"),
                item.get("last_seen_editor"),
                item.get("last_seen_editor_comment"),
                item.get("last_seen_final_answer"),
                item.get("submitted_at"),
                now,
                now,
                StatusPollingState.ACTIVE.value,
            ),
        )

    async def get_submitted_application(
        self,
        application_id: str,
    ) -> SubmittedApplication | None:
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM submitted_applications WHERE application_id = ?",
                (application_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._submitted_from_row(row) if row is not None else None

    async def list_submitted_applications(
        self,
        *,
        include_deferred: bool = False,
    ) -> list[SubmittedApplication]:
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            if include_deferred:
                cursor = await db.execute(
                    "SELECT * FROM submitted_applications ORDER BY created_at ASC"
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT *
                    FROM submitted_applications
                    WHERE next_status_check_at IS NULL
                       OR next_status_check_at = ''
                       OR next_status_check_at <= ?
                    ORDER BY created_at ASC
                    """,
                    (utc_now_iso(),),
                )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._submitted_from_row(row) for row in rows]

    async def update_submitted_application_status(
        self,
        application_id: str,
        *,
        sheet_name: str,
        last_known_status: str,
        last_seen_row_number: int | None,
        last_seen_editor: str | None = None,
        last_seen_editor_comment: str | None = None,
        spreadsheet_id: str | None = None,
        sheet_id: int | None = None,
        last_seen_final_answer: str | None = None,
        last_seen_scriptwriter_response: str | None = None,
        pending_editor_comment: str | None = None,
        pending_editor_comment_seen_count: int = 0,
        pending_scriptwriter_response: str | None = None,
        pending_scriptwriter_response_seen_count: int = 0,
    ) -> None:
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE submitted_applications
                SET spreadsheet_id = COALESCE(?, spreadsheet_id),
                    sheet_id = COALESCE(?, sheet_id),
                    sheet_name = ?,
                    last_known_status = ?,
                    last_seen_row_number = ?,
                    last_seen_editor = ?,
                    last_seen_editor_comment = ?,
                    last_seen_final_answer = ?,
                    last_seen_scriptwriter_response = ?,
                    pending_editor_comment = ?,
                    pending_editor_comment_seen_count = ?,
                    pending_scriptwriter_response = ?,
                    pending_scriptwriter_response_seen_count = ?,
                    polling_state = ?,
                    not_found_count = 0,
                    last_not_found_at = NULL,
                    next_status_check_at = NULL,
                    deletion_seen_count = 0,
                    deletion_last_seen_at = NULL,
                    deletion_error = NULL,
                    updated_at = ?
                WHERE application_id = ?
                """,
                (
                    spreadsheet_id,
                    sheet_id,
                    sheet_name,
                    last_known_status,
                    last_seen_row_number,
                    last_seen_editor,
                    last_seen_editor_comment,
                    last_seen_final_answer,
                    last_seen_scriptwriter_response,
                    pending_editor_comment,
                    pending_editor_comment_seen_count,
                    pending_scriptwriter_response,
                    pending_scriptwriter_response_seen_count,
                    StatusPollingState.ACTIVE.value,
                    utc_now_iso(),
                    application_id,
                ),
            )
            await db.commit()

    async def mark_submitted_application_not_found(
        self,
        application_id: str,
        *,
        threshold: int,
        recheck_seconds: int,
    ) -> None:
        now = datetime.now(timezone.utc)
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT telegram_user_id, not_found_count
                FROM submitted_applications
                WHERE application_id = ?
                """,
                (application_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                await db.commit()
                return
            count = int(row["not_found_count"] or 0) + 1
            next_check_at = (
                (now + timedelta(seconds=recheck_seconds)).isoformat()
                if count >= threshold
                else None
            )
            await db.execute(
                """
                UPDATE submitted_applications
                SET polling_state = ?,
                    not_found_count = ?,
                    last_not_found_at = ?,
                    next_status_check_at = ?,
                    updated_at = ?
                WHERE application_id = ?
                """,
                (
                    (
                        StatusPollingState.NOT_FOUND.value
                        if count >= threshold
                        else StatusPollingState.ACTIVE.value
                    ),
                    count,
                    now.isoformat(),
                    next_check_at,
                    now.isoformat(),
                    application_id,
                ),
            )
            polling_state = (
                StatusPollingState.NOT_FOUND.value
                if count >= threshold
                else StatusPollingState.ACTIVE.value
            )
            await self._record_application_event_in_connection(
                db,
                event_type="application_not_found",
                application_id=application_id,
                telegram_user_id=row["telegram_user_id"],
                new_value=str(count),
                metadata={
                    "threshold": threshold,
                    "recheck_seconds": recheck_seconds,
                    "next_status_check_at": next_check_at,
                    "polling_state": polling_state,
                },
                created_at=now.isoformat(),
            )
            await db.commit()

    async def mark_bulk_batch_applications_not_found(
        self,
        batch_id: str,
        *,
        threshold: int,
        recheck_seconds: int,
    ) -> int:
        """Count one confirmed missing-source check for every tracked row in a batch."""
        now = datetime.now(timezone.utc)
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT application_id, not_found_count FROM submitted_applications WHERE batch_id = ?",
                (batch_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                count = int(row["not_found_count"] or 0) + 1
                next_check_at = (
                    (now + timedelta(seconds=recheck_seconds)).isoformat()
                    if count >= threshold
                    else None
                )
                await db.execute(
                    """
                    UPDATE submitted_applications
                    SET polling_state = ?, not_found_count = ?,
                        last_not_found_at = ?, next_status_check_at = ?, updated_at = ?
                    WHERE application_id = ?
                    """,
                    (
                        (
                            StatusPollingState.NOT_FOUND.value
                            if count >= threshold
                            else StatusPollingState.ACTIVE.value
                        ),
                        count,
                        now.isoformat(),
                        next_check_at,
                        now.isoformat(),
                        row["application_id"],
                    ),
                )
            await db.commit()
        return len(rows)

    async def save_bulk_batch(
        self,
        *,
        batch_id: str | None = None,
        telegram_user_id: int,
        sheet_name: str,
        sheet_id: int,
        start_row: int,
        data_start_row: int,
        reserved_rows: int,
        data_end_row: int | None = None,
        spreadsheet_id: str = "",
        direction: str = "",
        batch_status: str | None = None,
        status_schema_version: int = 2,
        created_at: str | None = None,
    ) -> BulkBatch:
        batch_id = batch_id or generate_batch_id()
        now = created_at or utc_now_iso()
        async with self._connection() as db:
            await db.execute(
                """
                INSERT INTO bulk_batches (
                    batch_id, telegram_user_id, spreadsheet_id, direction, sheet_name, sheet_id,
                    start_row, data_start_row, reserved_rows, data_end_row, batch_status,
                    last_known_batch_status, status_schema_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    telegram_user_id = excluded.telegram_user_id,
                    spreadsheet_id = excluded.spreadsheet_id,
                    direction = excluded.direction,
                    sheet_name = excluded.sheet_name,
                    sheet_id = excluded.sheet_id,
                    start_row = excluded.start_row,
                    data_start_row = excluded.data_start_row,
                    reserved_rows = excluded.reserved_rows,
                    data_end_row = excluded.data_end_row,
                    batch_status = excluded.batch_status,
                    updated_at = excluded.updated_at
                """,
                (
                    batch_id,
                    telegram_user_id,
                    spreadsheet_id,
                    direction,
                    sheet_name,
                    sheet_id,
                    start_row,
                    data_start_row,
                    reserved_rows,
                    data_end_row or data_start_row + max(reserved_rows, 1) - 1,
                    batch_status or "Новая пачка",
                    batch_status or "Новая пачка",
                    status_schema_version,
                    now,
                    now,
                ),
            )
            await db.commit()
        batch = await self.get_bulk_batch(batch_id)
        if batch is None:
            raise LookupError(f"Bulk batch not found: {batch_id}")
        return batch

    async def get_bulk_batch(self, batch_id: str) -> BulkBatch | None:
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM bulk_batches WHERE batch_id = ?",
                (batch_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._bulk_batch_from_row(row) if row is not None else None

    async def list_bulk_batches(self) -> list[BulkBatch]:
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM bulk_batches ORDER BY created_at ASC")
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._bulk_batch_from_row(row) for row in rows]

    async def get_latest_unregistered_bulk_batch(
        self,
        telegram_user_id: int,
    ) -> BulkBatch | None:
        """Вернуть последнюю пачку пользователя, которую еще можно зарегистрировать."""
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT *
                FROM bulk_batches
                WHERE telegram_user_id = ?
                  AND registration_state != ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    telegram_user_id,
                    BulkRegistrationState.REGISTERED.value,
                ),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._bulk_batch_from_row(row) if row is not None else None

    async def list_active_bulk_batches(self) -> list[BulkBatch]:
        """Вернуть пачки, которые еще требуется проверять в частом polling."""
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT *
                FROM bulk_batches
                WHERE COALESCE(last_known_batch_status, '') != ?
                ORDER BY created_at ASC
                """,
                (BulkBatchStatus.DONE.value,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._bulk_batch_from_row(row) for row in rows]

    async def update_bulk_batch_status(
        self,
        batch_id: str,
        *,
        batch_status: str,
        last_known_batch_status: str,
        dashboard_projection: dict[str, Any] | None = None,
    ) -> None:
        async with self._connection() as db:
            now = utc_now_iso()
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                UPDATE bulk_batches
                SET batch_status = ?,
                    last_known_batch_status = ?,
                    updated_at = ?
                WHERE batch_id = ?
                """,
                (
                    batch_status,
                    last_known_batch_status,
                    now,
                    batch_id,
                ),
            )
            if dashboard_projection is not None:
                await self._upsert_dashboard_projection_in_connection(
                    db,
                    entity_type="BULK_BATCH",
                    entity_id=batch_id,
                    snapshot=dashboard_projection,
                    now=now,
                )
            await db.commit()

    async def restore_bulk_batch_location(
        self,
        batch_id: str,
        *,
        spreadsheet_id: str,
        sheet_name: str,
        sheet_id: int,
        start_row: int,
    ) -> BulkBatch | None:
        """Atomically move a batch and all tracked child rows to verified coordinates."""
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM bulk_batches WHERE batch_id = ?",
                (batch_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                await db.rollback()
                return None

            old_start_row = int(row["start_row"])
            row_delta = start_row - old_start_row
            data_start_row = start_row + 2
            old_data_end_row = row["data_end_row"]
            data_end_row = (
                int(old_data_end_row) + row_delta
                if old_data_end_row is not None
                else None
            )
            now = utc_now_iso()
            await db.execute(
                """
                UPDATE bulk_batches
                SET spreadsheet_id = ?, sheet_name = ?, sheet_id = ?,
                    start_row = ?, data_start_row = ?, data_end_row = ?,
                    location_state = ?, location_miss_count = 0,
                    last_location_search_at = ?, next_location_search_at = NULL,
                    last_location_error = NULL, updated_at = ?
                WHERE batch_id = ?
                """,
                (
                    spreadsheet_id,
                    sheet_name,
                    sheet_id,
                    start_row,
                    data_start_row,
                    data_end_row,
                    BulkBatchLocationState.KNOWN.value,
                    now,
                    now,
                    batch_id,
                ),
            )
            await db.execute(
                """
                UPDATE submitted_applications
                SET spreadsheet_id = ?, sheet_name = ?, sheet_id = ?,
                    last_seen_row_number = CASE
                        WHEN last_seen_row_number IS NULL THEN NULL
                        ELSE last_seen_row_number + ?
                    END,
                    polling_state = ?, not_found_count = 0,
                    last_not_found_at = NULL, next_status_check_at = NULL,
                    updated_at = ?
                WHERE batch_id = ?
                """,
                (
                    spreadsheet_id,
                    sheet_name,
                    sheet_id,
                    row_delta,
                    StatusPollingState.ACTIVE.value,
                    now,
                    batch_id,
                ),
            )
            insert_url = (
                f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
                f"#gid={sheet_id}&range=A{data_start_row}:G{data_start_row}"
            )
            await db.execute(
                """
                UPDATE bulk_creation_requests
                SET insert_url = ?, updated_at = ?
                WHERE batch_id = ?
                """,
                (insert_url, now, batch_id),
            )
            await db.commit()
        return await self.get_bulk_batch(batch_id)

    async def record_bulk_batch_location_problem(
        self,
        batch_id: str,
        *,
        state: str,
        error: str,
        recheck_seconds: int,
    ) -> None:
        now = datetime.now(timezone.utc)
        next_check = now + timedelta(seconds=recheck_seconds)
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE bulk_batches
                SET location_state = ?,
                    location_miss_count = location_miss_count + CASE WHEN ? = ? THEN 1 ELSE 0 END,
                    last_location_search_at = ?, next_location_search_at = ?,
                    last_location_error = ?, updated_at = ?
                WHERE batch_id = ?
                """,
                (
                    state,
                    state,
                    BulkBatchLocationState.MISSING.value,
                    now.isoformat(),
                    next_check.isoformat(),
                    error[:500],
                    now.isoformat(),
                    batch_id,
                ),
            )
            await db.commit()

    async def update_bulk_batch_reserved_rows(
        self,
        batch_id: str,
        *,
        reserved_rows: int,
    ) -> None:
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE bulk_batches
                SET reserved_rows = ?,
                    updated_at = ?
                WHERE batch_id = ?
                """,
                (
                    reserved_rows,
                    utc_now_iso(),
                    batch_id,
                ),
            )
            await db.commit()

    async def claim_bulk_batch_registration(
        self,
        batch_id: str,
        *,
        stale_after_seconds: int,
    ) -> str:
        """Атомарно захватить пачку для регистрации или восстановить stale-захват."""
        now = datetime.now(timezone.utc)
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT registration_state, registration_started_at
                FROM bulk_batches
                WHERE batch_id = ?
                """,
                (batch_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                await db.rollback()
                return "NOT_FOUND"

            state = row["registration_state"] or BulkRegistrationState.DRAFT.value
            if state == BulkRegistrationState.REGISTERED.value:
                await db.commit()
                return BulkRegistrationState.REGISTERED.value
            if state == BulkRegistrationState.REGISTERING.value:
                started_at = _parse_iso_datetime(row["registration_started_at"])
                if started_at is not None and now - started_at < timedelta(
                    seconds=stale_after_seconds
                ):
                    await db.commit()
                    return BulkRegistrationState.REGISTERING.value

            await db.execute(
                """
                UPDATE bulk_batches
                SET registration_state = ?,
                    registration_started_at = ?,
                    updated_at = ?
                WHERE batch_id = ?
                """,
                (
                    BulkRegistrationState.REGISTERING.value,
                    now.isoformat(),
                    now.isoformat(),
                    batch_id,
                ),
            )
            await db.commit()
        return "ACQUIRED"

    async def complete_bulk_batch_registration(
        self,
        batch_id: str,
        *,
        registered_count: int,
        data_end_row: int,
        dashboard_projection: dict[str, Any] | None = None,
    ) -> None:
        """Зафиксировать фактическую границу и успешный результат регистрации пачки."""
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE bulk_batches
                SET registration_state = ?,
                    registration_started_at = NULL,
                    registered_count = ?,
                    data_end_row = ?,
                    updated_at = ?
                WHERE batch_id = ?
                  AND registration_state = ?
                """,
                (
                    BulkRegistrationState.REGISTERED.value,
                    registered_count,
                    data_end_row,
                    utc_now_iso(),
                    batch_id,
                    BulkRegistrationState.REGISTERING.value,
                ),
            )
            if dashboard_projection is not None:
                await self._upsert_dashboard_projection_in_connection(
                    db,
                    entity_type="BULK_BATCH",
                    entity_id=batch_id,
                    snapshot=dashboard_projection,
                    now=utc_now_iso(),
                )
            await db.commit()

    async def upsert_dashboard_projection(
        self,
        *,
        entity_type: str,
        entity_id: str,
        snapshot: dict[str, Any],
    ) -> None:
        now = utc_now_iso()
        async with self._connection() as db:
            await self._upsert_dashboard_projection_in_connection(
                db,
                entity_type=entity_type,
                entity_id=entity_id,
                snapshot=snapshot,
                now=now,
            )
            await db.commit()

    async def claim_dashboard_projections(
        self,
        *,
        stale_after_seconds: int,
        limit: int = 100,
    ) -> list[DashboardOutboxItem]:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        stale_before = (now - timedelta(seconds=stale_after_seconds)).isoformat()
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                UPDATE dashboard_outbox
                SET state = ?, sending_started_at = NULL, updated_at = ?
                WHERE state = ? AND sending_started_at < ?
                """,
                (
                    DashboardOutboxState.PENDING.value,
                    now_iso,
                    DashboardOutboxState.SENDING.value,
                    stale_before,
                ),
            )
            cursor = await db.execute(
                """
                SELECT *
                FROM dashboard_outbox
                WHERE state = ?
                  AND COALESCE(next_attempt_at, '') <= ?
                ORDER BY updated_at, entity_type, entity_id
                LIMIT ?
                """,
                (DashboardOutboxState.PENDING.value, now_iso, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            keys = [(row["entity_type"], row["entity_id"]) for row in rows]
            for entity_type, entity_id in keys:
                await db.execute(
                    """
                    UPDATE dashboard_outbox
                    SET state = ?, sending_started_at = ?, updated_at = ?
                    WHERE entity_type = ? AND entity_id = ? AND state = ?
                    """,
                    (
                        DashboardOutboxState.SENDING.value,
                        now_iso,
                        now_iso,
                        entity_type,
                        entity_id,
                        DashboardOutboxState.PENDING.value,
                    ),
                )
            await db.commit()
        return [self._dashboard_outbox_from_row(row) for row in rows]

    async def complete_dashboard_projections(
        self,
        items: list[DashboardOutboxItem],
    ) -> None:
        if not items:
            return
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            for item in items:
                await db.execute(
                    """
                    DELETE FROM dashboard_outbox
                    WHERE entity_type = ? AND entity_id = ? AND state = ?
                    """,
                    (
                        item.entity_type,
                        item.entity_id,
                        DashboardOutboxState.SENDING.value,
                    ),
                )
            await db.commit()

    async def fail_dashboard_projections(
        self,
        items: list[DashboardOutboxItem],
        *,
        error: str,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ) -> None:
        if not items:
            return
        now = datetime.now(timezone.utc)
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            for item in items:
                attempts = item.attempts + 1
                delay = min(
                    retry_max_seconds,
                    retry_base_seconds * (2 ** max(attempts - 1, 0)),
                )
                await db.execute(
                    """
                    UPDATE dashboard_outbox
                    SET state = ?, attempts = ?, next_attempt_at = ?,
                        sending_started_at = NULL, last_error = ?, updated_at = ?
                    WHERE entity_type = ? AND entity_id = ? AND state = ?
                    """,
                    (
                        DashboardOutboxState.PENDING.value,
                        attempts,
                        (now + timedelta(seconds=delay)).isoformat(),
                        error[:1000],
                        now.isoformat(),
                        item.entity_type,
                        item.entity_id,
                        DashboardOutboxState.SENDING.value,
                    ),
                )
            await db.commit()

    async def list_dashboard_outbox(self) -> list[DashboardOutboxItem]:
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM dashboard_outbox ORDER BY updated_at, entity_type, entity_id"
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._dashboard_outbox_from_row(row) for row in rows]

    async def list_completed_bulk_batches(self) -> list[BulkBatch]:
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT *
                FROM bulk_batches
                WHERE COALESCE(last_known_batch_status, '') = ?
                ORDER BY created_at ASC
                """,
                (BulkBatchStatus.DONE.value,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._bulk_batch_from_row(row) for row in rows]

    async def release_bulk_batch_registration(self, batch_id: str) -> None:
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE bulk_batches
                SET registration_state = ?,
                    registration_started_at = NULL,
                    updated_at = ?
                WHERE batch_id = ?
                  AND registration_state = ?
                """,
                (
                    BulkRegistrationState.DRAFT.value,
                    utc_now_iso(),
                    batch_id,
                    BulkRegistrationState.REGISTERING.value,
                ),
            )
            await db.commit()

    async def enqueue_notification_event(
        self,
        *,
        telegram_user_id: int,
        event_type: str,
        dedupe_key: str,
        snapshot_json: str,
        chunks: list[str],
        application_updates: list[dict[str, Any]] | None = None,
        batch_updates: list[dict[str, Any]] | None = None,
        dashboard_projections: list[dict[str, Any]] | None = None,
        application_events: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Atomically persist an observed event and advance its tracking state."""
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            inserted = await self._insert_notification_event_in_connection(
                db,
                telegram_user_id=telegram_user_id,
                event_type=event_type,
                dedupe_key=dedupe_key,
                snapshot_json=snapshot_json,
                chunks=chunks,
                now=now,
            )
            if not inserted:
                await db.commit()
                return False

            for update in application_updates or []:
                await self._update_submitted_application_in_connection(db, update, now)
            for event in application_events or []:
                await self._record_application_event_in_connection(
                    db,
                    **event,
                    created_at=now,
                )
            for update in batch_updates or []:
                await db.execute(
                    """
                    UPDATE bulk_batches
                    SET batch_status = ?, last_known_batch_status = ?, updated_at = ?
                    WHERE batch_id = ?
                    """,
                    (
                        update["status"],
                        update["status"],
                        now,
                        update["batch_id"],
                    ),
                )
            for projection in dashboard_projections or []:
                await self._upsert_dashboard_projection_in_connection(
                    db,
                    entity_type=projection["entity_type"],
                    entity_id=projection["entity_id"],
                    snapshot=projection["snapshot"],
                    now=now,
                )
            await db.commit()
        return True

    async def _insert_notification_event_in_connection(
        self,
        db: aiosqlite.Connection,
        *,
        telegram_user_id: int,
        event_type: str,
        dedupe_key: str,
        snapshot_json: str,
        chunks: list[str],
        now: str,
    ) -> bool:
        cursor = await db.execute(
            "SELECT 1 FROM notification_outbox WHERE dedupe_key LIKE ? LIMIT 1",
            (f"{dedupe_key}:%",),
        )
        exists = await cursor.fetchone()
        await cursor.close()
        if exists is not None:
            return False

        chunk_count = len(chunks)
        for chunk_index, html in enumerate(chunks):
            await db.execute(
                """
                INSERT INTO notification_outbox (
                    event_id, dedupe_key, telegram_user_id, event_type,
                    snapshot_json, html, chunk_index, chunk_count,
                    state, next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generate_application_id(),
                    f"{dedupe_key}:{chunk_index}",
                    telegram_user_id,
                    event_type,
                    snapshot_json,
                    html,
                    chunk_index,
                    chunk_count,
                    NotificationOutboxState.PENDING.value,
                    now,
                    now,
                    now,
                ),
            )
        return True

    async def update_application_tracking_batch(
        self,
        updates: list[dict[str, Any]],
        *,
        dashboard_projections: list[dict[str, Any]] | None = None,
        application_events: list[dict[str, Any]] | None = None,
    ) -> None:
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            for update in updates:
                await self._update_submitted_application_in_connection(db, update, now)
            for event in application_events or []:
                await self._record_application_event_in_connection(
                    db,
                    **event,
                    created_at=now,
                )
            for projection in dashboard_projections or []:
                await self._upsert_dashboard_projection_in_connection(
                    db,
                    entity_type=projection["entity_type"],
                    entity_id=projection["entity_id"],
                    snapshot=projection["snapshot"],
                    now=now,
                )
            await db.commit()

    async def claim_next_notification(
        self,
        *,
        stale_after_seconds: int,
    ) -> NotificationOutboxItem | None:
        now = datetime.now(timezone.utc)
        stale_before = (now - timedelta(seconds=stale_after_seconds)).isoformat()
        now_iso = now.isoformat()
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                UPDATE notification_outbox
                SET state = ?, sending_started_at = NULL, updated_at = ?
                WHERE state = ? AND sending_started_at < ?
                """,
                (
                    NotificationOutboxState.PENDING.value,
                    now_iso,
                    NotificationOutboxState.SENDING.value,
                    stale_before,
                ),
            )
            cursor = await db.execute(
                """
                SELECT candidate.*
                FROM notification_outbox AS candidate
                WHERE candidate.state = ?
                  AND COALESCE(candidate.next_attempt_at, '') <= ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM notification_outbox AS earlier
                      WHERE earlier.telegram_user_id = candidate.telegram_user_id
                        AND earlier.state IN (?, ?)
                        AND earlier.rowid < candidate.rowid
                  )
                ORDER BY candidate.rowid
                LIMIT 1
                """,
                (
                    NotificationOutboxState.PENDING.value,
                    now_iso,
                    NotificationOutboxState.PENDING.value,
                    NotificationOutboxState.SENDING.value,
                ),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                await db.commit()
                return None
            await db.execute(
                """
                UPDATE notification_outbox
                SET state = ?, sending_started_at = ?, updated_at = ?
                WHERE event_id = ? AND state = ?
                """,
                (
                    NotificationOutboxState.SENDING.value,
                    now_iso,
                    now_iso,
                    row["event_id"],
                    NotificationOutboxState.PENDING.value,
                ),
            )
            await db.commit()
        return self._notification_outbox_from_row(row)

    async def complete_notification(
        self,
        event_id: str,
        *,
        telegram_message_id: int | None,
    ) -> None:
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE notification_outbox
                SET state = ?, telegram_message_id = ?, sending_started_at = NULL,
                    last_error = NULL, updated_at = ?
                WHERE event_id = ?
                """,
                (
                    NotificationOutboxState.SENT.value,
                    telegram_message_id,
                    utc_now_iso(),
                    event_id,
                ),
            )
            await db.commit()

    async def fail_notification(
        self,
        event_id: str,
        *,
        error: str,
        max_attempts: int,
        retry_base_seconds: int,
    ) -> None:
        now = datetime.now(timezone.utc)
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT attempts FROM notification_outbox WHERE event_id = ?",
                (event_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            attempts = int(row["attempts"] if row else 0) + 1
            state = (
                NotificationOutboxState.FAILED.value
                if attempts >= max_attempts
                else NotificationOutboxState.PENDING.value
            )
            delay = retry_base_seconds * min(2 ** max(attempts - 1, 0), 32)
            next_attempt_at = (
                None
                if state == NotificationOutboxState.FAILED.value
                else (now + timedelta(seconds=delay)).isoformat()
            )
            await db.execute(
                """
                UPDATE notification_outbox
                SET state = ?, attempts = ?, next_attempt_at = ?,
                    sending_started_at = NULL, last_error = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (
                    state,
                    attempts,
                    next_attempt_at,
                    error[:1000],
                    now.isoformat(),
                    event_id,
                ),
            )
            await db.commit()

    async def list_notification_outbox(self) -> list[NotificationOutboxItem]:
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM notification_outbox ORDER BY rowid"
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._notification_outbox_from_row(row) for row in rows]

    async def create_bulk_creation_request(
        self,
        *,
        idempotency_key: str,
        telegram_user_id: int,
        batch_id: str,
    ) -> BulkCreationRequest:
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO bulk_creation_requests (
                    idempotency_key, telegram_user_id, state, batch_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    telegram_user_id,
                    BulkCreationState.AWAITING_DIRECTION.value,
                    batch_id,
                    now,
                    now,
                ),
            )
            await db.commit()
        request = await self.get_bulk_creation_request(idempotency_key)
        if request is None:
            raise LookupError(f"Bulk creation request not found: {idempotency_key}")
        return request

    async def get_bulk_creation_request(
        self,
        idempotency_key: str,
    ) -> BulkCreationRequest | None:
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM bulk_creation_requests WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._bulk_creation_from_row(row) if row is not None else None

    async def claim_bulk_creation(
        self,
        idempotency_key: str,
        *,
        direction: str,
        stale_after_seconds: int,
    ) -> BulkCreationRequest | None:
        now = datetime.now(timezone.utc)
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM bulk_creation_requests WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                await db.rollback()
                return None
            state = row["state"]
            started_at = _parse_iso_datetime(row["started_at"])
            if state == BulkCreationState.BULK_CREATING.value and (
                started_at is None
                or now - started_at < timedelta(seconds=stale_after_seconds)
            ):
                await db.commit()
                return self._bulk_creation_from_row(row)
            if state == BulkCreationState.CREATED.value:
                await db.commit()
                return self._bulk_creation_from_row(row)
            await db.execute(
                """
                UPDATE bulk_creation_requests
                SET direction = ?, state = ?, started_at = ?, last_error = NULL, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (
                    direction,
                    BulkCreationState.BULK_CREATING.value,
                    now.isoformat(),
                    now.isoformat(),
                    idempotency_key,
                ),
            )
            await db.commit()
        return await self.get_bulk_creation_request(idempotency_key)

    async def complete_bulk_creation(
        self,
        idempotency_key: str,
        *,
        insert_url: str,
    ) -> None:
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE bulk_creation_requests
                SET state = ?, insert_url = ?, started_at = NULL,
                    last_error = NULL, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (
                    BulkCreationState.CREATED.value,
                    insert_url,
                    utc_now_iso(),
                    idempotency_key,
                ),
            )
            await db.commit()

    async def fail_bulk_creation(self, idempotency_key: str, *, error: str) -> None:
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE bulk_creation_requests
                SET state = ?, started_at = NULL, last_error = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (
                    BulkCreationState.FAILED.value,
                    error[:1000],
                    utc_now_iso(),
                    idempotency_key,
                ),
            )
            await db.commit()

    async def create_bulk_reservation(
        self,
        *,
        reservation_id: str,
        idempotency_key: str,
        telegram_user_id: int,
    ) -> BulkReservation:
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO bulk_reservations (
                    reservation_id, idempotency_key, telegram_user_id, state,
                    registered_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    reservation_id,
                    idempotency_key,
                    telegram_user_id,
                    BulkReservationState.AWAITING_DIRECTION.value,
                    now,
                    now,
                ),
            )
            await db.commit()
        reservation = await self.get_bulk_reservation(reservation_id)
        if reservation is None:
            raise LookupError(f"Bulk reservation not found: {reservation_id}")
        return reservation

    async def get_bulk_reservation(self, reservation_id: str) -> BulkReservation | None:
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM bulk_reservations WHERE reservation_id = ?",
                (reservation_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._bulk_reservation_from_row(row) if row is not None else None

    async def get_active_bulk_reservation(
        self,
        telegram_user_id: int,
    ) -> BulkReservation | None:
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT *
                FROM bulk_reservations
                WHERE telegram_user_id = ?
                  AND state NOT IN (?, ?, ?)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    telegram_user_id,
                    BulkReservationState.REGISTERED.value,
                    BulkReservationState.CANCELLED.value,
                    BulkReservationState.FAILED.value,
                ),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._bulk_reservation_from_row(row) if row is not None else None

    async def update_bulk_reservation_step(
        self,
        reservation_id: str,
        *,
        state: BulkReservationState,
        direction: str | None = None,
        target_kind: str | None = None,
        change_type: str | None = None,
        requested_count: int | None = None,
    ) -> BulkReservation | None:
        now = utc_now_iso()
        assignments = ["state = ?", "updated_at = ?"]
        params: list[Any] = [state.value, now]
        for column, value in (
            ("direction", direction),
            ("target_kind", target_kind),
            ("change_type", change_type),
            ("requested_count", requested_count),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(value)
        params.append(reservation_id)
        async with self._connection() as db:
            await db.execute(
                f"""
                UPDATE bulk_reservations
                SET {", ".join(assignments)}
                WHERE reservation_id = ?
                """,
                params,
            )
            await db.commit()
        return await self.get_bulk_reservation(reservation_id)

    async def claim_bulk_reservation_creation(
        self,
        reservation_id: str,
        *,
        stale_after_seconds: int,
    ) -> BulkReservation | None:
        now = datetime.now(timezone.utc)
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM bulk_reservations WHERE reservation_id = ?",
                (reservation_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                await db.commit()
                return None
            reservation = self._bulk_reservation_from_row(row)
            if reservation.state == BulkReservationState.CREATED.value:
                await db.commit()
                return reservation
            if reservation.state == BulkReservationState.CREATING.value:
                started = _parse_iso_datetime(reservation.started_at)
                if started and now - started < timedelta(seconds=stale_after_seconds):
                    await db.commit()
                    return reservation
            now_iso = now.isoformat()
            await db.execute(
                """
                UPDATE bulk_reservations
                SET state = ?, started_at = ?, last_error = NULL, updated_at = ?
                WHERE reservation_id = ?
                  AND state IN (?, ?, ?)
                """,
                (
                    BulkReservationState.CREATING.value,
                    now_iso,
                    now_iso,
                    reservation_id,
                    BulkReservationState.AWAITING_CONFIRMATION.value,
                    BulkReservationState.CREATING.value,
                    BulkReservationState.FAILED.value,
                ),
            )
            await db.commit()
        return await self.get_bulk_reservation(reservation_id)

    async def complete_bulk_reservation_creation_and_shift(
        self,
        reservation_id: str,
        *,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_name: str,
        start_row: int,
        end_row: int,
        insert_url: str,
        shifted_rows: int,
    ) -> BulkReservation | None:
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                UPDATE submitted_applications
                SET last_seen_row_number = last_seen_row_number + ?,
                    updated_at = ?
                WHERE spreadsheet_id = ?
                  AND sheet_id = ?
                  AND last_seen_row_number >= ?
                """,
                (shifted_rows, now, spreadsheet_id, sheet_id, start_row),
            )
            await db.execute(
                """
                UPDATE bulk_reservations
                SET start_row = CASE
                        WHEN start_row IS NULL THEN NULL
                        WHEN start_row >= ? THEN start_row + ?
                        ELSE start_row
                    END,
                    end_row = CASE
                        WHEN end_row IS NULL THEN NULL
                        WHEN end_row >= ? THEN end_row + ?
                        ELSE end_row
                    END,
                    updated_at = ?
                WHERE spreadsheet_id = ?
                  AND sheet_id = ?
                  AND reservation_id != ?
                  AND state NOT IN (?, ?, ?)
                """,
                (
                    start_row,
                    shifted_rows,
                    start_row,
                    shifted_rows,
                    now,
                    spreadsheet_id,
                    sheet_id,
                    reservation_id,
                    BulkReservationState.REGISTERED.value,
                    BulkReservationState.CANCELLED.value,
                    BulkReservationState.FAILED.value,
                ),
            )
            await db.execute(
                """
                UPDATE bulk_reservations
                SET state = ?, spreadsheet_id = ?, sheet_id = ?, sheet_name = ?,
                    start_row = ?, end_row = ?, insert_url = ?,
                    started_at = NULL, last_error = NULL, updated_at = ?
                WHERE reservation_id = ?
                """,
                (
                    BulkReservationState.CREATED.value,
                    spreadsheet_id,
                    sheet_id,
                    sheet_name,
                    start_row,
                    end_row,
                    insert_url,
                    now,
                    reservation_id,
                ),
            )
            await db.commit()
        return await self.get_bulk_reservation(reservation_id)

    async def complete_bulk_reservation_creation(
        self,
        reservation_id: str,
        *,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_name: str,
        start_row: int,
        end_row: int,
        insert_url: str,
    ) -> BulkReservation | None:
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE bulk_reservations
                SET state = ?, spreadsheet_id = ?, sheet_id = ?, sheet_name = ?,
                    start_row = ?, end_row = ?, insert_url = ?,
                    last_error = NULL, updated_at = ?
                WHERE reservation_id = ?
                """,
                (
                    BulkReservationState.CREATED.value,
                    spreadsheet_id,
                    sheet_id,
                    sheet_name,
                    start_row,
                    end_row,
                    insert_url,
                    now,
                    reservation_id,
                ),
            )
            await db.commit()
        return await self.get_bulk_reservation(reservation_id)

    async def fail_bulk_reservation(
        self,
        reservation_id: str,
        *,
        error: str,
    ) -> None:
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE bulk_reservations
                SET state = ?, started_at = NULL, last_error = ?, updated_at = ?
                WHERE reservation_id = ?
                """,
                (
                    BulkReservationState.FAILED.value,
                    error[:1000],
                    utc_now_iso(),
                    reservation_id,
                ),
            )
            await db.commit()

    async def cancel_bulk_reservation(self, reservation_id: str) -> None:
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE bulk_reservations
                SET state = ?, updated_at = ?
                WHERE reservation_id = ?
                """,
                (BulkReservationState.CANCELLED.value, utc_now_iso(), reservation_id),
            )
            await db.commit()

    async def claim_bulk_reservation_registration(
        self,
        reservation_id: str,
        *,
        stale_after_seconds: int,
    ) -> BulkReservation | None:
        now = datetime.now(timezone.utc)
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM bulk_reservations WHERE reservation_id = ?",
                (reservation_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                await db.commit()
                return None
            reservation = self._bulk_reservation_from_row(row)
            if reservation.state == BulkReservationState.REGISTERED.value:
                await db.commit()
                return reservation
            if reservation.state == BulkReservationState.REGISTERING.value:
                started = _parse_iso_datetime(reservation.started_at)
                if started and now - started < timedelta(seconds=stale_after_seconds):
                    await db.commit()
                    return reservation
            now_iso = now.isoformat()
            await db.execute(
                """
                UPDATE bulk_reservations
                SET state = ?, started_at = ?, last_error = NULL, updated_at = ?
                WHERE reservation_id = ?
                  AND state IN (?, ?, ?)
                """,
                (
                    BulkReservationState.REGISTERING.value,
                    now_iso,
                    now_iso,
                    reservation_id,
                    BulkReservationState.CREATED.value,
                    BulkReservationState.REGISTERING.value,
                    BulkReservationState.FAILED.value,
                ),
            )
            await db.commit()
        return await self.get_bulk_reservation(reservation_id)

    async def release_bulk_reservation_registration(
        self,
        reservation_id: str,
        *,
        error: str | None = None,
    ) -> None:
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE bulk_reservations
                SET state = ?, started_at = NULL, last_error = ?, updated_at = ?
                WHERE reservation_id = ?
                  AND state = ?
                """,
                (
                    BulkReservationState.CREATED.value,
                    (error or "")[:1000] or None,
                    utc_now_iso(),
                    reservation_id,
                    BulkReservationState.REGISTERING.value,
                ),
            )
            await db.commit()

    async def complete_bulk_reservation_registration(
        self,
        reservation_id: str,
        *,
        registered_count: int,
    ) -> None:
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE bulk_reservations
                SET state = ?, registered_count = ?, registered_at = ?,
                    started_at = NULL, last_error = NULL, updated_at = ?
                WHERE reservation_id = ?
                """,
                (
                    BulkReservationState.REGISTERED.value,
                    registered_count,
                    now,
                    now,
                    reservation_id,
                ),
            )
            await db.commit()

    async def complete_bulk_reservation_registration_with_updates(
        self,
        reservation_id: str,
        *,
        registered_count: int,
        tracking: list[dict[str, Any]],
        dashboard_projections: list[dict[str, Any]],
        notification_event: dict[str, Any] | None = None,
        spreadsheet_id: str | None = None,
        sheet_id: int | None = None,
        sheet_name: str | None = None,
        start_row: int | None = None,
        end_row: int | None = None,
        insert_url: str | None = None,
    ) -> None:
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            for item in tracking:
                await self._save_submitted_application_in_connection(db, item, now)
                # New bulk reservations bypass complete_submission, so record
                # the same exact submission event in their atomic write path.
                await self._record_application_event_in_connection(
                    db,
                    event_type="application_submitted",
                    application_id=item["application_id"],
                    telegram_user_id=item["telegram_user_id"],
                    event_at=item.get("submitted_at") or now,
                    metadata={
                        "spreadsheet_id": item.get("spreadsheet_id"),
                        "sheet_id": item.get("sheet_id"),
                        "sheet_name": item.get("sheet_name"),
                        "row_number": item.get("last_seen_row_number"),
                        "direction": item.get("direction"),
                        "answer_type": item.get("answer_type"),
                        "application_type": item.get("application_type"),
                        "change_type": item.get("change_type"),
                        "is_urgent": item.get("is_urgent"),
                    },
                    created_at=now,
                )
            for projection in dashboard_projections:
                await self._upsert_dashboard_projection_in_connection(
                    db,
                    entity_type=projection["entity_type"],
                    entity_id=projection["entity_id"],
                    snapshot=projection["snapshot"],
                    now=now,
                )
            if notification_event is not None:
                await self._insert_notification_event_in_connection(
                    db,
                    telegram_user_id=notification_event["telegram_user_id"],
                    event_type=notification_event["event_type"],
                    dedupe_key=notification_event["dedupe_key"],
                    snapshot_json=notification_event["snapshot_json"],
                    chunks=notification_event["chunks"],
                    now=now,
                )
            await db.execute(
                """
                UPDATE bulk_reservations
                SET state = ?, registered_count = ?, registered_at = ?,
                    spreadsheet_id = COALESCE(?, spreadsheet_id),
                    sheet_id = COALESCE(?, sheet_id),
                    sheet_name = COALESCE(?, sheet_name),
                    start_row = COALESCE(?, start_row),
                    end_row = COALESCE(?, end_row),
                    insert_url = COALESCE(?, insert_url),
                    started_at = NULL, last_error = NULL, updated_at = ?
                WHERE reservation_id = ?
                """,
                (
                    BulkReservationState.REGISTERED.value,
                    registered_count,
                    now,
                    spreadsheet_id,
                    sheet_id,
                    sheet_name,
                    start_row,
                    end_row,
                    insert_url,
                    now,
                    reservation_id,
                ),
            )
            await db.commit()

    async def acquire_bulk_section_lock(
        self,
        *,
        lock_key: str,
        owner: str,
        ttl_seconds: int,
    ) -> bool:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "DELETE FROM bulk_section_locks WHERE expires_at <= ?",
                (now_iso,),
            )
            cursor = await db.execute(
                "SELECT owner FROM bulk_section_locks WHERE lock_key = ?",
                (lock_key,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is not None and row[0] != owner:
                await db.commit()
                return False
            await db.execute(
                """
                INSERT INTO bulk_section_locks (
                    lock_key, owner, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(lock_key) DO UPDATE SET
                    owner = excluded.owner,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (lock_key, owner, expires_at, now_iso, now_iso),
            )
            await db.commit()
        return True

    async def release_bulk_section_lock(self, *, lock_key: str, owner: str) -> None:
        async with self._connection() as db:
            await db.execute(
                "DELETE FROM bulk_section_locks WHERE lock_key = ? AND owner = ?",
                (lock_key, owner),
            )
            await db.commit()

    async def shift_rows_after_insert(
        self,
        *,
        spreadsheet_id: str,
        sheet_id: int,
        from_row: int,
        delta: int,
    ) -> None:
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._shift_rows_after_insert_in_connection(
                db,
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                from_row=from_row,
                delta=delta,
                now=now,
            )
            await db.commit()

    @staticmethod
    async def _shift_rows_after_insert_in_connection(
        db: aiosqlite.Connection,
        *,
        spreadsheet_id: str,
        sheet_id: int,
        from_row: int,
        delta: int,
        now: str,
    ) -> None:
        await db.execute(
                """
                UPDATE submitted_applications
                SET last_seen_row_number = last_seen_row_number + ?,
                    updated_at = ?
                WHERE spreadsheet_id = ?
                  AND sheet_id = ?
                  AND last_seen_row_number >= ?
                """,
                (delta, now, spreadsheet_id, sheet_id, from_row),
            )
        await db.execute(
                """
                UPDATE bulk_reservations
                SET start_row = CASE
                        WHEN start_row IS NULL THEN NULL
                        WHEN start_row >= ? THEN start_row + ?
                        ELSE start_row
                    END,
                    end_row = CASE
                        WHEN end_row IS NULL THEN NULL
                        WHEN end_row >= ? THEN end_row + ?
                        ELSE end_row
                    END,
                    updated_at = ?
                WHERE spreadsheet_id = ?
                  AND sheet_id = ?
                  AND state NOT IN (?, ?, ?)
                """,
                (
                    from_row,
                    delta,
                    from_row,
                    delta,
                    now,
                    spreadsheet_id,
                    sheet_id,
                    BulkReservationState.REGISTERED.value,
                    BulkReservationState.CANCELLED.value,
                    BulkReservationState.FAILED.value,
                ),
            )

    async def mark_application_deletion_seen(
        self,
        application_id: str,
        *,
        error: str | None = None,
    ) -> int:
        now = utc_now_iso()
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT deletion_seen_count
                FROM submitted_applications
                WHERE application_id = ?
                """,
                (application_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                await db.commit()
                return 0
            count = int(row["deletion_seen_count"] or 0) + 1
            await db.execute(
                """
                UPDATE submitted_applications
                SET deletion_seen_count = ?,
                    deletion_last_seen_at = ?,
                    deletion_error = ?,
                    updated_at = ?
                WHERE application_id = ?
                """,
                (count, now, error, now, application_id),
            )
            await db.commit()
        return count

    async def reset_application_deletion_state(self, application_id: str) -> None:
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute(
                """
                UPDATE submitted_applications
                SET deletion_seen_count = 0,
                    deletion_last_seen_at = NULL,
                    deletion_error = NULL,
                    updated_at = ?
                WHERE application_id = ?
                """,
                (now, application_id),
            )
            await db.commit()

    async def record_application_deletion_error(
        self,
        application_id: str,
        error: str,
    ) -> None:
        now = utc_now_iso()
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT telegram_user_id FROM submitted_applications WHERE application_id = ?",
                (application_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            await db.execute(
                """
                UPDATE submitted_applications
                SET deletion_error = ?,
                    updated_at = ?
                WHERE application_id = ?
                """,
                (error[:1000], now, application_id),
            )
            await self._record_application_event_in_connection(
                db,
                event_type="application_deletion_error",
                application_id=application_id,
                telegram_user_id=row["telegram_user_id"] if row is not None else None,
                new_value=error[:1000],
                created_at=now,
            )
            await db.commit()

    async def complete_application_deletion(
        self,
        *,
        application_id: str,
        spreadsheet_id: str,
        sheet_id: int,
        deleted_row_number: int,
    ) -> dict[str, int]:
        now = utc_now_iso()
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT telegram_user_id FROM submitted_applications WHERE application_id = ?",
                (application_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            notification_deleted = await self._delete_notification_refs_in_connection(
                db,
                application_id,
            )
            cursor = await db.execute(
                "DELETE FROM dashboard_outbox WHERE entity_id = ?",
                (application_id,),
            )
            dashboard_deleted = cursor.rowcount
            await cursor.close()
            await self._upsert_dashboard_projection_in_connection(
                db,
                entity_type="APPLICATION",
                entity_id=application_id,
                snapshot={"action": "delete", "application_id": application_id},
                now=now,
            )
            cursor = await db.execute(
                "DELETE FROM submitted_applications WHERE application_id = ?",
                (application_id,),
            )
            submitted_deleted = cursor.rowcount
            await cursor.close()
            cursor = await db.execute(
                """
                UPDATE submitted_applications
                SET last_seen_row_number = last_seen_row_number - 1,
                    updated_at = ?
                WHERE spreadsheet_id = ?
                  AND sheet_id = ?
                  AND last_seen_row_number > ?
                """,
                (now, spreadsheet_id, sheet_id, deleted_row_number),
            )
            submitted_shifted = cursor.rowcount
            await cursor.close()
            cursor = await db.execute(
                """
                UPDATE bulk_reservations
                SET start_row = CASE
                        WHEN start_row IS NULL THEN NULL
                        WHEN start_row > ? THEN start_row - 1
                        ELSE start_row
                    END,
                    end_row = CASE
                        WHEN end_row IS NULL THEN NULL
                        WHEN end_row > ? THEN end_row - 1
                        ELSE end_row
                    END,
                    updated_at = ?
                WHERE spreadsheet_id = ?
                  AND sheet_id = ?
                  AND state NOT IN (?, ?, ?)
                """,
                (
                    deleted_row_number,
                    deleted_row_number,
                    now,
                    spreadsheet_id,
                    sheet_id,
                    BulkReservationState.REGISTERED.value,
                    BulkReservationState.CANCELLED.value,
                    BulkReservationState.FAILED.value,
                ),
            )
            reservations_shifted = cursor.rowcount
            await cursor.close()
            await self._record_application_event_in_connection(
                db,
                event_type="application_deleted",
                application_id=application_id,
                telegram_user_id=row["telegram_user_id"] if row is not None else None,
                metadata={
                    "spreadsheet_id": spreadsheet_id,
                    "sheet_id": sheet_id,
                    "deleted_row_number": deleted_row_number,
                },
                created_at=now,
            )
            await db.commit()
        return {
            "submitted_deleted": submitted_deleted,
            "notification_outbox_deleted": notification_deleted,
            "dashboard_outbox_deleted": dashboard_deleted,
            "submitted_shifted": submitted_shifted,
            "bulk_reservations_shifted": reservations_shifted,
        }

    @staticmethod
    async def _delete_notification_refs_in_connection(
        db: aiosqlite.Connection,
        application_id: str,
    ) -> int:
        cursor = await db.execute(
            """
            DELETE FROM notification_outbox
            WHERE COALESCE(snapshot_json, '') LIKE ?
               OR COALESCE(dedupe_key, '') LIKE ?
               OR COALESCE(html, '') LIKE ?
            """,
            (f"%{application_id}%", f"%{application_id}%", f"%{application_id}%"),
        )
        deleted = cursor.rowcount
        await cursor.close()
        return deleted

    @staticmethod
    async def _update_submitted_application_in_connection(
        db: aiosqlite.Connection,
        update: dict[str, Any],
        now: str,
    ) -> None:
        await db.execute(
            """
            UPDATE submitted_applications
            SET spreadsheet_id = COALESCE(?, spreadsheet_id),
                sheet_id = COALESCE(?, sheet_id),
                sheet_name = ?,
                last_known_status = ?,
                last_seen_row_number = ?,
                last_seen_editor = ?,
                last_seen_editor_comment = ?,
                last_seen_final_answer = ?,
                last_seen_scriptwriter_response = ?,
                pending_editor_comment = ?,
                pending_editor_comment_seen_count = ?,
                pending_scriptwriter_response = ?,
                pending_scriptwriter_response_seen_count = ?,
                change_type = COALESCE(?, change_type),
                polling_state = ?,
                not_found_count = 0,
                last_not_found_at = NULL,
                next_status_check_at = NULL,
                deletion_seen_count = 0,
                deletion_last_seen_at = NULL,
                deletion_error = NULL,
                updated_at = ?
            WHERE application_id = ?
            """,
            (
                update.get("spreadsheet_id"),
                update.get("sheet_id"),
                update["sheet_name"],
                update["last_known_status"],
                update.get("last_seen_row_number"),
                update.get("last_seen_editor"),
                update.get("last_seen_editor_comment"),
                update.get("last_seen_final_answer"),
                update.get("last_seen_scriptwriter_response"),
                update.get("pending_editor_comment"),
                update.get("pending_editor_comment_seen_count", 0),
                update.get("pending_scriptwriter_response"),
                update.get("pending_scriptwriter_response_seen_count", 0),
                update.get("change_type"),
                StatusPollingState.ACTIVE.value,
                now,
                update["application_id"],
            ),
        )

    async def _update_fields(self, telegram_user_id: int, values: dict[str, Any]) -> None:
        values = {**values, "updated_at": utc_now_iso()}
        assignments = ", ".join(f"{field} = ?" for field in values)
        params = [*values.values(), telegram_user_id]

        async with self._connection() as db:
            await db.execute(
                f"UPDATE drafts SET {assignments} WHERE telegram_user_id = ?",
                params,
            )
            await db.commit()

    @staticmethod
    def _draft_from_row(row: aiosqlite.Row) -> Draft:
        return Draft(
            telegram_user_id=row["telegram_user_id"],
            current_step=Step(row["current_step"]),
            application_id=row["application_id"],
            direction=row["direction"],
            answer_type=row["answer_type"],
            is_urgent=_row_bool(row["is_urgent"]),
            application_type=row["application_type"] or "Одиночная",
            change_type=row["change_type"],
            author_name=row["author_name"],
            intent=row["intent"],
            scriptwriter=row["scriptwriter"],
            reason=row["reason"],
            raw_change_description=row["raw_change_description"],
            formatted_change_description=row["formatted_change_description"],
            source_text=row["source_text"],
            source_text_formatting_json=row["source_text_formatting_json"],
            chip_text_before=row["chip_text_before"],
            chip_text_before_formatting_json=row["chip_text_before_formatting_json"],
            chip_text=row["chip_text"],
            chip_text_formatting_json=row["chip_text_formatting_json"],
            chip_after_text_action=row["chip_after_text_action"],
            chip_text_after=row["chip_text_after"],
            chip_text_after_formatting_json=row["chip_text_after_formatting_json"],
            priority=row["priority"],
            llm_check_status=row["llm_check_status"],
            llm_score=row["llm_score"],
            clarification_count=row["clarification_count"],
            submission_state=row["submission_state"] or SubmissionState.DRAFT.value,
            submission_started_at=row["submission_started_at"],
            submission_spreadsheet_id=row["submission_spreadsheet_id"],
            submission_sheet_name=row["submission_sheet_name"],
            submission_sheet_id=row["submission_sheet_id"],
            submission_row_number=row["submission_row_number"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _settings_from_row(row: aiosqlite.Row) -> UserSettings:
        return UserSettings(
            telegram_user_id=row["telegram_user_id"],
            default_direction=row["default_direction"],
            default_intent=row["default_intent"],
            default_scriptwriter=row["default_scriptwriter"],
            pending_action=row["pending_action"],
            active_chat_id=row["active_chat_id"],
            active_message_id=row["active_message_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _submitted_from_row(row: aiosqlite.Row) -> SubmittedApplication:
        return SubmittedApplication(
            application_id=row["application_id"],
            telegram_user_id=row["telegram_user_id"],
            sheet_name=row["sheet_name"],
            last_known_status=row["last_known_status"],
            spreadsheet_id=row["spreadsheet_id"],
            sheet_id=row["sheet_id"],
            direction=row["direction"],
            answer_type=row["answer_type"],
            application_type=row["application_type"],
            change_type=row["change_type"],
            is_urgent=_row_bool(row["is_urgent"]),
            batch_id=row["batch_id"],
            last_seen_row_number=row["last_seen_row_number"],
            last_seen_editor=row["last_seen_editor"],
            last_seen_editor_comment=row["last_seen_editor_comment"],
            last_seen_final_answer=row["last_seen_final_answer"],
            last_seen_scriptwriter_response=row["last_seen_scriptwriter_response"],
            pending_editor_comment=row["pending_editor_comment"],
            pending_editor_comment_seen_count=row["pending_editor_comment_seen_count"] or 0,
            pending_scriptwriter_response=row["pending_scriptwriter_response"],
            pending_scriptwriter_response_seen_count=(
                row["pending_scriptwriter_response_seen_count"] or 0
            ),
            submitted_at=row["submitted_at"],
            polling_state=row["polling_state"] or StatusPollingState.ACTIVE.value,
            not_found_count=row["not_found_count"] or 0,
            last_not_found_at=row["last_not_found_at"],
            next_status_check_at=row["next_status_check_at"],
            deletion_seen_count=row["deletion_seen_count"] or 0,
            deletion_last_seen_at=row["deletion_last_seen_at"],
            deletion_error=row["deletion_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _application_event_from_row(row: aiosqlite.Row) -> ApplicationEvent:
        return ApplicationEvent(
            id=row["id"],
            application_id=row["application_id"],
            telegram_user_id=row["telegram_user_id"],
            event_type=row["event_type"],
            event_at=row["event_at"],
            old_value=row["old_value"],
            new_value=row["new_value"],
            metadata_json=row["metadata_json"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _bulk_batch_from_row(row: aiosqlite.Row) -> BulkBatch:
        return BulkBatch(
            batch_id=row["batch_id"],
            telegram_user_id=row["telegram_user_id"],
            spreadsheet_id=row["spreadsheet_id"] or "",
            direction=row["direction"] or "",
            sheet_name=row["sheet_name"],
            sheet_id=row["sheet_id"],
            start_row=row["start_row"],
            data_start_row=row["data_start_row"],
            reserved_rows=row["reserved_rows"],
            data_end_row=row["data_end_row"],
            registration_state=(
                row["registration_state"] or BulkRegistrationState.DRAFT.value
            ),
            registration_started_at=row["registration_started_at"],
            registered_count=row["registered_count"] or 0,
            status_schema_version=row["status_schema_version"] or 1,
            batch_status=row["batch_status"] or "Новая пачка",
            last_known_batch_status=row["last_known_batch_status"] or "Новая пачка",
            last_seen_final_answers_digest_at=row["last_seen_final_answers_digest_at"],
            location_state=row["location_state"] or BulkBatchLocationState.KNOWN.value,
            location_miss_count=row["location_miss_count"] or 0,
            last_location_search_at=row["last_location_search_at"],
            next_location_search_at=row["next_location_search_at"],
            last_location_error=row["last_location_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _notification_outbox_from_row(row: aiosqlite.Row) -> NotificationOutboxItem:
        return NotificationOutboxItem(
            event_id=row["event_id"],
            dedupe_key=row["dedupe_key"],
            telegram_user_id=row["telegram_user_id"],
            event_type=row["event_type"],
            snapshot_json=row["snapshot_json"],
            html=row["html"],
            chunk_index=row["chunk_index"],
            chunk_count=row["chunk_count"],
            state=row["state"],
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            sending_started_at=row["sending_started_at"],
            last_error=row["last_error"],
            telegram_message_id=row["telegram_message_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _dashboard_outbox_from_row(row: aiosqlite.Row) -> DashboardOutboxItem:
        return DashboardOutboxItem(
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            snapshot_json=row["snapshot_json"],
            state=row["state"],
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            sending_started_at=row["sending_started_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _bulk_creation_from_row(row: aiosqlite.Row) -> BulkCreationRequest:
        return BulkCreationRequest(
            idempotency_key=row["idempotency_key"],
            telegram_user_id=row["telegram_user_id"],
            state=row["state"],
            batch_id=row["batch_id"],
            direction=row["direction"],
            insert_url=row["insert_url"],
            last_error=row["last_error"],
            started_at=row["started_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _bulk_reservation_from_row(row: aiosqlite.Row) -> BulkReservation:
        return BulkReservation(
            reservation_id=row["reservation_id"],
            idempotency_key=row["idempotency_key"],
            telegram_user_id=row["telegram_user_id"],
            state=row["state"],
            direction=row["direction"],
            target_kind=row["target_kind"],
            change_type=row["change_type"],
            requested_count=row["requested_count"],
            spreadsheet_id=row["spreadsheet_id"],
            sheet_id=row["sheet_id"],
            sheet_name=row["sheet_name"],
            start_row=row["start_row"],
            end_row=row["end_row"],
            insert_url=row["insert_url"],
            registered_count=row["registered_count"] or 0,
            last_error=row["last_error"],
            started_at=row["started_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            registered_at=row["registered_at"],
        )

    async def _update_user_settings(
        self,
        telegram_user_id: int,
        values: dict[str, Any],
    ) -> None:
        values = {**values, "updated_at": utc_now_iso()}
        assignments = ", ".join(f"{field} = ?" for field in values)
        params = [*values.values(), telegram_user_id]

        async with self._connection() as db:
            await db.execute(
                f"UPDATE user_settings SET {assignments} WHERE telegram_user_id = ?",
                params,
            )
            await db.commit()

    @staticmethod
    async def _upsert_dashboard_projection_in_connection(
        db: aiosqlite.Connection,
        *,
        entity_type: str,
        entity_id: str,
        snapshot: dict[str, Any],
        now: str,
    ) -> None:
        await db.execute(
            """
            INSERT INTO dashboard_outbox (
                entity_type, entity_id, snapshot_json, state, attempts,
                next_attempt_at, sending_started_at, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 0, ?, NULL, NULL, ?, ?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                snapshot_json = excluded.snapshot_json,
                state = excluded.state,
                attempts = 0,
                next_attempt_at = excluded.next_attempt_at,
                sending_started_at = NULL,
                last_error = NULL,
                updated_at = excluded.updated_at
            """,
            (
                entity_type,
                entity_id,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                DashboardOutboxState.PENDING.value,
                now,
                now,
                now,
            ),
        )

    @staticmethod
    async def _ensure_column(db: aiosqlite.Connection, name: str, definition: str) -> None:
        cursor = await db.execute("PRAGMA table_info(drafts)")
        rows = await cursor.fetchall()
        await cursor.close()
        existing_columns = {row[1] for row in rows}
        if name not in existing_columns:
            await db.execute(f"ALTER TABLE drafts ADD COLUMN {name} {definition}")

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Открыть соединение с WAL-совместимым ожиданием блокировок."""
        db = await aiosqlite.connect(self.sqlite_path, timeout=10)
        try:
            await db.execute("PRAGMA busy_timeout=10000")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA synchronous=NORMAL")
            yield db
        finally:
            await db.close()

    @staticmethod
    async def _migrate_llm_completeness_check(db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        version = int(row[0]) if row else 0
        if version >= 1:
            return
        await db.execute(
            """
            UPDATE drafts
            SET formatted_change_description = raw_change_description,
                llm_score = NULL
            WHERE current_step != ?
              AND raw_change_description IS NOT NULL
            """,
            (Step.COMPLETED.value,),
        )
        await db.execute("PRAGMA user_version = 1")

    @staticmethod
    async def _ensure_user_settings_column(
        db: aiosqlite.Connection,
        name: str,
        definition: str,
    ) -> None:
        cursor = await db.execute("PRAGMA table_info(user_settings)")
        rows = await cursor.fetchall()
        await cursor.close()
        existing_columns = {row[1] for row in rows}
        if name not in existing_columns:
            await db.execute(f"ALTER TABLE user_settings ADD COLUMN {name} {definition}")

    @staticmethod
    async def _ensure_submitted_applications_column(
        db: aiosqlite.Connection,
        name: str,
        definition: str,
    ) -> None:
        cursor = await db.execute("PRAGMA table_info(submitted_applications)")
        rows = await cursor.fetchall()
        await cursor.close()
        existing_columns = {row[1] for row in rows}
        if name not in existing_columns:
            await db.execute(f"ALTER TABLE submitted_applications ADD COLUMN {name} {definition}")

    @staticmethod
    async def _ensure_bulk_batches_column(
        db: aiosqlite.Connection,
        name: str,
        definition: str,
    ) -> bool:
        cursor = await db.execute("PRAGMA table_info(bulk_batches)")
        rows = await cursor.fetchall()
        await cursor.close()
        existing_columns = {row[1] for row in rows}
        if name not in existing_columns:
            await db.execute(f"ALTER TABLE bulk_batches ADD COLUMN {name} {definition}")
            return True
        return False


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "y", "on", "да"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "нет"}:
        return False
    return None
