from __future__ import annotations

import asyncio
import json
import logging
import re

import aiosqlite
import pytest
from googleapiclient.errors import HttpError

from app.keyboards import build_keyboard
from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
    ChangeType,
    Direction,
    FieldName,
    KeyboardKind,
    Step,
    SubmittedApplication,
)
from app.notifications import (
    GoogleSheetsStatusReader,
    SheetApplicationStatus,
    StatusNotificationService,
    _split_html_message,
    _working_data_rows,
)
import app.notifications as notifications_module
from app.repository import DraftRepository
from app.submission import (
    CHIPS_WORKSHEET_HEADERS,
    DirectionSpreadsheetConfig,
    LEGACY_WORKSHEET_HEADERS,
    PREVIOUS_CHIPS_WORKSHEET_HEADERS,
    PREVIOUS_WORKSHEET_HEADERS,
)


FL_SPREADSHEET = "fl-spreadsheet"
SME_SPREADSHEET = "sme-spreadsheet"
WEEK_SHEET = "01.06"


def test_long_notification_is_split_between_complete_html_blocks():
    first = '<a href="https://example.test/1">Первая заявка</a>'
    second = '<blockquote expandable>Подробный ответ</blockquote>'

    chunks = _split_html_message(f"{first}\n{second}\n{first}\n{second}", 100)

    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert all(chunk.count("<a ") == chunk.count("</a>") for chunk in chunks)
    assert all(
        chunk.count("<blockquote") == chunk.count("</blockquote>")
        for chunk in chunks
    )


def test_oversized_link_block_keeps_link_and_fits_limit():
    block = f'<a href="https://example.test/sheet">{"Текст" * 100}</a>'

    chunks = _split_html_message(block, 120)

    assert len(chunks) == 1
    assert len(chunks[0]) <= 120
    assert 'href="https://example.test/sheet"' in chunks[0]
    assert chunks[0].endswith("</a>")




def test_working_data_rows_reads_chips_v2_layout():
    chips_row = [""] * len(CHIPS_WORKSHEET_HEADERS)
    chips_row[7] = "Editor comment"
    chips_row[1] = ApplicationStatus.NEEDS_CLARIFICATION.value
    chips_row[9] = "Editor"
    chips_row[10] = "intent.chip"
    chips_row[11] = "CHIPSV20"
    chips_row[20] = ChangeType.CHIPS.value
    rows = [["CHIPS V2"], CHIPS_WORKSHEET_HEADERS, chips_row]

    found = _working_data_rows(rows)

    assert len(found) == 1
    row_number, _, layout = found[0]
    assert row_number == 3
    assert layout["status"] == 1
    assert layout["editor"] == 9
    assert layout["intent"] == 10
    assert layout["comment"] == 7
    assert layout["final_answer"] == -1
    assert layout["scriptwriter_response"] == 8


def test_working_data_rows_reads_previous_add_edit_layout():
    row = [""] * len(PREVIOUS_WORKSHEET_HEADERS)
    row[1] = "intent.old"
    row[9] = ApplicationStatus.ACCEPTED.value
    row[10] = "Editor"
    row[11] = "OLDADD01"
    row[23] = ChangeType.ADD.value
    rows = [PREVIOUS_WORKSHEET_HEADERS, row]

    found = _working_data_rows(rows)

    assert len(found) == 1
    row_number, _, layout = found[0]
    assert row_number == 2
    assert layout["status"] == 9
    assert layout["editor"] == 10
    assert layout["intent"] == 1


def test_working_data_rows_reads_previous_chips_layout():
    chips_row = [""] * len(PREVIOUS_CHIPS_WORKSHEET_HEADERS)
    chips_row[7] = "Editor comment"
    chips_row[9] = ApplicationStatus.NEEDS_CLARIFICATION.value
    chips_row[10] = "Editor"
    chips_row[11] = "CHIPOLD1"
    chips_row[20] = ChangeType.CHIPS.value
    rows = [["CHIPS V2"], PREVIOUS_CHIPS_WORKSHEET_HEADERS, chips_row]

    found = _working_data_rows(rows)

    assert len(found) == 1
    row_number, _, layout = found[0]
    assert row_number == 3
    assert layout["status"] == 9
    assert layout["editor"] == 10
    assert layout["intent"] == 1






def test_working_data_rows_keeps_legacy_layout_after_daily_separator():
    row = [""] * len(LEGACY_WORKSHEET_HEADERS)
    row[0] = "LEGACY01"
    rows = [
        LEGACY_WORKSHEET_HEADERS,
        ["03.06.26"],
        row,
    ]

    found = _working_data_rows(rows)

    assert len(found) == 1
    row_number, _, layout = found[0]
    assert row_number == 3
    assert layout["application_id"] == 0


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeValuesResource:
    def __init__(self, api: "FakeSheetsApi") -> None:
        self.api = api

    def get(self, **kwargs):
        self.api.value_get_calls.append(kwargs)
        spreadsheet_id = kwargs["spreadsheetId"]
        range_name = kwargs["range"]
        sheet_name = _sheet_name_from_range(range_name)
        if (spreadsheet_id, range_name) in self.api.rows:
            stored = self.api.rows[(spreadsheet_id, range_name)]
            if isinstance(stored, BaseException):
                return FakeRequest(stored)
            return FakeRequest({"values": stored})
        rows = self.api.rows.get((spreadsheet_id, sheet_name), [])
        return FakeRequest({"values": _slice_fake_rows(range_name, rows)})


class FakeSpreadsheetsResource:
    def __init__(self, api: "FakeSheetsApi") -> None:
        self.api = api

    def values(self):
        return FakeValuesResource(self.api)

    def get(self, **kwargs):
        self.api.metadata_get_calls.append(kwargs)
        spreadsheet_id = kwargs["spreadsheetId"]
        return FakeRequest({"sheets": self.api.sheets_by_spreadsheet.get(spreadsheet_id, [])})


class FakeSheetsApi:
    def __init__(self, *, rows: dict[tuple[str, str], list[list[str]]]) -> None:
        self.rows = rows
        self.sheets_by_spreadsheet = {
            FL_SPREADSHEET: [{"properties": {"sheetId": 100, "title": WEEK_SHEET}}],
            SME_SPREADSHEET: [{"properties": {"sheetId": 200, "title": WEEK_SHEET}}],
        }
        self.metadata_get_calls = []
        self.value_get_calls = []

    def spreadsheets(self):
        return FakeSpreadsheetsResource(self)


class FakeBatchGetValuesResource(FakeValuesResource):
    def batchGet(self, **kwargs):
        self.api.batch_get_calls.append(kwargs)
        spreadsheet_id = kwargs["spreadsheetId"]
        value_ranges = []
        for range_name in kwargs["ranges"]:
            sheet_name = _sheet_name_from_range(range_name)
            stored = self.api.rows.get(
                (spreadsheet_id, range_name),
                None,
            )
            if isinstance(stored, BaseException):
                return FakeRequest(stored)
            if stored is None:
                stored = _slice_fake_rows(
                    range_name,
                    self.api.rows.get((spreadsheet_id, sheet_name), []),
                )
            value_ranges.append({"range": range_name, "values": stored})
        return FakeRequest({"valueRanges": value_ranges})


class FakeBatchGetSpreadsheetsResource(FakeSpreadsheetsResource):
    def values(self):
        return FakeBatchGetValuesResource(self.api)


