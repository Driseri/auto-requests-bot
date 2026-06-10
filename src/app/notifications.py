from __future__ import annotations

import asyncio
import logging
from time import monotonic
from dataclasses import dataclass
from html import escape
from typing import Any, Callable, Protocol

from aiogram.types import LinkPreviewOptions
from googleapiclient.errors import HttpError

from app.keyboards import build_keyboard
from app.bulk import BULK_STAGING_HEADERS, LEGACY_BULK_STAGING_HEADERS
from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
    BulkApplicationStatus,
    BulkBatch,
    BulkBatchStatus,
    KeyboardKind,
    Step,
    SubmittedApplication,
)
from app.repository import DraftRepository
from app.submission import (
    DashboardSyncService,
    DirectionSpreadsheetConfig,
    EDITOR_NOT_SELECTED,
    LEGACY_WORKSHEET_HEADERS,
    SheetConfigurationError,
    WORKSHEET_HEADERS,
    build_google_sheets_api,
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
    def __init__(
        self,
        *,
        direction_spreadsheets: DirectionSpreadsheetConfig,
        credentials_path: str,
        sheets_api: Any | None = None,
    ) -> None:
        self.direction_spreadsheets = direction_spreadsheets
        self.credentials_path = credentials_path
        self._sheets_api = sheets_api
        self._sheet_ids_cache: dict[str, dict[str, int]] = {}

    async def read_statuses(self) -> dict[str, SheetApplicationStatus]:
        return await asyncio.to_thread(self._read_statuses_sync)

    async def read_statuses_for(
        self,
        applications: list[SubmittedApplication],
        *,
        fallback_full_scan: bool = False,
    ) -> dict[str, SheetApplicationStatus]:
        return await asyncio.to_thread(
            self._read_statuses_for_sync,
            applications,
            fallback_full_scan,
        )

    async def read_bulk_application_statuses(
        self,
        batches: list[BulkBatch],
    ) -> dict[str, SheetApplicationStatus]:
        return await asyncio.to_thread(self._read_bulk_application_statuses_sync, batches)

    async def read_batch_statuses(
        self,
        batches: list[BulkBatch],
    ) -> dict[str, SheetBulkBatchStatus]:
        return await asyncio.to_thread(self._read_batch_statuses_sync, batches)

    async def read_bulk_editor_comments(
        self,
        batches: list[BulkBatch],
    ) -> dict[str, list[BulkEditorComment]]:
        return await asyncio.to_thread(self._read_bulk_editor_comments_sync, batches)

    def _read_statuses_sync(self) -> dict[str, SheetApplicationStatus]:
        api = self._get_sheets_api()
        result: dict[str, SheetApplicationStatus] = {}
        for spreadsheet_id in self._spreadsheet_ids():
            sheet_ids = self._read_sheet_ids(api, spreadsheet_id)
            for sheet_name, sheet_id in sheet_ids.items():
                rows = self._read_sheet_rows(api, spreadsheet_id, sheet_name)
                for index, row, layout in _working_data_rows(rows):
                    application_id = _cell(row, 0).strip()
                    application_type = _cell(row, 2).strip()
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
                        batch_id=_cell(row, 1).strip() or None,
                        sheet_name=sheet_name,
                        sheet_id=sheet_id,
                        row_number=index,
                        direction=_cell(row, 4).strip() or None,
                        answer_type=_cell(row, 5).strip() or None,
                        is_urgent=_sheet_bool(_cell(row, 6)),
                        status=_cell(row, 9).strip(),
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
            rows = self._read_sheet_rows(api, spreadsheet_id, sheet_name)
            wanted_ids = {item.application_id for item in group}
            for index, row, layout in _working_data_rows(rows):
                application_id = _cell(row, 0).strip()
                if application_id not in wanted_ids:
                    continue
                result[application_id] = SheetApplicationStatus(
                    application_id=application_id,
                    spreadsheet_id=spreadsheet_id,
                    batch_id=_cell(row, 1).strip() or None,
                    sheet_name=sheet_name,
                    sheet_id=sheet_id or 0,
                    row_number=index,
                    direction=_cell(row, 4).strip() or None,
                    answer_type=_cell(row, 5).strip() or None,
                    is_urgent=_sheet_bool(_cell(row, 6)),
                    status=_cell(row, 9).strip(),
                    editor=_cell(row, layout["editor"]).strip(),
                    editor_comment=_cell(row, layout["comment"]).strip(),
                    final_answer=_cell(row, layout["final_answer"]).strip(),
                    end_column=layout["end_column"],
                )

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
            full_scan = self._read_statuses_sync()
            for application_id in missing_ids:
                current = full_scan.get(application_id)
                if current is not None:
                    result[application_id] = current
        return result

    def _read_bulk_application_statuses_sync(
        self,
        batches: list[BulkBatch],
    ) -> dict[str, SheetApplicationStatus]:
        api = self._get_sheets_api()
        result: dict[str, SheetApplicationStatus] = {}
        for batch in batches:
            spreadsheet_id = batch.spreadsheet_id
            if not spreadsheet_id:
                continue
            end_row = batch.data_start_row + batch.reserved_rows - 1
            range_name = (
                f"{quote_sheet_name(batch.sheet_name)}!"
                f"A{batch.data_start_row - 1}:M{end_row}"
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
                        "Bulk application sheet range is unavailable; skipping batch rows. "
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
            layout = _bulk_row_layout(rows[0] if rows else [])
            if layout is None:
                continue
            for offset, row in enumerate(rows[1:]):
                application_id = _cell(row, 7).strip()
                if not application_id:
                    continue
                answer_type = _cell(row, 0).strip()
                result[application_id] = SheetApplicationStatus(
                    application_id=application_id,
                    spreadsheet_id=spreadsheet_id,
                    batch_id=batch.batch_id,
                    sheet_name=batch.sheet_name,
                    sheet_id=batch.sheet_id,
                    row_number=batch.data_start_row + offset,
                    direction=batch.direction,
                    answer_type=answer_type or None,
                    is_urgent=answer_type == AnswerType.URGENT.value,
                    status=_cell(row, 8).strip() or ApplicationStatus.NEW.value,
                    editor=_cell(row, layout["editor"]).strip(),
                    editor_comment=_cell(row, layout["comment"]).strip(),
                    final_answer=_cell(row, layout["final_answer"]).strip(),
                    end_column=layout["end_column"],
                )
        return result

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
                f"A{batch.start_row}:M{batch.data_start_row - 1}"
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
            end_row = batch.data_start_row + max(batch.reserved_rows, 1) - 1
            range_name = (
                f"{quote_sheet_name(batch.sheet_name)}!"
                f"A{batch.data_start_row - 1}:M{end_row}"
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
                        application_id=_cell(row, 7).strip(),
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
        response = api.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A:W",
            majorDimension="ROWS",
        ).execute()
        return response.get("values", [])

    def _get_sheets_api(self) -> Any:
        if self._sheets_api is None:
            self._sheets_api = build_google_sheets_api(self.credentials_path)
        return self._sheets_api


class StatusNotificationService:
    def __init__(
        self,
        *,
        repository: DraftRepository,
        status_reader: GoogleSheetsStatusReader,
        notifier: TelegramNotifierProtocol,
        fallback_spreadsheet_id: str = "",
        dashboard_sync: DashboardSyncService | None = None,
        dashboard_sync_interval_seconds: float = 300,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.repository = repository
        self.status_reader = status_reader
        self.notifier = notifier
        self.fallback_spreadsheet_id = fallback_spreadsheet_id
        self.dashboard_sync = dashboard_sync
        self.dashboard_sync_interval_seconds = dashboard_sync_interval_seconds
        self.clock = clock
        self._last_dashboard_sync_at: float | None = None
        self._polling_iteration = 0

    async def run_once(self) -> None:
        self._polling_iteration += 1
        dashboard_sync_due = self._is_dashboard_sync_due()
        await self._process_bulk_batch_statuses(sync_dashboard=dashboard_sync_due)
        tracked = await self.repository.list_submitted_applications()
        if not tracked:
            if dashboard_sync_due:
                self._last_dashboard_sync_at = self.clock()
            return

        fallback_full_scan = self._polling_iteration % 10 == 0
        if hasattr(self.status_reader, "read_statuses_for"):
            statuses = await self.status_reader.read_statuses_for(
                tracked,
                fallback_full_scan=fallback_full_scan,
            )
        else:
            statuses = await self.status_reader.read_statuses()
        if hasattr(self.status_reader, "read_bulk_application_statuses"):
            statuses.update(
                await self.status_reader.read_bulk_application_statuses(
                    await self.repository.list_bulk_batches()
                )
            )
        notifications_by_user: dict[int, list[StatusNotification]] = {}
        non_notified_updates: list[StatusNotification] = []

        for application in tracked:
            current = statuses.get(application.application_id)
            if current is None:
                LOGGER.info(
                    "Tracked application not found in direction sheets: application_id=%s",
                    application.application_id,
                )
                continue

            status_changed = bool(current.status) and current.status != application.last_known_status
            editor_changed = (
                (current.editor or EDITOR_NOT_SELECTED)
                != (application.last_seen_editor or EDITOR_NOT_SELECTED)
            )
            raw_final_answer_changed = (
                bool(current.final_answer)
                and current.final_answer != (application.last_seen_final_answer or "")
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
                await self._update_tracking(
                    application,
                    current,
                    sync_dashboard=dashboard_sync_due,
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

        for notification in non_notified_updates:
            await self._update_tracking(
                notification.tracked,
                notification.current,
                sync_dashboard=dashboard_sync_due,
            )

        for telegram_user_id, notifications in notifications_by_user.items():
            text = await self._render_notification_message(notifications)
            await self.notifier.send_message(
                telegram_user_id,
                text,
                parse_mode="HTML",
                reply_markup=await self._reply_markup_for_user(telegram_user_id),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            for notification in notifications:
                await self._update_tracking(
                    notification.tracked,
                    notification.current,
                    sync_dashboard=dashboard_sync_due,
                )
        if dashboard_sync_due:
            self._last_dashboard_sync_at = self.clock()

    def _is_dashboard_sync_due(self) -> bool:
        if self.dashboard_sync is None:
            return False
        if self._last_dashboard_sync_at is None:
            return True
        return (
            self.clock() - self._last_dashboard_sync_at
            >= self.dashboard_sync_interval_seconds
        )

    def _should_notify(self, notification: StatusNotification) -> bool:
        if notification.final_answer_changed:
            return True
        if not notification.status_changed:
            return False
        if notification.tracked.batch_id:
            return notification.current.status == BulkApplicationStatus.NEEDS_CLARIFICATION.value
        return notification.current.status in SINGLE_IMPORTANT_STATUSES

    async def _process_bulk_batch_statuses(self, *, sync_dashboard: bool) -> None:
        if not hasattr(self.status_reader, "read_batch_statuses"):
            return
        batches = await self.repository.list_bulk_batches()
        if not batches:
            return
        current_by_batch = await self.status_reader.read_batch_statuses(batches)
        notifications_by_user: dict[int, list[tuple[BulkBatch, SheetBulkBatchStatus]]] = {}
        non_notified_updates: list[tuple[BulkBatch, SheetBulkBatchStatus]] = []
        for batch in batches:
            current = current_by_batch.get(batch.batch_id)
            if current is None:
                continue
            if not current.status or current.status == batch.last_known_batch_status:
                if current.status and sync_dashboard:
                    await self._sync_bulk_batch_dashboard(batch, current)
                continue
            item = (batch, current)
            if current.status == BulkBatchStatus.DONE.value:
                notifications_by_user.setdefault(batch.telegram_user_id, []).append(item)
            else:
                non_notified_updates.append(item)

        for batch, current in non_notified_updates:
            await self._update_bulk_batch_tracking(
                batch,
                current,
                sync_dashboard=sync_dashboard,
            )

        for telegram_user_id, items in notifications_by_user.items():
            await self.notifier.send_message(
                telegram_user_id,
                self._render_bulk_batch_status_message(items),
                parse_mode="HTML",
                reply_markup=await self._reply_markup_for_user(telegram_user_id),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            for batch, current in items:
                await self._update_bulk_batch_tracking(
                    batch,
                    current,
                    sync_dashboard=sync_dashboard,
                )

    async def _update_bulk_batch_tracking(
        self,
        batch: BulkBatch,
        current: SheetBulkBatchStatus,
        *,
        sync_dashboard: bool = True,
    ) -> None:
        if sync_dashboard:
            await self._sync_bulk_batch_dashboard(batch, current)
        await self.repository.update_bulk_batch_status(
            batch.batch_id,
            batch_status=current.status,
            last_known_batch_status=current.status,
        )

    async def _sync_bulk_batch_dashboard(
        self,
        batch: BulkBatch,
        current: SheetBulkBatchStatus,
    ) -> None:
        if self.dashboard_sync is None:
            return
        await self._safe_dashboard_sync(
            self.dashboard_sync.upsert_bulk_batch,
            batch=batch,
            status=current.status,
            row_link=self._batch_status_row_link(current),
            final_answer_present=False,
            editors=await self._tracked_editors_for_batch(batch.batch_id),
        )

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
        if self.dashboard_sync is not None and sync_dashboard and tracked.batch_id:
            batch = await self.repository.get_bulk_batch(tracked.batch_id)
            if batch is not None:
                await self._safe_dashboard_sync(
                    self.dashboard_sync.upsert_bulk_batch,
                    batch=batch,
                    status=batch.last_known_batch_status,
                    row_link=await self._batch_link(tracked.batch_id, current),
                    final_answer_present=bool(current.final_answer),
                    editors=await self._tracked_editors_for_batch(
                        tracked.batch_id,
                        current=current,
                    ),
                )
        elif self.dashboard_sync is not None and sync_dashboard:
            await self._safe_dashboard_sync(
                self.dashboard_sync.upsert_tracked_application,
                tracked=tracked,
                current=current,
                row_link=self._row_link(current),
            )
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

    async def _tracked_editors_for_batch(
        self,
        batch_id: str,
        *,
        current: SheetApplicationStatus | None = None,
    ) -> tuple[str, ...]:
        applications = await self.repository.list_submitted_applications()
        editors = []
        for application in applications:
            if application.batch_id != batch_id:
                continue
            if current is not None and application.application_id == current.application_id:
                editors.append(current.editor or EDITOR_NOT_SELECTED)
            else:
                editors.append(application.last_seen_editor or EDITOR_NOT_SELECTED)
        return tuple(editors)

    async def _safe_dashboard_sync(self, func, **kwargs) -> None:
        try:
            await asyncio.to_thread(func, **kwargs)
        except SheetConfigurationError as exc:
            LOGGER.warning("Google Sheets dashboard sync skipped: %s", exc)
        except HttpError as exc:
            if _is_google_rate_limit_error(exc):
                LOGGER.warning("Google Sheets dashboard sync skipped due to quota/rate limit: %s", exc)
                return
            raise

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
        return build_keyboard(await self._keyboard_kind_for_user(telegram_user_id))

    async def _keyboard_kind_for_user(self, telegram_user_id: int) -> KeyboardKind:
        settings = await self.repository.get_user_settings(telegram_user_id)
        if settings.pending_action:
            return KeyboardKind.DEFAULTS_BACK

        draft = await self.repository.get_by_user_id(telegram_user_id)
        if draft is None or not draft.is_active:
            return KeyboardKind.CREATE_MODE
        if draft.current_step == Step.DIRECTION and settings.default_direction:
            return KeyboardKind.DIRECTION_WITH_DEFAULT
        if draft.current_step == Step.DIRECTION:
            return KeyboardKind.DIRECTION
        if draft.current_step == Step.ANSWER_TYPE:
            return KeyboardKind.ANSWER_TYPE
        if draft.current_step == Step.INTENT and settings.default_intent:
            return KeyboardKind.INTENT_STEP_WITH_DEFAULT
        if draft.current_step == Step.SCRIPTWRITER and settings.default_scriptwriter:
            return KeyboardKind.SCRIPTWRITER_STEP_WITH_DEFAULT
        if draft.current_step == Step.URGENCY:
            return KeyboardKind.URGENCY
        if draft.current_step == Step.REVIEW:
            return KeyboardKind.REVIEW
        if draft.current_step == Step.COMPLETED:
            return KeyboardKind.CREATE_MODE
        return KeyboardKind.STEP


async def run_status_polling_loop(
    *,
    service: StatusNotificationService,
    interval_seconds: float,
) -> None:
    while True:
        try:
            await service.run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Status polling iteration failed")
        await asyncio.sleep(interval_seconds)


def _render_answer_block(answer: str, row_link: str) -> str:
    escaped = escape(answer)
    if len(escaped) > 1000:
        escaped = escaped[:1000] + "..."
    return f"<blockquote expandable>{escaped}</blockquote>\n<a href=\"{escape(row_link, quote=True)}\">Открыть строку</a>"


def _working_row_layout(header_row: list[Any]) -> dict[str, Any] | None:
    headers = [str(value).strip() for value in header_row]
    if headers[: len(WORKSHEET_HEADERS)] == WORKSHEET_HEADERS:
        return {
            "editor": 10,
            "comment": 17,
            "final_answer": 19,
            "end_column": "W",
        }
    if headers[: len(LEGACY_WORKSHEET_HEADERS)] == LEGACY_WORKSHEET_HEADERS:
        return {
            "editor": -1,
            "comment": 16,
            "final_answer": 18,
            "end_column": "V",
        }
    return None


def _working_data_rows(
    rows: list[list[Any]],
) -> list[tuple[int, list[Any], dict[str, Any]]]:
    result: list[tuple[int, list[Any], dict[str, Any]]] = []
    active_layout: dict[str, Any] | None = None
    for row_number, row in enumerate(rows, start=1):
        row_layout = _working_row_layout(row)
        if row_layout is not None:
            active_layout = row_layout
            continue
        if active_layout is None:
            continue
        application_id = _cell(row, 0).strip()
        if not application_id or application_id in {"ADD", "EDIT", "CHIPS"}:
            continue
        result.append((row_number, row, active_layout))
    return result


def _bulk_row_layout(header_row: list[Any]) -> dict[str, Any] | None:
    headers = [str(value).strip() for value in header_row]
    if headers[: len(BULK_STAGING_HEADERS)] == BULK_STAGING_HEADERS:
        return {
            "editor": 9,
            "comment": 10,
            "final_answer": 12,
            "batch_status": 10,
            "end_column": "M",
        }
    if headers[: len(LEGACY_BULK_STAGING_HEADERS)] == LEGACY_BULK_STAGING_HEADERS:
        return {
            "editor": -1,
            "comment": 9,
            "final_answer": 11,
            "batch_status": 9,
            "end_column": "L",
        }
    return None


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
