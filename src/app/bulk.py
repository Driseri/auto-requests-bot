from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html import escape
import json
import logging
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
    BulkReservation,
    BulkReservationState,
    BulkTargetKind,
    ChangeType,
    Direction,
    Draft,
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
    WORKSHEET_HEADERS,
    CHIPS_WORKSHEET_HEADERS,
    GoogleSheetsSubmissionService,
    dashboard_bulk_batch_row,
    dashboard_projection,
    build_google_sheets_api,
    quote_sheet_name,
    spreadsheet_row_link,
    sheet_section_kind,
    sheet_section_lock_key,
    _editor_data_validation_rule,
    _cell_data,
    _daily_separator_row_data,
    _previous_daily_group_request,
    _status_cell_data,
    _status_data_validation_rule,
    _worksheet_schema_layout,
    daily_separator_label,
    is_daily_separator_row,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_BULK_RESERVED_ROWS = 100
DEFAULT_BULK_MAX_ROWS = 50
BULK_BATCH_SPACING_ROWS = 2
DEFAULT_BULK_REGISTRATION_STALE_SECONDS = 600
BULK_RESERVATION_REQUIRED_BACKGROUND_COLOR = {"red": 1.0, "green": 0.97, "blue": 0.80}
BULK_RESERVATION_SCRIPTWRITER_BACKGROUND_COLOR = {"red": 0.91, "green": 0.94, "blue": 1.0}
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
    "Кейс или сообщения клиента",
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


@dataclass(slots=True)
class BulkReservationCreationResult:
    success: bool
    message: str
    reservation: BulkReservation | None = None
    insert_url: str | None = None
    retry_allowed: bool = False
    shifted_rows: int = 0


class BulkReservationMetadataLookupError(RuntimeError):
    """Metadata lookup failed, so reserve/register must fail closed."""


@dataclass(frozen=True, slots=True)
class BulkReservationRange:
    start_row: int
    end_row: int


@dataclass(frozen=True, slots=True)
class _BulkReservationRowsInsert:
    start_row: int
    shifted_rows: int


class BulkBatchServiceProtocol(Protocol):
    async def create_batch(
        self,
        telegram_user_id: int,
        direction: str,
        batch_id: str | None = None,
    ) -> BulkBatchCreationResult:
        ...


class BulkReservationServiceProtocol(Protocol):
    async def create_reservation(
        self,
        reservation: BulkReservation,
    ) -> BulkReservationCreationResult:
        ...

    async def create_reservation_with_lock(
        self,
        reservation: BulkReservation,
    ) -> BulkReservationCreationResult:
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


def _find_reservation_metadata_range(
    api: Any,
    *,
    spreadsheet_id: str,
    sheet_id: int,
    reservation_id: str,
    requested_count: int | None,
) -> BulkReservationRange | None:
    developer_metadata = getattr(api.spreadsheets(), "developerMetadata", None)
    if developer_metadata is None:
        raise BulkReservationMetadataLookupError(
            "Google Sheets developerMetadata API is unavailable."
        )
    try:
        result = developer_metadata().search(
            spreadsheetId=spreadsheet_id,
            body={
                "dataFilters": [
                    {
                        "developerMetadataLookup": {
                            "metadataKey": "bulk_reservation_id",
                            "metadataValue": reservation_id,
                            "locationType": "ROW",
                        }
                    }
                ]
            },
        ).execute()
    except Exception as exc:
        raise BulkReservationMetadataLookupError(
            "Не удалось безопасно проверить, были ли строки уже созданы."
        ) from exc
    if not isinstance(result, dict):
        raise BulkReservationMetadataLookupError(
            "Google Sheets developerMetadata returned an unexpected response."
        )
    matches = result.get("matchedDeveloperMetadata", [])
    if not isinstance(matches, list):
        raise BulkReservationMetadataLookupError(
            "Google Sheets developerMetadata returned malformed matches."
        )
    for match in matches:
        if not isinstance(match, dict):
            continue
        metadata = match.get("developerMetadata", match)
        if not isinstance(metadata, dict):
            continue
        dimension_range = metadata.get("location", {}).get("dimensionRange", {})
        if not isinstance(dimension_range, dict):
            continue
        if dimension_range.get("sheetId") != sheet_id:
            continue
        start_index = dimension_range.get("startIndex")
        end_index = dimension_range.get("endIndex")
        if start_index is None or end_index is None or end_index <= start_index:
            continue
        start_row = int(start_index) + 1
        count = max(int(requested_count or 1), 1)
        return BulkReservationRange(start_row=start_row, end_row=start_row + count - 1)
    return None