class FakeBatchGetSheetsApi(FakeSheetsApi):
    def __init__(self, *, rows):
        super().__init__(rows=rows)
        self.batch_get_calls = []

    def spreadsheets(self):
        return FakeBatchGetSpreadsheetsResource(self)


class FakeStatusReader:
    def __init__(self, statuses: dict[str, SheetApplicationStatus]) -> None:
        self.statuses = statuses
        self.calls = 0

    async def read_statuses(self) -> dict[str, SheetApplicationStatus]:
        self.calls += 1
        return self.statuses


class FakeDeletingStatusReader(FakeStatusReader):
    def __init__(self, statuses: dict[str, SheetApplicationStatus]) -> None:
        super().__init__(statuses)
        self.deleted = []
        self.fail_delete = False

    async def delete_application_row(self, current: SheetApplicationStatus) -> None:
        if self.fail_delete:
            raise RuntimeError("google delete failed")
        self.deleted.append(current.application_id)








class FakeNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup=None,
        link_preview_options=None,
    ):
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
                "link_preview_options": link_preview_options,
            }
        )


class FakeDashboardSync:
    def __init__(self) -> None:
        self.upserts = []

    def sync_projections(self, items):
        self.upserts.extend(items)

    def reset_client(self):
        return None


class SelectiveFailNotifier(FakeNotifier):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup=None,
        link_preview_options=None,
    ):
        if chat_id == 100:
            raise RuntimeError("telegram unavailable for one user")
        await super().send_message(
            chat_id,
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            link_preview_options=link_preview_options,
        )


class FakeResponse:
    def __init__(self, status: int = 429, reason: str = "Too Many Requests") -> None:
        self.status = status
        self.reason = reason


class RateLimitedDashboardSync:
    def upsert_tracked_application(self, *, tracked, current, row_link):
        raise HttpError(
            FakeResponse(),
            b'{"error":{"status":"RESOURCE_EXHAUSTED","message":"ReadRequestsPerMinutePerUser"}}',
        )


def direction_config() -> DirectionSpreadsheetConfig:
    return DirectionSpreadsheetConfig(
        fl_spreadsheet_id=FL_SPREADSHEET,
        sme_spreadsheet_id=SME_SPREADSHEET,
        ai_spreadsheet_id="",
        voice_collection_spreadsheet_id="",
    )










@pytest.mark.asyncio
async def test_status_reader_skips_single_source_when_sheet_range_is_missing():
    missing_range_error = HttpError(
        FakeResponse(status=400, reason="Bad Request"),
        "{\"error\":{\"message\":\"Unable to parse range: '15.06 ср'!A1:X152\"}}".encode(
            "utf-8"
        ),
    )
    sheet_name = "15.06 ср"
    api = FakeSheetsApi(
        rows={
            (FL_SPREADSHEET, f"'{sheet_name}'!A152:X152"): missing_range_error,
        }
    )
    reader = GoogleSheetsStatusReader(
        direction_spreadsheets=direction_config(),
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )
    tracked = [
        SubmittedApplication(
            application_id="A1B2C3D4",
            telegram_user_id=100,
            spreadsheet_id=FL_SPREADSHEET,
            sheet_id=100,
            sheet_name=sheet_name,
            last_known_status=ApplicationStatus.NEW.value,
            last_seen_row_number=152,
        )
    ]

    statuses = await reader.read_statuses_for(tracked)

    assert statuses == {}
    assert reader.unavailable_single_sources == {(FL_SPREADSHEET, sheet_name)}








@pytest.mark.asyncio
async def test_status_reader_supports_legacy_working_sheet():
    legacy_row = [""] * 22
    legacy_row[0] = "A1B2C3D4"
    legacy_row[2] = ApplicationType.SINGLE.value
    legacy_row[9] = ApplicationStatus.NEEDS_CLARIFICATION.value
    legacy_row[16] = "Уточните причину"
    legacy_row[18] = "Итог"
    api = FakeSheetsApi(
        rows={
            (FL_SPREADSHEET, WEEK_SHEET): [
                LEGACY_WORKSHEET_HEADERS,
                legacy_row,
            ]
        }
    )
    reader = GoogleSheetsStatusReader(
        direction_spreadsheets=direction_config(),
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    statuses = await reader.read_statuses()

    current = statuses["A1B2C3D4"]
    assert current.editor == ""
    assert current.editor_comment == "Уточните причину"
    assert current.final_answer == "Итог"
    assert current.end_column == "V"






















@pytest.mark.asyncio
async def test_notification_service_notifies_about_in_progress_status(tmp_path):
    repository = DraftRepository(str(tmp_path / "notifications.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=5,
                    status=ApplicationStatus.IN_PROGRESS.value,
                    editor_comment="",
                    final_answer="",
                )
            }
        ),
        notifier=notifier,
    )

    await service.run_once()
    tracked = await repository.get_submitted_application("A1B2C3D4")

    assert len(notifier.messages) == 1
    assert "В работе" in notifier.messages[0]["text"]
    assert tracked is not None
    assert tracked.last_known_status == ApplicationStatus.IN_PROGRESS.value
    assert tracked.last_seen_row_number == 5


@pytest.mark.asyncio
async def test_notification_service_records_status_changed_event(tmp_path):
    repository = DraftRepository(str(tmp_path / "status_event.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=0,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
        last_seen_row_number=5,
    )
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=0,
                    row_number=5,
                    status=ApplicationStatus.IN_PROGRESS.value,
                    editor_comment="",
                    final_answer="",
                )
            }
        ),
        notifier=FakeNotifier(),
    )

    await service.run_once()
    events = await repository.list_application_events(
        application_id="A1B2C3D4",
        event_type="status_changed",
    )

    assert len(events) == 1
    assert events[0].old_value == ApplicationStatus.NEW.value
    assert events[0].new_value == ApplicationStatus.IN_PROGRESS.value
    metadata = json.loads(events[0].metadata_json or "{}")
    assert metadata["sheet_id"] == 0
    assert metadata["row_number"] == 5


@pytest.mark.asyncio
async def test_notification_service_records_stable_editor_comment_event(tmp_path):
    repository = DraftRepository(str(tmp_path / "comment_event.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEEDS_CLARIFICATION.value,
        last_seen_row_number=5,
    )
    status = SheetApplicationStatus(
        application_id="A1B2C3D4",
        spreadsheet_id=FL_SPREADSHEET,
        sheet_name=WEEK_SHEET,
        sheet_id=100,
        row_number=5,
        status=ApplicationStatus.NEEDS_CLARIFICATION.value,
        editor_comment="Уточните деталь",
        final_answer="",
    )
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader({"A1B2C3D4": status}),
        notifier=FakeNotifier(),
    )

    await service.run_once()
    await service.run_once()
    await service.run_once()
    events = await repository.list_application_events(
        application_id="A1B2C3D4",
        event_type="editor_comment_added",
    )

    assert len(events) == 1
    assert events[0].old_value is None
    assert events[0].new_value == "Уточните деталь"


@pytest.mark.asyncio
async def test_notification_service_records_final_answer_event(tmp_path):
    repository = DraftRepository(str(tmp_path / "final_answer_event.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.IN_PROGRESS.value,
        last_seen_row_number=5,
    )
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=5,
                    status=ApplicationStatus.FINAL_ANSWER_READY.value,
                    editor_comment="",
                    final_answer="Готовый ответ",
                )
            }
        ),
        notifier=FakeNotifier(),
    )

    await service.run_once()
    events = await repository.list_application_events(application_id="A1B2C3D4")
    event_types = {event.event_type for event in events}
    final_answer = [
        event for event in events if event.event_type == "final_answer_added"
    ][0]

    assert "status_changed" in event_types
    assert "final_answer_added" in event_types
    assert final_answer.old_value is None
    assert final_answer.new_value == "Готовый ответ"


