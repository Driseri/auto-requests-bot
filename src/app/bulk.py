from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html import escape
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from app.google_api import GoogleApiRetryConfig, execute_with_retry_async
from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
    BulkReservation,
    BulkReservationState,
    BulkTargetKind,
    ChangeType,
    Draft,
    generate_application_id,
    utc_now_iso,
)
from app.repository import DraftRepository
from app.scheduling import DEFAULT_TIMEZONE
from app.sheet_dates import google_sheets_date_cell, utc_iso
from app.submission import (
    EDITOR_NOT_SELECTED,
    WORKSHEET_HEADERS,
    CHIPS_WORKSHEET_HEADERS,
    GoogleSheetsSubmissionService,
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
    _section_header_row_data,
    _status_cell_data,
    _status_data_validation_rule,
    _worksheet_schema_layout,
    daily_separator_label,
    is_daily_separator_row,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_BULK_MAX_ROWS = 50
DEFAULT_BULK_REGISTRATION_STALE_SECONDS = 600
BULK_RESERVATION_REQUIRED_BACKGROUND_COLOR = {"red": 1.0, "green": 0.97, "blue": 0.80}
BULK_RESERVATION_SCRIPTWRITER_BACKGROUND_COLOR = {"red": 0.91, "green": 0.94, "blue": 1.0}


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
            application_type=ApplicationType.SINGLE.value,
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
            application_type=ApplicationType.SINGLE.value,
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
        elif layout.startswith("sectioned:"):
            insert_row = _sectioned_bulk_insert_row(rows, change_type, target_marker)
            insert_plan = {
                "start_row": insert_row,
                "insert_row": insert_row,
                "inserted_rows": count,
                "prefix_requests": [],
                "protected_rows": [],
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
                "protected_rows": [],
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
                    "inheritFromBefore": insert_plan.get("inherit_from_before", True),
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
        self._submission._apply_sheet_protection_best_effort(
            api,
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            column_count=column_count,
            row_numbers=insert_plan.get("protected_rows", []),
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




































def _cell(row: list[Any], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return str(row[index])




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
        protected_rows: list[int] = []
    else:
        physical_insert_row = section_end_row
        start_row = section_end_row + 1
        inserted_rows = count + 1
        inserted_header_rows = [_daily_separator_row_data(label, column_count)]
        protected_rows = [physical_insert_row]
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
        "protected_rows": protected_rows,
        "inherit_from_before": bool(today_rows),
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
                    _section_header_row_data(CHIPS_WORKSHEET_HEADERS),
                ]
            )
        protected_rows = list(range(physical_insert_row, physical_insert_row + len(inserted_header_rows)))
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
                _section_header_row_data(CHIPS_WORKSHEET_HEADERS),
            ]
        )
        protected_rows = list(range(physical_insert_row, physical_insert_row + len(inserted_header_rows)))
        start_row = physical_insert_row + len(inserted_header_rows)
        inserted_rows = len(inserted_header_rows) + count
    elif change_type == ChangeType.CHIPS:
        physical_insert_row = day_end
        start_row = physical_insert_row
        inserted_rows = count
        protected_rows = []
    else:
        physical_insert_row = chips_marker or day_end
        start_row = physical_insert_row
        inserted_rows = count
        protected_rows = []
    return {
        "insert_row": physical_insert_row,
        "start_row": start_row,
        "inserted_rows": inserted_rows,
        "inserted_header_rows": inserted_header_rows,
        "prefix_requests": [],
        "group_request": group_request,
        "protected_rows": protected_rows,
        "inherit_from_before": today_row is not None,
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