class GoogleSheetsBulkReservationService:
    """Создаёт резерв строк в боевых листах без отдельной batch-сущности."""

    def __init__(
        self,
        *,
        submission_service: GoogleSheetsSubmissionService,
        repository: DraftRepository,
        google_api_retry: GoogleApiRetryConfig = GoogleApiRetryConfig(),
        daily_sheet_grouping_enabled: bool = True,
    ) -> None:
        self.repository = repository
        self.google_api_retry = google_api_retry
        self.daily_sheet_grouping_enabled = daily_sheet_grouping_enabled
        self._submission = submission_service
        self._section_locks: dict[str, asyncio.Lock] = {}

    async def create_reservation(
        self,
        reservation: BulkReservation,
    ) -> BulkReservationCreationResult:
        if not reservation.direction or not reservation.target_kind or not reservation.change_type:
            return BulkReservationCreationResult(
                success=False,
                message="Не выбраны направление, место или тип массовой заявки.",
            )
        if not reservation.requested_count or reservation.requested_count <= 0:
            return BulkReservationCreationResult(
                success=False,
                message="Количество строк должно быть больше нуля.",
            )
        try:
            result = await execute_with_retry_async(
                lambda: self._create_reservation_sync(reservation),
                config=self.google_api_retry,
                operation_id=f"bulk-reservation-create:{reservation.reservation_id}",
            )
            return result
        except BulkReservationMetadataLookupError:
            LOGGER.warning(
                "Bulk reservation metadata lookup failed closed during creation: "
                "reservation_id=%s telegram_user_id=%s direction=%s target_kind=%s "
                "change_type=%s requested_count=%s",
                reservation.reservation_id,
                reservation.telegram_user_id,
                reservation.direction,
                reservation.target_kind,
                reservation.change_type,
                reservation.requested_count,
            )
            return BulkReservationCreationResult(
                success=False,
                message=(
                    "Не удалось безопасно проверить, были ли строки уже созданы. "
                    "Повторите действие через несколько минут."
                ),
                retry_allowed=True,
            )
        except Exception as exc:
            return BulkReservationCreationResult(
                success=False,
                message=f"Не удалось создать строки для массовой заявки: {exc}",
            )

    def _create_reservation_sync(
        self,
        reservation: BulkReservation,
    ) -> BulkReservationCreationResult:
        target_kind = BulkTargetKind(reservation.target_kind or "")
        change_type = ChangeType.normalize(reservation.change_type)
        if change_type is None:
            raise ValueError("Неизвестный тип изменения.")
        draft = Draft(
            telegram_user_id=reservation.telegram_user_id,
            current_step="completed",
            application_id="",
            direction=reservation.direction,
            answer_type=_answer_type_for_bulk_target(target_kind),
            application_type=ApplicationType.BULK.value,
            change_type=change_type.value,
            is_urgent=target_kind == BulkTargetKind.URGENT,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
        spreadsheet_id, sheet_name = self._submission.resolve_target(draft)
        if not spreadsheet_id:
            raise ValueError("Для выбранного направления не задан ID Google-таблицы.")

        sheet_mode = {
            BulkTargetKind.ROLLOUT: "rollout",
            BulkTargetKind.URGENT: "urgent",
            BulkTargetKind.INTEGRATION: "flat",
        }[target_kind]
        api = self._submission._get_sheets_api()
        sheet_id, layout, marker = self._submission._ensure_sheet_ready(
            api,
            spreadsheet_id,
            sheet_name,
            sheet_mode=sheet_mode,
            change_type=change_type,
        )
        # In normal bot runtime this lock is held by the calling event loop before
        # the sync call reaches this method. Fake services in tests call directly.
        schema = layout.split(":", maxsplit=1)[1]
        existing_range = self._find_existing_reservation_range(
            api,
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            reservation_id=reservation.reservation_id,
            requested_count=reservation.requested_count,
        )
        if existing_range is not None:
            start_row, end_row = existing_range.start_row, existing_range.end_row
            shifted_rows = 0
            LOGGER.info(
                "Bulk reservation creation reused metadata range: reservation_id=%s "
                "telegram_user_id=%s spreadsheet_id=%s sheet_id=%s sheet_name=%s "
                "start_row=%s end_row=%s requested_count=%s",
                reservation.reservation_id,
                reservation.telegram_user_id,
                spreadsheet_id,
                sheet_id,
                sheet_name,
                start_row,
                end_row,
                reservation.requested_count,
            )
        else:
            insert_result = self._insert_blank_rows(
                api,
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                sheet_name=sheet_name,
                layout=layout,
                schema=schema,
                change_type=change_type,
                target_marker=marker,
                count=reservation.requested_count or 1,
                reservation_id=reservation.reservation_id,
            )
            start_row = insert_result.start_row
            shifted_rows = insert_result.shifted_rows
            end_row = start_row + (reservation.requested_count or 1) - 1
            LOGGER.info(
                "Bulk reservation rows inserted: reservation_id=%s telegram_user_id=%s "
                "spreadsheet_id=%s sheet_id=%s sheet_name=%s target_kind=%s "
                "change_type=%s start_row=%s end_row=%s requested_count=%s",
                reservation.reservation_id,
                reservation.telegram_user_id,
                spreadsheet_id,
                sheet_id,
                sheet_name,
                target_kind.value,
                change_type.value,
                start_row,
                end_row,
                reservation.requested_count,
            )
        insert_url = spreadsheet_row_link(
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            row_number=start_row,
            end_column=_worksheet_schema_layout(schema)["end_column"],
        )
        return BulkReservationCreationResult(
            success=True,
            message="Строки для массовой заявки созданы.",
            reservation=BulkReservation(
                reservation_id=reservation.reservation_id,
                idempotency_key=reservation.idempotency_key,
                telegram_user_id=reservation.telegram_user_id,
                state=BulkReservationState.CREATED.value,
                direction=reservation.direction,
                target_kind=reservation.target_kind,
                change_type=reservation.change_type,
                requested_count=reservation.requested_count,
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                sheet_name=sheet_name,
                start_row=start_row,
                end_row=end_row,
                insert_url=insert_url,
                created_at=reservation.created_at,
                updated_at=utc_now_iso(),
            ),
            insert_url=insert_url,
            shifted_rows=shifted_rows,
        )

    def _find_existing_reservation_range(
        self,
        api: Any,
        *,
        spreadsheet_id: str,
        sheet_id: int,
        reservation_id: str,
        requested_count: int | None,
    ) -> BulkReservationRange | None:
        return _find_reservation_metadata_range(
            api,
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            reservation_id=reservation_id,
            requested_count=requested_count,
        )

    async def create_reservation_with_lock(
        self,
        reservation: BulkReservation,
    ) -> BulkReservationCreationResult:
        if not reservation.direction or not reservation.target_kind or not reservation.change_type:
            return await self.create_reservation(reservation)
        target_kind = BulkTargetKind(reservation.target_kind)
        change_type = ChangeType.normalize(reservation.change_type)
        draft = Draft(
            telegram_user_id=reservation.telegram_user_id,
            current_step="completed",
            direction=reservation.direction,
            answer_type=_answer_type_for_bulk_target(target_kind),
            application_type=ApplicationType.BULK.value,
            change_type=change_type.value if change_type else reservation.change_type,
            is_urgent=target_kind == BulkTargetKind.URGENT,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
        spreadsheet_id, sheet_name = self._submission.resolve_target(draft)
        section_kind = sheet_section_kind(
            answer_type=draft.answer_type,
            change_type=change_type,
        )
        lock_key = sheet_section_lock_key(
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            section_kind=section_kind,
        )
        lock = self._section_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            acquired = await self.repository.acquire_bulk_section_lock(
                lock_key=lock_key,
                owner=reservation.reservation_id,
                ttl_seconds=600,
            )
            if not acquired:
                LOGGER.info(
                    "Bulk reservation section lock busy: reservation_id=%s "
                    "telegram_user_id=%s lock_key=%s",
                    reservation.reservation_id,
                    reservation.telegram_user_id,
                    lock_key,
                )
                return BulkReservationCreationResult(
                    success=False,
                    message=(
                        "Сейчас другой пользователь создаёт строки в этом же разделе. "
                        "Повторите действие через несколько секунд."
                    ),
                    reservation=reservation,
                    retry_allowed=True,
                )
            try:
                result = await self.create_reservation(reservation)
                if not result.success or result.reservation is None:
                    return result
                saved = await self.repository.complete_bulk_reservation_creation_and_shift(
                    reservation.reservation_id,
                    spreadsheet_id=result.reservation.spreadsheet_id or "",
                    sheet_id=result.reservation.sheet_id or 0,
                    sheet_name=result.reservation.sheet_name or "",
                    start_row=result.reservation.start_row or 0,
                    end_row=result.reservation.end_row or 0,
                    insert_url=result.insert_url or "",
                    shifted_rows=result.shifted_rows,
                )
                return BulkReservationCreationResult(
                    success=True,
                    message=result.message,
                    reservation=saved or result.reservation,
                    insert_url=result.insert_url,
                    retry_allowed=result.retry_allowed,
                )
            finally:
                await self.repository.release_bulk_section_lock(
                    lock_key=lock_key,
                    owner=reservation.reservation_id,
                )

    def _insert_blank_rows(
        self,
        api: Any,
        *,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_name: str,
        layout: str,
        schema: str,
        change_type: ChangeType,
        target_marker: str | None,
        count: int,
        reservation_id: str,
    ) -> _BulkReservationRowsInsert:
        rows = self._submission._read_rows(api, spreadsheet_id, sheet_name)
        if layout.startswith("urgent:"):
            if self.daily_sheet_grouping_enabled:
                insert_plan = _urgent_daily_bulk_reservation_insert_plan(
                    rows,
                    sheet_id=sheet_id,
                    label=daily_separator_label(
                        self._submission.clock(),
                        self._submission.timezone_name,
                    ),
                    change_type=change_type,
                    column_count=(
                        len(CHIPS_WORKSHEET_HEADERS)
                        if schema in {"chips", "previous_chips"}
                        else len(WORKSHEET_HEADERS)
                    ),
                    count=count,
                )
            else:
                insert_row = _urgent_bulk_insert_row(rows, change_type)
                insert_plan = {
                    "start_row": insert_row,
                    "insert_row": insert_row,
                    "inserted_rows": count,
                    "inserted_header_rows": [],
                    "prefix_requests": [],
                }
        elif layout.startswith("sectioned:"):
            insert_row = _sectioned_bulk_insert_row(rows, change_type, target_marker)
            insert_plan = {
                "start_row": insert_row,
                "insert_row": insert_row,
                "inserted_rows": count,
                "prefix_requests": [],
            }
        elif self.daily_sheet_grouping_enabled:
            insert_plan = _daily_bulk_reservation_insert_plan(
                rows,
                sheet_id=sheet_id,
                label=daily_separator_label(
                    self._submission.clock(),
                    self._submission.timezone_name,
                ),
                section_start_row=2,
                section_end_row=len(rows) + 1,
                column_count=(
                    len(CHIPS_WORKSHEET_HEADERS)
                    if schema in {"chips", "previous_chips"}
                    else len(WORKSHEET_HEADERS)
                ),
                count=count,
            )
        else:
            insert_row = len(rows) + 1
            insert_plan = {
                "start_row": insert_row,
                "insert_row": insert_row,
                "inserted_rows": count,
                "inserted_header_rows": [],
                "prefix_requests": [],
            }
        insert_row = insert_plan["start_row"]
        physical_insert_row = insert_plan["insert_row"]
        inserted_rows = insert_plan["inserted_rows"]
        insert_index = physical_insert_row - 1
        column_count = (
            len(CHIPS_WORKSHEET_HEADERS)
            if schema in {"chips", "previous_chips"}
            else len(WORKSHEET_HEADERS)
        )
        blank_row = {"values": [_cell_data("") for _ in range(column_count)]}
        requests = [
            {
                "insertDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": insert_index,
                        "endIndex": insert_index + inserted_rows,
                    },
                    "inheritFromBefore": True,
                }
            },
            {
                "updateCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": insert_index,
                        "endRowIndex": insert_index + inserted_rows,
                        "startColumnIndex": 0,
                        "endColumnIndex": column_count,
                    },
                    "rows": [
                        *insert_plan.get("inserted_header_rows", []),
                        *[blank_row for _ in range(count)],
                    ],
                    "fields": "userEnteredValue,userEnteredFormat",
                }
            },
            _clear_row_background_format_request(
                sheet_id=sheet_id,
                start_row=insert_row,
                end_row=insert_row + count - 1,
                column_count=column_count,
            ),
            {
                "createDeveloperMetadata": {
                    "developerMetadata": {
                        "metadataKey": "bulk_reservation_id",
                        "metadataValue": reservation_id,
                        "visibility": "DOCUMENT",
                        "location": {
                            "dimensionRange": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "startIndex": insert_row - 1,
                                "endIndex": insert_row,
                            }
                        },
                    }
                }
            },
            *_required_input_columns_format_requests(
                sheet_id=sheet_id,
                start_row=insert_row,
                end_row=insert_row + count - 1,
                schema=schema,
            ),
        ]
        api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
        self._submission._apply_daily_group_best_effort(
            api,
            spreadsheet_id=spreadsheet_id,
            group_request=insert_plan.get("group_request"),
        )
        return _BulkReservationRowsInsert(
            start_row=insert_row,
            shifted_rows=inserted_rows if physical_insert_row <= len(rows) else 0,
        )

    @staticmethod
    def _apply_reservation_borders(
        api: Any,
        *,
        spreadsheet_id: str,
        sheet_id: int,
        start_row: int,
        end_row: int,
        column_count: int,
    ) -> None:
        api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": _registered_reservation_border_requests(
                    sheet_id=sheet_id,
                    first_row=start_row,
                    last_row=end_row,
                    column_count=column_count,
                )
            },
        ).execute()


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