@pytest.mark.asyncio
async def test_notification_service_does_not_record_events_without_changes(tmp_path):
    repository = DraftRepository(str(tmp_path / "no_change_event.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
        last_seen_row_number=5,
        last_seen_editor="редактор 1",
    )
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=5,
                    status=ApplicationStatus.NEW.value,
                    editor="редактор 1",
                    editor_comment="",
                    final_answer="",
                )
            }
        ),
        notifier=FakeNotifier(),
    )

    await service.run_once()

    events = await repository.list_application_events(application_id="A1B2C3D4")
    assert [event.event_type for event in events] == ["application_indexed"]


@pytest.mark.asyncio
async def test_notification_service_defers_repeatedly_missing_tracking(tmp_path):
    repository = DraftRepository(str(tmp_path / "missing_tracking.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
        last_seen_row_number=2,
    )
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader({}),
        notifier=FakeNotifier(),
        status_not_found_threshold=2,
        status_not_found_recheck_seconds=3600,
    )

    await service.run_once()
    await service.run_once()

    deferred = await repository.get_submitted_application("A1B2C3D4")
    due = await repository.list_submitted_applications()
    all_tracked = await repository.list_submitted_applications(include_deferred=True)

    assert deferred is not None
    assert deferred.polling_state == "NOT_FOUND"
    assert deferred.not_found_count == 2
    assert deferred.next_status_check_at is not None
    assert due == []
    assert [item.application_id for item in all_tracked] == ["A1B2C3D4"]


@pytest.mark.asyncio
async def test_notification_service_does_not_mark_unavailable_single_source_missing(tmp_path):
    repository = DraftRepository(str(tmp_path / "unavailable_single_source.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name="15.06 ср",
        last_known_status=ApplicationStatus.NEW.value,
        last_seen_row_number=152,
    )
    reader = FakeStatusReader({})
    reader.unavailable_single_sources = {(FL_SPREADSHEET, "15.06 ср")}
    service = StatusNotificationService(
        repository=repository,
        status_reader=reader,
        notifier=FakeNotifier(),
        status_not_found_threshold=2,
        status_not_found_recheck_seconds=3600,
    )

    await service.run_once()

    tracked = await repository.get_submitted_application("A1B2C3D4")
    assert tracked is not None
    assert tracked.polling_state == "ACTIVE"
    assert tracked.not_found_count == 0
    assert tracked.next_status_check_at is None


@pytest.mark.asyncio
async def test_notification_service_updates_dashboard_for_tracked_change(tmp_path):
    repository = DraftRepository(str(tmp_path / "dashboard_updates.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
    )
    dashboard_sync = FakeDashboardSync()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=5,
                    status=ApplicationStatus.IN_PROGRESS.value,
                    editor_comment="",
                    final_answer="",
                )
            }
        ),
        notifier=FakeNotifier(),
        dashboard_sync=dashboard_sync,
    )

    await service.run_once()

    assert len(dashboard_sync.upserts) == 1
    item = dashboard_sync.upserts[0]
    row = json.loads(item.snapshot_json)["row"]
    assert item.entity_id == "A1B2C3D4"
    assert row[8] == ApplicationStatus.IN_PROGRESS.value
    assert row[11].endswith("range=A5:W5")


@pytest.mark.asyncio
async def test_notification_service_refreshes_unchanged_tracking_on_dashboard_interval(tmp_path):
    repository = DraftRepository(str(tmp_path / "dashboard_unchanged.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
    )
    dashboard_sync = FakeDashboardSync()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=5,
                    status=ApplicationStatus.NEW.value,
                    editor_comment="",
                    final_answer="",
                )
            }
        ),
        notifier=FakeNotifier(),
        dashboard_sync=dashboard_sync,
    )

    await service.run_once()

    assert len(dashboard_sync.upserts) == 1
    tracked = await repository.get_submitted_application("A1B2C3D4")
    assert tracked is not None
    assert tracked.last_seen_row_number == 5


@pytest.mark.asyncio
async def test_dashboard_refresh_respects_independent_interval(tmp_path):
    repository = DraftRepository(str(tmp_path / "dashboard_interval.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
    )
    dashboard_sync = FakeDashboardSync()
    now = [0.0]
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=5,
                    status=ApplicationStatus.NEW.value,
                    editor_comment="",
                    final_answer="",
                )
            }
        ),
        notifier=FakeNotifier(),
        dashboard_sync=dashboard_sync,
        dashboard_sync_interval_seconds=300,
        clock=lambda: now[0],
    )

    await service.run_once()
    now[0] = 100
    await service.run_once()
    now[0] = 301
    await service.run_once()

    assert len(dashboard_sync.upserts) == 2


@pytest.mark.asyncio
async def test_notification_service_logs_rate_limit_without_crashing_tracking(tmp_path):
    repository = DraftRepository(str(tmp_path / "dashboard_rate_limit.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
    )
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=5,
                    status=ApplicationStatus.IN_PROGRESS.value,
                    editor_comment="",
                    final_answer="",
                )
            }
        ),
        notifier=FakeNotifier(),
        dashboard_sync=RateLimitedDashboardSync(),
    )

    await service.run_once()

    tracked = await repository.get_submitted_application("A1B2C3D4")
    assert tracked is not None
    assert tracked.last_known_status == ApplicationStatus.IN_PROGRESS.value
    assert tracked.last_seen_row_number == 5


@pytest.mark.asyncio
async def test_notification_service_sends_status_and_final_answer(tmp_path):
    repository = DraftRepository(str(tmp_path / "grouped.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=7,
                    direction=Direction.FL.value,
                    answer_type=AnswerType.ROLLOUT.value,
                    change_type=ChangeType.ADD.value,
                    status=ApplicationStatus.FINAL_ANSWER_READY.value,
                    editor_comment="Уточните <деталь>",
                    final_answer="Можно использовать финальный текст",
                )
            }
        ),
        notifier=notifier,
    )

    await service.run_once()

    assert len(notifier.messages) == 1
    message = notifier.messages[0]
    assert message["chat_id"] == 100
    assert message["parse_mode"] == "HTML"
    assert message["link_preview_options"].is_disabled is True
    assert "<b>ФЛ</b>" in message["text"]
    assert f"<b>{AnswerType.ROLLOUT.value} / {ChangeType.ADD.value}</b>" in message["text"]
    assert (
        '• <a href="https://docs.google.com/spreadsheets/d/fl-spreadsheet/edit#gid=100&amp;range=A7:W7">A1B2C3D4</a>: '
        f"статус {ApplicationStatus.NEW.value} → {ApplicationStatus.FINAL_ANSWER_READY.value}; "
        "готов итоговый ответ"
        in message["text"]
    )
    assert "Итоговый ответ по заявке" in message["text"]
    assert "Можно использовать финальный текст" in message["text"]
    assert "Уточните &lt;деталь&gt;" not in message["text"]

    tracked = await repository.get_submitted_application("A1B2C3D4")
    assert tracked is not None
    assert tracked.last_known_status == ApplicationStatus.FINAL_ANSWER_READY.value
    assert tracked.last_seen_final_answer == "Можно использовать финальный текст"


