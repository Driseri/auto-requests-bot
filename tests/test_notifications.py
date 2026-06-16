from __future__ import annotations

import json

import pytest
from googleapiclient.errors import HttpError

from app.bulk import (
    BULK_STAGING_HEADERS,
    CURRENT_BULK_STAGING_HEADERS,
    LEGACY_BULK_STAGING_HEADERS,
)
from app.keyboards import build_keyboard
from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
    BulkApplicationStatus,
    BulkBatch,
    BulkBatchStatus,
    Direction,
    FieldName,
    KeyboardKind,
    Step,
    SubmittedApplication,
)
from app.notifications import (
    BulkEditorComment,
    GoogleSheetsStatusReader,
    SheetApplicationStatus,
    SheetBulkBatchStatus,
    StatusNotificationService,
    _split_html_message,
    _working_data_rows,
)
from app.repository import DraftRepository
from app.submission import (
    DirectionSpreadsheetConfig,
    LEGACY_WORKSHEET_HEADERS,
    SHEET_HEADERS,
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


def test_working_data_rows_reads_all_rollout_sections():
    add_row = app_row("ADD00001")
    edit_row = app_row("EDIT0001")
    chips_row = app_row("CHIPS001")
    rows = [
        ["ADD"],
        SHEET_HEADERS,
        add_row,
        ["EDIT"],
        SHEET_HEADERS,
        edit_row,
        ["CHIPS"],
        SHEET_HEADERS,
        chips_row,
    ]

    found = _working_data_rows(rows)

    assert [(row_number, row[11]) for row_number, row, _ in found] == [
        (3, "ADD00001"),
        (6, "EDIT0001"),
        (9, "CHIPS001"),
    ]


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
        return FakeRequest({"values": self.api.rows.get((spreadsheet_id, sheet_name), [])})


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


class FakeStatusReader:
    def __init__(self, statuses: dict[str, SheetApplicationStatus]) -> None:
        self.statuses = statuses
        self.calls = 0

    async def read_statuses(self) -> dict[str, SheetApplicationStatus]:
        self.calls += 1
        return self.statuses


class FakeStatusReaderWithBatches(FakeStatusReader):
    def __init__(
        self,
        statuses: dict[str, SheetApplicationStatus],
        batch_statuses: dict[str, SheetBulkBatchStatus],
        editor_comments: dict[str, list[BulkEditorComment]] | None = None,
        *,
        fail_comments: bool = False,
    ) -> None:
        super().__init__(statuses)
        self.batch_statuses = batch_statuses
        self.editor_comments = editor_comments or {}
        self.fail_comments = fail_comments
        self.batch_calls = 0
        self.comment_calls = 0

    async def read_batch_statuses(self, batches):
        self.batch_calls += 1
        return self.batch_statuses

    async def read_bulk_editor_comments(self, batches):
        self.comment_calls += 1
        if self.fail_comments:
            raise RuntimeError("comments unavailable")
        return {
            batch.batch_id: self.editor_comments.get(batch.batch_id, [])
            for batch in batches
        }


class FakeBulkRowsStatusReader(FakeStatusReaderWithBatches):
    def __init__(self, bulk_statuses, batch_statuses=None) -> None:
        super().__init__({}, batch_statuses or {})
        self.bulk_statuses = bulk_statuses
        self.bulk_application_calls = []

    async def read_bulk_application_statuses(self, batches):
        self.bulk_application_calls.append([batch.batch_id for batch in batches])
        return self.bulk_statuses


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


def app_row(
    application_id: str,
    *,
    batch_id: str = "",
    status: str = ApplicationStatus.NEW.value,
    comment: str = "",
    final_answer: str = "",
    direction: str = Direction.FL.value,
) -> list[str]:
    row = [""] * 24
    row[11] = application_id
    row[12] = batch_id
    row[13] = ApplicationType.BULK.value if batch_id else ApplicationType.SINGLE.value
    row[15] = direction
    row[16] = AnswerType.ROLLOUT.value
    row[17] = "Да"
    row[9] = status
    row[10] = "редактор 1"
    row[7] = comment
    row[5] = final_answer
    return row


@pytest.mark.asyncio
async def test_status_reader_indexes_direction_spreadsheets_by_application_id():
    api = FakeSheetsApi(
        rows={
            (FL_SPREADSHEET, WEEK_SHEET): [
                SHEET_HEADERS,
                app_row(
                    "A1B2C3D4",
                    status=ApplicationStatus.ACCEPTED.value,
                    comment="Готово",
                    final_answer="Финальный текст",
                ),
            ],
            (SME_SPREADSHEET, WEEK_SHEET): [
                SHEET_HEADERS,
                app_row(
                    "B1C2D3E4",
                    status=ApplicationStatus.POSTPONED.value,
                    direction=Direction.SME.value,
                ),
            ],
        }
    )
    reader = GoogleSheetsStatusReader(
        direction_spreadsheets=direction_config(),
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    statuses = await reader.read_statuses()

    assert set(statuses) == {"A1B2C3D4", "B1C2D3E4"}
    assert statuses["A1B2C3D4"].spreadsheet_id == FL_SPREADSHEET
    assert statuses["A1B2C3D4"].sheet_id == 100
    assert statuses["A1B2C3D4"].row_number == 2
    assert statuses["A1B2C3D4"].status == ApplicationStatus.ACCEPTED.value
    assert statuses["A1B2C3D4"].editor_comment == "Готово"
    assert statuses["A1B2C3D4"].final_answer == "Финальный текст"
    assert statuses["B1C2D3E4"].spreadsheet_id == SME_SPREADSHEET


@pytest.mark.asyncio
async def test_status_reader_reads_only_tracked_application_sheets():
    api = FakeSheetsApi(
        rows={
            (FL_SPREADSHEET, WEEK_SHEET): [
                SHEET_HEADERS,
                app_row("A1B2C3D4", status=ApplicationStatus.ACCEPTED.value),
            ],
            (FL_SPREADSHEET, "02.06"): [
                SHEET_HEADERS,
                app_row("UNTRACKED1", status=ApplicationStatus.REJECTED.value),
            ],
            (SME_SPREADSHEET, WEEK_SHEET): [
                SHEET_HEADERS,
                app_row("UNTRACKED2", status=ApplicationStatus.POSTPONED.value),
            ],
        }
    )
    api.sheets_by_spreadsheet[FL_SPREADSHEET].append(
        {"properties": {"sheetId": 101, "title": "02.06"}}
    )
    reader = GoogleSheetsStatusReader(
        direction_spreadsheets=direction_config(),
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    statuses = await reader.read_statuses_for(
        [
            SubmittedApplication(
                application_id="A1B2C3D4",
                telegram_user_id=100,
                spreadsheet_id=FL_SPREADSHEET,
                sheet_id=100,
                sheet_name=WEEK_SHEET,
                last_known_status=ApplicationStatus.NEW.value,
                last_seen_row_number=2,
            )
        ]
    )

    assert set(statuses) == {"A1B2C3D4"}
    assert [call["range"] for call in api.value_get_calls] == [f"'{WEEK_SHEET}'!A1:X2"]
    assert api.metadata_get_calls == []


@pytest.mark.asyncio
async def test_status_reader_does_not_accept_mismatched_expected_row():
    api = FakeSheetsApi(
        rows={
            (FL_SPREADSHEET, f"'{WEEK_SHEET}'!A4:X5"): [
                SHEET_HEADERS,
                app_row("OTHER001", status=ApplicationStatus.ACCEPTED.value),
            ],
            (FL_SPREADSHEET, WEEK_SHEET): [
                SHEET_HEADERS,
                app_row("A1B2C3D4", status=ApplicationStatus.ACCEPTED.value),
            ],
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
            sheet_name=WEEK_SHEET,
            last_known_status=ApplicationStatus.NEW.value,
            last_seen_row_number=5,
        )
    ]

    statuses = await reader.read_statuses_for(tracked)

    assert statuses == {}
    assert [call["range"] for call in api.value_get_calls] == [f"'{WEEK_SHEET}'!A4:X5"]


@pytest.mark.asyncio
async def test_status_reader_uses_full_sheet_only_for_fallback():
    api = FakeSheetsApi(
        rows={
            (FL_SPREADSHEET, f"'{WEEK_SHEET}'!A4:X5"): [
                SHEET_HEADERS,
                app_row("OTHER001", status=ApplicationStatus.ACCEPTED.value),
            ],
            (FL_SPREADSHEET, WEEK_SHEET): [
                SHEET_HEADERS,
                app_row("A1B2C3D4", status=ApplicationStatus.ACCEPTED.value),
            ],
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
            sheet_name=WEEK_SHEET,
            last_known_status=ApplicationStatus.NEW.value,
            last_seen_row_number=5,
        )
    ]

    statuses = await reader.read_statuses_for(tracked, fallback_full_scan=True)

    assert set(statuses) == {"A1B2C3D4"}
    assert [call["range"] for call in api.value_get_calls] == [
        f"'{WEEK_SHEET}'!A4:X5",
        f"'{WEEK_SHEET}'!A:X",
    ]


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
async def test_status_reader_reads_bulk_rows_from_batch_sheet():
    api = FakeSheetsApi(
        rows={
            (FL_SPREADSHEET, "Массовый ввод"): [],
            (FL_SPREADSHEET, "'Массовый ввод'!A3:N6"): [
                CURRENT_BULK_STAGING_HEADERS,
                [
                    AnswerType.ROLLOUT.value,
                    "intent.one",
                    "Writer",
                    "Reason",
                    "Change",
                    "Source",
                    "",
                    "A1B2C3D4",
                    ApplicationStatus.FINAL_ANSWER_READY.value,
                    "редактор 1",
                    "Комментарий редактора",
                    "Ответ сценариста",
                    "Итоговый ответ",
                ]
            ],
        }
    )
    reader = GoogleSheetsStatusReader(
        direction_spreadsheets=direction_config(),
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )
    batch = BulkBatch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name="Массовый ввод",
        sheet_id=300,
        start_row=2,
        data_start_row=4,
        reserved_rows=3,
    )

    statuses = await reader.read_bulk_application_statuses([batch])

    assert set(statuses) == {"A1B2C3D4"}
    current = statuses["A1B2C3D4"]
    assert current.batch_id == "BATCH-ABC12345"
    assert current.sheet_name == "Массовый ввод"
    assert current.row_number == 4
    assert current.status == ApplicationStatus.FINAL_ANSWER_READY.value
    assert current.editor == "редактор 1"
    assert current.editor_comment == "Комментарий редактора"
    assert current.final_answer == "Итоговый ответ"
    assert current.end_column == "M"


@pytest.mark.asyncio
async def test_status_reader_reads_new_bulk_schema():
    row = [""] * 14
    row[0] = AnswerType.ROLLOUT.value
    row[1] = "ADD"
    row[7] = "Итоговый ответ"
    row[9] = "Комментарий редактора"
    row[11] = BulkApplicationStatus.NEEDS_CLARIFICATION.value
    row[12] = "редактор 2"
    row[13] = "A1B2C3D4"
    api = FakeSheetsApi(
        rows={
            (FL_SPREADSHEET, "'Массовый ввод'!A3:N4"): [
                BULK_STAGING_HEADERS,
                row,
            ],
        }
    )
    reader = GoogleSheetsStatusReader(
        direction_spreadsheets=direction_config(),
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )
    batch = BulkBatch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name="Массовый ввод",
        sheet_id=300,
        start_row=2,
        data_start_row=4,
        reserved_rows=1,
    )

    statuses = await reader.read_bulk_application_statuses([batch])

    current = statuses["A1B2C3D4"]
    assert current.status == BulkApplicationStatus.NEEDS_CLARIFICATION.value
    assert current.editor == "редактор 2"
    assert current.editor_comment == "Комментарий редактора"
    assert current.final_answer == "Итоговый ответ"
    assert current.end_column == "N"


@pytest.mark.asyncio
async def test_status_reader_supports_legacy_bulk_section():
    legacy_row = [""] * 12
    legacy_row[0] = AnswerType.ROLLOUT.value
    legacy_row[7] = "A1B2C3D4"
    legacy_row[8] = ApplicationStatus.NEEDS_CLARIFICATION.value
    legacy_row[9] = "Комментарий"
    legacy_row[11] = "Итог"
    api = FakeSheetsApi(
        rows={
            (FL_SPREADSHEET, "'Массовый ввод'!A3:N4"): [
                LEGACY_BULK_STAGING_HEADERS,
                legacy_row,
            ]
        }
    )
    reader = GoogleSheetsStatusReader(
        direction_spreadsheets=direction_config(),
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )
    batch = BulkBatch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name="Массовый ввод",
        sheet_id=300,
        start_row=2,
        data_start_row=4,
        reserved_rows=1,
    )

    statuses = await reader.read_bulk_application_statuses([batch])

    current = statuses["A1B2C3D4"]
    assert current.editor == ""
    assert current.editor_comment == "Комментарий"
    assert current.final_answer == "Итог"
    assert current.end_column == "L"


