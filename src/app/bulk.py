from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from app.google_api import GoogleApiRetryConfig, execute_with_retry_async
from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
    BulkApplicationStatus,
    BulkBatch,
    BulkBatchStatus,
    BulkRegistrationState,
    ChangeType,
    Direction,
    generate_application_id,
    generate_batch_id,
    utc_now_iso,
)
from app.repository import DraftRepository
from app.scheduling import DEFAULT_TIMEZONE
from app.sheet_dates import google_sheets_date_cell, utc_iso
from app.submission import (
    DashboardSyncService,
    DirectionSpreadsheetConfig,
    EDITOR_NOT_SELECTED,
    dashboard_bulk_batch_row,
    dashboard_projection,
    build_google_sheets_api,
    quote_sheet_name,
    _editor_data_validation_rule,
)


DEFAULT_BULK_RESERVED_ROWS = 100
BULK_BATCH_SPACING_ROWS = 2
DEFAULT_BULK_REGISTRATION_STALE_SECONDS = 600
_BATCH_HEADER_PATTERN = re.compile(r"^Пачка (BATCH-[0-9A-F]+)$")
CURRENT_BULK_INPUT_HEADERS = [
    "Тип ответа",
    "Интент",
    "Закрепленный сценарист",
    "Причина изменений",
    "Суть изменений",
    "Исходный текст",
    "Тип изменения",
]
CURRENT_BULK_SERVICE_HEADERS = [
    "ID заявки",
    "Статус",
    "Редактор",
    "Вопросы/комментарии редактора",
    "Ответ/комментарий сценариста",
    "Итоговый ответ редактора",
]
LEGACY_BULK_SERVICE_HEADERS = [
    "ID заявки",
    "Статус",
    "Вопросы/комментарии редактора",
    "Ответ/комментарий сценариста",
    "Итоговый ответ редактора",
]
BULK_INPUT_HEADERS = [
    "Тип ответа",
    "Тип изменения",
    "Закрепленный сценарист",
    "Интент",
    "Причина изменений",
    "Суть изменений",
    "Исходный текст",
]
BULK_SERVICE_HEADERS = [
    "Итоговый ответ редактора",
    "Комментарий качества",
    "Вопросы/комментарии редактора",
    "Ответ сценариста",
    "Статус",
    "Редактор",
    "ID заявки",
]
LEGACY_BULK_STAGING_HEADERS = [
    *CURRENT_BULK_INPUT_HEADERS,
    *LEGACY_BULK_SERVICE_HEADERS,
]
CURRENT_BULK_STAGING_HEADERS = [
    *CURRENT_BULK_INPUT_HEADERS,
    *CURRENT_BULK_SERVICE_HEADERS,
]
BULK_STAGING_HEADERS = [*BULK_INPUT_HEADERS, *BULK_SERVICE_HEADERS]
BULK_STAGING_COLUMN_COUNT = len(BULK_STAGING_HEADERS)
BULK_STAGING_SHEET_NAME = "Массовый ввод"


@dataclass(slots=True)
class BulkBatchCreationResult:
    success: bool
    message: str
    batch: BulkBatch | None = None
    insert_url: str | None = None


@dataclass(slots=True)
class BulkRegistrationResult:
    success: bool
    message: str
    registered_count: int = 0
    retry_allowed: bool = True
    insert_url: str | None = None


class BulkBatchServiceProtocol(Protocol):
    async def create_batch(
        self,
        telegram_user_id: int,
        direction: str,
        batch_id: str | None = None,
    ) -> BulkBatchCreationResult:
        ...


