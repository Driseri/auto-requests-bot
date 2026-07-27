from __future__ import annotations

import asyncio
import ctypes
import gc
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from time import monotonic
from dataclasses import dataclass, field
from html import escape
from typing import Any, Callable, Protocol

from aiogram.types import LinkPreviewOptions
from googleapiclient.errors import HttpError

from app.google_api import GoogleApiRetryConfig, execute_with_retry_async
from app.keyboards import build_keyboard
from app.health import write_heartbeat
from app.bulk import (
    BULK_STAGING_HEADERS,
    CURRENT_BULK_STAGING_HEADERS,
    LEGACY_BULK_STAGING_HEADERS,
)
from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
    BulkApplicationStatus,
    BulkBatch,
    BulkBatchLocationState,
    BulkBatchStatus,
    ChangeType,
    DashboardEntityType,
    KeyboardKind,
    SubmittedApplication,
)
from app.repository import DraftRepository
from app.submission import (
    CHIPS_WORKSHEET_HEADERS,
    CURRENT_WORKSHEET_HEADERS,
    DashboardSyncService,
    DirectionSpreadsheetConfig,
    EDITOR_NOT_SELECTED,
    LEGACY_WORKSHEET_HEADERS,
    PREVIOUS_CHIPS_WORKSHEET_HEADERS,
    PREVIOUS_WORKSHEET_HEADERS,
    WORKSHEET_HEADERS,
    build_google_sheets_api,
    dashboard_bulk_batch_row,
    dashboard_projection,
    dashboard_tracked_row,
    is_daily_separator_row,
    quote_sheet_name,
    sheet_section_kind,
    sheet_section_lock_key,
)


LOGGER = logging.getLogger(__name__)
URGENT_EDITOR_NOTIFICATION_EVENT_TYPE = "urgent-editor-application-created"
URGENT_EDITOR_SCRIPTWRITER_RESPONSE_EVENT_TYPE = "urgent-editor-scriptwriter-response"
URGENT_EDITOR_BULK_RESERVATION_EVENT_TYPE = "urgent-editor-bulk-reservation-created"
SCRIPTWRITER_RESPONSE_PREVIEW_LIMIT = 1800
STABLE_NOTIFICATION_POLLS = 3
SINGLE_IMPORTANT_STATUSES = {
    ApplicationStatus.IN_PROGRESS.value,
    ApplicationStatus.FINAL_ANSWER_READY.value,
    ApplicationStatus.ACCEPTED.value,
    ApplicationStatus.REJECTED.value,
    ApplicationStatus.POSTPONED.value,
}


def _unique_batches(batches: list[BulkBatch]) -> list[BulkBatch]:
    result: list[BulkBatch] = []
    seen: set[str] = set()
    for batch in batches:
        if batch.batch_id in seen:
            continue
        seen.add(batch.batch_id)
        result.append(batch)
    return result


def _stable_text_field_change(
    *,
    current_value: str | None,
    last_sent_value: str | None,
    pending_value: str | None,
    pending_seen_count: int,
    pending_field: str,
    pending_count_field: str,
    last_sent_field: str,
    stable_polls: int = STABLE_NOTIFICATION_POLLS,
) -> StableFieldChange:
    value = (current_value or "").strip()
    last_sent = (last_sent_value or "").strip()
    if not value or value == last_sent:
        return StableFieldChange(
            ready=False,
            updates={
                pending_field: None,
                pending_count_field: 0,
            },
        )
    if value == (pending_value or "").strip():
        next_count = pending_seen_count + 1
    else:
        next_count = 1
    if next_count >= stable_polls:
        return StableFieldChange(
            ready=True,
            updates={
                last_sent_field: value,
                pending_field: None,
                pending_count_field: 0,
            },
        )
    return StableFieldChange(
        ready=False,
        updates={
            pending_field: value,
            pending_count_field: next_count,
        },
    )


class TelegramNotifierProtocol(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: Any | None = None,
        link_preview_options: LinkPreviewOptions | None = None,
    ) -> Any:
        ...


@dataclass(slots=True)
class SheetApplicationStatus:
    application_id: str
    spreadsheet_id: str
    sheet_name: str
    sheet_id: int
    row_number: int
    status: str
    editor_comment: str
    final_answer: str
    scriptwriter_response: str = ""
    editor: str = ""
    batch_id: str | None = None
    direction: str | None = None
    answer_type: str | None = None
    is_urgent: bool | None = None
    change_type: str | None = None
    scriptwriter: str | None = None
    intent: str | None = None
    end_column: str = "W"
    application_id_column_index: int = 11
    status_column_index: int = 1


@dataclass(slots=True)
class SheetBulkBatchStatus:
    batch_id: str
    spreadsheet_id: str
    sheet_name: str
    sheet_id: int
    row_number: int
    status: str
    end_column: str = "L"


@dataclass(slots=True)
class BulkEditorComment:
    application_id: str
    spreadsheet_id: str
    sheet_name: str
    sheet_id: int
    row_number: int
    comment: str
    end_column: str = "L"


@dataclass(frozen=True, slots=True)
class BulkBatchLocation:
    batch_id: str
    spreadsheet_id: str
    sheet_name: str
    sheet_id: int
    start_row: int


@dataclass(slots=True)
class BulkBatchLocationScan:
    locations: dict[str, BulkBatchLocation]
    confirmed_missing_ids: set[str]
    ambiguous_rows: dict[str, tuple[int, ...]]
    unavailable_ids: set[str]
    deferred_ids: set[str]


@dataclass(slots=True)
class StatusNotification:
    tracked: SubmittedApplication
    current: SheetApplicationStatus
    status_changed: bool
    final_answer_changed: bool
    editor_changed: bool = False
    editor_comment_ready: bool = False
    stable_tracking_updates: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StableFieldChange:
    ready: bool
    updates: dict[str, Any]