@pytest.mark.asyncio
async def test_status_reader_reads_bulk_editor_comments_from_batch_rows():
    api = FakeSheetsApi(
        rows={
            (FL_SPREADSHEET, "'РњР°СЃСЃРѕРІС‹Р№ РІРІРѕРґ'!A3:N6"): [
                CURRENT_BULK_STAGING_HEADERS,
                ["", "", "", "", "", "", "", "A1B2C3D4", "", "редактор 1", "Clarify <intent>", "", ""],
                ["", "", "", "", "", "", "", "B1C2D3E4", "", "редактор 1", "", "", ""],
                ["", "", "", "", "", "", "", "C1D2E3F4", "", "редактор 2", "Add source text", "", ""],
            ],
        }
    )
    reader = GoogleSheetsStatusReader(
        direction_spreadsheets=direction_config(),
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )
    batch = BulkBatch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name="РњР°СЃСЃРѕРІС‹Р№ РІРІРѕРґ",
        sheet_id=300,
        start_row=2,
        data_start_row=4,
        reserved_rows=3,
    )

    comments = await reader.read_bulk_editor_comments([batch])

    assert set(comments) == {"BATCH-ABC12345"}
    assert [item.application_id for item in comments["BATCH-ABC12345"]] == [
        "A1B2C3D4",
        "C1D2E3F4",
    ]
    assert [item.row_number for item in comments["BATCH-ABC12345"]] == [4, 6]
    assert comments["BATCH-ABC12345"][0].comment == "Clarify <intent>"
    assert comments["BATCH-ABC12345"][0].end_column == "M"