class InMemoryBulkBatchService:
    def __init__(self) -> None:
        self.created: list[int] = []

    async def create_batch(
        self,
        telegram_user_id: int,
        direction: str,
        batch_id: str | None = None,
    ) -> BulkBatchCreationResult:
        self.created.append(telegram_user_id)
        batch = BulkBatch(
            batch_id=batch_id or generate_batch_id(),
            telegram_user_id=telegram_user_id,
            spreadsheet_id="test-spreadsheet",
            direction=direction,
            sheet_name=BULK_STAGING_SHEET_NAME,
            sheet_id=100,
            start_row=1,
            data_start_row=3,
            reserved_rows=DEFAULT_BULK_RESERVED_ROWS,
            data_end_row=2 + DEFAULT_BULK_RESERVED_ROWS,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
        return BulkBatchCreationResult(
            success=True,
            message="Место для массовой вставки создано.",
            batch=batch,
            insert_url="https://docs.google.com/spreadsheets/d/test/edit#gid=100&range=A3:G3",
        )


class GoogleSheetsBulkBatchService:
    """Создает изолированные секции массовых заявок в листах направлений."""

    def __init__(
        self,
        *,
        direction_spreadsheets: DirectionSpreadsheetConfig | None = None,
        spreadsheet_id: str = "",
        sheet_name: str = "",
        credentials_path: str,
        repository: DraftRepository,
        sheets_api: Any | None = None,
        reserved_rows: int = DEFAULT_BULK_RESERVED_ROWS,
        google_api_retry: GoogleApiRetryConfig = GoogleApiRetryConfig(),
        timezone_name: str = DEFAULT_TIMEZONE,
        clock: Callable[[], datetime] | None = None,
        application_editors: tuple[str, ...] = ("редактор 1", "редактор 2"),
    ) -> None:
        self.direction_spreadsheets = direction_spreadsheets or DirectionSpreadsheetConfig(
            fl_spreadsheet_id=spreadsheet_id,
            sme_spreadsheet_id=spreadsheet_id,
            ai_spreadsheet_id=spreadsheet_id,
            voice_collection_spreadsheet_id=spreadsheet_id,
        )
        self.legacy_sheet_name = sheet_name
        self.credentials_path = credentials_path
        self.repository = repository
        self._sheets_api = sheets_api
        self._external_sheets_api = sheets_api is not None
        self.google_api_retry = google_api_retry
        self.reserved_rows = reserved_rows
        self.application_editors = application_editors
        self.timezone_name = timezone_name
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._sheet_locks: dict[tuple[str, str], asyncio.Lock] = {}
        if self.reserved_rows <= 0:
            raise ValueError("reserved_rows must be greater than 0")

        if sheets_api is None and not Path(self.credentials_path).is_file():
            raise RuntimeError(f"Google credentials file not found: {self.credentials_path}")

    async def create_batch(
        self,
        telegram_user_id: int,
        direction: str,
        batch_id: str | None = None,
    ) -> BulkBatchCreationResult:
        """Создать секцию под блокировкой листа и сохранить ее точные границы."""
        spreadsheet_id = self.direction_spreadsheets.spreadsheet_id_for(direction)
        sheet_name = _bulk_sheet_name(direction)
        lock = self._sheet_locks.setdefault((spreadsheet_id, sheet_name), asyncio.Lock())
        try:
            async with lock:
                existing_batches = await self.repository.list_bulk_batches()
                batch_id = batch_id or generate_batch_id()
                created_at = self.clock()
                batch, insert_url = await execute_with_retry_async(
                    lambda: self._create_batch_sync(
                        telegram_user_id,
                        direction,
                        existing_batches,
                        batch_id,
                        created_at,
                    ),
                    config=self.google_api_retry,
                    operation_id=f"bulk-create:{telegram_user_id}:{direction}",
                    reset_client=self._reset_sheets_api,
                )
                saved_batch = await self.repository.save_bulk_batch(
                    batch_id=batch.batch_id,
                    telegram_user_id=batch.telegram_user_id,
                    spreadsheet_id=batch.spreadsheet_id,
                    direction=batch.direction,
                    sheet_name=batch.sheet_name,
                    sheet_id=batch.sheet_id,
                    start_row=batch.start_row,
                    data_start_row=batch.data_start_row,
                    reserved_rows=batch.reserved_rows,
                    data_end_row=batch.data_end_row,
                    status_schema_version=batch.status_schema_version,
                    created_at=batch.created_at,
                )
        except Exception as exc:
            return BulkBatchCreationResult(
                success=False,
                message=f"Не удалось создать место для массовой вставки. Причина: {exc}",
            )
        return BulkBatchCreationResult(
            success=True,
            message="Место для массовой вставки создано.",
            batch=saved_batch,
            insert_url=insert_url,
        )

    def _create_batch_sync(
        self,
        telegram_user_id: int,
        direction: str,
        existing_batches: list[BulkBatch],
        batch_id: str,
        created_at: datetime,
    ) -> tuple[BulkBatch, str]:
        api = self._get_sheets_api()
        spreadsheet_id = self.direction_spreadsheets.spreadsheet_id_for(direction)
        if not spreadsheet_id:
            raise RuntimeError("Для выбранного направления не задан ID Google-таблицы")
        sheet_name = _bulk_sheet_name(direction)
        sheet_id = self._get_or_create_sheet_id(api, spreadsheet_id, sheet_name, BULK_STAGING_COLUMN_COUNT)
        existing_rows = self._read_rows(api, spreadsheet_id, sheet_name, "A:N")
        existing_start_row = next(
            (
                row_number
                for row_number, row in enumerate(existing_rows, start=1)
                if _cell(row, 1).strip() == batch_id
            ),
            None,
        )
        if existing_start_row is not None:
            data_start_row = existing_start_row + 2
            batch = BulkBatch(
                batch_id=batch_id,
                telegram_user_id=telegram_user_id,
                spreadsheet_id=spreadsheet_id,
                direction=direction,
                sheet_name=sheet_name,
                sheet_id=sheet_id,
                start_row=existing_start_row,
                data_start_row=data_start_row,
                reserved_rows=self.reserved_rows,
                data_end_row=data_start_row + self.reserved_rows - 1,
                status_schema_version=2,
                created_at=utc_iso(created_at),
                updated_at=utc_iso(created_at),
            )
            return batch, self._insert_url(
                spreadsheet_id,
                sheet_id,
                data_start_row,
            )
        start_row = _next_batch_start_row(
            existing_rows=existing_rows,
            existing_batches=existing_batches,
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
        )
        data_start_row = start_row + 2
        data_end_row = data_start_row + self.reserved_rows - 1
        now = utc_iso(created_at)

        rows = [
            *[
                _empty_row()
                for _ in range(_spacing_rows_before_batch(existing_rows, start_row))
            ],
            _bulk_header_row(
                batch_id,
                telegram_user_id,
                created_at,
                direction,
                timezone_name=self.timezone_name,
            ),
            _bulk_column_header_row(),
            *[
                _bulk_input_row(self.application_editors)
                for _ in range(self.reserved_rows)
            ],
        ]
        api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "appendCells": {
                            "sheetId": sheet_id,
                            "rows": rows,
                            "fields": "userEnteredValue,userEnteredFormat,dataValidation",
                        }
                    },
                    *_bulk_batch_status_format_rules(sheet_id, start_row),
                    _bulk_active_group_border_request(
                        sheet_id,
                        start_row=start_row,
                        end_row=data_end_row,
                    ),
                ]
            },
        ).execute()

        batch = BulkBatch(
            batch_id=batch_id,
            telegram_user_id=telegram_user_id,
            spreadsheet_id=spreadsheet_id,
            direction=direction,
            sheet_name=sheet_name,
            sheet_id=sheet_id,
            start_row=start_row,
            data_start_row=data_start_row,
            reserved_rows=self.reserved_rows,
            data_end_row=data_end_row,
            status_schema_version=2,
            created_at=now,
            updated_at=now,
        )
        return batch, self._insert_url(spreadsheet_id, sheet_id, data_start_row)

    def _get_or_create_sheet_id(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_name: str,
        column_count: int,
    ) -> int:
        metadata = api.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties",
        ).execute()
        for sheet in metadata.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("title") == sheet_name:
                current_columns = int(
                    properties.get("gridProperties", {}).get("columnCount", column_count)
                )
                if current_columns < column_count:
                    api.spreadsheets().batchUpdate(
                        spreadsheetId=spreadsheet_id,
                        body={
                            "requests": [
                                {
                                    "updateSheetProperties": {
                                        "properties": {
                                            "sheetId": properties["sheetId"],
                                            "gridProperties": {
                                                "columnCount": column_count,
                                            },
                                        },
                                        "fields": "gridProperties.columnCount",
                                    }
                                }
                            ]
                        },
                    ).execute()
                return int(properties["sheetId"])

        result = api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": sheet_name,
                                "gridProperties": {
                                    "rowCount": 1000,
                                    "columnCount": column_count,
                                },
                            }
                        }
                    }
                ]
            },
        ).execute()
        return int(result["replies"][0]["addSheet"]["properties"]["sheetId"])

    def _read_rows(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_name: str,
        range_suffix: str,
    ) -> list[list[Any]]:
        result = api.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!{range_suffix}",
            majorDimension="ROWS",
        ).execute()
        return result.get("values", [])

    def _insert_url(self, spreadsheet_id: str, sheet_id: int, data_start_row: int) -> str:
        return (
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
            f"#gid={sheet_id}&range=A{data_start_row}:G{data_start_row}"
        )

    def _get_sheets_api(self) -> Any:
        if self._sheets_api is None:
            self._sheets_api = build_google_sheets_api(self.credentials_path)
        return self._sheets_api

    def _reset_sheets_api(self) -> None:
        if not self._external_sheets_api:
            self._sheets_api = None