class GoogleSheetsStatusReader:
    """Читает статусы по ID и не зависит от текущего номера строки."""

    def __init__(
        self,
        *,
        direction_spreadsheets: DirectionSpreadsheetConfig,
        credentials_path: str,
        sheets_api: Any | None = None,
        google_api_retry: GoogleApiRetryConfig = GoogleApiRetryConfig(),
    ) -> None:
        self.direction_spreadsheets = direction_spreadsheets
        self.credentials_path = credentials_path
        self._sheets_api = sheets_api
        self._external_sheets_api = sheets_api is not None
        self.google_api_retry = google_api_retry
        self._sheet_ids_cache: dict[str, dict[str, int]] = {}
        self.unavailable_single_sources: set[tuple[str, str]] = set()

    async def read_statuses(self) -> dict[str, SheetApplicationStatus]:
        return await self._run_with_retry(self._read_statuses_sync, "status-full-scan")

    async def read_statuses_for(
        self,
        applications: list[SubmittedApplication],
        *,
        fallback_full_scan: bool = False,
    ) -> dict[str, SheetApplicationStatus]:
        """Читать ожидаемые листы, используя полный scan только как fallback."""
        return await self._run_with_retry(
            lambda: self._read_statuses_for_sync(applications, fallback_full_scan),
            "status-tracked-scan",
        )

    async def read_bulk_application_statuses(
        self,
        batches: list[BulkBatch],
    ) -> dict[str, SheetApplicationStatus]:
        """Прочитать строки только активных пачек в их фактических границах."""
        return await self._run_with_retry(
            lambda: self._read_bulk_application_statuses_sync(batches),
            "status-bulk-rows",
        )

    async def read_batch_statuses(
        self,
        batches: list[BulkBatch],
    ) -> dict[str, SheetBulkBatchStatus]:
        return await self._run_with_retry(
            lambda: self._read_batch_statuses_sync(batches),
            "status-bulk-batches",
        )

    async def resolve_bulk_batch_locations(
        self,
        batches: list[BulkBatch],
        *,
        search_batch_ids: set[str],
    ) -> BulkBatchLocationScan:
        return await self._run_with_retry(
            lambda: self._resolve_bulk_batch_locations_sync(batches, search_batch_ids),
            "status-bulk-locations",
        )

    async def read_bulk_editor_comments(
        self,
        batches: list[BulkBatch],
    ) -> dict[str, list[BulkEditorComment]]:
        return await self._run_with_retry(
            lambda: self._read_bulk_editor_comments_sync(batches),
            "status-bulk-comments",
        )

    async def delete_application_row(self, current: SheetApplicationStatus) -> None:
        return await self._run_with_retry(
            lambda: self._delete_application_row_sync(current),
            f"status-delete-application:{current.application_id}",
        )

    async def _run_with_retry(self, operation: Callable[[], Any], operation_id: str) -> Any:
        return await execute_with_retry_async(
            operation,
            config=self.google_api_retry,
            operation_id=operation_id,
            reset_client=self._reset_sheets_api,
        )

    def _read_statuses_sync(self) -> dict[str, SheetApplicationStatus]:
        api = self._get_sheets_api()
        result: dict[str, SheetApplicationStatus] = {}
        for spreadsheet_id in self._spreadsheet_ids():
            sheet_ids = self._read_sheet_ids(api, spreadsheet_id)
            for sheet_name, sheet_id in sheet_ids.items():
                rows = self._read_sheet_rows(api, spreadsheet_id, sheet_name)
                for index, row, layout in _working_data_rows(rows):
                    application_id = _cell(row, layout["application_id"]).strip()
                    application_type = _cell(row, layout["application_type"]).strip()
                    if (
                        not application_id
                        or application_id == "ID заявки"
                        or application_type not in {
                            ApplicationType.SINGLE.value,
                            ApplicationType.BULK.value,
                        }
                    ):
                        continue
                    result[application_id] = SheetApplicationStatus(
                        application_id=application_id,
                        spreadsheet_id=spreadsheet_id,
                        batch_id=_cell(row, layout["batch_id"]).strip() or None,
                        sheet_name=sheet_name,
                        sheet_id=sheet_id,
                        row_number=index,
                        direction=_cell(row, layout["direction"]).strip() or None,
                        answer_type=_cell(row, layout["answer_type"]).strip() or None,
                        is_urgent=_sheet_bool(_cell(row, layout["is_urgent"])),
                        change_type=_cell(row, layout.get("change_type", -1)).strip() or None,
                        status=_cell(row, layout["status"]).strip(),
                        editor=_cell(row, layout["editor"]).strip(),
                        editor_comment=_cell(row, layout["comment"]).strip(),
                        final_answer=_cell(row, layout["final_answer"]).strip(),
                        scriptwriter_response=_cell(
                            row,
                            layout.get("scriptwriter_response", -1),
                        ).strip(),
                        end_column=layout["end_column"],
                        application_id_column_index=layout["application_id"],
                        status_column_index=layout["status"],
                    )
        return result

    def _delete_application_row_sync(self, current: SheetApplicationStatus) -> None:
        api = self._get_sheets_api()
        range_suffix = f"A{current.row_number}:{current.end_column}{current.row_number}"
        row = self._read_sheet_range(
            api,
            current.spreadsheet_id,
            current.sheet_name,
            range_suffix,
        )
        values = row[0] if row else []
        application_id = _cell(values, current.application_id_column_index).strip()
        status = _cell(values, current.status_column_index).strip()
        if application_id != current.application_id:
            raise RuntimeError(
                "Application deletion verification failed: "
                f"expected application_id={current.application_id} found={application_id or '<empty>'}"
            )
        if status != ApplicationStatus.DELETION.value:
            raise RuntimeError(
                "Application deletion verification failed: "
                f"application_id={current.application_id} status={status or '<empty>'}"
            )
        api.spreadsheets().batchUpdate(
            spreadsheetId=current.spreadsheet_id,
            body={
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": current.sheet_id,
                                "dimension": "ROWS",
                                "startIndex": current.row_number - 1,
                                "endIndex": current.row_number,
                            }
                        }
                    }
                ]
            },
        ).execute()

    def _read_statuses_for_sync(
        self,
        applications: list[SubmittedApplication],
        fallback_full_scan: bool,
    ) -> dict[str, SheetApplicationStatus]:
        api = self._get_sheets_api()
        result: dict[str, SheetApplicationStatus] = {}
        self.unavailable_single_sources = set()
        grouped: dict[tuple[str, str], list[SubmittedApplication]] = {}
        for application in applications:
            if application.batch_id or not application.spreadsheet_id or not application.sheet_name:
                continue
            grouped.setdefault(
                (application.spreadsheet_id, application.sheet_name),
                [],
            ).append(application)

        point_checked = 0
        point_found = 0
        fallback_needed: dict[tuple[str, str], list[SubmittedApplication]] = {}
        for (spreadsheet_id, sheet_name), group in grouped.items():
            sheet_id = next((item.sheet_id for item in group if item.sheet_id is not None), None)
            if sheet_id is None:
                sheet_id = self._read_sheet_ids(api, spreadsheet_id).get(sheet_name)
            point_entries = [
                (
                    item,
                    (
                        f"{quote_sheet_name(sheet_name)}!"
                        f"A{item.last_seen_row_number}:"
                        f"{_point_status_end_column(item)}{item.last_seen_row_number}"
                    ),
                )
                for item in group
                if item.last_seen_row_number is not None and item.last_seen_row_number > 1
            ]
            no_coordinate = [
                item
                for item in group
                if item.last_seen_row_number is None or item.last_seen_row_number <= 1
            ]
            if no_coordinate:
                fallback_needed.setdefault((spreadsheet_id, sheet_name), []).extend(no_coordinate)
            if not point_entries:
                continue
            point_checked += len(point_entries)
            try:
                rows_by_range = self._read_point_status_rows(
                    api,
                    spreadsheet_id,
                    point_entries,
                )
            except HttpError as exc:
                if not _is_unparseable_range_error(exc):
                    raise
                self.unavailable_single_sources.add((spreadsheet_id, sheet_name))
                LOGGER.warning(
                    "Single application point ranges are unavailable; skipping source. "
                    "spreadsheet_id=%s sheet_name=%s ranges=%s error=%s",
                    spreadsheet_id,
                    sheet_name,
                    len(point_entries),
                    exc,
                )
                continue
            for (application, _), rows in zip(point_entries, rows_by_range, strict=True):
                row = rows[0] if rows else []
                current = _status_from_tracked_row(
                    application=application,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    sheet_id=sheet_id or application.sheet_id or 0,
                    row=row,
                )
                if current is None:
                    fallback_needed.setdefault((spreadsheet_id, sheet_name), []).append(application)
                    continue
                result[current.application_id] = current
                point_found += 1

        missing_ids = {
            item.application_id
            for item in applications
            if not item.batch_id
        } - set(result)
        if missing_ids:
            fallback_count = sum(len(items) for items in fallback_needed.values())
            LOGGER.info(
                "Single status polling point check completed: "
                "tracked=%s point_checked=%s point_found=%s fallback_needed=%s "
                "fallback_full_scan=%s",
                len([item for item in applications if not item.batch_id]),
                point_checked,
                point_found,
                fallback_count,
                fallback_full_scan,
            )
        if missing_ids and (fallback_needed or fallback_full_scan):
            LOGGER.info(
                "Running fallback sheet scan for missing applications: count=%s",
                len(missing_ids),
            )
            for (spreadsheet_id, sheet_name), group in grouped.items():
                wanted_ids = {item.application_id for item in group} & missing_ids
                if not wanted_ids:
                    continue
                sheet_id = next(
                    (item.sheet_id for item in group if item.sheet_id is not None),
                    None,
                )
                if sheet_id is None:
                    sheet_id = self._read_sheet_ids(api, spreadsheet_id).get(sheet_name)
                try:
                    rows = self._read_sheet_rows(api, spreadsheet_id, sheet_name)
                except HttpError as exc:
                    if not _is_unparseable_range_error(exc):
                        raise
                    self.unavailable_single_sources.add((spreadsheet_id, sheet_name))
                    LOGGER.warning(
                        "Single application full sheet range is unavailable; "
                        "skipping source. spreadsheet_id=%s sheet_name=%s error=%s",
                        spreadsheet_id,
                        sheet_name,
                        exc,
                    )
                    continue
                for index, row, layout in _working_data_rows(rows):
                    application_id = _cell(row, layout["application_id"]).strip()
                    if application_id not in wanted_ids:
                        continue
                    result[application_id] = _status_from_working_row(
                        application_id=application_id,
                        spreadsheet_id=spreadsheet_id,
                        sheet_name=sheet_name,
                        sheet_id=sheet_id or 0,
                        row_number=index,
                        row=row,
                        layout=layout,
                    )
        return result

    @staticmethod
    def _read_point_status_rows(
        api: Any,
        spreadsheet_id: str,
        entries: list[tuple[SubmittedApplication, str]],
    ) -> list[list[list[Any]]]:
        rows_by_range: list[list[list[Any]]] = []
        values_resource = api.spreadsheets().values()
        for chunk_start in range(0, len(entries), 100):
            chunk = entries[chunk_start : chunk_start + 100]
            ranges = [range_name for _, range_name in chunk]
            if hasattr(values_resource, "batchGet"):
                response = values_resource.batchGet(
                    spreadsheetId=spreadsheet_id,
                    ranges=ranges,
                    majorDimension="ROWS",
                ).execute()
                chunk_rows = [
                    item.get("values", [])
                    for item in response.get("valueRanges", [])
                ]
                chunk_rows.extend([[]] * (len(chunk) - len(chunk_rows)))
                rows_by_range.extend(chunk_rows)
                continue
            for _, range_name in chunk:
                response = values_resource.get(
                    spreadsheetId=spreadsheet_id,
                    range=range_name,
                    majorDimension="ROWS",
                ).execute()
                rows_by_range.append(response.get("values", []))
        return rows_by_range

    def _read_expected_application_status(
        self,
        api: Any,
        application: SubmittedApplication,
        *,
        spreadsheet_id: str,
        sheet_name: str,
        sheet_id: int,
    ) -> SheetApplicationStatus | None:
        row_number = application.last_seen_row_number
        if row_number is None or row_number <= 1:
            return None
        rows = self._read_sheet_range(
            api,
            spreadsheet_id,
            sheet_name,
            f"A{row_number - 1}:X{row_number}",
        )
        for index, row, layout in _working_data_rows(rows, start_row=row_number - 1):
            application_id = _cell(row, layout["application_id"]).strip()
            if application_id != application.application_id:
                continue
            return _status_from_working_row(
                application_id=application_id,
                spreadsheet_id=spreadsheet_id,
                sheet_name=sheet_name,
                sheet_id=sheet_id,
                row_number=index,
                row=row,
                layout=layout,
            )
        return None

    def _read_bulk_application_statuses_sync(
        self,
        batches: list[BulkBatch],
    ) -> dict[str, SheetApplicationStatus]:
        api = self._get_sheets_api()
        result: dict[str, SheetApplicationStatus] = {}
        grouped: dict[str, list[tuple[BulkBatch, str]]] = {}
        for batch in batches:
            if not batch.spreadsheet_id:
                continue
            range_name = (
                f"{quote_sheet_name(batch.sheet_name)}!"
                f"A{batch.data_start_row - 1}:N{_bulk_batch_end_row(batch)}"
            )
            grouped.setdefault(batch.spreadsheet_id, []).append((batch, range_name))

        for spreadsheet_id, entries in grouped.items():
            for chunk_start in range(0, len(entries), 100):
                chunk = entries[chunk_start : chunk_start + 100]
                values_resource = api.spreadsheets().values()
                if hasattr(values_resource, "batchGet"):
                    try:
                        response = values_resource.batchGet(
                            spreadsheetId=spreadsheet_id,
                            ranges=[range_name for _, range_name in chunk],
                            majorDimension="ROWS",
                        ).execute()
                    except HttpError as exc:
                        if not _is_unparseable_range_error(exc):
                            raise
                        rows_by_range = [
                            self._read_bulk_range_rows(
                                values_resource,
                                spreadsheet_id,
                                batch,
                                range_name,
                            )
                            for batch, range_name in chunk
                        ]
                    else:
                        rows_by_range = [
                            item.get("values", [])
                            for item in response.get("valueRanges", [])
                        ]
                        rows_by_range.extend([[]] * (len(chunk) - len(rows_by_range)))
                else:
                    rows_by_range = [
                        self._read_bulk_range_rows(
                            values_resource,
                            spreadsheet_id,
                            batch,
                            range_name,
                        )
                        for batch, range_name in chunk
                    ]
                for (batch, _), rows in zip(chunk, rows_by_range, strict=True):
                    self._collect_bulk_application_statuses(result, batch, rows)
        return result

    @staticmethod
    def _read_bulk_range_rows(
        values_resource: Any,
        spreadsheet_id: str,
        batch: BulkBatch,
        range_name: str,
    ) -> list[list[Any]]:
        try:
            return values_resource.get(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                majorDimension="ROWS",
            ).execute().get("values", [])
        except HttpError as exc:
            if not _is_unparseable_range_error(exc):
                raise
            LOGGER.warning(
                "Bulk application range is unavailable; skipping batch. "
                "batch_id=%s spreadsheet_id=%s range=%s error=%s",
                batch.batch_id,
                spreadsheet_id,
                range_name,
                exc,
            )
            return []

    @staticmethod
    def _collect_bulk_application_statuses(
        result: dict[str, SheetApplicationStatus],
        batch: BulkBatch,
        rows: list[list[Any]],
    ) -> None:
        layout = _bulk_row_layout(rows[0] if rows else [])
        if layout is None:
            return
        for offset, row in enumerate(rows[1:]):
            application_id = _cell(row, layout["application_id"]).strip()
            if not application_id:
                continue
            answer_type = _cell(row, layout["answer_type"]).strip()
            result[application_id] = SheetApplicationStatus(
                application_id=application_id,
                spreadsheet_id=batch.spreadsheet_id,
                batch_id=batch.batch_id,
                sheet_name=batch.sheet_name,
                sheet_id=batch.sheet_id,
                row_number=batch.data_start_row + offset,
                direction=batch.direction,
                answer_type=answer_type or None,
                is_urgent=answer_type == AnswerType.URGENT.value,
                status=(
                    _cell(row, layout["status"]).strip()
                    or ApplicationStatus.NEW.value
                ),
                editor=_cell(row, layout["editor"]).strip(),
                editor_comment=_cell(row, layout["comment"]).strip(),
                application_id_column_index=layout["application_id"],
                status_column_index=layout["status"],
                final_answer=_cell(row, layout["final_answer"]).strip(),
                scriptwriter_response=_cell(
                    row,
                    layout.get("scriptwriter_response", -1),
                ).strip(),
                end_column=layout["end_column"],
            )

    def _read_batch_statuses_sync(
        self,
        batches: list[BulkBatch],
    ) -> dict[str, SheetBulkBatchStatus]:
        api = self._get_sheets_api()
        result: dict[str, SheetBulkBatchStatus] = {}
        for batch in batches:
            spreadsheet_id = batch.spreadsheet_id
            if not spreadsheet_id:
                continue
            range_name = (
                f"{quote_sheet_name(batch.sheet_name)}!"
                f"A{batch.start_row}:N{batch.data_start_row - 1}"
            )
            try:
                response = api.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id,
                    range=range_name,
                    majorDimension="ROWS",
                ).execute()
            except HttpError as exc:
                if _is_unparseable_range_error(exc):
                    LOGGER.warning(
                        "Bulk batch status sheet range is unavailable; skipping batch status. "
                        "batch_id=%s spreadsheet_id=%s sheet_name=%s range=%s error=%s",
                        batch.batch_id,
                        spreadsheet_id,
                        batch.sheet_name,
                        range_name,
                        exc,
                    )
                    continue
                raise
            rows = response.get("values", [])
            header_row = rows[-1] if rows else []
            layout = _bulk_row_layout(header_row)
            if layout is None:
                continue
            row = rows[0] if rows else []
            status = _cell(row, layout["batch_status"]).strip()
            if not status:
                continue
            result[batch.batch_id] = SheetBulkBatchStatus(
                batch_id=batch.batch_id,
                spreadsheet_id=spreadsheet_id,
                sheet_name=batch.sheet_name,
                sheet_id=batch.sheet_id,
                row_number=batch.start_row,
                status=status,
                end_column=layout["end_column"],
            )
        return result

    def _resolve_bulk_batch_locations_sync(
        self,
        batches: list[BulkBatch],
        search_batch_ids: set[str],
    ) -> BulkBatchLocationScan:
        api = self._get_sheets_api()
        locations: dict[str, BulkBatchLocation] = {}
        unavailable_ids: set[str] = set()
        unresolved_by_sheet: dict[
            tuple[str, int, str], list[BulkBatch]
        ] = {}
        expected_by_spreadsheet: dict[
            str, list[tuple[BulkBatch, str, int, str]]
        ] = {}
        fresh_sheet_ids: dict[str, dict[str, int]] = {}

        for batch in batches:
            sheet_ids = fresh_sheet_ids.get(batch.spreadsheet_id)
            if sheet_ids is None:
                sheet_ids = self._read_sheet_ids(
                    api,
                    batch.spreadsheet_id,
                    refresh=True,
                )
                fresh_sheet_ids[batch.spreadsheet_id] = sheet_ids
            current_name = next(
                (name for name, sheet_id in sheet_ids.items() if sheet_id == batch.sheet_id),
                None,
            )
            if current_name is None and sheet_ids.get(batch.sheet_name) == batch.sheet_id:
                current_name = batch.sheet_name
            if current_name is None:
                unavailable_ids.add(batch.batch_id)
                continue
            range_name = (
                f"{quote_sheet_name(current_name)}!"
                f"A{batch.start_row}:N{batch.start_row + 1}"
            )
            expected_by_spreadsheet.setdefault(batch.spreadsheet_id, []).append(
                (batch, range_name, batch.sheet_id, current_name)
            )

        for spreadsheet_id, entries in expected_by_spreadsheet.items():
            for chunk_start in range(0, len(entries), 100):
                chunk = entries[chunk_start : chunk_start + 100]
                values_resource = api.spreadsheets().values()
                try:
                    if hasattr(values_resource, "batchGet"):
                        response = values_resource.batchGet(
                            spreadsheetId=spreadsheet_id,
                            ranges=[entry[1] for entry in chunk],
                            majorDimension="ROWS",
                        ).execute()
                        rows_by_range = [
                            item.get("values", [])
                            for item in response.get("valueRanges", [])
                        ]
                        rows_by_range.extend([[]] * (len(chunk) - len(rows_by_range)))
                    else:
                        rows_by_range = [
                            values_resource.get(
                                spreadsheetId=spreadsheet_id,
                                range=range_name,
                                majorDimension="ROWS",
                            ).execute().get("values", [])
                            for _, range_name, _, _ in chunk
                        ]
                except HttpError as exc:
                    if not _is_unparseable_range_error(exc):
                        raise
                    unavailable_ids.update(entry[0].batch_id for entry in chunk)
                    continue

                for (batch, _, sheet_id, sheet_name), rows in zip(
                    chunk,
                    rows_by_range,
                    strict=True,
                ):
                    if _is_bulk_batch_block(rows, batch.batch_id):
                        locations[batch.batch_id] = BulkBatchLocation(
                            batch_id=batch.batch_id,
                            spreadsheet_id=batch.spreadsheet_id,
                            sheet_name=sheet_name,
                            sheet_id=sheet_id,
                            start_row=batch.start_row,
                        )
                        continue
                    unresolved_by_sheet.setdefault(
                        (batch.spreadsheet_id, sheet_id, sheet_name), []
                    ).append(batch)

        confirmed_missing_ids: set[str] = set()
        ambiguous_rows: dict[str, tuple[int, ...]] = {}
        deferred_ids: set[str] = set()
        for (spreadsheet_id, sheet_id, sheet_name), sheet_batches in unresolved_by_sheet.items():
            due_batches = [
                batch for batch in sheet_batches if batch.batch_id in search_batch_ids
            ]
            deferred_ids.update(
                batch.batch_id
                for batch in sheet_batches
                if batch.batch_id not in search_batch_ids
            )
            if not due_batches:
                continue
            try:
                rows = self._read_sheet_range(
                    api,
                    spreadsheet_id,
                    sheet_name,
                    "A:N",
                )
            except HttpError as exc:
                if not _is_unparseable_range_error(exc):
                    raise
                unavailable_ids.update(batch.batch_id for batch in due_batches)
                continue
            for batch in due_batches:
                candidates = tuple(
                    row_number
                    for row_number in range(1, len(rows) + 1)
                    if _cell(rows[row_number - 1], 1).strip() == batch.batch_id
                    and _is_bulk_batch_block(rows[row_number - 1 : row_number + 1], batch.batch_id)
                )
                if len(candidates) == 1:
                    locations[batch.batch_id] = BulkBatchLocation(
                        batch_id=batch.batch_id,
                        spreadsheet_id=spreadsheet_id,
                        sheet_name=sheet_name,
                        sheet_id=sheet_id,
                        start_row=candidates[0],
                    )
                elif not candidates:
                    confirmed_missing_ids.add(batch.batch_id)
                else:
                    ambiguous_rows[batch.batch_id] = candidates

        return BulkBatchLocationScan(
            locations=locations,
            confirmed_missing_ids=confirmed_missing_ids,
            ambiguous_rows=ambiguous_rows,
            unavailable_ids=unavailable_ids,
            deferred_ids=deferred_ids,
        )

    def _read_bulk_editor_comments_sync(
        self,
        batches: list[BulkBatch],
    ) -> dict[str, list[BulkEditorComment]]:
        api = self._get_sheets_api()
        result: dict[str, list[BulkEditorComment]] = {}
        for batch in batches:
            spreadsheet_id = batch.spreadsheet_id
            if not spreadsheet_id:
                continue
            end_row = _bulk_batch_end_row(batch)
            range_name = (
                f"{quote_sheet_name(batch.sheet_name)}!"
                f"A{batch.data_start_row - 1}:N{end_row}"
            )
            response = api.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                majorDimension="ROWS",
            ).execute()
            rows = response.get("values", [])
            layout = _bulk_row_layout(rows[0] if rows else [])
            if layout is None:
                result[batch.batch_id] = []
                continue
            comments: list[BulkEditorComment] = []
            for offset, row in enumerate(rows[1:]):
                comment = _cell(row, layout["comment"]).strip()
                if not comment:
                    continue
                comments.append(
                    BulkEditorComment(
                        application_id=_cell(row, layout["application_id"]).strip(),
                        spreadsheet_id=spreadsheet_id,
                        sheet_name=batch.sheet_name,
                        sheet_id=batch.sheet_id,
                        row_number=batch.data_start_row + offset,
                        comment=comment,
                        end_column=layout["end_column"],
                    )
                )
            result[batch.batch_id] = comments
        return result

    def _spreadsheet_ids(self) -> list[str]:
        ids = [
            self.direction_spreadsheets.fl_spreadsheet_id,
            self.direction_spreadsheets.sme_spreadsheet_id,
            self.direction_spreadsheets.ai_spreadsheet_id,
            self.direction_spreadsheets.voice_collection_spreadsheet_id,
        ]
        result: list[str] = []
        for spreadsheet_id in ids:
            if spreadsheet_id and spreadsheet_id not in result:
                result.append(spreadsheet_id)
        return result

    def _read_sheet_ids(
        self,
        api: Any,
        spreadsheet_id: str,
        *,
        refresh: bool = False,
    ) -> dict[str, int]:
        if not refresh and spreadsheet_id in self._sheet_ids_cache:
            return self._sheet_ids_cache[spreadsheet_id]
        metadata = api.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties",
        ).execute()
        result = {
            sheet["properties"]["title"]: int(sheet["properties"]["sheetId"])
            for sheet in metadata.get("sheets", [])
        }
        self._sheet_ids_cache[spreadsheet_id] = result
        return result

    def _read_sheet_rows(self, api: Any, spreadsheet_id: str, sheet_name: str) -> list[list[Any]]:
        return self._read_sheet_range(api, spreadsheet_id, sheet_name, "A:X")

    @staticmethod
    def _read_sheet_range(
        api: Any,
        spreadsheet_id: str,
        sheet_name: str,
        range_suffix: str,
    ) -> list[list[Any]]:
        response = api.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!{range_suffix}",
            majorDimension="ROWS",
        ).execute()
        return response.get("values", [])

    def _get_sheets_api(self) -> Any:
        if self._sheets_api is None:
            self._sheets_api = build_google_sheets_api(self.credentials_path)
        return self._sheets_api

    def _reset_sheets_api(self) -> None:
        if self._external_sheets_api:
            return
        self._sheets_api = None
        self._sheet_ids_cache.clear()