@pytest.mark.asyncio
async def test_status_reader_skips_bulk_rows_when_sheet_range_is_missing():
    missing_range_error = HttpError(
        FakeResponse(status=400, reason="Bad Request"),
        b"{\"error\":{\"message\":\"Unable to parse range: 'Mass input'!A3:N6\"}}",
    )
    api = FakeSheetsApi(
        rows={
            (FL_SPREADSHEET, "'Массовый ввод'!A3:N6"): missing_range_error,
        }
    )
    reader = GoogleSheetsStatusReader(
        direction_spreadsheets=direction_config(),
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )
    batch = BulkBatch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name="Массовый ввод",
        sheet_id=300,
        start_row=2,
        data_start_row=4,
        reserved_rows=3,
    )

    statuses = await reader.read_bulk_application_statuses([batch])

    assert statuses == {}


@pytest.mark.asyncio
async def test_status_reader_skips_bulk_batch_status_when_sheet_range_is_missing():
    missing_range_error = HttpError(
        FakeResponse(status=400, reason="Bad Request"),
        b"{\"error\":{\"message\":\"Unable to parse range: 'Mass input'!A2:N3\"}}",
    )
    api = FakeSheetsApi(
        rows={
            (FL_SPREADSHEET, "'Массовый ввод'!A2:N3"): missing_range_error,
        }
    )
    reader = GoogleSheetsStatusReader(
        direction_spreadsheets=direction_config(),
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )
    batch = BulkBatch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name="Массовый ввод",
        sheet_id=300,
        start_row=2,
        data_start_row=4,
        reserved_rows=3,
    )

    statuses = await reader.read_batch_statuses([batch])

    assert statuses == {}