class BulkApplicationRegistrar:
    """Идемпотентно регистрирует заполненные строки одной массовой заявки."""

    def __init__(
        self,
        *,
        repository: DraftRepository,
        spreadsheet_id: str,
        credentials_path: str,
        sheets_api: Any | None = None,
        dashboard_sync: DashboardSyncService | None = None,
        application_editors: tuple[str, ...] = ("редактор 1", "редактор 2"),
        registration_stale_seconds: int = DEFAULT_BULK_REGISTRATION_STALE_SECONDS,
        google_api_retry: GoogleApiRetryConfig = GoogleApiRetryConfig(),
    ) -> None:
        self.repository = repository
        self.spreadsheet_id = spreadsheet_id
        self.credentials_path = credentials_path
        self._sheets_api = sheets_api
        self._external_sheets_api = sheets_api is not None
        self.google_api_retry = google_api_retry
        self.dashboard_sync = dashboard_sync
        self.application_editors = application_editors
        self.registration_stale_seconds = registration_stale_seconds
        self._batch_locks: dict[str, asyncio.Lock] = {}
        if self.registration_stale_seconds <= 0:
            raise ValueError("registration_stale_seconds must be greater than 0")

    async def register_batch(self, batch_id: str, telegram_user_id: int) -> BulkRegistrationResult:
        """Зарегистрировать пачку под per-batch lock, не выходя за ее диапазон."""
        lock = self._batch_locks.setdefault(batch_id, asyncio.Lock())
        async with lock:
            return await self._register_batch_locked(batch_id, telegram_user_id)

    async def _register_batch_locked(
        self,
        batch_id: str,
        telegram_user_id: int,
    ) -> BulkRegistrationResult:
        batch = await self.repository.get_bulk_batch(batch_id)
        if batch is None:
            return BulkRegistrationResult(success=False, message="Массовая заявка не найдена.")
        insert_url = self._batch_insert_url(batch)
        if batch.telegram_user_id != telegram_user_id:
            return BulkRegistrationResult(
                success=False,
                message="Эту массовую заявку может подтвердить только ее автор.",
            )

        claim = await self.repository.claim_bulk_batch_registration(
            batch_id,
            stale_after_seconds=self.registration_stale_seconds,
        )
        if claim == BulkRegistrationState.REGISTERED.value:
            return BulkRegistrationResult(
                success=True,
                message=(
                    "Массовая заявка уже зарегистрирована. "
                    f"Строк зарегистрировано: {batch.registered_count}."
                ),
                registered_count=batch.registered_count,
                retry_allowed=False,
                insert_url=insert_url,
            )
        if claim == BulkRegistrationState.REGISTERING.value:
            return BulkRegistrationResult(
                success=False,
                message="Массовая заявка уже регистрируется. Дождитесь завершения обработки.",
                retry_allowed=False,
                insert_url=insert_url,
            )

        try:
            result = await self._register_claimed_batch(batch)
        except Exception as exc:
            await self.repository.release_bulk_batch_registration(batch_id)
            return BulkRegistrationResult(
                success=False,
                message=f"Не удалось зарегистрировать массовую заявку. Причина: {exc}",
                insert_url=insert_url,
            )
        if not result.success:
            await self.repository.release_bulk_batch_registration(batch_id)
        result.insert_url = result.insert_url or insert_url
        return result

    async def _register_claimed_batch(self, batch: BulkBatch) -> BulkRegistrationResult:
        """Проверить всю пачку и завершить регистрацию без частичного успеха."""
        layout = await self._run_google(
            lambda: self._read_batch_layout(self._get_sheets_api(), batch),
            f"bulk-layout:{batch.batch_id}",
        )
        if layout is None:
            return BulkRegistrationResult(
                success=False,
                message="Структура колонок массовой заявки не соответствует поддерживаемым схемам.",
            )
        if await self._run_google(
            lambda: self._has_overflow_data(self._get_sheets_api(), batch, layout),
            f"bulk-overflow:{batch.batch_id}",
        ):
            return BulkRegistrationResult(
                success=False,
                message=(
                    "Данные выходят за выделенный диапазон массовой заявки. "
                    f"Допустимые строки: {batch.data_start_row}-{_batch_data_end_row(batch)}."
                ),
            )
        registered = 0
        user_rows = 0
        actual_rows = 0
        rows = await self._run_google(
            lambda: self._read_batch_rows(self._get_sheets_api(), batch),
            f"bulk-read:{batch.batch_id}",
        )
        invalid_change_type_rows: list[int] = []
        normalized_change_types: list[tuple[int, str]] = []
        for offset, row in enumerate(rows):
            if not _has_user_bulk_data(row, layout["user_indices"]):
                continue
            row_number = batch.data_start_row + offset
            raw_change_type = _cell(row, layout["change_type"])
            normalized_change_type = ChangeType.normalize(raw_change_type)
            if normalized_change_type is None:
                invalid_change_type_rows.append(row_number)
                continue
            row.extend([""] * (layout["change_type"] + 1 - len(row)))
            row[layout["change_type"]] = normalized_change_type.value
            if raw_change_type.strip() != normalized_change_type.value:
                normalized_change_types.append((row_number, normalized_change_type.value))

        if invalid_change_type_rows:
            row_list = ", ".join(str(row_number) for row_number in invalid_change_type_rows)
            return BulkRegistrationResult(
                success=False,
                message=(
                    "Массовая заявка не зарегистрирована. "
                    "Укажите ADD, EDIT или CHIPS в колонке «Тип изменения» "
                    f"для строк: {row_list}."
                ),
            )
        if normalized_change_types:
            await self._run_google(
                lambda: self._write_normalized_change_types(
                    self._get_sheets_api(),
                    batch,
                    normalized_change_types,
                    layout,
                ),
                f"bulk-normalize:{batch.batch_id}",
            )
        for offset, row in enumerate(rows):
            staging_row_number = batch.data_start_row + offset
            if not _has_user_bulk_data(row, layout["user_indices"]):
                continue
            user_rows += 1
            actual_rows = offset + 1
            application_id = _cell(row, layout["application_id"]).strip()
            if application_id:
                existing = await self.repository.get_submitted_application(application_id)
                if existing is None:
                    await self._save_existing_application(
                        batch, row, staging_row_number, application_id, layout
                    )
                    registered += 1
                continue

            application_id = generate_application_id()
            await self._run_google(
                lambda: self._write_registration_cells(
                    self._get_sheets_api(),
                    batch,
                    staging_row_number,
                    application_id,
                    layout,
                ),
                f"bulk-register:{batch.batch_id}:{staging_row_number}",
            )
            await self._save_new_application(
                batch, row, staging_row_number, application_id, layout
            )
            registered += 1

        if user_rows == 0:
            return BulkRegistrationResult(
                success=False,
                message=(
                    "В массовой заявке не найдены заполненные строки. "
                    "Заполните данные в Google Sheets и нажмите «Заявка заполнена» еще раз."
                ),
            )
        data_end_row = batch.data_start_row + actual_rows - 1
        await self._run_google(
            lambda: self._organize_registered_batch_rows(
                self._get_sheets_api(),
                batch,
                actual_rows,
            ),
            f"bulk-organize-rows:{batch.batch_id}",
        )
        editors = await self._batch_editors(batch.batch_id)
        await self.repository.complete_bulk_batch_registration(
            batch.batch_id,
            registered_count=user_rows,
            data_end_row=data_end_row,
            dashboard_projection=(
                dashboard_projection(
                    dashboard_bulk_batch_row(
                        batch=batch,
                        status=batch.batch_status,
                        row_link=self._batch_row_link(batch),
                        final_answer_present=_has_final_answer(
                            rows,
                            final_answer_index=layout["final_answer"],
                            user_indices=layout["user_indices"],
                        ),
                        editors=editors,
                    )
                )
                if self.dashboard_sync is not None
                else None
            ),
        )
        return BulkRegistrationResult(
            success=True,
            message=f"Массовая заявка зарегистрирована. Строк зарегистрировано: {user_rows}.",
            registered_count=user_rows,
            retry_allowed=False,
        )

    async def _run_google(self, operation, operation_id: str):
        return await execute_with_retry_async(
            operation,
            config=self.google_api_retry,
            operation_id=operation_id,
            reset_client=self._reset_sheets_api,
        )

    async def _batch_editors(self, batch_id: str) -> tuple[str, ...]:
        applications = await self.repository.list_submitted_applications(
            include_deferred=True
        )
        return tuple(
            application.last_seen_editor or EDITOR_NOT_SELECTED
            for application in applications
            if application.batch_id == batch_id
        )

    @staticmethod
    def _batch_row_link(batch: BulkBatch) -> str:
        return (
            f"https://docs.google.com/spreadsheets/d/{batch.spreadsheet_id}/edit"
            f"#gid={batch.sheet_id}&range=A{batch.start_row}:N{batch.start_row}"
        )

    @staticmethod
    def _batch_insert_url(batch: BulkBatch) -> str:
        return (
            f"https://docs.google.com/spreadsheets/d/{batch.spreadsheet_id}/edit"
            f"#gid={batch.sheet_id}&range=A{batch.data_start_row}:G{batch.data_start_row}"
        )

    async def _save_existing_application(
        self,
        batch: BulkBatch,
        row: list[Any],
        row_number: int,
        application_id: str,
        layout: dict[str, Any],
    ) -> None:
        await self.repository.save_submitted_application(
            application_id=application_id,
            telegram_user_id=batch.telegram_user_id,
            spreadsheet_id=batch.spreadsheet_id,
            sheet_id=batch.sheet_id,
            sheet_name=batch.sheet_name,
            last_known_status=(
                _cell(row, layout["status"]).strip() or ApplicationStatus.NEW.value
            ),
            direction=batch.direction,
            answer_type=_normalize_answer_type(_cell(row, layout["answer_type"])),
            application_type=ApplicationType.BULK.value,
            is_urgent=_bulk_row_is_urgent(row, layout),
            batch_id=batch.batch_id,
            last_seen_row_number=row_number,
            last_seen_editor=(
                _cell(row, layout["editor"]).strip() or EDITOR_NOT_SELECTED
            ),
            last_seen_editor_comment=_cell(row, layout["comment"]).strip(),
            last_seen_final_answer=_cell(row, layout["final_answer"]).strip(),
        )

    async def _save_new_application(
        self,
        batch: BulkBatch,
        row: list[Any],
        row_number: int,
        application_id: str,
        layout: dict[str, Any],
    ) -> None:
        await self.repository.save_submitted_application(
            application_id=application_id,
            telegram_user_id=batch.telegram_user_id,
            spreadsheet_id=batch.spreadsheet_id,
            sheet_id=batch.sheet_id,
            sheet_name=batch.sheet_name,
            last_known_status=ApplicationStatus.NEW.value,
            direction=batch.direction,
            answer_type=_normalize_answer_type(_cell(row, layout["answer_type"])),
            application_type=ApplicationType.BULK.value,
            is_urgent=_bulk_row_is_urgent(row, layout),
            batch_id=batch.batch_id,
            last_seen_row_number=row_number,
            last_seen_editor=EDITOR_NOT_SELECTED,
            last_seen_editor_comment="",
            last_seen_final_answer="",
        )

    def _read_batch_layout(
        self,
        api: Any,
        batch: BulkBatch,
    ) -> dict[str, Any] | None:
        header_row = batch.data_start_row - 1
        result = api.spreadsheets().values().get(
            spreadsheetId=batch.spreadsheet_id or self.spreadsheet_id,
            range=(
                f"{quote_sheet_name(batch.sheet_name)}!"
                f"A{header_row}:N{header_row}"
            ),
            majorDimension="ROWS",
        ).execute()
        rows = result.get("values", [])
        headers = [str(value).strip() for value in (rows[0] if rows else [])]
        return _bulk_schema_layout(headers)

    def _get_or_create_sheet_id(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_name: str,
        column_count: int,
    ) -> int:
        metadata = api.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties",
        ).execute()
        for sheet in metadata.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("title") == sheet_name:
                return int(properties["sheetId"])

        result = api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": sheet_name,
                                "gridProperties": {
                                    "rowCount": 1000,
                                    "columnCount": column_count,
                                },
                            }
                        }
                    }
                ]
            },
        ).execute()
        return int(result["replies"][0]["addSheet"]["properties"]["sheetId"])

    def _read_batch_rows(self, api: Any, batch: BulkBatch) -> list[list[Any]]:
        """Прочитать сохраненный диапазон и остановиться перед следующей шапкой."""
        end_row = _batch_data_end_row(batch)
        result = api.spreadsheets().values().get(
            spreadsheetId=batch.spreadsheet_id or self.spreadsheet_id,
            range=f"{quote_sheet_name(batch.sheet_name)}!A{batch.data_start_row}:N{end_row}",
            majorDimension="ROWS",
        ).execute()
        rows = result.get("values", [])
        for index, row in enumerate(rows):
            if _is_batch_header_row(row):
                return rows[:index]
        return rows

    def _has_overflow_data(
        self,
        api: Any,
        batch: BulkBatch,
        layout: dict[str, Any],
    ) -> bool:
        end_row = _batch_data_end_row(batch)
        result = api.spreadsheets().values().get(
            spreadsheetId=batch.spreadsheet_id or self.spreadsheet_id,
            range=(
                f"{quote_sheet_name(batch.sheet_name)}!"
                f"A{end_row + 1}:N"
            ),
            majorDimension="ROWS",
        ).execute()
        for row in result.get("values", []):
            if _is_batch_header_row(row):
                return False
            if _has_user_bulk_data(row, layout["user_indices"]):
                return True
        return False

    def _write_registration_cells(
        self,
        api: Any,
        batch: BulkBatch,
        row_number: int,
        application_id: str,
        layout: dict[str, Any],
    ) -> None:
        cell_by_index = {
            layout["application_id"]: _string_cell(application_id),
            layout["status"]: _status_cell(batch.status_schema_version),
        }
        if layout["editor"] >= 0:
            cell_by_index[layout["editor"]] = _editor_cell(self.application_editors)
        start_column = min(cell_by_index)
        end_column = max(cell_by_index) + 1
        values = [
            cell_by_index.get(column_index, {})
            for column_index in range(start_column, end_column)
        ]
        api.spreadsheets().batchUpdate(
            spreadsheetId=batch.spreadsheet_id or self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateCells": {
                            "range": {
                                "sheetId": batch.sheet_id,
                                "startRowIndex": row_number - 1,
                                "endRowIndex": row_number,
                                "startColumnIndex": start_column,
                                "endColumnIndex": end_column,
                            },
                            "rows": [
                                {
                                    "values": values
                                }
                            ],
                            "fields": "userEnteredValue,dataValidation,userEnteredFormat",
                        }
                    },
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": batch.sheet_id,
                                "startRowIndex": row_number - 1,
                                "endRowIndex": row_number,
                                "startColumnIndex": 0,
                                "endColumnIndex": len(layout["user_indices"]),
                            },
                            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}},
                            "fields": "userEnteredFormat.backgroundColor",
                        }
                    },
                    *(
                        _bulk_application_status_format_rules(
                            batch.sheet_id,
                            row_number,
                            column_index=layout["status"],
                        )
                        if batch.status_schema_version >= 2
                        else []
                    ),
                ]
            },
        ).execute()

    def _write_normalized_change_types(
        self,
        api: Any,
        batch: BulkBatch,
        values: list[tuple[int, str]],
        layout: dict[str, Any],
    ) -> None:
        requests = [
            {
                "updateCells": {
                    "range": {
                        "sheetId": batch.sheet_id,
                        "startRowIndex": row_number - 1,
                        "endRowIndex": row_number,
                        "startColumnIndex": layout["change_type"],
                        "endColumnIndex": layout["change_type"] + 1,
                    },
                    "rows": [{"values": [_string_cell(value)]}],
                    "fields": "userEnteredValue",
                }
            }
            for row_number, value in values
        ]
        api.spreadsheets().batchUpdate(
            spreadsheetId=batch.spreadsheet_id or self.spreadsheet_id,
            body={"requests": requests},
        ).execute()

    def _organize_registered_batch_rows(
        self,
        api: Any,
        batch: BulkBatch,
        actual_rows: int,
    ) -> None:
        """Сгруппировать заполненную часть и скрыть пустой хвост резерва."""
        filled_start_index = batch.data_start_row - 1
        filled_end_index = filled_start_index + actual_rows
        if actual_rows <= 0:
            return

        reserved_end_row = _batch_reserved_end_row(batch)
        existing_groups = self._batch_row_group_ranges(api, batch)
        requests: list[dict[str, Any]] = []
        if (filled_start_index, filled_end_index) not in existing_groups:
            requests.append(
                {
                    "addDimensionGroup": {
                        "range": {
                            "sheetId": batch.sheet_id,
                            "dimension": "ROWS",
                            "startIndex": filled_start_index,
                            "endIndex": filled_end_index,
                        }
                    }
                }
            )
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": batch.sheet_id,
                        "dimension": "ROWS",
                        "startIndex": filled_start_index,
                        "endIndex": filled_end_index,
                    },
                    "properties": {"hiddenByUser": False},
                    "fields": "hiddenByUser",
                }
            }
        )
        if filled_end_index < reserved_end_row:
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": batch.sheet_id,
                            "dimension": "ROWS",
                            "startIndex": filled_end_index,
                            "endIndex": reserved_end_row,
                        },
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                }
            )
        api.spreadsheets().batchUpdate(
            spreadsheetId=batch.spreadsheet_id or self.spreadsheet_id,
            body={"requests": requests},
        ).execute()

    def _batch_row_group_ranges(
        self,
        api: Any,
        batch: BulkBatch,
    ) -> set[tuple[int, int]]:
        response = api.spreadsheets().get(
            spreadsheetId=batch.spreadsheet_id or self.spreadsheet_id,
            fields="sheets(properties(sheetId),rowGroups(range))",
        ).execute()
        ranges: set[tuple[int, int]] = set()
        for sheet in response.get("sheets", []):
            properties = sheet.get("properties", {})
            if int(properties.get("sheetId", -1)) != batch.sheet_id:
                continue
            for group in sheet.get("rowGroups", []):
                group_range = group.get("range", {})
                start_index = group_range.get("startIndex")
                end_index = group_range.get("endIndex")
                if isinstance(start_index, int) and isinstance(end_index, int):
                    ranges.add((start_index, end_index))
        return ranges

    def _get_sheets_api(self) -> Any:
        if self._sheets_api is None:
            self._sheets_api = build_google_sheets_api(self.credentials_path)
        return self._sheets_api

    def _reset_sheets_api(self) -> None:
        if not self._external_sheets_api:
            self._sheets_api = None