@pytest.mark.asyncio
async def test_notification_service_groups_regular_notifications_by_direction_and_type(tmp_path):
    repository = DraftRepository(str(tmp_path / "grouped_regular_notifications.db"))
    await repository.init()
    applications = [
        (
            "FLADD001",
            Direction.FL.value,
            AnswerType.ROLLOUT.value,
            ChangeType.ADD.value,
            ApplicationStatus.ACCEPTED.value,
            FL_SPREADSHEET,
            100,
            5,
        ),
        (
            "FLCHIP01",
            Direction.FL.value,
            AnswerType.URGENT.value,
            ChangeType.CHIPS.value,
            ApplicationStatus.POSTPONED.value,
            FL_SPREADSHEET,
            100,
            6,
        ),
        (
            "SMEEDIT1",
            Direction.SME.value,
            AnswerType.INTEGRATION.value,
            ChangeType.EDIT.value,
            ApplicationStatus.REJECTED.value,
            SME_SPREADSHEET,
            200,
            7,
        ),
    ]
    statuses = {}
    for (
        application_id,
        direction,
        answer_type,
        change_type,
        status,
        spreadsheet_id,
        sheet_id,
        row_number,
    ) in applications:
        await repository.save_submitted_application(
            application_id=application_id,
            telegram_user_id=100,
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            sheet_name=WEEK_SHEET,
            last_known_status=ApplicationStatus.NEW.value,
            direction=direction,
            answer_type=answer_type,
            change_type=change_type,
        )
        statuses[application_id] = SheetApplicationStatus(
            application_id=application_id,
            spreadsheet_id=spreadsheet_id,
            sheet_name=WEEK_SHEET,
            sheet_id=sheet_id,
            row_number=row_number,
            direction=direction,
            answer_type=answer_type,
            change_type=change_type,
            status=status,
            editor_comment="",
            final_answer="",
        )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(statuses),
        notifier=notifier,
    )

    await service.run_once()

    assert len(notifier.messages) == 1
    text = notifier.messages[0]["text"]
    assert text.count("<b>Изменения по заявкам</b>") == 1
    assert "<b>ФЛ</b>" in text
    assert "<b>SME</b>" in text
    assert f"<b>{AnswerType.ROLLOUT.value} / {ChangeType.ADD.value}</b>" in text
    assert f"<b>{AnswerType.URGENT.value} / {ChangeType.CHIPS.value}</b>" in text
    assert f"<b>{AnswerType.INTEGRATION.value} / {ChangeType.EDIT.value}</b>" in text
    assert "• " in text
    assert "FLADD001" in text
    assert "FLCHIP01" in text
    assert "SMEEDIT1" in text
    outbox = await repository.list_notification_outbox()
    assert len(outbox) == 1
    assert outbox[0].event_type == "application-status"


@pytest.mark.asyncio
async def test_chips_notifies_status_but_never_renders_final_answer(tmp_path):
    repository = DraftRepository(str(tmp_path / "chips-status.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="CHIPS001",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
        change_type=ChangeType.CHIPS.value,
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "CHIPS001": SheetApplicationStatus(
                    application_id="CHIPS001",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=7,
                    change_type=ChangeType.CHIPS.value,
                    status=ApplicationStatus.FINAL_ANSWER_READY.value,
                    editor_comment="",
                    final_answer="Legacy final answer must be ignored",
                )
            }
        ),
        notifier=notifier,
    )

    await service.run_once()

    assert len(notifier.messages) == 1
    assert "Legacy final answer must be ignored" not in notifier.messages[0]["text"]
    assert "CHIPS001" in notifier.messages[0]["text"]


@pytest.mark.asyncio
async def test_single_in_progress_status_notifies_scriptwriter(tmp_path):
    repository = DraftRepository(str(tmp_path / "in_progress_notification.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="WORK0001",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
        direction=Direction.FL.value,
        answer_type=AnswerType.ROLLOUT.value,
        change_type=ChangeType.ADD.value,
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "WORK0001": SheetApplicationStatus(
                    application_id="WORK0001",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=7,
                    direction=Direction.FL.value,
                    answer_type=AnswerType.ROLLOUT.value,
                    change_type=ChangeType.ADD.value,
                    status=ApplicationStatus.IN_PROGRESS.value,
                    editor_comment="",
                    final_answer="",
                )
            }
        ),
        notifier=notifier,
    )

    await service.run_once()

    assert len(notifier.messages) == 1
    assert "статус Новая → В работе" in notifier.messages[0]["text"]
    tracked = await repository.get_submitted_application("WORK0001")
    assert tracked is not None
    assert tracked.last_known_status == ApplicationStatus.IN_PROGRESS.value


@pytest.mark.asyncio
async def test_single_editor_comment_is_sent_after_three_stable_polls(tmp_path):
    repository = DraftRepository(str(tmp_path / "clarification_comment.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.IN_PROGRESS.value,
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=7,
                    status=ApplicationStatus.IN_PROGRESS.value,
                    editor_comment="Уточните <деталь>",
                    final_answer="",
                )
            }
        ),
        notifier=notifier,
    )

    await service.run_once()
    first = await repository.get_submitted_application("A1B2C3D4")
    await service.run_once()
    second = await repository.get_submitted_application("A1B2C3D4")
    await service.run_once()

    assert first is not None
    assert first.pending_editor_comment == "Уточните <деталь>"
    assert first.pending_editor_comment_seen_count == 1
    assert second is not None
    assert second.pending_editor_comment_seen_count == 2
    assert len(notifier.messages) == 1
    assert "Комментарий редактора по заявке" in notifier.messages[0]["text"]
    assert "Уточните &lt;деталь&gt;" in notifier.messages[0]["text"]
    tracked = await repository.get_submitted_application("A1B2C3D4")
    assert tracked is not None
    assert tracked.last_seen_editor_comment == "Уточните <деталь>"
    assert tracked.pending_editor_comment is None
    assert tracked.pending_editor_comment_seen_count == 0