@pytest.mark.asyncio
async def test_notification_service_updates_non_important_status_without_message(tmp_path):
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

    assert notifier.messages == []
    assert tracked is not None
    assert tracked.last_known_status == ApplicationStatus.IN_PROGRESS.value
    assert tracked.last_seen_row_number == 5


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
    assert (
        '<a href="https://docs.google.com/spreadsheets/d/fl-spreadsheet/edit#gid=100&amp;range=A7:W7">A1B2C3D4</a>: '
        f"{ApplicationStatus.NEW.value} -> {ApplicationStatus.FINAL_ANSWER_READY.value}"
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
async def test_single_comment_is_sent_only_for_needs_clarification(tmp_path):
    repository = DraftRepository(str(tmp_path / "clarification_comment.db"))
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
                    status=ApplicationStatus.NEEDS_CLARIFICATION.value,
                    editor_comment="Уточните <деталь>",
                    final_answer="",
                )
            }
        ),
        notifier=notifier,
    )

    await service.run_once()

    assert "комментарий: Уточните &lt;деталь&gt;" in notifier.messages[0]["text"]


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

    assert kind == KeyboardKind.NOTIFICATION_BULK_BACK
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].text == "Назад к заявке"
    assert keyboard.inline_keyboard[0][0].callback_data == "app:notification:new"