def _bulk_header_row(
    batch_id: str,
    telegram_user_id: int,
    created_at: datetime | str,
    direction: str,
    *,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    values = [{} for _ in range(BULK_STAGING_COLUMN_COUNT)]
    values[0] = _formatted_string_cell(f"Пачка {batch_id}", bold=True)
    values[1] = _formatted_string_cell(batch_id, bold=True)
    values[2] = google_sheets_date_cell(
        created_at,
        timezone_name=timezone_name,
        bold=True,
    )
    values[3] = _formatted_string_cell(direction, bold=True)
    values[4] = _formatted_string_cell(f"Telegram ID: {telegram_user_id}", bold=True)
    values[6] = _formatted_string_cell("Заполняйте строки ниже в колонках A:G", bold=True)
    values[11] = _bulk_batch_status_cell(BulkBatchStatus.NEW.value, status_schema_version=2)
    return {"values": values}


def _bulk_column_header_row() -> dict[str, Any]:
    return {"values": [_formatted_string_cell(header, bold=True) for header in BULK_STAGING_HEADERS]}


def _bulk_input_row(application_editors: tuple[str, ...]) -> dict[str, Any]:
    values = [_input_cell() for _ in BULK_INPUT_HEADERS]
    values[6]["userEnteredFormat"]["wrapStrategy"] = "CLIP"
    values[1]["dataValidation"] = {
        "condition": {
            "type": "ONE_OF_LIST",
            "values": [
                {"userEnteredValue": change_type.value}
                for change_type in ChangeType
            ],
        },
        "strict": True,
        "showCustomUi": True,
    }
    values.extend(
        [
            _service_cell(),
            _service_cell(),
            _service_cell(),
            _service_cell(),
            _service_cell(),
            _service_cell(),
            _service_cell(),
        ]
    )
    return {"values": values}


def _empty_row() -> dict[str, Any]:
    return {"values": [{} for _ in range(BULK_STAGING_COLUMN_COUNT)]}


def _bulk_batch_status_cell(
    status: str,
    *,
    status_schema_version: int,
) -> dict[str, Any]:
    statuses = (
        [status.value for status in BulkBatchStatus]
        if status_schema_version >= 2
        else [
            "Новая пачка",
            "В работе",
            "Нужны пояснения",
            "Частично готова",
            "Готова",
            "Отклонена",
            "Отложена",
        ]
    )
    return {
        "userEnteredValue": {"stringValue": status},
        "dataValidation": {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [{"userEnteredValue": value} for value in statuses],
            },
            "strict": True,
            "showCustomUi": True,
        },
        "userEnteredFormat": {
            "backgroundColor": _bulk_status_color(status),
            "textFormat": {"bold": True},
        },
    }