@pytest.mark.asyncio
async def test_single_editor_comment_change_resets_stable_counter(tmp_path):
    repository = DraftRepository(str(tmp_path / "clarification_comment_change.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.IN_PROGRESS.value,
    )
    current = SheetApplicationStatus(
        application_id="A1B2C3D4",
        spreadsheet_id=FL_SPREADSHEET,
        sheet_name=WEEK_SHEET,
        sheet_id=100,
        row_number=7,
        status=ApplicationStatus.IN_PROGRESS.value,
        editor_comment="Черновик вопроса",
        final_answer="",
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader({"A1B2C3D4": current}),
        notifier=notifier,
    )

    await service.run_once()
    current.editor_comment = "Финальный вопрос"
    await service.run_once()
    second = await repository.get_submitted_application("A1B2C3D4")
    await service.run_once()
    await service.run_once()

    assert second is not None
    assert second.pending_editor_comment == "Финальный вопрос"
    assert second.pending_editor_comment_seen_count == 1
    assert len(notifier.messages) == 1
    assert "Черновик вопроса" not in notifier.messages[0]["text"]
    assert "Финальный вопрос" in notifier.messages[0]["text"]


@pytest.mark.asyncio
async def test_empty_editor_comment_clears_pending_without_notification(tmp_path):
    repository = DraftRepository(str(tmp_path / "clarification_comment_empty.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.IN_PROGRESS.value,
    )
    current = SheetApplicationStatus(
        application_id="A1B2C3D4",
        spreadsheet_id=FL_SPREADSHEET,
        sheet_name=WEEK_SHEET,
        sheet_id=100,
        row_number=7,
        status=ApplicationStatus.IN_PROGRESS.value,
        editor_comment="Черновик вопроса",
        final_answer="",
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader({"A1B2C3D4": current}),
        notifier=notifier,
    )

    await service.run_once()
    current.editor_comment = ""
    await service.run_once()

    tracked = await repository.get_submitted_application("A1B2C3D4")
    assert notifier.messages == []
    assert tracked is not None
    assert tracked.pending_editor_comment is None
    assert tracked.pending_editor_comment_seen_count == 0


@pytest.mark.asyncio
async def test_scriptwriter_response_notifies_editor_chat(tmp_path):
    repository = DraftRepository(str(tmp_path / "urgent_scriptwriter_response.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="URGRESP1",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name="Срочные",
        last_known_status=ApplicationStatus.NEEDS_CLARIFICATION.value,
        direction=Direction.FL.value,
        answer_type=AnswerType.URGENT.value,
        application_type=ApplicationType.SINGLE.value,
        is_urgent=True,
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "URGRESP1": SheetApplicationStatus(
                    application_id="URGRESP1",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name="Срочные",
                    sheet_id=100,
                    row_number=12,
                    status=ApplicationStatus.NEEDS_CLARIFICATION.value,
                    editor_comment="",
                    final_answer="",
                    scriptwriter_response="Сценарист уточнил детали <важно>",
                    editor="Редактор",
                    direction=Direction.FL.value,
                    answer_type=AnswerType.URGENT.value,
                    is_urgent=True,
                    scriptwriter="Петров Петр",
                    intent="urgent.intent",
                    end_column="X",
                )
            }
        ),
        notifier=notifier,
        urgent_editor_notifications_enabled=True,
        editor_urgent_chat_id=-100123456,
    )

    await service.run_once()
    await service.run_once()
    await service.run_once()

    assert len(notifier.messages) == 1
    message = notifier.messages[0]
    assert message["chat_id"] == -100123456
    assert message["reply_markup"] is None
    assert "Сценарист ответил по заявке" in message["text"]
    assert "Тип ответа:</b> Срочные" in message["text"]
    assert "Петров Петр" in message["text"]
    assert "urgent.intent" in message["text"]
    assert "Сценарист уточнил детали &lt;важно&gt;" in message["text"]
    assert "Открыть заявку" in message["text"]
    tracked = await repository.get_submitted_application("URGRESP1")
    assert tracked is not None
    assert tracked.last_seen_scriptwriter_response == "Сценарист уточнил детали <важно>"
    outbox = await repository.list_notification_outbox()
    assert len(outbox) == 1
    assert outbox[0].event_type == "editor-scriptwriter-response"

    events = await repository.list_application_events(
        application_id="URGRESP1",
        event_type="scriptwriter_response_added",
    )
    assert len(events) == 1
    assert events[0].telegram_user_id == 100
    assert events[0].old_value is None
    assert events[0].new_value == tracked.last_seen_scriptwriter_response


@pytest.mark.asyncio
async def test_urgent_scriptwriter_response_is_not_duplicated(tmp_path):
    repository = DraftRepository(str(tmp_path / "urgent_scriptwriter_dedupe.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="URGRESP2",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name="Срочные",
        last_known_status=ApplicationStatus.NEEDS_CLARIFICATION.value,
        answer_type=AnswerType.URGENT.value,
        application_type=ApplicationType.SINGLE.value,
        is_urgent=True,
    )
    status_reader = FakeStatusReader(
        {
            "URGRESP2": SheetApplicationStatus(
                application_id="URGRESP2",
                spreadsheet_id=FL_SPREADSHEET,
                sheet_name="Срочные",
                sheet_id=100,
                row_number=12,
                status=ApplicationStatus.NEEDS_CLARIFICATION.value,
                editor_comment="",
                final_answer="",
                scriptwriter_response="Один и тот же ответ",
                answer_type=AnswerType.URGENT.value,
                is_urgent=True,
            )
        }
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=status_reader,
        notifier=notifier,
        urgent_editor_notifications_enabled=True,
        editor_urgent_chat_id=-100123456,
    )

    await service.run_once()
    await service.run_once()
    await service.run_once()
    await service.run_once()

    assert len(notifier.messages) == 1
    outbox = await repository.list_notification_outbox()
    events = await repository.list_application_events(
        application_id="URGRESP2",
        event_type="scriptwriter_response_added",
    )
    assert len(outbox) == 1
    assert len(events) == 1


@pytest.mark.asyncio
async def test_changed_urgent_scriptwriter_response_notifies_again(tmp_path):
    repository = DraftRepository(str(tmp_path / "urgent_scriptwriter_changed.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="URGRESP3",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name="Срочные",
        last_known_status=ApplicationStatus.NEEDS_CLARIFICATION.value,
        answer_type=AnswerType.URGENT.value,
        application_type=ApplicationType.SINGLE.value,
        is_urgent=True,
    )
    current = SheetApplicationStatus(
        application_id="URGRESP3",
        spreadsheet_id=FL_SPREADSHEET,
        sheet_name="Срочные",
        sheet_id=100,
        row_number=12,
        status=ApplicationStatus.NEEDS_CLARIFICATION.value,
        editor_comment="",
        final_answer="",
        scriptwriter_response="Первый ответ",
        answer_type=AnswerType.URGENT.value,
        is_urgent=True,
    )
    status_reader = FakeStatusReader({"URGRESP3": current})
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=status_reader,
        notifier=notifier,
        urgent_editor_notifications_enabled=True,
        editor_urgent_chat_id=-100123456,
    )

    await service.run_once()
    await service.run_once()
    await service.run_once()
    current.scriptwriter_response = "Исправленный ответ"
    await service.run_once()
    await service.run_once()
    await service.run_once()

    assert len(notifier.messages) == 2
    assert "Первый ответ" in notifier.messages[0]["text"]
    assert "Исправленный ответ" in notifier.messages[1]["text"]

    events = await repository.list_application_events(
        application_id="URGRESP3",
        event_type="scriptwriter_response_added",
    )
    assert len(events) == 2
    assert events[0].new_value == current.scriptwriter_response
    assert events[1].new_value == "Первый ответ"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer_type",
    [AnswerType.ROLLOUT.value, AnswerType.INTEGRATION.value],
)
async def test_non_urgent_scriptwriter_response_notifies_editor_chat(
    tmp_path,
    answer_type,
):
    repository = DraftRepository(str(tmp_path / "non_urgent_scriptwriter_response.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="REGRESP1",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEEDS_CLARIFICATION.value,
        answer_type=answer_type,
        change_type=ChangeType.ADD.value,
        application_type=ApplicationType.SINGLE.value,
        is_urgent=False,
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "REGRESP1": SheetApplicationStatus(
                    application_id="REGRESP1",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=12,
                    status=ApplicationStatus.NEEDS_CLARIFICATION.value,
                    editor_comment="",
                    final_answer="",
                    scriptwriter_response="Ответ по несрочной заявке",
                    answer_type=answer_type,
                    change_type=ChangeType.ADD.value,
                    is_urgent=False,
                )
            }
        ),
        notifier=notifier,
        urgent_editor_notifications_enabled=True,
        editor_urgent_chat_id=-100123456,
    )

    await service.run_once()
    await service.run_once()
    await service.run_once()

    assert len(notifier.messages) == 1
    assert notifier.messages[0]["chat_id"] == -100123456
    assert f"Тип ответа:</b> {answer_type}" in notifier.messages[0]["text"]
    assert "Тип изменения:</b> ADD" in notifier.messages[0]["text"]
    tracked = await repository.get_submitted_application("REGRESP1")
    assert tracked is not None
    assert tracked.last_seen_scriptwriter_response == "Ответ по несрочной заявке"