class StatusNotificationService:
    """Сравнивает Sheets с SQLite, уведомляет пользователей и обновляет дашборд."""

    def __init__(
        self,
        *,
        repository: DraftRepository,
        status_reader: GoogleSheetsStatusReader,
        notifier: TelegramNotifierProtocol,
        fallback_spreadsheet_id: str = "",
        dashboard_sync: DashboardSyncService | None = None,
        dashboard_sync_interval_seconds: float = 300,
        completed_bulk_dashboard_scan_interval_seconds: float = 3600,
        bulk_relocation_search_interval_seconds: int = 3600,
        legacy_bulk_enabled: bool = False,
        dashboard_outbox_retry_base_seconds: int = 60,
        dashboard_outbox_retry_max_seconds: int = 3600,
        dashboard_outbox_sending_stale_seconds: int = 300,
        google_api_retry: GoogleApiRetryConfig = GoogleApiRetryConfig(),
        notification_max_attempts: int = 10,
        notification_retry_base_seconds: int = 30,
        notification_sending_stale_seconds: int = 300,
        notification_message_max_chars: int = 3500,
        status_not_found_threshold: int = 20,
        status_not_found_recheck_seconds: int = 3600,
        urgent_editor_notifications_enabled: bool = False,
        editor_urgent_chat_id: int | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.repository = repository
        self.status_reader = status_reader
        self.notifier = notifier
        self.fallback_spreadsheet_id = fallback_spreadsheet_id
        self.dashboard_sync = dashboard_sync
        self.dashboard_sync_interval_seconds = dashboard_sync_interval_seconds
        self.completed_bulk_dashboard_scan_interval_seconds = (
            completed_bulk_dashboard_scan_interval_seconds
        )
        self.bulk_relocation_search_interval_seconds = (
            bulk_relocation_search_interval_seconds
        )
        self.legacy_bulk_enabled = legacy_bulk_enabled
        self.dashboard_outbox_retry_base_seconds = dashboard_outbox_retry_base_seconds
        self.dashboard_outbox_retry_max_seconds = dashboard_outbox_retry_max_seconds
        self.dashboard_outbox_sending_stale_seconds = (
            dashboard_outbox_sending_stale_seconds
        )
        self.google_api_retry = google_api_retry
        self.notification_max_attempts = notification_max_attempts
        self.notification_retry_base_seconds = notification_retry_base_seconds
        self.notification_sending_stale_seconds = notification_sending_stale_seconds
        self.notification_message_max_chars = notification_message_max_chars
        self.status_not_found_threshold = status_not_found_threshold
        self.status_not_found_recheck_seconds = status_not_found_recheck_seconds
        self.urgent_editor_notifications_enabled = urgent_editor_notifications_enabled
        self.editor_urgent_chat_id = editor_urgent_chat_id
        self.clock = clock
        self._last_dashboard_sync_at: float | None = None
        self._last_completed_bulk_scan_at: float | None = None
        self._polling_iteration = 0

    async def run_once(self) -> None:
        """Deliver durable events, observe Sheets, and persist dashboard projections."""
        self._polling_iteration += 1
        await self._deliver_outbox()
        await self._deliver_dashboard_outbox()
        dashboard_sync_due = self._is_dashboard_sync_due()
        completed_bulk_scan_due = (
            self.legacy_bulk_enabled and self._is_completed_bulk_scan_due()
        )
        # New bulk reservations are tracked as ordinary submitted applications.
        # Do not scan retired bulk_batches in the production polling loop.
        if self.legacy_bulk_enabled:
            if hasattr(self.repository, "list_active_bulk_batches"):
                active_batches = await self.repository.list_active_bulk_batches()
            else:
                active_batches = await self.repository.list_bulk_batches()
            completed_batches = (
                await self.repository.list_completed_bulk_batches()
                if completed_bulk_scan_due
                else []
            )
            scan_batches = _unique_batches([*active_batches, *completed_batches])
        else:
            active_batches = []
            completed_batches = []
            scan_batches = []
        relocated_batch_ids: set[str] = set()
        if scan_batches and hasattr(self.status_reader, "resolve_bulk_batch_locations"):
            (
                active_batches,
                completed_batches,
                scan_batches,
                verified_batch_ids,
                relocated_batch_ids,
            ) = await self._resolve_bulk_batch_locations(
                active_batches,
                completed_batches,
                scan_batches,
            )
            scanned_batch_ids = verified_batch_ids
        else:
            scanned_batch_ids = {batch.batch_id for batch in scan_batches}
        current_batch_statuses = (
            await self.status_reader.read_batch_statuses(active_batches)
            if active_batches and hasattr(self.status_reader, "read_batch_statuses")
            else {}
        )
        tracked = await self.repository.list_submitted_applications()

        fallback_full_scan = bool(tracked) and self._polling_iteration % 10 == 0
        if tracked and hasattr(self.status_reader, "read_statuses_for"):
            statuses = await self.status_reader.read_statuses_for(
                tracked,
                fallback_full_scan=fallback_full_scan,
            )
        elif tracked:
            statuses = await self.status_reader.read_statuses()
        else:
            statuses = {}
        if scan_batches and hasattr(self.status_reader, "read_bulk_application_statuses"):
            statuses.update(
                await self.status_reader.read_bulk_application_statuses(scan_batches)
            )
        unavailable_single_sources = getattr(
            self.status_reader,
            "unavailable_single_sources",
            set(),
        )
        await self._process_bulk_batch_statuses(
            active_batches,
            current_batch_statuses,
            statuses,
        )
        notifications_by_user: dict[int, list[StatusNotification]] = {}
        non_notified_updates: list[StatusNotification] = []
        deletion_candidates: list[tuple[SubmittedApplication, SheetApplicationStatus]] = []

        for application in tracked:
            current = statuses.get(application.application_id)
            if current is None:
                # A missing row is meaningful only when its source was actually
                # scanned in this polling iteration. Completed bulk batches are
                # checked by the archive scan, so their child rows must not
                # accumulate false not_found counters between archive passes.
                if application.batch_id and application.batch_id not in scanned_batch_ids:
                    continue
                if (
                    not application.batch_id
                    and application.spreadsheet_id
                    and application.sheet_name
                    and (application.spreadsheet_id, application.sheet_name)
                    in unavailable_single_sources
                ):
                    continue
                await self.repository.mark_submitted_application_not_found(
                    application.application_id,
                    threshold=self.status_not_found_threshold,
                    recheck_seconds=self.status_not_found_recheck_seconds,
                )
                next_count = application.not_found_count + 1
                if next_count <= self.status_not_found_threshold:
                    LOGGER.info(
                        "Tracked application not found in direction sheets: "
                        "application_id=%s not_found_count=%s threshold=%s",
                        application.application_id,
                        next_count,
                        self.status_not_found_threshold,
                    )
                continue

            if current.status == ApplicationStatus.DELETION.value:
                deletion_candidates.append((application, current))
                continue
            if application.deletion_seen_count:
                await self.repository.reset_application_deletion_state(
                    application.application_id
                )

            status_changed = bool(current.status) and current.status != application.last_known_status
            editor_changed = (
                (current.editor or EDITOR_NOT_SELECTED)
                != (application.last_seen_editor or EDITOR_NOT_SELECTED)
            )
            is_chips = (
                ChangeType.normalize(current.change_type or application.change_type)
                == ChangeType.CHIPS
            )
            # CHIPS uses "Ответ сценариста" for clarification replies, not as
            # the regular final-answer signal that triggers scenario-writer
            # notifications for ADD/EDIT rows.
            if application.batch_id or is_chips:
                final_answer_changed = False
            else:
                # The sheet may receive the final status and answer text in
                # different polling cycles, in either order.
                final_answer_changed = (
                    current.status == ApplicationStatus.FINAL_ANSWER_READY.value
                    and bool((current.final_answer or "").strip())
                    and (
                        status_changed
                        or (current.final_answer or "").strip()
                        != (application.last_seen_final_answer or "").strip()
                    )
                )
            stable_updates: dict[str, Any] = {}
            editor_comment_change = StableFieldChange(False, {})
            scriptwriter_response_change = StableFieldChange(False, {})
            if not application.batch_id:
                editor_comment_change = _stable_text_field_change(
                    current_value=current.editor_comment,
                    last_sent_value=application.last_seen_editor_comment,
                    pending_value=application.pending_editor_comment,
                    pending_seen_count=application.pending_editor_comment_seen_count,
                    pending_field="pending_editor_comment",
                    pending_count_field="pending_editor_comment_seen_count",
                    last_sent_field="last_seen_editor_comment",
                )
                stable_updates.update(editor_comment_change.updates)
                scriptwriter_response_change = self._scriptwriter_response_change(
                    application,
                    current,
                )
                stable_updates.update(scriptwriter_response_change.updates)
            notification = StatusNotification(
                tracked=application,
                current=current,
                status_changed=status_changed,
                final_answer_changed=final_answer_changed,
                editor_changed=editor_changed,
                editor_comment_ready=editor_comment_change.ready,
                stable_tracking_updates=stable_updates,
            )
            scriptwriter_response_event = (
                self._urgent_scriptwriter_response_event(notification)
                if scriptwriter_response_change.ready
                else None
            )
            should_notify_user = self._should_notify(notification)
            if scriptwriter_response_event is not None:
                await self.repository.enqueue_notification_event(
                    **scriptwriter_response_event,
                    application_updates=[
                        self._application_tracking_update(
                            notification.tracked,
                            notification.current,
                            stable_updates=notification.stable_tracking_updates,
                        )
                    ],
                    application_events=self._application_events(notification),
                    dashboard_projections=(
                        []
                        if should_notify_user
                        else self._tracking_dashboard_projections(
                            [notification],
                            scan_batches,
                            current_batch_statuses,
                            statuses,
                            include_unchanged_singles=True,
                        )
                    ),
                )
                if not should_notify_user:
                    continue
            tracking_fields_changed = self._stable_tracking_updates_changed(
                application,
                stable_updates,
            )
            if (
                not status_changed
                and not final_answer_changed
                and not editor_changed
                and not notification.editor_comment_ready
                and not tracking_fields_changed
            ):
                non_notified_updates.append(
                    notification
                )
                continue

            if should_notify_user:
                notifications_by_user.setdefault(application.telegram_user_id, []).append(
                    notification
                )
            else:
                non_notified_updates.append(notification)

        if non_notified_updates:
            await self.repository.update_application_tracking_batch(
                [
                    self._application_tracking_update(
                        item.tracked,
                        item.current,
                        stable_updates=item.stable_tracking_updates,
                    )
                    for item in non_notified_updates
                ],
                application_events=[
                    event
                    for item in non_notified_updates
                    for event in self._application_events(item)
                ],
                dashboard_projections=self._tracking_dashboard_projections(
                    non_notified_updates,
                    scan_batches,
                    current_batch_statuses,
                    statuses,
                    include_unchanged_singles=dashboard_sync_due,
                ),
            )

        for telegram_user_id, notifications in notifications_by_user.items():
            text = await self._render_notification_message(notifications)
            snapshot = [
                self._notification_snapshot(notification)
                for notification in notifications
            ]
            snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            dedupe_key = self._dedupe_key("application-status", telegram_user_id, snapshot_json)
            await self.repository.enqueue_notification_event(
                telegram_user_id=telegram_user_id,
                event_type="application-status",
                dedupe_key=dedupe_key,
                snapshot_json=snapshot_json,
                chunks=_split_html_message(text, self.notification_message_max_chars),
                application_updates=[
                    self._application_tracking_update(
                        item.tracked,
                        item.current,
                        stable_updates=item.stable_tracking_updates,
                    )
                    for item in notifications
                ],
                application_events=[
                    event
                    for item in notifications
                    for event in self._application_events(item)
                ],
                dashboard_projections=self._tracking_dashboard_projections(
                    notifications,
                    scan_batches,
                    current_batch_statuses,
                    statuses,
                    include_unchanged_singles=True,
                ),
            )
        if dashboard_sync_due:
            await self._enqueue_periodic_dashboard_projections(
                tracked,
                statuses,
                scan_batches,
                current_batch_statuses,
            )
            self._last_dashboard_sync_at = self.clock()
        elif relocated_batch_ids:
            await self._enqueue_periodic_dashboard_projections(
                [],
                statuses,
                [
                    batch
                    for batch in scan_batches
                    if batch.batch_id in relocated_batch_ids
                ],
                current_batch_statuses,
            )
        for application, current in sorted(
            deletion_candidates,
            key=lambda item: (
                item[1].spreadsheet_id,
                item[1].sheet_id,
                item[1].row_number,
            ),
            reverse=True,
        ):
            await self._process_application_deletion(application, current)
        if completed_bulk_scan_due:
            self._last_completed_bulk_scan_at = self.clock()
        await self._deliver_outbox()
        await self._deliver_dashboard_outbox()

    async def _process_application_deletion(
        self,
        application: SubmittedApplication,
        current: SheetApplicationStatus,
    ) -> None:
        if application.batch_id or current.batch_id:
            message = "Deletion status is unsupported for legacy bulk batch rows"
            LOGGER.warning(
                "Application deletion skipped for legacy bulk row: application_id=%s batch_id=%s",
                application.application_id,
                application.batch_id or current.batch_id,
            )
            await self.repository.record_application_deletion_error(
                application.application_id,
                message,
            )
            return

        seen_count = await self.repository.mark_application_deletion_seen(
            application.application_id
        )
        if seen_count < 2:
            LOGGER.info(
                "Application deletion requested: application_id=%s seen_count=%s",
                application.application_id,
                seen_count,
            )
            return

        if not current.spreadsheet_id or current.sheet_id is None or not current.sheet_name:
            await self.repository.record_application_deletion_error(
                application.application_id,
                "Deletion skipped: missing sheet coordinates",
            )
            return

        section_kind = sheet_section_kind(
            answer_type=current.answer_type or application.answer_type,
            change_type=ChangeType.normalize(current.change_type or application.change_type),
        )
        lock_key = sheet_section_lock_key(
            spreadsheet_id=current.spreadsheet_id,
            sheet_name=current.sheet_name,
            section_kind=section_kind,
        )
        owner = f"delete:{application.application_id}"
        acquired = await self.repository.acquire_bulk_section_lock(
            lock_key=lock_key,
            owner=owner,
            ttl_seconds=600,
        )
        if not acquired:
            await self.repository.record_application_deletion_error(
                application.application_id,
                "Deletion deferred: section lock is busy",
            )
            LOGGER.info(
                "Application deletion deferred by section lock: application_id=%s lock_key=%s",
                application.application_id,
                lock_key,
            )
            return
        try:
            await self.status_reader.delete_application_row(current)
            result = await self.repository.complete_application_deletion(
                application_id=application.application_id,
                spreadsheet_id=current.spreadsheet_id,
                sheet_id=current.sheet_id,
                deleted_row_number=current.row_number,
            )
            LOGGER.warning(
                "Application deleted by status: application_id=%s sheet=%s row=%s result=%s",
                application.application_id,
                current.sheet_name,
                current.row_number,
                result,
            )
        except Exception as exc:
            await self.repository.record_application_deletion_error(
                application.application_id,
                f"{type(exc).__name__}: {exc}",
            )
            LOGGER.exception(
                "Application deletion failed: application_id=%s sheet=%s row=%s",
                application.application_id,
                current.sheet_name,
                current.row_number,
            )
        finally:
            await self.repository.release_bulk_section_lock(lock_key=lock_key, owner=owner)

    async def _resolve_bulk_batch_locations(
        self,
        active_batches: list[BulkBatch],
        completed_batches: list[BulkBatch],
        scan_batches: list[BulkBatch],
    ) -> tuple[list[BulkBatch], list[BulkBatch], list[BulkBatch], set[str], set[str]]:
        search_batch_ids = {
            batch.batch_id
            for batch in scan_batches
            if _bulk_location_search_due(batch)
        }
        scan = await self.status_reader.resolve_bulk_batch_locations(
            scan_batches,
            search_batch_ids=search_batch_ids,
        )
        batches_by_id = {batch.batch_id: batch for batch in scan_batches}
        resolved_by_id: dict[str, BulkBatch] = {}
        relocated_batch_ids: set[str] = set()

        for batch_id, location in scan.locations.items():
            batch = batches_by_id[batch_id]
            moved = (
                batch.spreadsheet_id != location.spreadsheet_id
                or batch.sheet_name != location.sheet_name
                or batch.sheet_id != location.sheet_id
                or batch.start_row != location.start_row
            )
            needs_reset = batch.location_state != BulkBatchLocationState.KNOWN.value
            if moved or needs_reset:
                restored = await self.repository.restore_bulk_batch_location(
                    batch_id,
                    spreadsheet_id=location.spreadsheet_id,
                    sheet_name=location.sheet_name,
                    sheet_id=location.sheet_id,
                    start_row=location.start_row,
                )
                if restored is not None:
                    resolved_by_id[batch_id] = restored
                    if moved:
                        relocated_batch_ids.add(batch_id)
                        LOGGER.warning(
                            "Bulk batch location restored: batch_id=%s sheet_id=%s "
                            "old_sheet=%s new_sheet=%s old_start_row=%s new_start_row=%s",
                            batch_id,
                            location.sheet_id,
                            batch.sheet_name,
                            location.sheet_name,
                            batch.start_row,
                            location.start_row,
                        )
                    continue
            resolved_by_id[batch_id] = batch

        for batch_id in scan.confirmed_missing_ids:
            await self.repository.record_bulk_batch_location_problem(
                batch_id,
                state=BulkBatchLocationState.MISSING.value,
                error="Batch ID was not found on its source sheet",
                recheck_seconds=self.bulk_relocation_search_interval_seconds,
            )
            await self.repository.mark_bulk_batch_applications_not_found(
                batch_id,
                threshold=self.status_not_found_threshold,
                recheck_seconds=self.status_not_found_recheck_seconds,
            )
            LOGGER.warning(
                "Bulk batch was not found on its source sheet: batch_id=%s",
                batch_id,
            )

        for batch_id, rows in scan.ambiguous_rows.items():
            message = f"Duplicate batch ID candidates at rows: {','.join(map(str, rows))}"
            await self.repository.record_bulk_batch_location_problem(
                batch_id,
                state=BulkBatchLocationState.AMBIGUOUS.value,
                error=message,
                recheck_seconds=self.bulk_relocation_search_interval_seconds,
            )
            LOGGER.error(
                "Bulk batch location is ambiguous: batch_id=%s candidate_rows=%s",
                batch_id,
                rows,
            )

        verified_ids = set(scan.locations)

        def resolved_batches(source: list[BulkBatch]) -> list[BulkBatch]:
            return [
                resolved_by_id[batch.batch_id]
                for batch in source
                if batch.batch_id in verified_ids
            ]

        resolved_active = resolved_batches(active_batches)
        resolved_completed = resolved_batches(completed_batches)
        return (
            resolved_active,
            resolved_completed,
            _unique_batches([*resolved_active, *resolved_completed]),
            verified_ids,
            relocated_batch_ids,
        )

    def _is_dashboard_sync_due(self) -> bool:
        if self.dashboard_sync is None:
            return False
        if self._last_dashboard_sync_at is None:
            return True
        return (
            self.clock() - self._last_dashboard_sync_at
            >= self.dashboard_sync_interval_seconds
        )

    def _is_completed_bulk_scan_due(self) -> bool:
        if self.dashboard_sync is None:
            return False
        if self._last_completed_bulk_scan_at is None:
            return True
        return (
            self.clock() - self._last_completed_bulk_scan_at
            >= self.completed_bulk_dashboard_scan_interval_seconds
        )

    def _should_notify(self, notification: StatusNotification) -> bool:
        if notification.final_answer_changed:
            return True
        if notification.editor_comment_ready and not notification.tracked.batch_id:
            return True
        if not notification.status_changed:
            return False
        if notification.tracked.batch_id:
            return notification.current.status == BulkApplicationStatus.NEEDS_CLARIFICATION.value
        return notification.current.status in SINGLE_IMPORTANT_STATUSES

    @staticmethod
    def _stable_tracking_updates_changed(
        tracked: SubmittedApplication,
        updates: dict[str, Any],
    ) -> bool:
        return any(getattr(tracked, key) != value for key, value in updates.items())

    def _scriptwriter_response_change(
        self,
        tracked: SubmittedApplication,
        current: SheetApplicationStatus,
    ) -> StableFieldChange:
        if not self.urgent_editor_notifications_enabled or self.editor_urgent_chat_id is None:
            return StableFieldChange(False, {})
        if tracked.batch_id or current.batch_id:
            return StableFieldChange(False, {})
        is_urgent = (
            current.answer_type == AnswerType.URGENT.value
            or tracked.answer_type == AnswerType.URGENT.value
            or current.is_urgent is True
            or tracked.is_urgent is True
        )
        if not is_urgent:
            return StableFieldChange(False, {})
        return _stable_text_field_change(
            current_value=current.scriptwriter_response,
            last_sent_value=tracked.last_seen_scriptwriter_response,
            pending_value=tracked.pending_scriptwriter_response,
            pending_seen_count=tracked.pending_scriptwriter_response_seen_count,
            pending_field="pending_scriptwriter_response",
            pending_count_field="pending_scriptwriter_response_seen_count",
            last_sent_field="last_seen_scriptwriter_response",
        )

    def _urgent_scriptwriter_response_event(
        self,
        notification: StatusNotification,
    ) -> dict[str, Any] | None:
        """Build editor-chat event when a writer answers an urgent clarification."""
        if not self.urgent_editor_notifications_enabled:
            return None
        if self.editor_urgent_chat_id is None:
            return None
        tracked = notification.tracked
        current = notification.current
        if tracked.batch_id or current.batch_id:
            return None
        is_urgent = (
            current.answer_type == AnswerType.URGENT.value
            or tracked.answer_type == AnswerType.URGENT.value
            or current.is_urgent is True
            or tracked.is_urgent is True
        )
        if not is_urgent:
            return None
        scriptwriter_response = (current.scriptwriter_response or "").strip()
        if not scriptwriter_response:
            return None
        if scriptwriter_response == (tracked.last_seen_scriptwriter_response or "").strip():
            return None

        response_hash = hashlib.sha256(
            scriptwriter_response.encode("utf-8")
        ).hexdigest()
        snapshot = {
            "application_id": tracked.application_id,
            "direction": current.direction or tracked.direction or "",
            "scriptwriter": current.scriptwriter or "",
            "intent": current.intent or "",
            "status": current.status,
            "scriptwriter_response": scriptwriter_response,
            "row_link": self._row_link(current),
        }
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        dedupe_key = (
            f"{URGENT_EDITOR_SCRIPTWRITER_RESPONSE_EVENT_TYPE}:"
            f"{tracked.application_id}:{response_hash}"
        )
        return {
            "telegram_user_id": self.editor_urgent_chat_id,
            "event_type": URGENT_EDITOR_SCRIPTWRITER_RESPONSE_EVENT_TYPE,
            "dedupe_key": dedupe_key,
            "snapshot_json": snapshot_json,
            "chunks": _split_html_message(
                _render_urgent_scriptwriter_response_notification(snapshot),
                self.notification_message_max_chars,
            ),
        }

    async def _process_bulk_batch_statuses(
        self,
        batches: list[BulkBatch],
        current_by_batch: dict[str, SheetBulkBatchStatus],
        application_statuses: dict[str, SheetApplicationStatus],
    ) -> dict[str, SheetBulkBatchStatus]:
        """Обработать статусы пачек и уведомить только о завершении."""
        if not batches:
            return {}
        notifications_by_user: dict[int, list[tuple[BulkBatch, SheetBulkBatchStatus]]] = {}
        for batch in batches:
            current = current_by_batch.get(batch.batch_id)
            if current is None:
                continue
            if not current.status or current.status == batch.last_known_batch_status:
                continue
            item = (batch, current)
            if current.status == BulkBatchStatus.DONE.value:
                notifications_by_user.setdefault(batch.telegram_user_id, []).append(item)
            else:
                projection = self._bulk_dashboard_projection(
                    batch,
                    current.status,
                    application_statuses,
                    row_link=self._batch_status_row_link(current),
                )
                await self.repository.update_bulk_batch_status(
                    batch.batch_id,
                    batch_status=current.status,
                    last_known_batch_status=current.status,
                    dashboard_projection=projection["snapshot"],
                )

        for telegram_user_id, items in notifications_by_user.items():
            snapshot_json = json.dumps(
                [
                    {
                        "batch_id": batch.batch_id,
                        "from": batch.last_known_batch_status,
                        "to": current.status,
                        "row": current.row_number,
                    }
                    for batch, current in items
                ],
                ensure_ascii=False,
                sort_keys=True,
            )
            await self.repository.enqueue_notification_event(
                telegram_user_id=telegram_user_id,
                event_type="bulk-batch-status",
                dedupe_key=self._dedupe_key(
                    "bulk-batch-status",
                    telegram_user_id,
                    snapshot_json,
                ),
                snapshot_json=snapshot_json,
                chunks=_split_html_message(
                    self._render_bulk_batch_status_message(items),
                    self.notification_message_max_chars,
                ),
                batch_updates=[
                    {"batch_id": batch.batch_id, "status": current.status}
                    for batch, current in items
                ],
                dashboard_projections=[
                    self._bulk_dashboard_projection(
                        batch,
                        current.status,
                        application_statuses,
                        row_link=self._batch_status_row_link(current),
                    )
                    for batch, current in items
                ],
            )
        return current_by_batch

    def _render_bulk_batch_status_message(
        self,
        items: list[tuple[BulkBatch, SheetBulkBatchStatus]],
    ) -> str:
        lines = ["<b>Массовая заявка готова</b>", ""]
        for _, current in items:
            lines.append(
                f'<a href="{escape(self._batch_status_row_link(current), quote=True)}">'
                f"Открыть массовую заявку {escape(current.batch_id)}</a>"
            )
        return "\n".join(lines)

    async def _update_tracking(
        self,
        tracked: SubmittedApplication,
        current: SheetApplicationStatus,
        *,
        sync_dashboard: bool = True,
    ) -> None:
        await self.repository.update_submitted_application_status(
            tracked.application_id,
            spreadsheet_id=current.spreadsheet_id,
            sheet_id=current.sheet_id,
            sheet_name=current.sheet_name,
            last_known_status=current.status or tracked.last_known_status,
            last_seen_row_number=current.row_number,
            last_seen_editor=current.editor or EDITOR_NOT_SELECTED,
            last_seen_editor_comment=current.editor_comment,
            last_seen_final_answer=current.final_answer,
        )

    async def _deliver_outbox(self) -> None:
        while True:
            item = await self.repository.claim_next_notification(
                stale_after_seconds=self.notification_sending_stale_seconds,
            )
            if item is None:
                return
            try:
                result = await self.notifier.send_message(
                    item.telegram_user_id,
                    item.html,
                    parse_mode="HTML",
                    reply_markup=await self._reply_markup_for_outbox_item(item),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except Exception as exc:
                LOGGER.exception(
                    "Telegram outbox delivery failed: event_id=%s telegram_user_id=%s",
                    item.event_id,
                    item.telegram_user_id,
                )
                await self.repository.fail_notification(
                    item.event_id,
                    error=str(exc),
                    max_attempts=self.notification_max_attempts,
                    retry_base_seconds=self.notification_retry_base_seconds,
                )
                continue
            await self.repository.complete_notification(
                item.event_id,
                telegram_message_id=getattr(result, "message_id", None),
            )

    async def _deliver_dashboard_outbox(self) -> None:
        if self.dashboard_sync is None:
            return
        items = await self.repository.claim_dashboard_projections(
            stale_after_seconds=self.dashboard_outbox_sending_stale_seconds,
        )
        if not items:
            return
        try:
            await execute_with_retry_async(
                lambda: self.dashboard_sync.sync_projections(items),
                config=self.google_api_retry,
                operation_id="dashboard-outbox-batch",
                reset_client=getattr(self.dashboard_sync, "reset_client", None),
            )
        except Exception as exc:
            LOGGER.exception(
                "Dashboard outbox delivery failed: count=%s",
                len(items),
            )
            await self.repository.fail_dashboard_projections(
                items,
                error=str(exc),
                retry_base_seconds=self.dashboard_outbox_retry_base_seconds,
                retry_max_seconds=self.dashboard_outbox_retry_max_seconds,
            )
            return
        await self.repository.complete_dashboard_projections(items)

    async def _enqueue_periodic_dashboard_projections(
        self,
        tracked: list[SubmittedApplication],
        statuses: dict[str, SheetApplicationStatus],
        batches: list[BulkBatch],
        batch_statuses: dict[str, SheetBulkBatchStatus],
    ) -> None:
        if self.dashboard_sync is None:
            return
        for application in tracked:
            if application.batch_id:
                continue
            current = statuses.get(application.application_id)
            if current is None:
                continue
            projection = self._application_dashboard_projection(application, current)
            await self.repository.upsert_dashboard_projection(
                entity_type=projection["entity_type"],
                entity_id=projection["entity_id"],
                snapshot=projection["snapshot"],
            )
        for batch in batches:
            current = batch_statuses.get(batch.batch_id)
            status = current.status if current is not None else batch.last_known_batch_status
            row_link = (
                self._batch_status_row_link(current)
                if current is not None
                else self._batch_row_link(batch)
            )
            projection = self._bulk_dashboard_projection(
                batch,
                status,
                statuses,
                row_link=row_link,
            )
            await self.repository.upsert_dashboard_projection(
                entity_type=projection["entity_type"],
                entity_id=projection["entity_id"],
                snapshot=projection["snapshot"],
            )

    def _tracking_dashboard_projections(
        self,
        notifications: list[StatusNotification],
        batches: list[BulkBatch],
        batch_statuses: dict[str, SheetBulkBatchStatus],
        application_statuses: dict[str, SheetApplicationStatus],
        *,
        include_unchanged_singles: bool,
    ) -> list[dict[str, Any]]:
        if self.dashboard_sync is None:
            return []
        projections: dict[tuple[str, str], dict[str, Any]] = {}
        batches_by_id = {batch.batch_id: batch for batch in batches}
        for notification in notifications:
            batch_id = notification.tracked.batch_id
            changed = (
                notification.status_changed
                or notification.final_answer_changed
                or notification.editor_changed
            )
            if not batch_id:
                if include_unchanged_singles or changed:
                    projection = self._application_dashboard_projection(
                        notification.tracked,
                        notification.current,
                    )
                    projections[
                        (projection["entity_type"], projection["entity_id"])
                    ] = projection
                continue
            if not changed:
                continue
            batch = batches_by_id.get(batch_id)
            if batch is None:
                continue
            current_batch = batch_statuses.get(batch_id)
            projection = self._bulk_dashboard_projection(
                batch,
                (
                    current_batch.status
                    if current_batch is not None
                    else batch.last_known_batch_status
                ),
                application_statuses,
                row_link=(
                    self._batch_status_row_link(current_batch)
                    if current_batch is not None
                    else self._batch_row_link(batch)
                ),
            )
            projections[(projection["entity_type"], projection["entity_id"])] = projection
        return list(projections.values())

    def _application_dashboard_projection(
        self,
        tracked: SubmittedApplication,
        current: SheetApplicationStatus,
    ) -> dict[str, Any]:
        return {
            "entity_type": DashboardEntityType.APPLICATION.value,
            "entity_id": tracked.application_id,
            "snapshot": dashboard_projection(
                dashboard_tracked_row(
                    tracked=tracked,
                    current=current,
                    row_link=self._row_link(current),
                )
            ),
        }

    def _bulk_dashboard_projection(
        self,
        batch: BulkBatch,
        status: str,
        application_statuses: dict[str, SheetApplicationStatus],
        *,
        row_link: str,
    ) -> dict[str, Any]:
        batch_rows = [
            current
            for current in application_statuses.values()
            if current.batch_id == batch.batch_id
        ]
        return {
            "entity_type": DashboardEntityType.BULK_BATCH.value,
            "entity_id": batch.batch_id,
            "snapshot": dashboard_projection(
                dashboard_bulk_batch_row(
                    batch=batch,
                    status=status,
                    row_link=row_link,
                    final_answer_present=any(
                        bool(current.final_answer) for current in batch_rows
                    ),
                    editors=tuple(
                        current.editor or EDITOR_NOT_SELECTED
                        for current in batch_rows
                    ),
                )
            ),
        }

    @staticmethod
    def _application_tracking_update(
        tracked: SubmittedApplication,
        current: SheetApplicationStatus,
        *,
        stable_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stable_updates = stable_updates or {}
        return {
            "application_id": tracked.application_id,
            "spreadsheet_id": current.spreadsheet_id,
            "sheet_id": current.sheet_id,
            "sheet_name": current.sheet_name,
            "last_known_status": current.status or tracked.last_known_status,
            "last_seen_row_number": current.row_number,
            "last_seen_editor": current.editor or EDITOR_NOT_SELECTED,
            "last_seen_editor_comment": stable_updates.get(
                "last_seen_editor_comment",
                tracked.last_seen_editor_comment,
            ),
            "last_seen_final_answer": current.final_answer,
            "last_seen_scriptwriter_response": stable_updates.get(
                "last_seen_scriptwriter_response",
                tracked.last_seen_scriptwriter_response,
            ),
            "pending_editor_comment": stable_updates.get(
                "pending_editor_comment",
                tracked.pending_editor_comment,
            ),
            "pending_editor_comment_seen_count": stable_updates.get(
                "pending_editor_comment_seen_count",
                tracked.pending_editor_comment_seen_count,
            ),
            "pending_scriptwriter_response": stable_updates.get(
                "pending_scriptwriter_response",
                tracked.pending_scriptwriter_response,
            ),
            "pending_scriptwriter_response_seen_count": stable_updates.get(
                "pending_scriptwriter_response_seen_count",
                tracked.pending_scriptwriter_response_seen_count,
            ),
            "change_type": current.change_type or tracked.change_type,
        }

    @staticmethod
    def _application_events(notification: StatusNotification) -> list[dict[str, Any]]:
        tracked = notification.tracked
        current = notification.current
        metadata = {
            "spreadsheet_id": current.spreadsheet_id or tracked.spreadsheet_id,
            "sheet_id": (
                current.sheet_id
                if current.sheet_id is not None
                else tracked.sheet_id
            ),
            "sheet_name": current.sheet_name or tracked.sheet_name,
            "row_number": current.row_number,
            "direction": current.direction or tracked.direction,
            "answer_type": current.answer_type or tracked.answer_type,
            "change_type": current.change_type or tracked.change_type,
            "batch_id": current.batch_id or tracked.batch_id,
        }
        base = {
            "application_id": tracked.application_id,
            "telegram_user_id": tracked.telegram_user_id,
            "metadata": metadata,
        }
        events: list[dict[str, Any]] = []
        if notification.status_changed:
            events.append(
                {
                    **base,
                    "event_type": "status_changed",
                    "old_value": tracked.last_known_status,
                    "new_value": current.status,
                }
            )
        if notification.editor_changed:
            events.append(
                {
                    **base,
                    "event_type": "editor_changed",
                    "old_value": tracked.last_seen_editor,
                    "new_value": current.editor,
                }
            )
        if notification.editor_comment_ready:
            events.append(
                {
                    **base,
                    "event_type": "editor_comment_added",
                    "old_value": tracked.last_seen_editor_comment,
                    "new_value": current.editor_comment,
                }
            )
        if notification.final_answer_changed:
            events.append(
                {
                    **base,
                    "event_type": "final_answer_added",
                    "old_value": tracked.last_seen_final_answer,
                    "new_value": current.final_answer,
                }
            )
        if "last_seen_scriptwriter_response" in notification.stable_tracking_updates:
            events.append(
                {
                    **base,
                    "event_type": "scriptwriter_response_added",
                    "old_value": tracked.last_seen_scriptwriter_response,
                    "new_value": notification.stable_tracking_updates[
                        "last_seen_scriptwriter_response"
                    ],
                }
            )
        return events

    @staticmethod
    def _notification_snapshot(notification: StatusNotification) -> dict[str, Any]:
        return {
            "application_id": notification.tracked.application_id,
            "from": notification.tracked.last_known_status,
            "to": notification.current.status,
            "row": notification.current.row_number,
            "comment": notification.current.editor_comment,
            "final_answer": notification.current.final_answer,
        }

    @staticmethod
    def _dedupe_key(event_type: str, telegram_user_id: int, snapshot_json: str) -> str:
        digest = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        return f"{event_type}:{telegram_user_id}:{digest}"

    async def _render_notification_message(self, notifications: list[StatusNotification]) -> str:
        regular_notifications = [
            notification for notification in notifications if not notification.tracked.batch_id
        ]
        bulk_notifications: dict[str, list[StatusNotification]] = {}
        for notification in notifications:
            if notification.tracked.batch_id:
                bulk_notifications.setdefault(notification.tracked.batch_id, []).append(
                    notification
                )

        lines: list[str] = []
        if regular_notifications:
            lines.append("<b>Изменения по заявкам</b>")
            grouped_regular: dict[str, dict[str, list[StatusNotification]]] = {}
            for notification in regular_notifications:
                direction = (
                    notification.current.direction
                    or notification.tracked.direction
                    or "Без направления"
                )
                group_key = _notification_type_group(notification)
                grouped_regular.setdefault(direction, {}).setdefault(
                    group_key,
                    [],
                ).append(notification)

            for direction in sorted(grouped_regular):
                lines.append("")
                lines.append(f"<b>{escape(direction)}</b>")
                for group_key in sorted(grouped_regular[direction]):
                    lines.append(f"<b>{escape(group_key)}</b>")
                    for notification in grouped_regular[direction][group_key]:
                        lines.extend(self._render_regular_lines(notification))

        for batch_id, batch_notifications in bulk_notifications.items():
            if lines:
                lines.append("")
            lines.extend(
                await self._render_bulk_clarification_lines(
                    batch_id,
                    batch_notifications,
                )
            )

        return "\n".join(lines)

    def _render_regular_lines(self, notification: StatusNotification) -> list[str]:
        current = notification.current
        result: list[str] = []
        summary = _regular_notification_summary(notification)
        if summary:
            result.append(f"• {self._application_link(current)}: {summary}")
        if notification.final_answer_changed:
            result.append(f"<b>Итоговый ответ по заявке {self._application_link(current)}</b>")
            result.append(_render_answer_block(current.final_answer, self._row_link(current)))
        if current.editor_comment and notification.editor_comment_ready:
            result.append(
                f"<b>Комментарий редактора по заявке {self._application_link(current)}</b>"
            )
            if current.status:
                result.append(f"<b>Статус:</b> {escape(current.status)}")
            result.append(f"<blockquote>{escape(current.editor_comment)}</blockquote>")
        return result

    async def _render_bulk_clarification_lines(
        self,
        batch_id: str,
        notifications: list[StatusNotification],
    ) -> list[str]:
        first = notifications[0]
        batch_link = await self._batch_link(batch_id, first.current)
        lines = [
            "<b>Нужны пояснения по массовой заявке</b>",
            f'<a href="{escape(batch_link, quote=True)}">'
            f"Открыть массовую заявку {escape(batch_id)}</a>",
            "",
        ]
        for notification in notifications:
            line = self._application_link(notification.current)
            if notification.current.editor_comment:
                line += f": {escape(notification.current.editor_comment)}"
            else:
                line += ": комментарий редактора не указан"
            lines.append(line)
        return lines

    def _application_link(self, current: SheetApplicationStatus) -> str:
        return f'<a href="{escape(self._row_link(current), quote=True)}">{escape(current.application_id)}</a>'

    def _batch_status_link(self, current: SheetBulkBatchStatus) -> str:
        return f'<a href="{escape(self._batch_status_row_link(current), quote=True)}">{escape(current.batch_id)}</a>'

    def _bulk_editor_comment_link(self, comment: BulkEditorComment) -> str:
        label = comment.application_id or f"строка {comment.row_number}"
        return f'<a href="{escape(self._bulk_editor_comment_row_link(comment), quote=True)}">{escape(label)}</a>'

    def _row_link(self, current: SheetApplicationStatus) -> str:
        return (
            f"https://docs.google.com/spreadsheets/d/{current.spreadsheet_id}/edit"
            f"#gid={current.sheet_id}&range=A{current.row_number}:{current.end_column}{current.row_number}"
        )

    def _batch_status_row_link(self, current: SheetBulkBatchStatus) -> str:
        return (
            f"https://docs.google.com/spreadsheets/d/{current.spreadsheet_id}/edit"
            f"#gid={current.sheet_id}&range=A{current.row_number}:{current.end_column}{current.row_number}"
        )

    @staticmethod
    def _batch_row_link(batch: BulkBatch) -> str:
        return (
            f"https://docs.google.com/spreadsheets/d/{batch.spreadsheet_id}/edit"
            f"#gid={batch.sheet_id}&range=A{batch.start_row}:N{batch.start_row}"
        )

    def _bulk_editor_comment_row_link(self, comment: BulkEditorComment) -> str:
        return (
            f"https://docs.google.com/spreadsheets/d/{comment.spreadsheet_id}/edit"
            f"#gid={comment.sheet_id}&range=A{comment.row_number}:{comment.end_column}{comment.row_number}"
        )

    async def _batch_link(self, batch_id: str, fallback: SheetApplicationStatus) -> str:
        batch = await self.repository.get_bulk_batch(batch_id)
        if batch is None:
            return self._row_link(fallback)
        return (
            f"https://docs.google.com/spreadsheets/d/{batch.spreadsheet_id or fallback.spreadsheet_id}/edit"
            f"#gid={batch.sheet_id}&range=A{batch.start_row}:M{batch.start_row}"
        )

    async def _reply_markup_for_user(self, telegram_user_id: int) -> Any | None:
        return build_keyboard(
            await self._keyboard_kind_for_user(telegram_user_id)
        )

    async def _reply_markup_for_outbox_item(
        self,
        item: Any,
    ) -> Any | None:
        if item.event_type in {
            URGENT_EDITOR_NOTIFICATION_EVENT_TYPE,
            URGENT_EDITOR_SCRIPTWRITER_RESPONSE_EVENT_TYPE,
            URGENT_EDITOR_BULK_RESERVATION_EVENT_TYPE,
        }:
            return None
        return await self._reply_markup_for_user(item.telegram_user_id)

    async def _keyboard_kind_for_user(self, telegram_user_id: int) -> KeyboardKind:
        settings = await self.repository.get_user_settings(telegram_user_id)
        pending_action = settings.pending_action or ""
        if pending_action.startswith("bulk_reservation_count:"):
            return KeyboardKind.NOTIFICATION_BULK_BACK

        reservation = await self.repository.get_active_bulk_reservation(telegram_user_id)
        if reservation is not None:
            return KeyboardKind.NOTIFICATION_BULK_BACK

        batch = await self.repository.get_latest_unregistered_bulk_batch(
            telegram_user_id
        )
        if batch is not None:
            return KeyboardKind.NOTIFICATION_BULK_BACK

        draft = await self.repository.get_by_user_id(telegram_user_id)
        if draft is not None and draft.is_active:
            return KeyboardKind.NOTIFICATION_SINGLE_BACK
        return KeyboardKind.NOTIFICATION


async def run_status_polling_loop(
    *,
    service: StatusNotificationService,
    interval_seconds: float,
    heartbeat_path: str | None = None,
    memory_log_interval: int = 40,
) -> None:
    """Запускать polling постоянно и писать heartbeat только после успеха."""
    consecutive_errors = 0
    successful_iterations = 0
    attempted_iterations = 0
    previous_rss_mb: float | None = None
    while True:
        attempted_iterations += 1
        try:
            await service.run_once()
            successful_iterations += 1
            if heartbeat_path:
                write_heartbeat(heartbeat_path, iteration=successful_iterations)
            if consecutive_errors:
                LOGGER.info(
                    "Status polling recovered after %s consecutive errors",
                    consecutive_errors,
                )
            consecutive_errors = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_errors += 1
            LOGGER.exception(
                "Status polling iteration failed: consecutive_errors=%s",
                consecutive_errors,
            )
        _run_memory_maintenance()
        if memory_log_interval > 0 and attempted_iterations % memory_log_interval == 0:
            snapshot = _process_memory_snapshot_mb()
            if snapshot is not None:
                rss_mb, vms_mb = snapshot
                delta_rss_mb = (
                    0.0 if previous_rss_mb is None else rss_mb - previous_rss_mb
                )
                previous_rss_mb = rss_mb
                LOGGER.info(
                    "Status polling memory: iteration=%s rss_mb=%.1f "
                    "vms_mb=%.1f delta_rss_mb=%.1f consecutive_errors=%s",
                    attempted_iterations,
                    rss_mb,
                    vms_mb,
                    delta_rss_mb,
                    consecutive_errors,
                )
        await asyncio.sleep(interval_seconds)


def _run_memory_maintenance() -> None:
    try:
        gc.collect()
        _malloc_trim()
    except Exception:
        LOGGER.debug("Status polling memory maintenance failed", exc_info=True)


def _malloc_trim() -> None:
    try:
        libc = ctypes.CDLL("libc.so.6")
        trim = getattr(libc, "malloc_trim", None)
        if trim is None:
            return
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        trim(0)
    except OSError:
        return


def _process_memory_snapshot_mb() -> tuple[float, float] | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as file:
            values = {}
            for line in file:
                if line.startswith(("VmRSS:", "VmSize:")):
                    name, raw_value = line.split(":", maxsplit=1)
                    values[name] = float(raw_value.strip().split()[0]) / 1024
    except OSError:
        return None
    rss_mb = values.get("VmRSS")
    vms_mb = values.get("VmSize")
    if rss_mb is None or vms_mb is None:
        return None
    return rss_mb, vms_mb


def _render_answer_block(answer: str, row_link: str) -> str:
    escaped = escape(answer)
    if len(escaped) > 1000:
        escaped = escaped[:1000] + "..."
    return f"<blockquote expandable>{escaped}</blockquote>\n<a href=\"{escape(row_link, quote=True)}\">Открыть строку</a>"


def _notification_type_group(notification: StatusNotification) -> str:
    answer_type = (
        notification.current.answer_type
        or notification.tracked.answer_type
        or "Тип ответа не указан"
    )
    change_type = (
        notification.current.change_type
        or notification.tracked.change_type
        or "Тип изменения не указан"
    )
    return f"{answer_type} / {change_type}"


def _regular_notification_summary(notification: StatusNotification) -> str:
    parts: list[str] = []
    if notification.status_changed:
        parts.append(
            "статус "
            f"{escape(notification.tracked.last_known_status)} → "
            f"{escape(notification.current.status)}"
        )
    if notification.final_answer_changed:
        parts.append("готов итоговый ответ")
    if notification.editor_comment_ready:
        parts.append("комментарий редактора")
    if notification.editor_changed:
        editor = notification.current.editor or EDITOR_NOT_SELECTED
        parts.append(f"редактор: {escape(editor)}")
    return "; ".join(parts)


def _split_html_message(text: str, max_chars: int) -> list[str]:
    """Split only between rendered blocks so Telegram never receives broken HTML."""
    blocks = [block for block in text.split("\n") if block]
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for block in blocks:
        safe_block = _fit_html_block(block, max_chars)
        added_length = len(safe_block) + (1 if current else 0)
        if current and current_length + added_length > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
        current.append(safe_block)
        current_length += len(safe_block) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def _fit_html_block(block: str, max_chars: int) -> str:
    if len(block) <= max_chars:
        return block
    match = re.search(r'<a href="([^"]+)">([^<]+)</a>', block)
    if match is not None:
        link = match.group(1)
        prefix = "<b>Уведомление сокращено.</b>\n"
        opening = f'<a href="{link}">'
        closing = "</a>"
        available = max_chars - len(prefix) - len(opening) - len(closing)
        if available < 4:
            prefix = ""
            available = max_chars - len(opening) - len(closing)
        if available > 0:
            label = match.group(2)
            shortened = label[:available]
            if len(shortened) < len(label) and available >= 3:
                shortened = f"{shortened[:-3]}..."
            return f"{prefix}{opening}{shortened}{closing}"
    suffix = "..."
    available = max(max_chars - len(suffix), 1)
    shortened = escape(block[:available])
    while len(shortened) + len(suffix) > max_chars and available > 1:
        available -= 1
        shortened = escape(block[:available])
    return f"{shortened}{suffix}"


def _render_urgent_scriptwriter_response_notification(snapshot: dict[str, Any]) -> str:
    response = _truncate_text(
        str(snapshot.get("scriptwriter_response") or ""),
        SCRIPTWRITER_RESPONSE_PREVIEW_LIMIT,
    )
    lines = [
        "💬 <b>Сценарист ответил по срочной заявке</b>",
        "",
        f"<b>Направление:</b> {escape(str(snapshot.get('direction') or '-'))}",
        f"<b>Сценарист:</b> {escape(str(snapshot.get('scriptwriter') or '-'))}",
        f"<b>Интент:</b> {escape(str(snapshot.get('intent') or '-'))}",
        f"<b>Статус:</b> {escape(str(snapshot.get('status') or '-'))}",
        "",
        "<b>Ответ сценариста:</b>",
        f"<blockquote>{escape(response)}</blockquote>",
        "",
        f'<a href="{escape(str(snapshot.get("row_link") or ""), quote=True)}">Открыть заявку</a>',
    ]
    return "\n".join(lines)


def _truncate_text(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"
    return f"{text[: limit - 1].rstrip()}…"


def _status_from_working_row(
    *,
    application_id: str,
    spreadsheet_id: str,
    sheet_name: str,
    sheet_id: int,
    row_number: int,
    row: list[Any],
    layout: dict[str, Any],
) -> SheetApplicationStatus:
    return SheetApplicationStatus(
        application_id=application_id,
        spreadsheet_id=spreadsheet_id,
        batch_id=_cell(row, layout["batch_id"]).strip() or None,
        sheet_name=sheet_name,
        sheet_id=sheet_id,
        row_number=row_number,
        direction=_cell(row, layout["direction"]).strip() or None,
        answer_type=_cell(row, layout["answer_type"]).strip() or None,
        is_urgent=_sheet_bool(_cell(row, layout["is_urgent"])),
        change_type=_cell(row, layout.get("change_type", -1)).strip() or None,
        scriptwriter=_cell(row, layout.get("scriptwriter", -1)).strip() or None,
        intent=_cell(row, layout.get("intent", -1)).strip() or None,
        status=_cell(row, layout["status"]).strip(),
        editor=_cell(row, layout["editor"]).strip(),
        editor_comment=_cell(row, layout["comment"]).strip(),
        final_answer=_cell(row, layout["final_answer"]).strip(),
        scriptwriter_response=_cell(row, layout.get("scriptwriter_response", -1)).strip(),
        end_column=layout["end_column"],
        application_id_column_index=layout["application_id"],
        status_column_index=layout["status"],
    )


def _status_from_tracked_row(
    *,
    application: SubmittedApplication,
    spreadsheet_id: str,
    sheet_name: str,
    sheet_id: int,
    row: list[Any],
) -> SheetApplicationStatus | None:
    row_number = application.last_seen_row_number
    if row_number is None or row_number <= 1:
        return None
    for layout in _point_status_layouts(application):
        application_id = _cell(row, layout["application_id"]).strip()
        if application_id != application.application_id:
            continue
        application_type = _cell(row, layout["application_type"]).strip()
        if application_type and application_type not in {
            ApplicationType.SINGLE.value,
            ApplicationType.BULK.value,
        }:
            continue
        return _status_from_working_row(
            application_id=application_id,
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            sheet_id=sheet_id,
            row_number=row_number,
            row=row,
            layout=layout,
        )
    return None


def _point_status_end_column(application: SubmittedApplication) -> str:
    if ChangeType.normalize(application.change_type) == ChangeType.CHIPS:
        return "U"
    return "X"


def _point_status_layouts(application: SubmittedApplication) -> list[dict[str, Any]]:
    change_type = ChangeType.normalize(application.change_type)
    if change_type == ChangeType.CHIPS:
        return [
            _working_row_layout(CHIPS_WORKSHEET_HEADERS),
            _working_row_layout(PREVIOUS_CHIPS_WORKSHEET_HEADERS),
            _working_row_layout(WORKSHEET_HEADERS),
            _working_row_layout(PREVIOUS_WORKSHEET_HEADERS),
            _working_row_layout(CURRENT_WORKSHEET_HEADERS),
            _working_row_layout(LEGACY_WORKSHEET_HEADERS),
        ]
    return [
        _working_row_layout(WORKSHEET_HEADERS),
        _working_row_layout(PREVIOUS_WORKSHEET_HEADERS),
        _working_row_layout(CURRENT_WORKSHEET_HEADERS),
        _working_row_layout(LEGACY_WORKSHEET_HEADERS),
        _working_row_layout(CHIPS_WORKSHEET_HEADERS),
        _working_row_layout(PREVIOUS_CHIPS_WORKSHEET_HEADERS),
    ]


def _working_row_layout(header_row: list[Any]) -> dict[str, Any] | None:
    headers = [str(value).strip() for value in header_row]
    if headers[: len(CHIPS_WORKSHEET_HEADERS)] == CHIPS_WORKSHEET_HEADERS:
        return {
            "application_id": 11,
            "batch_id": 12,
            "application_type": 13,
            "direction": 15,
            "answer_type": 16,
            "is_urgent": 17,
            "status": 1,
            "editor": 9,
            "comment": 7,
            "final_answer": -1,
            "scriptwriter_response": 8,
            "change_type": 20,
            "scriptwriter": 0,
            "intent": 10,
            "end_column": "U",
        }
    if headers[: len(PREVIOUS_CHIPS_WORKSHEET_HEADERS)] == PREVIOUS_CHIPS_WORKSHEET_HEADERS:
        return {
            "application_id": 11,
            "batch_id": 12,
            "application_type": 13,
            "direction": 15,
            "answer_type": 16,
            "is_urgent": 17,
            "status": 9,
            "editor": 10,
            "comment": 7,
            "final_answer": -1,
            "scriptwriter_response": 8,
            "change_type": 20,
            "scriptwriter": 0,
            "intent": 1,
            "end_column": "U",
        }
    if headers[: len(WORKSHEET_HEADERS)] == WORKSHEET_HEADERS:
        return {
            "application_id": 11,
            "batch_id": 12,
            "application_type": 13,
            "direction": 15,
            "answer_type": 16,
            "is_urgent": 17,
            "status": 1,
            "editor": 9,
            "comment": 7,
            "final_answer": 5,
            "scriptwriter_response": 8,
            "change_type": 23,
            "scriptwriter": 0,
            "intent": 10,
            "end_column": "X",
        }
    if headers[: len(PREVIOUS_WORKSHEET_HEADERS)] == PREVIOUS_WORKSHEET_HEADERS:
        return {
            "application_id": 11,
            "batch_id": 12,
            "application_type": 13,
            "direction": 15,
            "answer_type": 16,
            "is_urgent": 17,
            "status": 9,
            "editor": 10,
            "comment": 7,
            "final_answer": 5,
            "scriptwriter_response": 8,
            "change_type": 23,
            "scriptwriter": 0,
            "intent": 1,
            "end_column": "X",
        }
    if headers[: len(CURRENT_WORKSHEET_HEADERS)] == CURRENT_WORKSHEET_HEADERS:
        return {
            "application_id": 0,
            "batch_id": 1,
            "application_type": 2,
            "direction": 4,
            "answer_type": 5,
            "is_urgent": 6,
            "status": 9,
            "editor": 10,
            "comment": 17,
            "final_answer": 19,
            "scriptwriter_response": 18,
            "change_type": 22,
            "scriptwriter": 12,
            "intent": 11,
            "end_column": "W",
        }
    if headers[: len(LEGACY_WORKSHEET_HEADERS)] == LEGACY_WORKSHEET_HEADERS:
        return {
            "application_id": 0,
            "batch_id": 1,
            "application_type": 2,
            "direction": 4,
            "answer_type": 5,
            "is_urgent": 6,
            "status": 9,
            "editor": -1,
            "comment": 16,
            "final_answer": 18,
            "scriptwriter_response": 17,
            "change_type": 21,
            "scriptwriter": 11,
            "intent": 10,
            "end_column": "V",
        }
    return None


def _working_data_rows(
    rows: list[list[Any]],
    *,
    start_row: int = 1,
) -> list[tuple[int, list[Any], dict[str, Any]]]:
    result: list[tuple[int, list[Any], dict[str, Any]]] = []
    active_layout: dict[str, Any] | None = None
    default_layout: dict[str, Any] | None = None
    for row_number, row in enumerate(rows, start=start_row):
        if is_daily_separator_row(row):
            active_layout = default_layout
            continue
        row_layout = _working_row_layout(row)
        if row_layout is not None:
            active_layout = row_layout
            if row_layout["end_column"] != "U":
                default_layout = row_layout
            continue
        if _cell(row, 0).strip() in {ChangeType.ADD.value, ChangeType.EDIT.value, ChangeType.CHIPS.value, "CHIPS V2"}:
            continue
        if active_layout is None:
            continue
        application_id = _cell(row, active_layout["application_id"]).strip()
        if not application_id or application_id in {"ADD", "EDIT", "CHIPS"}:
            continue
        result.append((row_number, row, active_layout))
    return result


def _bulk_row_layout(header_row: list[Any]) -> dict[str, Any] | None:
    headers = [str(value).strip() for value in header_row]
    if headers[: len(BULK_STAGING_HEADERS)] == BULK_STAGING_HEADERS:
        return {
            "answer_type": 0,
            "application_id": 13,
            "status": 11,
            "editor": 12,
            "comment": 9,
            "final_answer": 7,
            "batch_status": 11,
            "end_column": "N",
        }
    if headers[: len(CURRENT_BULK_STAGING_HEADERS)] == CURRENT_BULK_STAGING_HEADERS:
        return {
            "answer_type": 0,
            "application_id": 7,
            "status": 8,
            "editor": 9,
            "comment": 10,
            "final_answer": 12,
            "batch_status": 10,
            "end_column": "M",
        }
    if headers[: len(LEGACY_BULK_STAGING_HEADERS)] == LEGACY_BULK_STAGING_HEADERS:
        return {
            "answer_type": 0,
            "application_id": 7,
            "status": 8,
            "editor": -1,
            "comment": 9,
            "final_answer": 11,
            "batch_status": 9,
            "end_column": "L",
        }
    return None


def _is_bulk_batch_block(rows: list[list[Any]], batch_id: str) -> bool:
    return (
        len(rows) >= 2
        and _cell(rows[0], 1).strip() == batch_id
        and _bulk_row_layout(rows[1]) is not None
    )


def _bulk_batch_end_row(batch: BulkBatch) -> int:
    if batch.data_end_row is not None:
        return batch.data_end_row
    return batch.data_start_row + max(batch.reserved_rows, 1) - 1


def _bulk_location_search_due(batch: BulkBatch) -> bool:
    if not batch.next_location_search_at:
        return True
    try:
        next_check = datetime.fromisoformat(batch.next_location_search_at)
    except ValueError:
        return True
    if next_check.tzinfo is None:
        next_check = next_check.replace(tzinfo=timezone.utc)
    return next_check <= datetime.now(timezone.utc)


def _cell(row: list[Any], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return str(row[index])


def _sheet_bool(value: str) -> bool | None:
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in {"да", "yes", "true", "1"}:
        return True
    if normalized in {"нет", "no", "false", "0"}:
        return False
    return None


def _is_google_rate_limit_error(exc: HttpError) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 429:
        return True
    content = getattr(exc, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="ignore")
    return "ReadRequestsPerMinutePerUser" in str(content) or "quota" in str(content).lower()


def _is_unparseable_range_error(exc: HttpError) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status != 400:
        return False
    content = getattr(exc, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="ignore")
    text = str(content).lower()
    return "unable to parse range" in text