def _bulk_status_color(status: str) -> dict[str, float]:
    if status == BulkBatchStatus.NEW.value:
        return {"red": 0.78, "green": 0.88, "blue": 1.0}
    if status == BulkBatchStatus.IN_PROGRESS.value:
        return {"red": 1.0, "green": 0.93, "blue": 0.62}
    if status == BulkBatchStatus.DONE.value:
        return {"red": 0.74, "green": 0.93, "blue": 0.76}
    return {"red": 0.86, "green": 0.86, "blue": 0.86}


def _bulk_application_status_color(status: str) -> dict[str, float]:
    if status == BulkApplicationStatus.NEW.value:
        return {"red": 0.78, "green": 0.88, "blue": 1.0}
    if status == BulkApplicationStatus.NEEDS_CLARIFICATION.value:
        return {"red": 1.0, "green": 0.80, "blue": 0.55}
    return {"red": 0.74, "green": 0.93, "blue": 0.76}


def _bulk_batch_status_format_rules(sheet_id: int, row_number: int) -> list[dict[str, Any]]:
    return _status_format_rules(
        sheet_id=sheet_id,
        row_number=row_number,
        column_index=11,
        statuses=[item.value for item in BulkBatchStatus],
        color_getter=_bulk_status_color,
    )


def _bulk_application_status_format_rules(
    sheet_id: int,
    row_number: int,
    *,
    column_index: int = 11,
) -> list[dict[str, Any]]:
    return _status_format_rules(
        sheet_id=sheet_id,
        row_number=row_number,
        column_index=column_index,
        statuses=[item.value for item in BulkApplicationStatus],
        color_getter=_bulk_application_status_color,
    )