@pytest.mark.asyncio
async def test_existing_scriptwriter_response_is_baselined_without_notification(tmp_path):
    database_path = tmp_path / "scriptwriter_response_baseline.db"
    repository = DraftRepository(str(database_path))
    await repository.init()
    await repository.save_submitted_application(
        application_id="BASELINE1",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.IN_PROGRESS.value,
        answer_type=AnswerType.ROLLOUT.value,
        application_type=ApplicationType.SINGLE.value,
        is_urgent=False,
    )
    async with aiosqlite.connect(database_path) as db:
        await db.execute(
            """
            UPDATE submitted_applications
            SET scriptwriter_response_tracking_initialized = 0
            WHERE application_id = 'BASELINE1'
            """
        )
        await db.commit()

    current = SheetApplicationStatus(
        application_id="BASELINE1",
        spreadsheet_id=FL_SPREADSHEET,
        sheet_name=WEEK_SHEET,
        sheet_id=100,
        row_number=12,
        status=ApplicationStatus.IN_PROGRESS.value,
        editor_comment="",
        final_answer="",
        scriptwriter_response="Ответ до релиза",
        answer_type=AnswerType.ROLLOUT.value,
        is_urgent=False,
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader({"BASELINE1": current}),
        notifier=notifier,
        urgent_editor_notifications_enabled=True,
        editor_urgent_chat_id=-100123456,
    )

    await service.run_once()
    await service.run_once()
    await service.run_once()

    assert notifier.messages == []
    assert await repository.list_application_events(
        application_id="BASELINE1",
        event_type="scriptwriter_response_added",
    ) == []
    tracked = await repository.get_submitted_application("BASELINE1")
    assert tracked is not None
    assert tracked.scriptwriter_response_tracking_initialized is True
    assert tracked.last_seen_scriptwriter_response == "Ответ до релиза"

    current.scriptwriter_response = "Новый ответ после релиза"
    await service.run_once()
    await service.run_once()
    await service.run_once()

    assert len(notifier.messages) == 1
    assert "Новый ответ после релиза" in notifier.messages[0]["text"]


@pytest.mark.asyncio
async def test_scriptwriter_response_is_tracked_when_editor_delivery_is_disabled(tmp_path):
    repository = DraftRepository(str(tmp_path / "disabled_editor_delivery.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="DISABLED1",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.IN_PROGRESS.value,
        answer_type=AnswerType.ROLLOUT.value,
        application_type=ApplicationType.SINGLE.value,
        is_urgent=False,
    )
    current = SheetApplicationStatus(
        application_id="DISABLED1",
        spreadsheet_id=FL_SPREADSHEET,
        sheet_name=WEEK_SHEET,
        sheet_id=100,
        row_number=12,
        status=ApplicationStatus.IN_PROGRESS.value,
        editor_comment="",
        final_answer="",
        scriptwriter_response="Ответ при выключенной доставке",
        answer_type=AnswerType.ROLLOUT.value,
        is_urgent=False,
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader({"DISABLED1": current}),
        notifier=notifier,
        urgent_editor_notifications_enabled=False,
        editor_urgent_chat_id=-100123456,
    )

    await service.run_once()
    await service.run_once()
    await service.run_once()

    assert notifier.messages == []
    assert await repository.list_notification_outbox() == []
    events = await repository.list_application_events(
        application_id="DISABLED1",
        event_type="scriptwriter_response_added",
    )
    assert len(events) == 1
    tracked = await repository.get_submitted_application("DISABLED1")
    assert tracked is not None
    assert tracked.last_seen_scriptwriter_response == current.scriptwriter_response


@pytest.mark.asyncio
async def test_urgent_chips_scriptwriter_response_notifies_editor_chat(tmp_path):
    repository = DraftRepository(str(tmp_path / "urgent_chips_scriptwriter_response.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="CHIPRESP",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name="Срочные",
        last_known_status=ApplicationStatus.NEEDS_CLARIFICATION.value,
        direction=Direction.FL.value,
        answer_type=AnswerType.URGENT.value,
        application_type=ApplicationType.SINGLE.value,
        change_type=ChangeType.CHIPS.value,
        is_urgent=True,
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "CHIPRESP": SheetApplicationStatus(
                    application_id="CHIPRESP",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name="Срочные",
                    sheet_id=100,
                    row_number=12,
                    status=ApplicationStatus.NEEDS_CLARIFICATION.value,
                    editor_comment="",
                    final_answer="",
                    scriptwriter_response="Ответ сценариста по CHIPS",
                    direction=Direction.FL.value,
                    answer_type=AnswerType.URGENT.value,
                    is_urgent=True,
                    change_type=ChangeType.CHIPS.value,
                    scriptwriter="Петров Петр",
                    intent="chips.intent",
                    end_column="U",
                )
            }
        ),
        notifier=notifier,
        urgent_editor_notifications_enabled=True,
        editor_urgent_chat_id=-100123456,
    )

    await service.run_once()
    await service.run_once()
    await service.run_once()

    assert len(notifier.messages) == 1
    assert notifier.messages[0]["chat_id"] == -100123456
    assert notifier.messages[0]["reply_markup"] is None
    assert "Ответ сценариста по CHIPS" in notifier.messages[0]["text"]
    assert "chips.intent" in notifier.messages[0]["text"]
    assert "Итоговый ответ" not in notifier.messages[0]["text"]
    tracked = await repository.get_submitted_application("CHIPRESP")
    assert tracked is not None
    assert tracked.last_seen_scriptwriter_response == "Ответ сценариста по CHIPS"


@pytest.mark.asyncio
async def test_editor_change_updates_dashboard_without_notification(tmp_path):
    repository = DraftRepository(str(tmp_path / "editor_change.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
        last_seen_editor="Редактор не выбран",
    )
    notifier = FakeNotifier()
    dashboard_sync = FakeDashboardSync()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=7,
                    status=ApplicationStatus.NEW.value,
                    editor_comment="",
                    final_answer="",
                    editor="редактор 2",
                )
            }
        ),
        notifier=notifier,
        dashboard_sync=dashboard_sync,
    )

    await service.run_once()

    assert notifier.messages == []
    assert len(dashboard_sync.upserts) == 1
    tracked = await repository.get_submitted_application("A1B2C3D4")
    assert tracked is not None
    assert tracked.last_seen_editor == "редактор 2"


@pytest.mark.asyncio
async def test_single_final_answer_waits_for_final_answer_ready_status(tmp_path):
    repository = DraftRepository(str(tmp_path / "final_answer_gate.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=7,
                    status=ApplicationStatus.NEW.value,
                    editor_comment="",
                    final_answer="Финальный текст до смены статуса",
                )
            }
        ),
        notifier=notifier,
    )

    await service.run_once()
    tracked = await repository.get_submitted_application("A1B2C3D4")

    assert notifier.messages == []
    assert tracked is not None
    assert tracked.last_known_status == ApplicationStatus.NEW.value
    assert tracked.last_seen_final_answer == "Финальный текст до смены статуса"

    service.status_reader = FakeStatusReader(
        {
            "A1B2C3D4": SheetApplicationStatus(
                application_id="A1B2C3D4",
                spreadsheet_id=FL_SPREADSHEET,
                sheet_name=WEEK_SHEET,
                sheet_id=100,
                row_number=7,
                status=ApplicationStatus.FINAL_ANSWER_READY.value,
                editor_comment="",
                final_answer="Финальный текст до смены статуса",
            )
        }
    )

    await service.run_once()

    assert len(notifier.messages) == 1
    assert "Финальный текст до смены статуса" in notifier.messages[0]["text"]


