from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

from app.models import (
    BulkBatch,
    Draft,
    Step,
    SubmittedApplication,
    TEXT_FIELDS,
    UserSettings,
    generate_application_id,
    generate_batch_id,
    utc_now_iso,
)


class DraftRepository:
    def __init__(self, sqlite_path: str) -> None:
        self.sqlite_path = sqlite_path

    async def init(self) -> None:
        db_path = Path(self.sqlite_path)
        if db_path.parent and str(db_path.parent) != ".":
            db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.sqlite_path) as db:
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
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_settings (
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
                    status_schema_version INTEGER NOT NULL DEFAULT 1,
                    batch_status TEXT,
                    last_known_batch_status TEXT,
                    last_seen_final_answers_digest_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
            await self._ensure_user_settings_column(db, "pending_action", "TEXT")
            await self._ensure_user_settings_column(db, "default_direction", "TEXT")
            await db.commit()

    async def get_or_create(self, telegram_user_id: int) -> Draft:
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
        async with aiosqlite.connect(self.sqlite_path) as db:
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
        async with aiosqlite.connect(self.sqlite_path) as db:
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
        async with aiosqlite.connect(self.sqlite_path) as db:
            await db.execute("DELETE FROM drafts WHERE telegram_user_id = ?", (telegram_user_id,))
            await db.commit()

    async def complete(self, telegram_user_id: int) -> Draft:
        return await self.set_step(telegram_user_id, Step.COMPLETED)

    async def get_user_settings(self, telegram_user_id: int) -> UserSettings:
        async with aiosqlite.connect(self.sqlite_path) as db:
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
        async with aiosqlite.connect(self.sqlite_path) as db:
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
    ) -> SubmittedApplication:
        now = utc_now_iso()
        async with aiosqlite.connect(self.sqlite_path) as db:
            await db.execute(
                """
                INSERT INTO submitted_applications (
                    application_id, telegram_user_id, spreadsheet_id, sheet_id, sheet_name,
                    last_known_status, direction, answer_type, application_type, is_urgent,
                    batch_id, last_seen_row_number, last_seen_editor,
                    last_seen_editor_comment, last_seen_final_answer, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        async with aiosqlite.connect(self.sqlite_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM submitted_applications WHERE application_id = ?",
                (application_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._submitted_from_row(row) if row is not None else None

    async def list_submitted_applications(self) -> list[SubmittedApplication]:
        async with aiosqlite.connect(self.sqlite_path) as db:
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
        async with aiosqlite.connect(self.sqlite_path) as db:
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
        spreadsheet_id: str = "",
        direction: str = "",
        batch_status: str | None = None,
        status_schema_version: int = 2,
    ) -> BulkBatch:
        batch_id = batch_id or generate_batch_id()
        now = utc_now_iso()
        async with aiosqlite.connect(self.sqlite_path) as db:
            await db.execute(
                """
                INSERT INTO bulk_batches (
                    batch_id, telegram_user_id, spreadsheet_id, direction, sheet_name, sheet_id,
                    start_row, data_start_row, reserved_rows, batch_status,
                    last_known_batch_status, status_schema_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    telegram_user_id = excluded.telegram_user_id,
                    spreadsheet_id = excluded.spreadsheet_id,
                    direction = excluded.direction,
                    sheet_name = excluded.sheet_name,
                    sheet_id = excluded.sheet_id,
                    start_row = excluded.start_row,
                    data_start_row = excluded.data_start_row,
                    reserved_rows = excluded.reserved_rows,
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
        async with aiosqlite.connect(self.sqlite_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM bulk_batches WHERE batch_id = ?",
                (batch_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._bulk_batch_from_row(row) if row is not None else None

    async def list_bulk_batches(self) -> list[BulkBatch]:
        async with aiosqlite.connect(self.sqlite_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM bulk_batches ORDER BY created_at ASC")
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._bulk_batch_from_row(row) for row in rows]

    async def update_bulk_batch_status(
        self,
        batch_id: str,
        *,
        batch_status: str,
        last_known_batch_status: str,
    ) -> None:
        async with aiosqlite.connect(self.sqlite_path) as db:
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
                    utc_now_iso(),
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
        async with aiosqlite.connect(self.sqlite_path) as db:
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

    async def _update_fields(self, telegram_user_id: int, values: dict[str, Any]) -> None:
        values = {**values, "updated_at": utc_now_iso()}
        assignments = ", ".join(f"{field} = ?" for field in values)
        params = [*values.values(), telegram_user_id]

        async with aiosqlite.connect(self.sqlite_path) as db:
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
            status_schema_version=row["status_schema_version"] or 1,
            batch_status=row["batch_status"] or "Новая пачка",
            last_known_batch_status=row["last_known_batch_status"] or "Новая пачка",
            last_seen_final_answers_digest_at=row["last_seen_final_answers_digest_at"],
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

        async with aiosqlite.connect(self.sqlite_path) as db:
            await db.execute(
                f"UPDATE user_settings SET {assignments} WHERE telegram_user_id = ?",
                params,
            )
            await db.commit()

    @staticmethod
    async def _ensure_column(db: aiosqlite.Connection, name: str, definition: str) -> None:
        cursor = await db.execute("PRAGMA table_info(drafts)")
        rows = await cursor.fetchall()
        await cursor.close()
        existing_columns = {row[1] for row in rows}
        if name not in existing_columns:
            await db.execute(f"ALTER TABLE drafts ADD COLUMN {name} {definition}")

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
    ) -> None:
        cursor = await db.execute("PRAGMA table_info(bulk_batches)")
        rows = await cursor.fetchall()
        await cursor.close()
        existing_columns = {row[1] for row in rows}
        if name not in existing_columns:
            await db.execute(f"ALTER TABLE bulk_batches ADD COLUMN {name} {definition}")


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