def _status_format_rules(
    *,
    sheet_id: int,
    row_number: int,
    column_index: int,
    statuses: list[str],
    color_getter,
) -> list[dict[str, Any]]:
    return [
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [
                        {
                            "sheetId": sheet_id,
                            "startRowIndex": row_number - 1,
                            "endRowIndex": row_number,
                            "startColumnIndex": column_index,
                            "endColumnIndex": column_index + 1,
                        }
                    ],
                    "booleanRule": {
                        "condition": {
                            "type": "TEXT_EQ",
                            "values": [{"userEnteredValue": status}],
                        },
                        "format": {
                            "backgroundColor": color_getter(status),
                            "textFormat": {"bold": True},
                        },
                    },
                },
                "index": 0,
            }
        }
        for status in statuses
    ]


def _bulk_active_group_border_request(
    sheet_id: int,
    *,
    start_row: int,
    end_row: int,
) -> dict[str, Any]:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row - 1,
                "endRowIndex": end_row,
                "startColumnIndex": 12,
                "endColumnIndex": 13,
            },
            "cell": {
                "userEnteredFormat": {
                    "borders": {
                        "right": {
                            "style": "SOLID_THICK",
                            "color": {"red": 0.35, "green": 0.35, "blue": 0.35},
                        }
                    }
                }
            },
            "fields": "userEnteredFormat.borders.right",
        }
    }