@pytest.mark.asyncio
async def test_final_answer_event_is_recorded_when_text_appears_after_final_status(tmp_path):
    repository = DraftRepository(str(tmp_path / "final_answer_after_status.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.FINAL_ANSWER_READY.value,
        last_seen_final_answer="",
    )
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=7,
                    status=ApplicationStatus.FINAL_ANSWER_READY.value,
                    editor_comment="",
                    final_answer="Финальный текст после смены статуса",
                )
            }
        ),
        notifier=FakeNotifier(),
    )

    await service.run_once()
    events = await repository.list_application_events(
        application_id="A1B2C3D4",
        event_type="final_answer_added",
    )

    assert len(events) == 1
    assert events[0].new_value == "Финальный текст после смены статуса"


@pytest.mark.asyncio
async def test_notification_service_persists_event_if_send_fails(tmp_path):
    repository = DraftRepository(str(tmp_path / "send_failure.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
    )
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=7,
                    status=ApplicationStatus.ACCEPTED.value,
                    editor_comment="",
                    final_answer="",
                )
            }
        ),
        notifier=FakeNotifier(fail=True),
    )

    await service.run_once()

    tracked = await repository.get_submitted_application("A1B2C3D4")
    assert tracked is not None
    assert tracked.last_known_status == ApplicationStatus.ACCEPTED.value
    outbox = await repository.list_notification_outbox()
    assert len(outbox) == 1
    assert outbox[0].state == "PENDING"
    assert outbox[0].attempts == 1
    assert tracked.last_seen_row_number == 7


@pytest.mark.asyncio
async def test_notification_keyboard_returns_to_active_single_draft(tmp_path):
    repository = DraftRepository(str(tmp_path / "active_keyboard.db"))
    await repository.init()
    await repository.get_or_create(100)
    await repository.save_answer(100, FieldName.DIRECTION.value, Direction.FL.value)
    await repository.set_step(100, Step.ANSWER_TYPE)
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=100,
                    row_number=7,
                    status=ApplicationStatus.ACCEPTED.value,
                    editor_comment="",
                    final_answer="",
                )
            }
        ),
        notifier=notifier,
    )

    await service.run_once()

    keyboard = notifier.messages[0]["reply_markup"].inline_keyboard
    assert keyboard[0][0].text == "Назад к заведению заявки"
    assert keyboard[0][0].callback_data == "app:notification:new"


@pytest.mark.asyncio
async def test_urgent_editor_notification_is_sent_without_keyboard(tmp_path):
    repository = DraftRepository(str(tmp_path / "urgent_editor_notification.db"))
    await repository.init()
    await repository.enqueue_notification_event(
        telegram_user_id=-100123456,
        event_type="urgent-editor-application-created",
        dedupe_key="urgent-editor-application-created:A1B2C3D4",
        snapshot_json='{"application_id":"A1B2C3D4"}',
        chunks=[
            '🚨 <b>Новая срочная заявка</b>\n\n'
            '<a href="https://docs.google.com/">Открыть заявку</a>'
        ],
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader({}),
        notifier=notifier,
    )

    await service.run_once()

    assert len(notifier.messages) == 1
    assert notifier.messages[0]["chat_id"] == -100123456
    assert notifier.messages[0]["reply_markup"] is None
    assert notifier.messages[0]["parse_mode"] == "HTML"
    outbox = await repository.list_notification_outbox()
    assert outbox[0].state == "SENT"


@pytest.mark.asyncio
async def test_notification_keyboard_returns_to_pending_bulk_creation(tmp_path):
    repository = DraftRepository(str(tmp_path / "pending_bulk_keyboard.db"))
    await repository.init()
    await repository.save_user_setting(
        100,
        "pending_action",
        "create_bulk_direction:idempotency-key",
    )
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader({}),
        notifier=FakeNotifier(),
    )

    kind = await service._keyboard_kind_for_user(100)
    keyboard = build_keyboard(kind)

    assert kind == KeyboardKind.NOTIFICATION
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].text == "Главное меню"
    assert keyboard.inline_keyboard[0][0].callback_data == "app:notification:new"




@pytest.mark.asyncio
async def test_notification_keyboard_uses_main_menu_without_active_workflow(tmp_path):
    repository = DraftRepository(str(tmp_path / "neutral_keyboard.db"))
    await repository.init()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader({}),
        notifier=FakeNotifier(),
    )

    kind = await service._keyboard_kind_for_user(100)
    keyboard = build_keyboard(kind)

    assert kind == KeyboardKind.NOTIFICATION
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].text == "Главное меню"











@pytest.mark.asyncio
async def test_notification_failure_for_one_user_does_not_block_others(tmp_path):
    repository = DraftRepository(str(tmp_path / "isolated_users.db"))
    await repository.init()
    for application_id, user_id in (("A1B2C3D4", 100), ("B1C2D3E4", 200)):
        await repository.save_submitted_application(
            application_id=application_id,
            telegram_user_id=user_id,
            spreadsheet_id=FL_SPREADSHEET,
            sheet_id=100,
            sheet_name=WEEK_SHEET,
            last_known_status=ApplicationStatus.NEW.value,
        )
    statuses = {
        application_id: SheetApplicationStatus(
            application_id=application_id,
            spreadsheet_id=FL_SPREADSHEET,
            sheet_name=WEEK_SHEET,
            sheet_id=100,
            row_number=row_number,
            status=ApplicationStatus.ACCEPTED.value,
            editor_comment="",
            final_answer="",
        )
        for application_id, row_number in (("A1B2C3D4", 2), ("B1C2D3E4", 3))
    }
    notifier = SelectiveFailNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(statuses),
        notifier=notifier,
    )

    await service.run_once()

    failed = await repository.get_submitted_application("A1B2C3D4")
    successful = await repository.get_submitted_application("B1C2D3E4")
    assert failed is not None
    assert successful is not None
    assert failed.last_known_status == ApplicationStatus.ACCEPTED.value
    assert successful.last_known_status == ApplicationStatus.ACCEPTED.value
    outbox = await repository.list_notification_outbox()
    failed_outbox = [item for item in outbox if item.telegram_user_id == 100]
    assert len(failed_outbox) == 1
    assert failed_outbox[0].state == "PENDING"
    assert [message["chat_id"] for message in notifier.messages] == [200]