@pytest.mark.asyncio
async def test_notification_keyboard_prefers_bulk_over_single_draft(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_priority_keyboard.db"))
    await repository.init()
    await repository.get_or_create(100)
    await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=WEEK_SHEET,
        sheet_id=300,
        start_row=20,
        data_start_row=22,
        reserved_rows=100,
    )
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader({}),
        notifier=FakeNotifier(),
    )

    kind = await service._keyboard_kind_for_user(100)

    assert kind == KeyboardKind.NOTIFICATION_BULK_BACK


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
async def test_notification_service_does_not_notify_for_bulk_row_changes(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_notifications.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=WEEK_SHEET,
        sheet_id=300,
        start_row=20,
        data_start_row=22,
        reserved_rows=200,
    )
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=300,
        sheet_name=WEEK_SHEET,
        last_known_status=ApplicationStatus.NEW.value,
        batch_id="BATCH-ABC12345",
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
                    sheet_id=300,
                    row_number=25,
                    status=ApplicationStatus.ACCEPTED.value,
                    editor_comment="Готово",
                    final_answer="Финальный ответ",
                    batch_id="BATCH-ABC12345",
                )
            }
        ),
        notifier=notifier,
    )

    await service.run_once()

    assert notifier.messages == []
    saved = await repository.get_submitted_application("A1B2C3D4")
    assert saved is not None
    assert saved.last_known_status == ApplicationStatus.ACCEPTED.value
    assert saved.last_seen_final_answer == "Финальный ответ"


@pytest.mark.asyncio
async def test_notification_service_groups_new_bulk_clarifications_by_batch(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_clarifications.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=WEEK_SHEET,
        sheet_id=300,
        start_row=20,
        data_start_row=22,
        reserved_rows=3,
    )
    for application_id in ("A1B2C3D4", "B1C2D3E4"):
        await repository.save_submitted_application(
            application_id=application_id,
            telegram_user_id=100,
            spreadsheet_id=FL_SPREADSHEET,
            sheet_id=300,
            sheet_name=WEEK_SHEET,
            last_known_status=BulkApplicationStatus.NEW.value,
            batch_id="BATCH-ABC12345",
        )
    notifier = FakeNotifier()
    statuses = {
        "A1B2C3D4": SheetApplicationStatus(
            application_id="A1B2C3D4",
            spreadsheet_id=FL_SPREADSHEET,
            sheet_name=WEEK_SHEET,
            sheet_id=300,
            row_number=22,
            status=BulkApplicationStatus.NEEDS_CLARIFICATION.value,
            editor_comment="Уточните <интент>",
            final_answer="Не отправлять",
            batch_id="BATCH-ABC12345",
            end_column="M",
        ),
        "B1C2D3E4": SheetApplicationStatus(
            application_id="B1C2D3E4",
            spreadsheet_id=FL_SPREADSHEET,
            sheet_name=WEEK_SHEET,
            sheet_id=300,
            row_number=23,
            status=BulkApplicationStatus.NEEDS_CLARIFICATION.value,
            editor_comment="",
            final_answer="",
            batch_id="BATCH-ABC12345",
            end_column="M",
        ),
    }
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(statuses),
        notifier=notifier,
    )

    await service.run_once()
    await service.run_once()

    assert len(notifier.messages) == 1
    text = notifier.messages[0]["text"]
    assert "<b>Нужны пояснения по массовой заявке</b>" in text
    assert (
        '<a href="https://docs.google.com/spreadsheets/d/fl-spreadsheet/edit#gid=300&amp;range=A20:M20">'
        "Открыть массовую заявку BATCH-ABC12345</a>"
    ) in text
    assert "A1B2C3D4</a>: Уточните &lt;интент&gt;" in text
    assert "B1C2D3E4</a>: комментарий редактора не указан" in text
    assert "Не отправлять" not in text
    for application_id in statuses:
        saved = await repository.get_submitted_application(application_id)
        assert saved is not None
        assert saved.last_known_status == BulkApplicationStatus.NEEDS_CLARIFICATION.value