def _bulk_schema_layout(headers: list[Any]) -> dict[str, Any] | None:
    normalized = [str(value).strip() for value in headers]
    if normalized[: len(BULK_STAGING_HEADERS)] == BULK_STAGING_HEADERS:
        return {
            "schema": "new",
            "answer_type": 0,
            "change_type": 1,
            "user_indices": tuple(range(7)),
            "final_answer": 7,
            "comment": 9,
            "response": 10,
            "status": 11,
            "editor": 12,
            "application_id": 13,
            "batch_status": 11,
            "end_column": "N",
        }
    if normalized[: len(CURRENT_BULK_STAGING_HEADERS)] == CURRENT_BULK_STAGING_HEADERS:
        return {
            "schema": "current",
            "answer_type": 0,
            "change_type": 6,
            "user_indices": tuple(range(7)),
            "application_id": 7,
            "status": 8,
            "editor": 9,
            "comment": 10,
            "response": 11,
            "final_answer": 12,
            "batch_status": 10,
            "end_column": "M",
        }
    if normalized[: len(LEGACY_BULK_STAGING_HEADERS)] == LEGACY_BULK_STAGING_HEADERS:
        return {
            "schema": "legacy",
            "answer_type": 0,
            "change_type": 6,
            "user_indices": tuple(range(7)),
            "application_id": 7,
            "status": 8,
            "editor": -1,
            "comment": 9,
            "response": 10,
            "final_answer": 11,
            "batch_status": 9,
            "end_column": "L",
        }
    return None