class BulkReservationRegistrar:
    """Регистрирует заполненные строки резерва как обычные одиночные заявки."""

    def __init__(
        self,
        *,
        repository: DraftRepository,
        credentials_path: str,
        application_editors: tuple[str, ...] = (),
        urgent_editor_notifications_enabled: bool = False,
        editor_urgent_chat_id: int | None = None,
        registration_stale_seconds: int = DEFAULT_BULK_REGISTRATION_STALE_SECONDS,
        google_api_retry: GoogleApiRetryConfig = GoogleApiRetryConfig(),
        timezone_name: str = DEFAULT_TIMEZONE,
        clock: Callable[[], datetime] | None = None,
        sheets_api: Any | None = None,
    ) -> None:
        self.repository = repository
        self.credentials_path = credentials_path
        self.application_editors = application_editors
        self.urgent_editor_notifications_enabled = urgent_editor_notifications_enabled
        self.editor_urgent_chat_id = editor_urgent_chat_id
        self.registration_stale_seconds = registration_stale_seconds
        self.google_api_retry = google_api_retry
        self.timezone_name = timezone_name
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._sheets_api = sheets_api
        self._reservation_locks: dict[str, asyncio.Lock] = {}

    async def register_reservation(
        self,
        reservation_id: str,
        telegram_user_id: int,
    ) -> BulkRegistrationResult:
        lock = self._reservation_locks.setdefault(reservation_id, asyncio.Lock())
        async with lock:
            reservation = await self.repository.get_bulk_reservation(reservation_id)
            if reservation is None:
                return BulkRegistrationResult(False, "Массовый резерв не найден.")
            if reservation.telegram_user_id != telegram_user_id:
                return BulkRegistrationResult(False, "Этот массовый резерв создан другим пользователем.")
            claim = await self.repository.claim_bulk_reservation_registration(
                reservation_id,
                stale_after_seconds=self.registration_stale_seconds,
            )
            if claim is None:
                return BulkRegistrationResult(False, "Массовый резерв не найден.")
            if claim.state == BulkReservationState.REGISTERED.value:
                return BulkRegistrationResult(
                    True,
                    f"Этот резерв уже зарегистрирован. Заявок: {claim.registered_count}.",
                    registered_count=claim.registered_count,
                    retry_allowed=False,
                    insert_url=claim.insert_url,
                )
            if claim.state != BulkReservationState.REGISTERING.value:
                return BulkRegistrationResult(
                    False,
                    "Резерв ещё не создан в Google Sheets.",
                    insert_url=claim.insert_url,
                )
            LOGGER.info(
                "Bulk reservation registration started: reservation_id=%s "
                "telegram_user_id=%s spreadsheet_id=%s sheet_id=%s sheet_name=%s "
                "start_row=%s end_row=%s requested_count=%s target_kind=%s change_type=%s",
                claim.reservation_id,
                telegram_user_id,
                claim.spreadsheet_id,
                claim.sheet_id,
                claim.sheet_name,
                claim.start_row,
                claim.end_row,
                claim.requested_count,
                claim.target_kind,
                claim.change_type,
            )
            try:
                result, tracking, projections = await execute_with_retry_async(
                    lambda: self._register_reservation_sync(claim),
                    config=self.google_api_retry,
                    operation_id=f"bulk-reservation-register:{reservation_id}",
                )
                notification_event = self._urgent_editor_notification_event(
                    claim,
                    result=result,
                    application_ids=[item["application_id"] for item in tracking],
                )
                await self.repository.complete_bulk_reservation_registration_with_updates(
                    reservation_id,
                    registered_count=result.registered_count,
                    tracking=tracking,
                    dashboard_projections=projections,
                    notification_event=notification_event,
                    spreadsheet_id=claim.spreadsheet_id,
                    sheet_id=claim.sheet_id,
                    sheet_name=claim.sheet_name,
                    start_row=claim.start_row,
                    end_row=claim.end_row,
                    insert_url=claim.insert_url,
                )
                LOGGER.info(
                    "Bulk reservation registration completed: reservation_id=%s "
                    "telegram_user_id=%s registered_count=%s spreadsheet_id=%s "
                    "sheet_id=%s sheet_name=%s start_row=%s end_row=%s "
                    "google_requests_batch=1 dashboard_projections=%s notification_enqueued=%s",
                    claim.reservation_id,
                    telegram_user_id,
                    result.registered_count,
                    claim.spreadsheet_id,
                    claim.sheet_id,
                    claim.sheet_name,
                    claim.start_row,
                    claim.end_row,
                    len(projections),
                    notification_event is not None,
                )
                return result
            except BulkReservationMetadataLookupError:
                await self.repository.release_bulk_reservation_registration(
                    reservation_id,
                    error="metadata lookup failed",
                )
                LOGGER.warning(
                    "Bulk reservation metadata lookup failed closed during registration: "
                    "reservation_id=%s telegram_user_id=%s spreadsheet_id=%s sheet_id=%s "
                    "sheet_name=%s start_row=%s end_row=%s",
                    claim.reservation_id,
                    telegram_user_id,
                    claim.spreadsheet_id,
                    claim.sheet_id,
                    claim.sheet_name,
                    claim.start_row,
                    claim.end_row,
                )
                return BulkRegistrationResult(
                    False,
                    (
                        "Не удалось безопасно проверить актуальное расположение резерва. "
                        "Повторите действие через несколько минут."
                    ),
                    insert_url=claim.insert_url,
                )
            except Exception as exc:
                await self.repository.release_bulk_reservation_registration(
                    reservation_id,
                    error=str(exc),
                )
                LOGGER.warning(
                    "Bulk reservation registration failed: reservation_id=%s "
                    "telegram_user_id=%s spreadsheet_id=%s sheet_id=%s sheet_name=%s "
                    "start_row=%s end_row=%s error=%r",
                    claim.reservation_id,
                    telegram_user_id,
                    claim.spreadsheet_id,
                    claim.sheet_id,
                    claim.sheet_name,
                    claim.start_row,
                    claim.end_row,
                    exc,
                )
                return BulkRegistrationResult(
                    False,
                    str(exc),
                    insert_url=claim.insert_url,
                )

    def _urgent_editor_notification_event(
        self,
        reservation: BulkReservation,
        *,
        result: BulkRegistrationResult,
        application_ids: list[str],
    ) -> dict[str, Any] | None:
        if not self.urgent_editor_notifications_enabled:
            return None
        if self.editor_urgent_chat_id is None:
            return None
        if reservation.target_kind != BulkTargetKind.URGENT.value:
            return None
        if result.registered_count <= 0:
            return None
        snapshot = {
            "reservation_id": reservation.reservation_id,
            "direction": reservation.direction,
            "change_type": reservation.change_type,
            "registered_count": result.registered_count,
            "insert_url": reservation.insert_url,
            "application_ids": application_ids,
        }
        html = "\n".join(
            [
                "🚨 <b>Новые срочные заявки</b>",
                "",
                f"<b>Направление:</b> {escape(reservation.direction or '-')}",
                f"<b>Тип:</b> {escape(reservation.change_type or '-')}",
                f"<b>Количество:</b> {result.registered_count}",
                "",
                (
                    f'<a href="{escape(reservation.insert_url or "", quote=True)}">'
                    "Открыть диапазон</a>"
                ),
            ]
        )
        return {
            "telegram_user_id": self.editor_urgent_chat_id,
            "event_type": "urgent-editor-bulk-reservation-created",
            "dedupe_key": f"urgent-editor-bulk-reservation-created:{reservation.reservation_id}",
            "snapshot_json": json.dumps(snapshot, ensure_ascii=False),
            "chunks": [html],
        }

    def _get_sheets_api(self) -> Any:
        if self._sheets_api is None:
            self._sheets_api = build_google_sheets_api(self.credentials_path)
        return self._sheets_api

    def _register_reservation_sync(
        self,
        reservation: BulkReservation,
    ) -> tuple[BulkRegistrationResult, list[dict[str, Any]], list[dict[str, Any]]]:
        if (
            not reservation.spreadsheet_id
            or reservation.sheet_id is None
            or not reservation.sheet_name
            or reservation.start_row is None
            or reservation.end_row is None
        ):
            raise ValueError("У резерва не сохранены координаты диапазона.")
        change_type = ChangeType.normalize(reservation.change_type)
        if change_type is None:
            raise ValueError("У резерва не сохранён тип изменения.")
        schema = "chips" if change_type == ChangeType.CHIPS else "new"
        headers = CHIPS_WORKSHEET_HEADERS if schema == "chips" else WORKSHEET_HEADERS
        metadata_range = _find_reservation_metadata_range(
            self._get_sheets_api(),
            spreadsheet_id=reservation.spreadsheet_id,
            sheet_id=reservation.sheet_id,
            reservation_id=reservation.reservation_id,
            requested_count=reservation.requested_count,
        )
        if metadata_range is not None:
            if (
                metadata_range.start_row != reservation.start_row
                or metadata_range.end_row != reservation.end_row
            ):
                LOGGER.info(
                    "Bulk reservation metadata range adjusted before registration: "
                    "reservation_id=%s spreadsheet_id=%s sheet_id=%s sheet_name=%s "
                    "old_start_row=%s old_end_row=%s new_start_row=%s new_end_row=%s",
                    reservation.reservation_id,
                    reservation.spreadsheet_id,
                    reservation.sheet_id,
                    reservation.sheet_name,
                    reservation.start_row,
                    reservation.end_row,
                    metadata_range.start_row,
                    metadata_range.end_row,
                )
            reservation.start_row = metadata_range.start_row
            reservation.end_row = metadata_range.end_row
            reservation.insert_url = spreadsheet_row_link(
                spreadsheet_id=reservation.spreadsheet_id,
                sheet_id=reservation.sheet_id,
                row_number=metadata_range.start_row,
                end_column=_worksheet_schema_layout(schema)["end_column"],
            )
        rows = self._read_range(
            reservation.spreadsheet_id,
            reservation.sheet_name,
            reservation.start_row,
            reservation.end_row,
            len(headers),
        )
        required = _reservation_required_indices(schema)
        visible = _reservation_visible_indices(schema)
        filled: list[tuple[int, list[Any]]] = []
        errors: list[str] = []
        for offset, row in enumerate(rows):
            row_number = reservation.start_row + offset
            if not any(_cell(row, index).strip() for index in visible):
                continue
            missing = [headers[index] for index in required if not _cell(row, index).strip()]
            if missing:
                errors.append(f"строка {row_number}: не заполнено {', '.join(missing)}")
            else:
                filled.append((row_number, row))
        if errors:
            raise ValueError(
                "Не удалось зарегистрировать массовый ввод.\n\nИсправьте строки:\n"
                + "\n".join(f"• {item}" for item in errors[:20])
            )
        if not filled:
            raise ValueError("В диапазоне нет заполненных строк.")

        submitted_at = self.clock()
        tracking: list[dict[str, Any]] = []
        projections: list[dict[str, Any]] = []
        update_requests: list[dict[str, Any]] = []
        registered_row_numbers = [row_number for row_number, _ in filled]
        registered_row_set = set(registered_row_numbers)
        for row_number, row in filled:
            full_row = _pad_row(row, len(headers))
            application_id = _cell(full_row, headers.index("ID заявки")).strip() or generate_application_id()
            _apply_registered_bulk_values(
                full_row,
                headers=headers,
                application_id=application_id,
                reservation=reservation,
                submitted_at=submitted_at,
            )
            update_requests.append(
                self._registered_row_update_request(
                    reservation,
                    row_number=row_number,
                    row=full_row,
                    schema=schema,
                )
            )
            update_requests.append(
                self._registered_row_editor_validation_request(
                    reservation,
                    row_number=row_number,
                    schema=schema,
                )
            )
            update_requests.append(
                self._registered_row_status_validation_request(
                    reservation,
                    row_number=row_number,
                    schema=schema,
                )
            )
            tracking.append(
                {
                    "application_id": application_id,
                    "telegram_user_id": reservation.telegram_user_id,
                    "spreadsheet_id": reservation.spreadsheet_id,
                    "sheet_id": reservation.sheet_id,
                    "sheet_name": reservation.sheet_name,
                    "last_known_status": ApplicationStatus.NEW.value,
                    "direction": reservation.direction,
                    "answer_type": _answer_type_for_bulk_target(
                        BulkTargetKind(reservation.target_kind or BulkTargetKind.ROLLOUT.value)
                    ),
                    "application_type": ApplicationType.SINGLE.value,
                    "change_type": reservation.change_type,
                    "is_urgent": reservation.target_kind == BulkTargetKind.URGENT.value,
                    "batch_id": None,
                    "last_seen_row_number": row_number,
                    "submitted_at": utc_iso(submitted_at),
                }
            )
            projections.append(
                {
                    "entity_type": "APPLICATION",
                    "entity_id": application_id,
                    "snapshot": dashboard_projection(
                        _bulk_reservation_dashboard_row(
                            application_id=application_id,
                            reservation=reservation,
                            row_number=row_number,
                            submitted_at=submitted_at,
                            end_column=_worksheet_schema_layout(schema)["end_column"],
                        )
                    ),
                }
            )
        unused_row_numbers = [
            row_number
            for row_number in range(reservation.start_row, reservation.end_row + 1)
            if row_number not in registered_row_set
        ]
        self._get_sheets_api().spreadsheets().batchUpdate(
            spreadsheetId=reservation.spreadsheet_id,
            body={
                "requests": [
                    *update_requests,
                    *_registered_reservation_border_requests(
                        sheet_id=reservation.sheet_id,
                        first_row=min(registered_row_numbers),
                        last_row=max(registered_row_numbers),
                        column_count=len(headers),
                    ),
                    *_clear_required_input_columns_format_requests(
                        sheet_id=reservation.sheet_id,
                        row_numbers=unused_row_numbers,
                        schema=schema,
                    ),
                ]
            },
        ).execute()
        empty_count = max((reservation.requested_count or 0) - len(filled), 0)
        return (
            BulkRegistrationResult(
                True,
                f"Готово.\n\nЗарегистрировано заявок: {len(filled)}\nПустых строк оставлено: {empty_count}",
                registered_count=len(filled),
                retry_allowed=False,
                insert_url=reservation.insert_url,
            ),
            tracking,
            projections,
        )

    def _read_range(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        start_row: int,
        end_row: int,
        column_count: int,
    ) -> list[list[Any]]:
        end_col = "U" if column_count == len(CHIPS_WORKSHEET_HEADERS) else "X"
        result = self._get_sheets_api().spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A{start_row}:{end_col}{end_row}",
            majorDimension="ROWS",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        return result.get("values", [])

    def _registered_row_update_request(
        self,
        reservation: BulkReservation,
        *,
        row_number: int,
        row: list[Any],
        schema: str,
    ) -> dict[str, Any]:
        layout = _worksheet_schema_layout(schema)
        cells = [_cell_data(value) for value in row]
        cells[0].setdefault("userEnteredFormat", {})["backgroundColor"] = (
            BULK_RESERVATION_SCRIPTWRITER_BACKGROUND_COLOR
        )
        cells[layout["status"]] = _status_cell_data(ApplicationStatus.NEW.value)
        cells[layout["date"]] = google_sheets_date_cell(
            row[layout["date"]],
            timezone_name=self.timezone_name,
        )
        return {
            "updateCells": {
                "range": {
                    "sheetId": reservation.sheet_id,
                    "startRowIndex": row_number - 1,
                    "endRowIndex": row_number,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(cells),
                },
                "rows": [{"values": cells}],
                "fields": "userEnteredValue,userEnteredFormat",
            }
        }

    def _registered_row_editor_validation_request(
        self,
        reservation: BulkReservation,
        *,
        row_number: int,
        schema: str,
    ) -> dict[str, Any]:
        layout = _worksheet_schema_layout(schema)
        editor_column = layout["editor"]
        return {
            "repeatCell": {
                "range": {
                    "sheetId": reservation.sheet_id,
                    "startRowIndex": row_number - 1,
                    "endRowIndex": row_number,
                    "startColumnIndex": editor_column,
                    "endColumnIndex": editor_column + 1,
                },
                "cell": {
                    "dataValidation": _editor_data_validation_rule(
                        self.application_editors
                    )
                },
                "fields": "dataValidation",
            }
        }

    def _registered_row_status_validation_request(
        self,
        reservation: BulkReservation,
        *,
        row_number: int,
        schema: str,
    ) -> dict[str, Any]:
        layout = _worksheet_schema_layout(schema)
        status_column = layout["status"]
        return {
            "repeatCell": {
                "range": {
                    "sheetId": reservation.sheet_id,
                    "startRowIndex": row_number - 1,
                    "endRowIndex": row_number,
                    "startColumnIndex": status_column,
                    "endColumnIndex": status_column + 1,
                },
                "cell": {"dataValidation": _status_data_validation_rule()},
                "fields": "dataValidation",
            }
        }


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
        overflow_range = await self._overflow_check_range(batch)
        if overflow_range is not None and await self._run_google(
            lambda: self._has_overflow_data(
                self._get_sheets_api(),
                batch,
                layout,
                overflow_range=overflow_range,
            ),
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

    async def _overflow_check_range(self, batch: BulkBatch) -> tuple[int, int] | None:
        overflow_start = _batch_reserved_end_row(batch) + 1
        overflow_end = await self._overflow_check_end_row(batch)
        if overflow_start > overflow_end:
            return None
        return overflow_start, overflow_end

    async def _overflow_check_end_row(self, batch: BulkBatch) -> int:
        current_reserved_end = _batch_reserved_end_row(batch)
        spreadsheet_id = batch.spreadsheet_id or self.spreadsheet_id
        batches = await self.repository.list_bulk_batches()
        next_start_rows = [
            candidate.start_row
            for candidate in batches
            if candidate.batch_id != batch.batch_id
            and (candidate.spreadsheet_id or self.spreadsheet_id) == spreadsheet_id
            and candidate.sheet_name == batch.sheet_name
            and candidate.start_row > current_reserved_end
        ]
        if next_start_rows:
            return min(next_start_rows) - 1
        return current_reserved_end + max(batch.reserved_rows, 1)

    def _has_overflow_data(
        self,
        api: Any,
        batch: BulkBatch,
        layout: dict[str, Any],
        *,
        overflow_range: tuple[int, int],
    ) -> bool:
        start_row, end_row = overflow_range
        result = api.spreadsheets().values().get(
            spreadsheetId=batch.spreadsheet_id or self.spreadsheet_id,
            range=(
                f"{quote_sheet_name(batch.sheet_name)}!"
                f"A{start_row}:N{end_row}"
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


def _answer_type_for_bulk_target(target_kind: BulkTargetKind) -> str:
    if target_kind == BulkTargetKind.URGENT:
        return AnswerType.URGENT.value
    if target_kind == BulkTargetKind.INTEGRATION:
        return AnswerType.INTEGRATION.value
    return AnswerType.ROLLOUT.value


def _sectioned_bulk_insert_row(
    rows: list[list[Any]],
    change_type: ChangeType,
    target_marker: str | None,
) -> int:
    marker_rows = {
        _cell(row, 0).strip(): index + 1
        for index, row in enumerate(rows)
        if _cell(row, 0).strip()
        in {ChangeType.ADD.value, ChangeType.EDIT.value, ChangeType.CHIPS.value, "CHIPS V2"}
    }
    selected_marker = target_marker or change_type.value
    selected_row = marker_rows.get(selected_marker)
    if selected_row is None:
        raise ValueError(f"Секция {selected_marker} не найдена.")
    following = sorted(row for row in marker_rows.values() if row > selected_row)
    return following[0] if following else len(rows) + 1


def _urgent_bulk_insert_row(rows: list[list[Any]], change_type: ChangeType) -> int:
    marker_positions = [
        index
        for index, row in enumerate(rows)
        if _cell(row, 0).strip() == ChangeType.CHIPS.value
    ]
    if len(marker_positions) != 1:
        raise ValueError("В листе срочных заявок отсутствует однозначная секция CHIPS.")
    marker_position = marker_positions[0]
    if change_type == ChangeType.CHIPS:
        return len(rows) + 1
    return marker_position + 1


def _urgent_bulk_section_start_row(rows: list[list[Any]], change_type: ChangeType) -> int:
    if change_type != ChangeType.CHIPS:
        return 2
    marker_positions = [
        index
        for index, row in enumerate(rows)
        if _cell(row, 0).strip() == ChangeType.CHIPS.value
    ]
    if len(marker_positions) != 1:
        raise ValueError("В листе срочных заявок отсутствует однозначная секция CHIPS.")
    return marker_positions[0] + 3


def _daily_bulk_reservation_insert_plan(
    rows: list[list[Any]],
    *,
    sheet_id: int,
    label: str,
    section_start_row: int,
    section_end_row: int,
    column_count: int,
    count: int,
) -> dict[str, Any]:
    section_start_row = max(section_start_row, 1)
    section_end_row = max(section_end_row, section_start_row)
    separator_rows = [
        row_number
        for row_number in range(section_start_row, min(section_end_row, len(rows) + 1))
        if is_daily_separator_row(rows[row_number - 1])
    ]
    today_rows = [
        row_number
        for row_number in separator_rows
        if _cell(rows[row_number - 1], 0).strip() == label
    ]
    group_request: dict[str, Any] | None = None
    inserted_header_rows: list[dict[str, Any]] = []
    if today_rows:
        today_row = today_rows[-1]
        next_separator = next(
            (row_number for row_number in separator_rows if row_number > today_row),
            None,
        )
        physical_insert_row = next_separator or section_end_row
        start_row = physical_insert_row
        inserted_rows = count
    else:
        physical_insert_row = section_end_row
        start_row = section_end_row + 1
        inserted_rows = count + 1
        inserted_header_rows = [_daily_separator_row_data(label, column_count)]
        group_request = _previous_daily_group_request(
            separator_rows,
            new_separator_row=physical_insert_row,
            sheet_id=sheet_id,
        )
    return {
        "insert_row": physical_insert_row,
        "start_row": start_row,
        "inserted_rows": inserted_rows,
        "inserted_header_rows": inserted_header_rows,
        "prefix_requests": [],
        "group_request": group_request,
    }


def _urgent_daily_bulk_reservation_insert_plan(
    rows: list[list[Any]],
    *,
    sheet_id: int,
    label: str,
    change_type: ChangeType,
    column_count: int,
    count: int,
) -> dict[str, Any]:
    separator_rows = [
        row_number
        for row_number in range(2, len(rows) + 1)
        if is_daily_separator_row(rows[row_number - 1])
    ]
    today_row = next(
        (
            row_number
            for row_number in reversed(separator_rows)
            if _cell(rows[row_number - 1], 0).strip() == label
        ),
        None,
    )
    group_request: dict[str, Any] | None = None
    if today_row is None:
        day_start = len(rows) + 1
        day_end = len(rows) + 1
        group_request = _previous_daily_group_request(
            separator_rows,
            new_separator_row=day_start,
            sheet_id=sheet_id,
        )
    else:
        day_start = today_row
        day_end = next(
            (row_number for row_number in separator_rows if row_number > today_row),
            len(rows) + 1,
        )
    chips_marker = _chips_marker_row_in_day(rows, day_start=day_start, day_end=day_end)
    inserted_header_rows: list[dict[str, Any]] = []
    if today_row is None:
        physical_insert_row = day_start
        inserted_header_rows.append(_daily_separator_row_data(label, column_count))
        if change_type == ChangeType.CHIPS:
            inserted_header_rows.extend(
                [
                    _bulk_section_marker_row_data(
                        ChangeType.CHIPS.value,
                        len(CHIPS_WORKSHEET_HEADERS),
                    ),
                    {"values": [_cell_data(value) for value in CHIPS_WORKSHEET_HEADERS]},
                ]
            )
        start_row = physical_insert_row + len(inserted_header_rows)
        inserted_rows = len(inserted_header_rows) + count
    elif change_type == ChangeType.CHIPS and chips_marker is None:
        physical_insert_row = day_end
        inserted_header_rows.extend(
            [
                _bulk_section_marker_row_data(
                    ChangeType.CHIPS.value,
                    len(CHIPS_WORKSHEET_HEADERS),
                ),
                {"values": [_cell_data(value) for value in CHIPS_WORKSHEET_HEADERS]},
            ]
        )
        start_row = physical_insert_row + len(inserted_header_rows)
        inserted_rows = len(inserted_header_rows) + count
    elif change_type == ChangeType.CHIPS:
        physical_insert_row = day_end
        start_row = physical_insert_row
        inserted_rows = count
    else:
        physical_insert_row = chips_marker or day_end
        start_row = physical_insert_row
        inserted_rows = count
    return {
        "insert_row": physical_insert_row,
        "start_row": start_row,
        "inserted_rows": inserted_rows,
        "inserted_header_rows": inserted_header_rows,
        "prefix_requests": [],
        "group_request": group_request,
    }


def _chips_marker_row_in_day(
    rows: list[list[Any]],
    *,
    day_start: int,
    day_end: int,
) -> int | None:
    for row_number in range(day_start + 1, min(day_end, len(rows) + 1)):
        if _cell(rows[row_number - 1], 0).strip() != ChangeType.CHIPS.value:
            continue
        header_row_number = row_number + 1
        if header_row_number >= day_end or header_row_number > len(rows):
            raise ValueError("Повреждена CHIPS-секция внутри дневного блока.")
        if not _is_chips_header_row(rows[header_row_number - 1]):
            raise ValueError("Повреждена CHIPS-шапка внутри дневного блока.")
        return row_number
    return None


def _is_chips_header_row(row: list[Any]) -> bool:
    normalized = [str(value).strip() for value in row]
    return normalized[: len(CHIPS_WORKSHEET_HEADERS)] == CHIPS_WORKSHEET_HEADERS


def _bulk_section_marker_row_data(marker: str, column_count: int) -> dict[str, Any]:
    values = [_cell_data(marker if index == 0 else "") for index in range(column_count)]
    for cell in values:
        cell["userEnteredFormat"] = {
            "backgroundColor": {"red": 0.90, "green": 0.90, "blue": 0.90},
            "textFormat": {"bold": True},
        }
    return {"values": values}


def _reservation_required_indices(schema: str) -> tuple[int, ...]:
    if schema in {"chips", "previous_chips"}:
        return (0, 2, 3, 4, 5, 10)
    return (0, 2, 3, 4, 10)


def _reservation_required_column_indices(schema: str) -> tuple[int, ...]:
    return _reservation_required_indices(schema)


def _required_input_columns_format_requests(
    *,
    sheet_id: int,
    start_row: int,
    end_row: int,
    schema: str,
) -> list[dict[str, Any]]:
    return [
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": start_row - 1,
                    "endRowIndex": end_row,
                    "startColumnIndex": column_index,
                    "endColumnIndex": column_index + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": (
                            BULK_RESERVATION_SCRIPTWRITER_BACKGROUND_COLOR
                            if column_index == 0
                            else BULK_RESERVATION_REQUIRED_BACKGROUND_COLOR
                        )
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        }
        for column_index in _reservation_required_column_indices(schema)
    ]


def _clear_required_input_columns_format_requests(
    *,
    sheet_id: int,
    row_numbers: list[int],
    schema: str,
) -> list[dict[str, Any]]:
    return [
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_number - 1,
                    "endRowIndex": row_number,
                    "startColumnIndex": column_index,
                    "endColumnIndex": column_index + 1,
                },
                "cell": {"userEnteredFormat": {}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        }
        for row_number in row_numbers
        for column_index in _reservation_required_column_indices(schema)
    ]