@pytest.mark.asyncio
async def test_bulk_clarification_notification_retries_after_send_failure(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_clarification_failure.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="A1B2C3D4",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=300,
        sheet_name=WEEK_SHEET,
        last_known_status=BulkApplicationStatus.NEW.value,
        batch_id="BATCH-ABC12345",
    )
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeStatusReader(
            {
                "A1B2C3D4": SheetApplicationStatus(
                    application_id="A1B2C3D4",
                    spreadsheet_id=FL_SPREADSHEET,
                    sheet_name=WEEK_SHEET,
                    sheet_id=300,
                    row_number=22,
                    status=BulkApplicationStatus.NEEDS_CLARIFICATION.value,
                    editor_comment="Нужен контекст",
                    final_answer="",
                    batch_id="BATCH-ABC12345",
                    end_column="M",
                )
            }
        ),
        notifier=FakeNotifier(fail=True),
    )

    await service.run_once()

    saved = await repository.get_submitted_application("A1B2C3D4")
    assert saved is not None
    assert saved.last_known_status == BulkApplicationStatus.NEEDS_CLARIFICATION.value
    outbox = await repository.list_notification_outbox()
    assert len(outbox) == 1
    assert outbox[0].state == "PENDING"

@pytest.mark.asyncio
async def test_notification_service_sends_bulk_batch_status_change(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_batch_status.db"))
    await repository.init()
    batch = await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=WEEK_SHEET,
        sheet_id=300,
        start_row=20,
        data_start_row=22,
        reserved_rows=200,
    )
    notifier = FakeNotifier()
    reader = FakeStatusReaderWithBatches(
        {},
        {
            batch.batch_id: SheetBulkBatchStatus(
                batch_id=batch.batch_id,
                spreadsheet_id=FL_SPREADSHEET,
                sheet_name=WEEK_SHEET,
                sheet_id=300,
                row_number=20,
                status=BulkBatchStatus.DONE.value,
            )
        },
    )
    service = StatusNotificationService(
        repository=repository,
        status_reader=reader,
        notifier=notifier,
    )

    await service.run_once()

    assert len(notifier.messages) == 1
    text = notifier.messages[0]["text"]
    assert "<b>Массовая заявка готова</b>" in text
    assert (
        '<a href="https://docs.google.com/spreadsheets/d/fl-spreadsheet/edit#gid=300&amp;range=A20:L20">Открыть массовую заявку BATCH-ABC12345</a>'
        in text
    )
    assert reader.comment_calls == 0
    saved = await repository.get_bulk_batch(batch.batch_id)
    assert saved is not None
    assert saved.last_known_batch_status == BulkBatchStatus.DONE.value


