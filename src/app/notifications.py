from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from time import monotonic
from dataclasses import dataclass
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
    BulkBatchStatus,
    DashboardEntityType,
    KeyboardKind,
    SubmittedApplication,
)
from app.repository import DraftRepository
from app.submission import (
    CURRENT_WORKSHEET_HEADERS,
    DashboardSyncService,
    DirectionSpreadsheetConfig,
    EDITOR_NOT_SELECTED,
    LEGACY_WORKSHEET_HEADERS,
    WORKSHEET_HEADERS,
    build_google_sheets_api,
    dashboard_bulk_batch_row,
    dashboard_projection,
    dashboard_tracked_row,
    quote_sheet_name,
)


LOGGER = logging.getLogger(__name__)
SINGLE_IMPORTANT_STATUSES = {
    ApplicationStatus.NEEDS_CLARIFICATION.value,
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
    editor: str = ""
    batch_id: str | None = None
    direction: str | None = None
    answer_type: str | None = None
    is_urgent: bool | None = None
    end_column: str = "W"


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


@dataclass(slots=True)
class StatusNotification:
    tracked: SubmittedApplication
    current: SheetApplicationStatus
    status_changed: bool
    final_answer_changed: bool
    editor_changed: bool = False


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

    async def read_bulk_editor_comments(
        self,
        batches: list[BulkBatch],
    ) -> dict[str, list[BulkEditorComment]]:
        return await self._run_with_retry(
            lambda: self._read_bulk_editor_comments_sync(batches),
            "status-bulk-comments",
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
                        status=_cell(row, layout["status"]).strip(),
                        editor=_cell(row, layout["editor"]).strip(),
                        editor_comment=_cell(row, layout["comment"]).strip(),
                        final_answer=_cell(row, layout["final_answer"]).strip(),
                        end_column=layout["end_column"],
                    )
        return result

    def _read_statuses_for_sync(
        self,
        applications: list[SubmittedApplication],
        fallback_full_scan: bool,
    ) -> dict[str, SheetApplicationStatus]:
        api = self._get_sheets_api()
        result: dict[str, SheetApplicationStatus] = {}
        grouped: dict[tuple[str, str], list[SubmittedApplication]] = {}
        for application in applications:
            if application.batch_id or not application.spreadsheet_id or not application.sheet_name:
                continue
            grouped.setdefault(
                (application.spreadsheet_id, application.sheet_name),
                [],
            ).append(application)

        for (spreadsheet_id, sheet_name), group in grouped.items():
            sheet_id = next((item.sheet_id for item in group if item.sheet_id is not None), None)
            if sheet_id is None:
                sheet_id = self._read_sheet_ids(api, spreadsheet_id).get(sheet_name)
            for application in group:
                current = self._read_expected_application_status(
                    api,
                    application,
                    spreadsheet_id=spreadsheet_id,
                    sheet_name=sheet_name,
                    sheet_id=sheet_id or 0,
                )
                if current is not None:
                    result[current.application_id] = current

        missing_ids = {
            item.application_id
            for item in applications
            if not item.batch_id
        } - set(result)
        if fallback_full_scan and missing_ids:
            LOGGER.info(
                "Running fallback full scan for missing applications: count=%s",
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
                rows = self._read_sheet_rows(api, spreadsheet_id, sheet_name)
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
                final_answer=_cell(row, layout["final_answer"]).strip(),
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

    def _read_sheet_ids(self, api: Any, spreadsheet_id: str) -> dict[str, int]:
        if spreadsheet_id in self._sheet_ids_cache:
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
        completed_bulk_scan_due = self._is_completed_bulk_scan_due()
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
        await self._process_bulk_batch_statuses(
            active_batches,
            current_batch_statuses,
            statuses,
        )
        notifications_by_user: dict[int, list[StatusNotification]] = {}
        non_notified_updates: list[StatusNotification] = []

        for application in tracked:
            current = statuses.get(application.application_id)
            if current is None:
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

            status_changed = bool(current.status) and current.status != application.last_known_status
            editor_changed = (
                (current.editor or EDITOR_NOT_SELECTED)
                != (application.last_seen_editor or EDITOR_NOT_SELECTED)
            )
            if application.batch_id:
                final_answer_changed = False
            else:
                final_answer_changed = (
                    status_changed
                    and current.status == ApplicationStatus.FINAL_ANSWER_READY.value
                    and bool(current.final_answer)
                )
            if not status_changed and not final_answer_changed and not editor_changed:
                non_notified_updates.append(
                    StatusNotification(
                        tracked=application,
                        current=current,
                        status_changed=False,
                        final_answer_changed=False,
                        editor_changed=False,
                    )
                )
                continue

            notification = StatusNotification(
                tracked=application,
                current=current,
                status_changed=status_changed,
                final_answer_changed=final_answer_changed,
                editor_changed=editor_changed,
            )
            if self._should_notify(notification):
                notifications_by_user.setdefault(application.telegram_user_id, []).append(
                    notification
                )
            else:
                non_notified_updates.append(notification)

        if non_notified_updates:
            await self.repository.update_application_tracking_batch(
                [
                    self._application_tracking_update(item.tracked, item.current)
                    for item in non_notified_updates
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
                    self._application_tracking_update(item.tracked, item.current)
                    for item in notifications
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
        if completed_bulk_scan_due:
            self._last_completed_bulk_scan_at = self.clock()
        await self._deliver_outbox()
        await self._deliver_dashboard_outbox()

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
        if not notification.status_changed:
            return False
        if notification.tracked.batch_id:
            return notification.current.status == BulkApplicationStatus.NEEDS_CLARIFICATION.value
        return notification.current.status in SINGLE_IMPORTANT_STATUSES

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
                await self.repository.update_bulk_batch_status(
                    batch.batch_id,
                    batch_status=current.status,
                    last_known_batch_status=current.status,
                    dashboard_projection=self._bulk_dashboard_projection(
                        batch,
                        current.status,
                        application_statuses,
                        row_link=self._batch_status_row_link(current),
                    ),
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
                    reply_markup=await self._reply_markup_for_user(
                        item.telegram_user_id
                    ),
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
    ) -> dict[str, Any]:
        return {
            "application_id": tracked.application_id,
            "spreadsheet_id": current.spreadsheet_id,
            "sheet_id": current.sheet_id,
            "sheet_name": current.sheet_name,
            "last_known_status": current.status or tracked.last_known_status,
            "last_seen_row_number": current.row_number,
            "last_seen_editor": current.editor or EDITOR_NOT_SELECTED,
            "last_seen_editor_comment": current.editor_comment,
            "last_seen_final_answer": current.final_answer,
        }

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
            for notification in regular_notifications:
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
        if notification.status_changed:
            result.append(
                f'{self._application_link(current)}: '
                f"{escape(notification.tracked.last_known_status)} -> {escape(current.status)}"
            )
        if notification.final_answer_changed:
            result.append("")
            result.append(f"<b>Итоговый ответ по заявке {self._application_link(current)}</b>")
            result.append(_render_answer_block(current.final_answer, self._row_link(current)))
        if (
            current.editor_comment
            and notification.status_changed
            and current.status == ApplicationStatus.NEEDS_CLARIFICATION.value
        ):
            result[-1] += f"; комментарий: {escape(current.editor_comment)}"
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

    async def _keyboard_kind_for_user(self, telegram_user_id: int) -> KeyboardKind:
        settings = await self.repository.get_user_settings(telegram_user_id)
        pending_action = settings.pending_action or ""
        if pending_action.startswith("create_bulk_direction:"):
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
) -> None:
    """Запускать polling постоянно и писать heartbeat только после успеха."""
    consecutive_errors = 0
    successful_iterations = 0
    while True:
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
        await asyncio.sleep(interval_seconds)


def _render_answer_block(answer: str, row_link: str) -> str:
    escaped = escape(answer)
    if len(escaped) > 1000:
        escaped = escaped[:1000] + "..."
    return f"<blockquote expandable>{escaped}</blockquote>\n<a href=\"{escape(row_link, quote=True)}\">Открыть строку</a>"


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
        status=_cell(row, layout["status"]).strip(),
        editor=_cell(row, layout["editor"]).strip(),
        editor_comment=_cell(row, layout["comment"]).strip(),
        final_answer=_cell(row, layout["final_answer"]).strip(),
        end_column=layout["end_column"],
    )


def _working_row_layout(header_row: list[Any]) -> dict[str, Any] | None:
    headers = [str(value).strip() for value in header_row]
    if headers[: len(WORKSHEET_HEADERS)] == WORKSHEET_HEADERS:
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
    for row_number, row in enumerate(rows, start=start_row):
        row_layout = _working_row_layout(row)
        if row_layout is not None:
            active_layout = row_layout
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


def _bulk_batch_end_row(batch: BulkBatch) -> int:
    if batch.data_end_row is not None:
        return batch.data_end_row
    return batch.data_start_row + max(batch.reserved_rows, 1) - 1


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