def _clear_row_background_format_request(
    *,
    sheet_id: int,
    start_row: int,
    end_row: int,
    column_count: int,
) -> dict[str, Any]:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row - 1,
                "endRowIndex": end_row,
                "startColumnIndex": 0,
                "endColumnIndex": column_count,
            },
            "cell": {"userEnteredFormat": {}},
            "fields": "userEnteredFormat.backgroundColor",
        }
    }


def _registered_reservation_border_requests(
    *,
    sheet_id: int,
    first_row: int,
    last_row: int,
    column_count: int,
) -> list[dict[str, Any]]:
    border = {"style": "SOLID_THICK", "color": {"red": 0, "green": 0, "blue": 0}}
    return [
        {
            "updateBorders": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": first_row - 1,
                    "endRowIndex": first_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": column_count,
                },
                "top": border,
            }
        },
        {
            "updateBorders": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": last_row - 1,
                    "endRowIndex": last_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": column_count,
                },
                "bottom": border,
            }
        },
    ]


def _reservation_visible_indices(schema: str) -> tuple[int, ...]:
    if schema == "chips":
        return (0, 2, 3, 4, 5, 6, 7, 8, 9, 10)
    return (0, 2, 3, 4, 5, 6, 7, 8, 9, 10)


def _pad_row(row: list[Any], length: int) -> list[Any]:
    return [*row[:length], *([""] * max(length - len(row), 0))]