@pytest.mark.asyncio
async def test_bulk_done_notification_retries_when_telegram_send_fails(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_batch_send_failure.db"))
    await repository.init()
    batch = await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=WEEK_SHEET,
        sheet_id=300,
        start_row=20,
        data_start_row=22,
        reserved_rows=3,
    )
    reader = FakeStatusReaderWithBatches(
        {},
        {
            batch.batch_id: SheetBulkBatchStatus(
                batch_id=batch.batch_id,
                spreadsheet_id=FL_SPREADSHEET,
                sheet_name=WEEK_SHEET,
                sheet_id=300,
                row_number=20,
                status=BulkBatchStatus.DONE.value,
            )
        },
    )
    service = StatusNotificationService(
        repository=repository,
        status_reader=reader,
        notifier=FakeNotifier(fail=True),
    )

    await service.run_once()

    saved = await repository.get_bulk_batch(batch.batch_id)
    assert saved is not None
    assert saved.last_known_batch_status == BulkBatchStatus.DONE.value
    outbox = await repository.list_notification_outbox()
    assert len(outbox) == 1
    assert outbox[0].state == "PENDING"


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
async def test_notification_service_does_not_notify_for_bulk_batch_in_progress(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_batch_clarification_comments.db"))
    await repository.init()
    batch = await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=WEEK_SHEET,
        sheet_id=300,
        start_row=20,
        data_start_row=22,
        reserved_rows=3,
    )
    reader = FakeStatusReaderWithBatches(
        {},
        {
            batch.batch_id: SheetBulkBatchStatus(
                batch_id=batch.batch_id,
                spreadsheet_id=FL_SPREADSHEET,
                sheet_name=WEEK_SHEET,
                sheet_id=300,
                row_number=20,
                status=BulkBatchStatus.IN_PROGRESS.value,
            )
        },
    )
    notifier = FakeNotifier()
    service = StatusNotificationService(
        repository=repository,
        status_reader=reader,
        notifier=notifier,
    )

    await service.run_once()

    assert reader.comment_calls == 0
    assert notifier.messages == []
    saved = await repository.get_bulk_batch(batch.batch_id)
    assert saved is not None
    assert saved.last_known_batch_status == BulkBatchStatus.IN_PROGRESS.value


@pytest.mark.asyncio
async def test_bulk_dashboard_projection_aggregates_final_answers_and_editors(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_dashboard_aggregate.db"))
    await repository.init()
    batch = await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=WEEK_SHEET,
        sheet_id=300,
        start_row=20,
        data_start_row=22,
        reserved_rows=3,
    )
    statuses = {
        application_id: SheetApplicationStatus(
            application_id=application_id,
            spreadsheet_id=FL_SPREADSHEET,
            sheet_name=WEEK_SHEET,
            sheet_id=300,
            row_number=row_number,
            status=ApplicationStatus.IN_PROGRESS.value,
            editor_comment="",
            final_answer=final_answer,
            editor=editor,
            batch_id=batch.batch_id,
        )
        for application_id, row_number, editor, final_answer in (
            ("A1B2C3D4", 22, "редактор 1", ""),
            ("B1C2D3E4", 23, "редактор 2", "Готовый ответ"),
        )
    }
    dashboard_sync = FakeDashboardSync()
    service = StatusNotificationService(
        repository=repository,
        status_reader=FakeBulkRowsStatusReader(statuses),
        notifier=FakeNotifier(),
        dashboard_sync=dashboard_sync,
    )

    await service.run_once()

    assert len(dashboard_sync.upserts) == 1
    row = json.loads(dashboard_sync.upserts[0].snapshot_json)["row"]
    assert row[1] == batch.batch_id
    assert row[9] == "Несколько редакторов"
    assert row[10] == "Да"


@pytest.mark.asyncio
async def test_completed_bulk_batches_are_scanned_no_more_than_hourly(tmp_path):
    repository = DraftRepository(str(tmp_path / "completed_bulk_archive.db"))
    await repository.init()
    batch = await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=100,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=WEEK_SHEET,
        sheet_id=300,
        start_row=20,
        data_start_row=22,
        reserved_rows=3,
    )
    await repository.update_bulk_batch_status(
        batch.batch_id,
        batch_status=BulkBatchStatus.DONE.value,
        last_known_batch_status=BulkBatchStatus.DONE.value,
    )
    reader = FakeBulkRowsStatusReader({})
    now = [0.0]
    service = StatusNotificationService(
        repository=repository,
        status_reader=reader,
        notifier=FakeNotifier(),
        dashboard_sync=FakeDashboardSync(),
        completed_bulk_dashboard_scan_interval_seconds=3600,
        clock=lambda: now[0],
    )

    await service.run_once()
    now[0] = 100
    await service.run_once()
    now[0] = 3601
    await service.run_once()

    assert reader.bulk_application_calls == [
        [batch.batch_id],
        [batch.batch_id],
    ]


def _sheet_name_from_range(range_name: str) -> str:
    quoted_name = range_name.split("!", maxsplit=1)[0]
    if quoted_name.startswith("'") and quoted_name.endswith("'"):
        return quoted_name[1:-1].replace("''", "'")
    return quoted_name