def _has_user_bulk_data(
    row: list[Any],
    user_indices: tuple[int, ...] = tuple(range(7)),
) -> bool:
    return any(_cell(row, index).strip() for index in user_indices)


def _has_final_answer(
    rows: list[list[Any]],
    *,
    final_answer_index: int = 12,
    user_indices: tuple[int, ...] = tuple(range(7)),
) -> bool:
    return any(
        _cell(row, final_answer_index).strip()
        for row in rows
        if _has_user_bulk_data(row, user_indices)
    )


def _normalize_answer_type(value: str) -> str:
    text = value.strip()
    valid_values = {answer_type.value for answer_type in AnswerType}
    return text if text in valid_values else ""


def _bulk_row_is_urgent(row: list[Any], layout: dict[str, Any]) -> bool:
    return (
        _normalize_answer_type(_cell(row, layout["answer_type"]))
        == AnswerType.URGENT.value
    )


def _cell(row: list[Any], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return str(row[index])


def _bulk_sheet_name(direction: str) -> str:
    if direction in {Direction.VOICEBOT.value, Direction.COLLECTION.value}:
        return f"{direction} {BULK_STAGING_SHEET_NAME}"
    return BULK_STAGING_SHEET_NAME


def _next_batch_start_row(
    *,
    existing_rows: list[list[Any]],
    existing_batches: list[BulkBatch],
    spreadsheet_id: str,
    sheet_name: str,
) -> int:
    last_non_empty_row = _last_non_empty_row_number(existing_rows)
    last_allocated_row = max(
        (
            _batch_reserved_end_row(batch)
            for batch in existing_batches
            if batch.spreadsheet_id == spreadsheet_id and batch.sheet_name == sheet_name
        ),
        default=0,
    )
    last_used_row = max(last_non_empty_row, last_allocated_row)
    if last_used_row == 0:
        return 1
    next_row = last_used_row + BULK_BATCH_SPACING_ROWS + 1
    return next_row


def _spacing_rows_before_batch(
    existing_rows: list[list[Any]],
    start_row: int,
) -> int:
    return max(start_row - len(existing_rows) - 1, 0)


def _last_non_empty_row_number(rows: list[list[Any]]) -> int:
    for index in range(len(rows), 0, -1):
        if any(str(value).strip() for value in rows[index - 1]):
            return index
    return 0


def _batch_data_end_row(batch: BulkBatch) -> int:
    if batch.data_end_row is not None:
        return batch.data_end_row
    return _batch_reserved_end_row(batch)


def _batch_reserved_end_row(batch: BulkBatch) -> int:
    return batch.data_start_row + max(batch.reserved_rows, 1) - 1


def _is_batch_header_row(row: list[Any]) -> bool:
    first_cell = _cell(row, 0).strip()
    second_cell = _cell(row, 1).strip()
    match = _BATCH_HEADER_PATTERN.fullmatch(first_cell)
    return match is not None and match.group(1) == second_cell


def _string_cell(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    return {"userEnteredValue": {"stringValue": str(value)}}


def _status_cell(status_schema_version: int) -> dict[str, Any]:
    if status_schema_version >= 2:
        status = BulkApplicationStatus.NEW.value
        allowed_statuses = [item.value for item in BulkApplicationStatus]
        color = _bulk_application_status_color(status)
    else:
        status = ApplicationStatus.NEW.value
        allowed_statuses = [item.value for item in ApplicationStatus]
        color = {"red": 0.78, "green": 0.88, "blue": 1.0}
    return {
        "userEnteredValue": {"stringValue": status},
        "dataValidation": {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [
                    {"userEnteredValue": allowed_status}
                    for allowed_status in allowed_statuses
                ],
            },
            "strict": True,
            "showCustomUi": True,
        },
        "userEnteredFormat": {
            "backgroundColor": color,
            "textFormat": {"bold": True},
        },
    }


def _editor_cell(application_editors: tuple[str, ...]) -> dict[str, Any]:
    cell = _service_cell()
    cell["userEnteredValue"] = {"stringValue": EDITOR_NOT_SELECTED}
    cell["dataValidation"] = _editor_data_validation_rule(application_editors)
    return cell


def _formatted_string_cell(value: Any, *, bold: bool = False) -> dict[str, Any]:
    cell = _string_cell(value)
    cell["userEnteredFormat"] = {
        "backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.94},
        "textFormat": {"bold": bold},
        "wrapStrategy": "WRAP",
    }
    return cell


def _input_cell() -> dict[str, Any]:
    return {
        "userEnteredFormat": {
            "backgroundColor": {"red": 1.0, "green": 0.97, "blue": 0.80},
            "wrapStrategy": "WRAP",
        }
    }


def _service_cell() -> dict[str, Any]:
    return {
        "userEnteredFormat": {
            "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
            "wrapStrategy": "WRAP",
        }
    }