def _apply_registered_bulk_values(
    row: list[Any],
    *,
    headers: list[str],
    application_id: str,
    reservation: BulkReservation,
    submitted_at: datetime,
) -> None:
    values = {
        "Статус": ApplicationStatus.NEW.value,
        "ID заявки": application_id,
        "ID пачки": "",
        "Тип заявки": ApplicationType.SINGLE.value,
        "Дата заявки": utc_iso(submitted_at),
        "Направление": reservation.direction or "",
        "Тип ответа": _answer_type_for_bulk_target(
            BulkTargetKind(reservation.target_kind or BulkTargetKind.ROLLOUT.value)
        ),
        "Срочная": "Да" if reservation.target_kind == BulkTargetKind.URGENT.value else "Нет",
        "Автор заявки": "",
        "Telegram ID": reservation.telegram_user_id,
        "Тип изменения": reservation.change_type or "",
    }
    for header, value in values.items():
        if header in headers:
            row[headers.index(header)] = value


def _bulk_reservation_dashboard_row(
    *,
    application_id: str,
    reservation: BulkReservation,
    row_number: int,
    submitted_at: datetime,
    end_column: str,
) -> list[Any]:
    return [
        application_id,
        "",
        utc_iso(submitted_at),
        reservation.direction or "",
        ApplicationType.SINGLE.value,
        _answer_type_for_bulk_target(
            BulkTargetKind(reservation.target_kind or BulkTargetKind.ROLLOUT.value)
        ),
        "Да" if reservation.target_kind == BulkTargetKind.URGENT.value else "Нет",
        f"Telegram {reservation.telegram_user_id}",
        ApplicationStatus.NEW.value,
        EDITOR_NOT_SELECTED,
        "Нет",
        spreadsheet_row_link(
            spreadsheet_id=reservation.spreadsheet_id or "",
            sheet_id=reservation.sheet_id or 0,
            row_number=row_number,
            end_column=end_column,
        ),
    ]


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