@pytest.mark.asyncio
async def test_deletion_status_requires_two_stable_polling_cycles(tmp_path):
    repository = DraftRepository(str(tmp_path / "deletion_status.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="DEL00001",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
        last_seen_row_number=5,
    )
    await repository.save_submitted_application(
        application_id="LOWER001",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
        last_seen_row_number=6,
    )
    statuses = {
        "DEL00001": SheetApplicationStatus(
            application_id="DEL00001",
            spreadsheet_id=FL_SPREADSHEET,
            sheet_name=WEEK_SHEET,
            sheet_id=100,
            row_number=5,
            status=ApplicationStatus.DELETION.value,
            editor_comment="",
            final_answer="",
        )
    }
    reader = FakeDeletingStatusReader(statuses)
    service = StatusNotificationService(
        repository=repository,
        status_reader=reader,
        notifier=FakeNotifier(),
    )

    await service.run_once()

    pending = await repository.get_submitted_application("DEL00001")
    assert pending is not None
    assert pending.deletion_seen_count == 1
    assert reader.deleted == []

    await service.run_once()

    assert await repository.get_submitted_application("DEL00001") is None
    shifted = await repository.get_submitted_application("LOWER001")
    assert shifted is not None
    assert shifted.last_seen_row_number == 5
    assert reader.deleted == ["DEL00001"]
    dashboard_outbox = await repository.list_dashboard_outbox()
    assert len(dashboard_outbox) == 1
    assert json.loads(dashboard_outbox[0].snapshot_json) == {
        "action": "delete",
        "application_id": "DEL00001",
    }


@pytest.mark.asyncio
async def test_deletion_status_allows_zero_sheet_id(tmp_path):
    repository = DraftRepository(str(tmp_path / "deletion_zero_sheet_id.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="DEL00000",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=0,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
        last_seen_row_number=5,
    )
    statuses = {
        "DEL00000": SheetApplicationStatus(
            application_id="DEL00000",
            spreadsheet_id=FL_SPREADSHEET,
            sheet_name=WEEK_SHEET,
            sheet_id=0,
            row_number=5,
            status=ApplicationStatus.DELETION.value,
            editor_comment="",
            final_answer="",
        )
    }
    reader = FakeDeletingStatusReader(statuses)
    service = StatusNotificationService(
        repository=repository,
        status_reader=reader,
        notifier=FakeNotifier(),
    )

    await service.run_once()
    await service.run_once()

    assert await repository.get_submitted_application("DEL00000") is None
    assert reader.deleted == ["DEL00000"]


@pytest.mark.asyncio
async def test_deletion_status_processes_same_sheet_rows_bottom_up(tmp_path):
    repository = DraftRepository(str(tmp_path / "deletion_bottom_up.db"))
    await repository.init()
    rows = {
        "DEL00003": 3,
        "DEL00006": 6,
        "DEL00007": 7,
        "LOWER008": 8,
    }
    for application_id, row_number in rows.items():
        await repository.save_submitted_application(
            application_id=application_id,
            telegram_user_id=100,
            spreadsheet_id=FL_SPREADSHEET,
            sheet_id=100,
            sheet_name=WEEK_SHEET,
            last_known_status=ApplicationStatus.NEW.value,
            last_seen_row_number=row_number,
        )
    statuses = {
        application_id: SheetApplicationStatus(
            application_id=application_id,
            spreadsheet_id=FL_SPREADSHEET,
            sheet_name=WEEK_SHEET,
            sheet_id=100,
            row_number=row_number,
            status=ApplicationStatus.DELETION.value,
            editor_comment="",
            final_answer="",
        )
        for application_id, row_number in rows.items()
        if application_id.startswith("DEL")
    }
    statuses["LOWER008"] = SheetApplicationStatus(
        application_id="LOWER008",
        spreadsheet_id=FL_SPREADSHEET,
        sheet_name=WEEK_SHEET,
        sheet_id=100,
        row_number=8,
        status=ApplicationStatus.NEW.value,
        editor_comment="",
        final_answer="",
    )
    reader = FakeDeletingStatusReader(statuses)
    service = StatusNotificationService(
        repository=repository,
        status_reader=reader,
        notifier=FakeNotifier(),
    )

    await service.run_once()
    await service.run_once()

    assert reader.deleted == ["DEL00007", "DEL00006", "DEL00003"]
    assert await repository.get_submitted_application("DEL00003") is None
    assert await repository.get_submitted_application("DEL00006") is None
    assert await repository.get_submitted_application("DEL00007") is None
    lower = await repository.get_submitted_application("LOWER008")
    assert lower is not None
    assert lower.last_seen_row_number == 5


@pytest.mark.asyncio
async def test_deletion_status_reset_when_status_changes(tmp_path):
    repository = DraftRepository(str(tmp_path / "deletion_reset.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="DEL00002",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
        last_seen_row_number=5,
    )
    statuses = {
        "DEL00002": SheetApplicationStatus(
            application_id="DEL00002",
            spreadsheet_id=FL_SPREADSHEET,
            sheet_name=WEEK_SHEET,
            sheet_id=100,
            row_number=5,
            status=ApplicationStatus.DELETION.value,
            editor_comment="",
            final_answer="",
        )
    }
    reader = FakeDeletingStatusReader(statuses)
    service = StatusNotificationService(
        repository=repository,
        status_reader=reader,
        notifier=FakeNotifier(),
    )

    await service.run_once()
    statuses["DEL00002"] = SheetApplicationStatus(
        application_id="DEL00002",
        spreadsheet_id=FL_SPREADSHEET,
        sheet_name=WEEK_SHEET,
        sheet_id=100,
        row_number=5,
        status=ApplicationStatus.IN_PROGRESS.value,
        editor_comment="",
        final_answer="",
    )
    await service.run_once()

    tracked = await repository.get_submitted_application("DEL00002")
    assert tracked is not None
    assert tracked.deletion_seen_count == 0
    assert tracked.last_known_status == ApplicationStatus.IN_PROGRESS.value
    assert reader.deleted == []




















def test_status_polling_memory_maintenance_collects_and_trims(monkeypatch):
    calls = []

    monkeypatch.setattr(notifications_module.gc, "collect", lambda: calls.append("gc"))
    monkeypatch.setattr(
        notifications_module,
        "_malloc_trim",
        lambda: calls.append("trim"),
    )

    notifications_module._run_memory_maintenance()

    assert calls == ["gc", "trim"]


@pytest.mark.asyncio
async def test_status_polling_memory_log_uses_configured_interval(monkeypatch, caplog):
    class FakePollingService:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self) -> None:
            self.calls += 1

    service = FakePollingService()
    maintenance_calls = []
    snapshots = [(100.0, 200.0), (115.0, 220.0)]
    sleep_calls = 0

    async def fake_sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(
        notifications_module,
        "_run_memory_maintenance",
        lambda: maintenance_calls.append("cleanup"),
    )
    monkeypatch.setattr(
        notifications_module,
        "_process_memory_snapshot_mb",
        lambda: snapshots.pop(0),
    )
    monkeypatch.setattr(notifications_module.asyncio, "sleep", fake_sleep)

    with caplog.at_level(logging.INFO):
        with pytest.raises(asyncio.CancelledError):
            await notifications_module.run_status_polling_loop(
                service=service,
                interval_seconds=30,
                memory_log_interval=2,
            )

    assert service.calls == 3
    assert maintenance_calls == ["cleanup", "cleanup", "cleanup"]
    memory_logs = [
        record.message for record in caplog.records if "Status polling memory" in record.message
    ]
    assert len(memory_logs) == 1
    assert "iteration=2" in memory_logs[0]
    assert "rss_mb=100.0" in memory_logs[0]


def _sheet_name_from_range(range_name: str) -> str:
    quoted_name = range_name.split("!", maxsplit=1)[0]
    if quoted_name.startswith("'") and quoted_name.endswith("'"):
        return quoted_name[1:-1].replace("''", "'")
    return quoted_name


def _slice_fake_rows(range_name: str, rows: list[list[str]]) -> list[list[str]]:
    if "!" not in range_name:
        return rows
    suffix = range_name.split("!", maxsplit=1)[1]
    match = re.fullmatch(r"[A-Z]+(?P<start>\d+):[A-Z]+(?P<end>\d+)", suffix)
    if match is not None:
        start = int(match.group("start"))
        end = int(match.group("end"))
        return rows[start - 1 : end]
    if re.fullmatch(r"[A-Z]+:[A-Z]+", suffix):
        return rows
    return rows
