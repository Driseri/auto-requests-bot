from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from app.models import (
    BulkCreationRequest,
    BulkCreationState,
    BulkBatch,
    BulkBatchStatus,
    BulkRegistrationState,
    DashboardOutboxItem,
    DashboardOutboxState,
    Draft,
    NotificationOutboxItem,
    NotificationOutboxState,
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
                    is_urgent INTEGER,
                    batch_id TEXT,
                    last_seen_row_number INTEGER,
                    last_seen_editor TEXT,
                    last_seen_editor_comment TEXT,
                    last_seen_final_answer TEXT,
                    submitted_at TEXT,
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
            await self._ensure_submitted_applications_column(db, "is_urgent", "INTEGER")
            await self._ensure_submitted_applications_column(db, "last_seen_final_answer", "TEXT")
            await self._ensure_submitted_applications_column(db, "last_seen_editor", "TEXT")
            await self._ensure_submitted_applications_column(db, "submitted_at", "TEXT")
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
            await self._ensure_bulk_batches_column(db, "spreadsheet_id", "TEXT")
            await self._ensure_bulk_batches_column(db, "direction", "TEXT")
            await self._ensure_bulk_batches_column(db, "batch_status", "TEXT")
            await self._ensure_bulk_batches_column(db, "last_known_batch_status", "TEXT")
            await self._ensure_bulk_batches_column(db, "last_seen_final_answers_digest_at", "TEXT")
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
        submitted_at: str | None = None,
        dashboard_projection: dict[str, Any] | None = None,
    ) -> Draft:
        """Одной транзакцией сохранить tracking и отметить черновик отправленным."""
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO submitted_applications (
                    application_id, telegram_user_id, spreadsheet_id, sheet_id, sheet_name,
                    last_known_status, direction, answer_type, application_type, is_urgent,
                    last_seen_row_number, submitted_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(application_id) DO UPDATE SET
                    spreadsheet_id = excluded.spreadsheet_id,
                    sheet_id = excluded.sheet_id,
                    sheet_name = excluded.sheet_name,
                    submitted_at = COALESCE(
                        submitted_applications.submitted_at,
                        excluded.submitted_at
                    ),
                    last_seen_row_number = COALESCE(
                        excluded.last_seen_row_number,
                        submitted_applications.last_seen_row_number
                    ),
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
                    1 if is_urgent else 0 if is_urgent is not None else None,
                    row_number,
                    submitted_at,
                    now,
                    now,
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
        is_urgent: bool | None = None,
        batch_id: str | None = None,
        last_seen_row_number: int | None = None,
        last_seen_editor: str | None = None,
        last_seen_editor_comment: str | None = None,
        last_seen_final_answer: str | None = None,
        submitted_at: str | None = None,
    ) -> SubmittedApplication:
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute(
                """
                INSERT INTO submitted_applications (
                    application_id, telegram_user_id, spreadsheet_id, sheet_id, sheet_name,
                    last_known_status, direction, answer_type, application_type, is_urgent,
                    batch_id, last_seen_row_number, last_seen_editor,
                    last_seen_editor_comment, last_seen_final_answer, submitted_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(application_id) DO UPDATE SET
                    telegram_user_id = excluded.telegram_user_id,
                    spreadsheet_id = excluded.spreadsheet_id,
                    sheet_id = excluded.sheet_id,
                    sheet_name = excluded.sheet_name,
                    last_known_status = excluded.last_known_status,
                    direction = excluded.direction,
                    answer_type = excluded.answer_type,
                    application_type = excluded.application_type,
                    is_urgent = excluded.is_urgent,
                    batch_id = excluded.batch_id,
                    last_seen_row_number = excluded.last_seen_row_number,
                    last_seen_editor = excluded.last_seen_editor,
                    last_seen_editor_comment = excluded.last_seen_editor_comment,
                    last_seen_final_answer = excluded.last_seen_final_answer,
                    submitted_at = COALESCE(
                        submitted_applications.submitted_at,
                        excluded.submitted_at
                    ),
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
                    None if is_urgent is None else (1 if is_urgent else 0),
                    batch_id,
                    last_seen_row_number,
                    last_seen_editor,
                    last_seen_editor_comment,
                    last_seen_final_answer,
                    submitted_at,
                    now,
                    now,
                ),
            )
            await db.commit()
        submitted = await self.get_submitted_application(application_id)
        if submitted is None:
            raise LookupError(f"Submitted application not found: {application_id}")
        return submitted

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

    async def list_submitted_applications(self) -> list[SubmittedApplication]:
        async with self._connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM submitted_applications ORDER BY created_at ASC"
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
                    utc_now_iso(),
                    application_id,
                ),
            )
            await db.commit()

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
    ) -> bool:
        """Atomically persist an observed event and advance its tracking state."""
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT 1 FROM notification_outbox WHERE dedupe_key LIKE ? LIMIT 1",
                (f"{dedupe_key}:%",),
            )
            exists = await cursor.fetchone()
            await cursor.close()
            if exists is not None:
                await db.commit()
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

            for update in application_updates or []:
                await self._update_submitted_application_in_connection(db, update, now)
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

    async def update_application_tracking_batch(
        self,
        updates: list[dict[str, Any]],
        *,
        dashboard_projections: list[dict[str, Any]] | None = None,
    ) -> None:
        now = utc_now_iso()
        async with self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            for update in updates:
                await self._update_submitted_application_in_connection(db, update, now)
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
            is_urgent=_row_bool(row["is_urgent"]),
            batch_id=row["batch_id"],
            last_seen_row_number=row["last_seen_row_number"],
            last_seen_editor=row["last_seen_editor"],
            last_seen_editor_comment=row["last_seen_editor_comment"],
            last_seen_final_answer=row["last_seen_final_answer"],
            submitted_at=row["submitted_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
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
