from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re

import pytest

from app.google_api import GoogleApiRetryConfig
from app.formatting import TextFormattingSpan, serialize_formatting_spans
from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
    BulkBatch,
    BulkReservationState,
    ChangeType,
    DashboardOutboxItem,
    Direction,
    Draft,
    LlmCheckStatus,
    Step,
    SubmittedApplication,
)
from app.scheduling import RolloutSchedule, rollout_sheet_name
from app.sheet_dates import (
    GOOGLE_SHEETS_DATE_TIME_PATTERN,
    GOOGLE_SHEETS_EPOCH,
    google_sheets_date_cell,
)
from app.submission import (
    CHIPS_V2_MARKER,
    CHIPS_WORKSHEET_HEADERS,
    DASHBOARD_HEADERS,
    DASHBOARD_SHEET_NAME,
    DashboardSyncService,
    CURRENT_WORKSHEET_HEADERS,
    DirectionSpreadsheetConfig,
    GoogleSheetsSubmissionService,
    LEGACY_DASHBOARD_HEADERS,
    LEGACY_WORKSHEET_HEADERS,
    PREVIOUS_WORKSHEET_HEADERS,
    SHEET_HEADERS,
    SheetConfigurationError,
    URGENT_SHEET_NAME,
    build_google_sheets_api,
    dashboard_projection,
    dashboard_tracked_row,
    chips_draft_to_sheet_row,
    _draft_to_row_data,
    _dashboard_duplicate_row_numbers,
    _repair_dashboard_headers,
    _repair_sheet_bool,
    _working_sheet_schema,
    draft_to_sheet_row,
    sheet_protection_requests,
    target_sheet_name,
    week_sheet_name,
)
from app.repository import DraftRepository


FL_SPREADSHEET = "fl-spreadsheet"
DASHBOARD_SPREADSHEET = "dashboard-spreadsheet"


def test_new_working_sheet_uses_client_case_header():
    assert "Кейс или сообщения клиента" in SHEET_HEADERS
    assert "Причина изменений" not in SHEET_HEADERS
    assert _working_sheet_schema(SHEET_HEADERS) == "new"


def test_old_working_sheet_headers_remain_supported():
    assert "Причина изменений" in LEGACY_WORKSHEET_HEADERS
    assert "Причина изменений" in CURRENT_WORKSHEET_HEADERS
    assert _working_sheet_schema(LEGACY_WORKSHEET_HEADERS) == "legacy"
    assert _working_sheet_schema(CURRENT_WORKSHEET_HEADERS) == "current"
    assert _working_sheet_schema(PREVIOUS_WORKSHEET_HEADERS) == "previous_new"


def test_build_google_sheets_api_disables_discovery_cache(monkeypatch):
    calls = {}

    class FakeCredentials:
        @staticmethod
        def from_service_account_file(path, scopes):
            calls["credentials_path"] = path
            calls["scopes"] = scopes
            return "credentials"

    def fake_build(service_name, version, **kwargs):
        calls["service_name"] = service_name
        calls["version"] = version
        calls["kwargs"] = kwargs
        return "api"

    import google.oauth2.service_account
    import googleapiclient.discovery

    monkeypatch.setattr(
        google.oauth2.service_account,
        "Credentials",
        FakeCredentials,
    )
    monkeypatch.setattr(googleapiclient.discovery, "build", fake_build)

    api = build_google_sheets_api("credentials.json")

    assert api == "api"
    assert calls["credentials_path"] == "credentials.json"
    assert calls["service_name"] == "sheets"
    assert calls["version"] == "v4"
    assert calls["kwargs"]["credentials"] == "credentials"
    assert calls["kwargs"]["cache_discovery"] is False


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if callable(self.result):
            return self.result()
        return self.result


class FakeValuesResource:
    def __init__(self, api: "FakeSheetsApi") -> None:
        self.api = api

    def get(self, **kwargs):
        self.api.value_get_calls.append(kwargs)
        spreadsheet_id = kwargs["spreadsheetId"]
        range_name = kwargs["range"]
        sheet_name = _sheet_name_from_range(range_name)
        if range_name.endswith("!A:A"):
            rows = self.api.rows.get((spreadsheet_id, sheet_name), [])
            return FakeRequest({"values": [[row[0]] for row in rows if row]})
        headers = self.api.headers.get((spreadsheet_id, sheet_name))
        if range_name.endswith("!A:X") and headers is not None:
            rows = self.api.rows.get((spreadsheet_id, sheet_name), [])
            if rows and rows[0] == headers:
                return FakeRequest({"values": rows})
            return FakeRequest({"values": [headers, *rows]})
        if range_name.endswith("1") and headers is not None:
            return FakeRequest({"values": [headers]})
        return FakeRequest({"values": self.api.rows.get((spreadsheet_id, sheet_name), [])})

    def update(self, **kwargs):
        self.api.value_updates.append(kwargs)
        spreadsheet_id = kwargs["spreadsheetId"]
        sheet_name = _sheet_name_from_range(kwargs["range"])
        values = kwargs["body"]["values"]
        if kwargs["range"].split("!", maxsplit=1)[1].startswith("A1"):
            self.api.headers[(spreadsheet_id, sheet_name)] = values[0]
            if len(values) > 1:
                self.api.rows[(spreadsheet_id, sheet_name)] = values
        else:
            self.api.updated_rows.append((spreadsheet_id, sheet_name, values[0]))
        range_part = kwargs["range"].split("!", maxsplit=1)[1]
        row_match = re.fullmatch(r"A(\d+):[A-Z]+(\d+)", range_part)
        if row_match:
            start_row = int(row_match.group(1))
            rows = self.api.rows.setdefault((spreadsheet_id, sheet_name), [])
            while len(rows) < start_row - 1 + len(values):
                rows.append([])
            for offset, row in enumerate(values):
                rows[start_row - 1 + offset] = row
        return FakeRequest({"updatedRows": len(values)})


class FakeSpreadsheetsResource:
    def __init__(self, api: "FakeSheetsApi") -> None:
        self.api = api

    def values(self):
        return FakeValuesResource(self.api)

    def get(self, **kwargs):
        spreadsheet_id = kwargs["spreadsheetId"]
        self.api.metadata_get_calls.append(kwargs)
        return FakeRequest({"sheets": self.api.sheets_by_spreadsheet.get(spreadsheet_id, [])})

    def batchUpdate(self, **kwargs):
        self.api.batch_updates.append(kwargs)

        def result():
            spreadsheet_id = kwargs["spreadsheetId"]
            replies = []
            self.api.sheets_by_spreadsheet.setdefault(spreadsheet_id, [])
            for request in kwargs["body"]["requests"]:
                if "addSheet" in request:
                    properties = {
                        "sheetId": self.api.next_sheet_id,
                        "title": request["addSheet"]["properties"]["title"],
                    }
                    self.api.sheets_by_spreadsheet[spreadsheet_id].append(
                        {"properties": properties}
                    )
                    replies.append({"addSheet": {"properties": properties}})
                    self.api.next_sheet_id += 1
                elif "appendCells" in request:
                    append_cells = request["appendCells"]
                    self.api.append_cells.append((spreadsheet_id, append_cells))
                    replies.append({"appendCells": {}})
                elif "addDimensionGroup" in request:
                    if self.api.fail_dimension_group:
                        raise RuntimeError("dimension group failed")
                    replies.append({})
                elif "updateCells" in request:
                    update_cells = request["updateCells"]
                    rows = update_cells.get("rows", [])
                    if rows and len(rows[0].get("values", [])) == len(SHEET_HEADERS):
                        self.api.append_cells.append(
                            (
                                spreadsheet_id,
                                {
                                    "sheetId": update_cells["range"]["sheetId"],
                                    "rows": rows,
                                },
                            )
                        )
                    replies.append({})
                else:
                    replies.append({})
            return {"replies": replies}

        return FakeRequest(result)


class FakeSheetsApi:
    def __init__(
        self,
        *,
        sheets: dict[str, dict[str, int]] | None = None,
        headers: dict[tuple[str, str], list[str] | None] | None = None,
        rows: dict[tuple[str, str], list[list[str]]] | None = None,
    ) -> None:
        self.next_sheet_id = 100
        self.sheets_by_spreadsheet = {
            spreadsheet_id: [
                {"properties": {"sheetId": sheet_id, "title": title}}
                for title, sheet_id in sheet_map.items()
            ]
            for spreadsheet_id, sheet_map in (sheets or {}).items()
        }
        self.headers = headers or {}
        self.rows = rows or {}
        self.metadata_get_calls = []
        self.value_get_calls = []
        self.value_updates = []
        self.updated_rows = []
        self.batch_updates = []
        self.append_cells = []
        self.fail_dimension_group = False

    def spreadsheets(self):
        return FakeSpreadsheetsResource(self)


class FakeGoogleHttpError(Exception):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message)
        self.resp = type("Response", (), {"status": status})()


class FailingMetadataSheetsApi(FakeSheetsApi):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def spreadsheets(self):
        api = self

        class Resource(FakeSpreadsheetsResource):
            def get(self, **kwargs):
                api.metadata_get_calls.append(kwargs)
                return FakeRequest(lambda: (_ for _ in ()).throw(api.error))

        return Resource(api)


def make_draft(**overrides) -> Draft:
    values = {
        "telegram_user_id": 123,
        "current_step": Step.REVIEW,
        "application_id": "A1B2C3D4",
        "direction": Direction.FL.value,
        "answer_type": AnswerType.ROLLOUT.value,
        "is_urgent": False,
        "application_type": ApplicationType.SINGLE.value,
        "change_type": ChangeType.ADD.value,
        "intent": "intent.change_limit",
        "scriptwriter": "Иван Иванов",
        "reason": "Изменились условия продукта",
        "raw_change_description": "Сырой текст изменений",
        "formatted_change_description": "Лаконичный текст изменений",
        "source_text": "Исходный ответ",
        "source_text_formatting_json": None,
        "llm_check_status": LlmCheckStatus.STUB_COMPLETE.value,
        "llm_score": 1.0,
        "created_at": "2026-05-18T13:00:00+00:00",
        "updated_at": "2026-05-18T13:05:00+00:00",
    }
    values.update(overrides)
    return Draft(**values)


def make_service(
    api: FakeSheetsApi,
    *,
    dashboard: bool = False,
    clock=None,
    rollout_schedule: RolloutSchedule | None = None,
    repository: DraftRepository | None = None,
) -> GoogleSheetsSubmissionService:
    return GoogleSheetsSubmissionService(
        direction_spreadsheets=DirectionSpreadsheetConfig(
            fl_spreadsheet_id=FL_SPREADSHEET,
            sme_spreadsheet_id="sme-spreadsheet",
            ai_spreadsheet_id="ai-spreadsheet",
            voice_collection_spreadsheet_id="voice-collection-spreadsheet",
        ),
        dashboard_spreadsheet_id=DASHBOARD_SPREADSHEET if dashboard else "",
        credentials_path="missing-for-test.json",
        sheets_api=api,
        clock=clock or (lambda: datetime(2026, 6, 3, 10, 59, 59, tzinfo=timezone.utc)),
        rollout_schedule=rollout_schedule or RolloutSchedule.from_strings(),
        repository=repository,
    )


def make_service_with_retry(
    api: FakeSheetsApi,
    retry: GoogleApiRetryConfig,
) -> GoogleSheetsSubmissionService:
    service = make_service(api)
    service.google_api_retry = retry
    return service


def test_draft_to_sheet_row_uses_new_direction_schema():
    row = draft_to_sheet_row(make_draft())

    assert row == [
        "Иван Иванов",
        ApplicationStatus.NEW.value,
        "Изменились условия продукта",
        "Лаконичный текст изменений",
        "Исходный ответ",
        "",
        "",
        "",
        "",
        "Редактор не выбран",
        "intent.change_limit",
        "A1B2C3D4",
        "",
        ApplicationType.SINGLE.value,
        "2026-05-18T13:00:00+00:00",
        Direction.FL.value,
        AnswerType.ROLLOUT.value,
        "Нет",
        "Иван Иванов",
        123,
        "Сырой текст изменений",
        "stub_complete",
        1.0,
        ChangeType.ADD.value,
    ]


def test_chips_draft_to_sheet_row_uses_dedicated_schema():
    draft = make_draft(
        change_type=ChangeType.CHIPS.value,
        chip_text_before="before",
        chip_text="chip",
        chip_text_after="after",
    )

    row = chips_draft_to_sheet_row(draft)

    assert row[:6] == [
        draft.scriptwriter,
        ApplicationStatus.NEW.value,
        draft.reason,
        "before",
        "chip",
        "after",
    ]
    assert row[9] == "Редактор не выбран"
    assert row[10] == draft.intent
    assert row[11] == draft.application_id
    assert row[20] == ChangeType.CHIPS.value


def test_google_sheets_date_cell_converts_utc_to_bot_timezone():
    cell = google_sheets_date_cell(
        datetime(2026, 6, 15, 10, 30, 45, tzinfo=timezone.utc),
        timezone_name="Europe/Moscow",
    )
    expected_serial = (
        datetime(2026, 6, 15, 13, 30, 45) - GOOGLE_SHEETS_EPOCH
    ).total_seconds() / 86400

    assert cell["userEnteredValue"]["numberValue"] == pytest.approx(expected_serial)
    assert cell["userEnteredFormat"]["numberFormat"] == {
        "type": "DATE_TIME",
        "pattern": GOOGLE_SHEETS_DATE_TIME_PATTERN,
    }


@pytest.mark.parametrize(
    ("timestamp", "expected_hour"),
    [
        ("2026-03-08T06:30:00+00:00", 1),
        ("2026-03-08T07:30:00+00:00", 3),
    ],
)
def test_google_sheets_date_cell_respects_dst(timestamp, expected_hour):
    cell = google_sheets_date_cell(
        timestamp,
        timezone_name="America/New_York",
    )
    serial = cell["userEnteredValue"]["numberValue"]
    local_value = GOOGLE_SHEETS_EPOCH + timedelta(days=serial)

    assert local_value.hour == expected_hour


def test_google_sheets_date_cell_treats_naive_datetime_as_utc():
    aware = google_sheets_date_cell(
        datetime(2026, 6, 15, 10, 30, tzinfo=timezone.utc),
        timezone_name="Europe/Moscow",
    )
    naive = google_sheets_date_cell(
        datetime(2026, 6, 15, 10, 30),
        timezone_name="Europe/Moscow",
    )

    assert naive == aware


@pytest.mark.parametrize(
    ("schema", "date_index"),
    [("new", 14), ("chips", 14), ("current", 3), ("legacy", 3)],
)
def test_all_working_sheet_schemas_use_typed_submission_date(schema, date_index):
    row_data = _draft_to_row_data(
        make_draft(created_at="2020-01-01T00:00:00+00:00"),
        schema=schema,
        submitted_at=datetime(2026, 6, 15, 10, 30, tzinfo=timezone.utc),
        timezone_name="Europe/Moscow",
    )

    date_cell = row_data["values"][date_index]
    assert "stringValue" not in date_cell["userEnteredValue"]
    assert date_cell["userEnteredFormat"]["numberFormat"]["type"] == "DATE_TIME"


def test_target_sheet_name_uses_week_or_integration():
    submitted_at = datetime(2026, 6, 3, 10, 59, 59, tzinfo=timezone.utc)
    assert target_sheet_name(make_draft(), submitted_at=submitted_at) == "08.06 (1)"
    assert (
        target_sheet_name(
            make_draft(
                answer_type=AnswerType.URGENT.value,
                is_urgent=True,
                created_at="2026-06-05T10:00:00+00:00",
            )
        )
        == "Срочные"
    )
    assert (
        target_sheet_name(make_draft(answer_type=AnswerType.INTEGRATION.value))
        == "Интеграции"
    )
    assert (
        target_sheet_name(
            make_draft(
                direction=Direction.VOICEBOT.value,
                answer_type=None,
                created_at="2026-06-05T10:00:00+00:00",
            )
        )
        == "VoiceBot 01.06"
    )


@pytest.mark.parametrize(
    ("submitted_at", "expected"),
    [
        (datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc), "08.06 (1)"),
        (datetime(2026, 6, 3, 10, 59, 59, tzinfo=timezone.utc), "08.06 (1)"),
        (datetime(2026, 6, 3, 11, 0, tzinfo=timezone.utc), "08.06 (2)"),
        (datetime(2026, 6, 4, 10, 59, 59, tzinfo=timezone.utc), "08.06 (2)"),
        (datetime(2026, 6, 4, 11, 0, tzinfo=timezone.utc), "15.06 (1)"),
        (datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc), "15.06 (1)"),
        (datetime(2026, 6, 7, 9, 0, tzinfo=timezone.utc), "15.06 (1)"),
        (datetime(2026, 12, 31, 11, 0, tzinfo=timezone.utc), "11.01 (1)"),
    ],
)
def test_rollout_sheet_name_uses_moscow_cutoffs(submitted_at, expected):
    assert rollout_sheet_name(submitted_at, RolloutSchedule.from_strings()) == expected


def test_rollout_sheet_name_supports_custom_cutoffs():
    schedule = RolloutSchedule.from_strings(
        timezone_name="Europe/Moscow",
        wednesday_cutoff="13:30",
        thursday_cutoff="15:00",
    )

    assert (
        rollout_sheet_name(
            datetime(2026, 6, 3, 10, 29, 59, tzinfo=timezone.utc),
            schedule,
        )
        == "08.06 (1)"
    )
    assert (
        rollout_sheet_name(
            datetime(2026, 6, 3, 10, 30, tzinfo=timezone.utc),
            schedule,
        )
        == "08.06 (2)"
    )
    assert (
        rollout_sheet_name(
            datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc),
            schedule,
        )
        == "15.06 (1)"
    )


def test_rollout_sheet_name_applies_cutoffs_in_configured_timezone():
    schedule = RolloutSchedule.from_strings(timezone_name="America/New_York")

    assert (
        rollout_sheet_name(
            datetime(2026, 6, 3, 17, 59, 59, tzinfo=timezone.utc),
            schedule,
        )
        == "08.06 (1)"
    )
    assert (
        rollout_sheet_name(
            datetime(2026, 6, 3, 18, 0, tzinfo=timezone.utc),
            schedule,
        )
        == "08.06 (2)"
    )


def test_target_sheet_name_prefixes_voicebot_rollout():
    assert (
        target_sheet_name(
            make_draft(direction=Direction.VOICEBOT.value),
            submitted_at=datetime(2026, 6, 3, 11, 0, tzinfo=timezone.utc),
        )
        == "VoiceBot 08.06 (2)"
    )


def urgent_draft(change_type: ChangeType, **overrides) -> Draft:
    values = {
        "answer_type": AnswerType.URGENT.value,
        "is_urgent": True,
        "change_type": change_type.value,
    }
    if change_type == ChangeType.CHIPS:
        values.update(
            chip_text_before="before",
            chip_text="chip",
            chip_text_after="after",
            llm_check_status=LlmCheckStatus.SKIPPED.value,
        )
    values.update(overrides)
    return make_draft(**values)


@pytest.mark.asyncio
async def test_new_urgent_sheet_creates_chips_section_and_inserts_add_above_it():
    api = FakeSheetsApi()
    service = make_service(api)

    result = await service.submit(urgent_draft(ChangeType.ADD))

    assert result.success is True
    assert result.sheet_name == "Срочные"
    assert api.rows[(FL_SPREADSHEET, "Срочные")] == [SHEET_HEADERS]
    requests = api.batch_updates[-1]["body"]["requests"]
    assert requests[0]["insertDimension"]["range"]["startIndex"] == 1
    assert requests[0]["insertDimension"]["range"]["endIndex"] == 3
    assert requests[1]["updateCells"]["range"]["startRowIndex"] == 1
    rows = requests[1]["updateCells"]["rows"]
    assert rows[0]["values"][0]["userEnteredValue"] == {"stringValue": "03.06.26"}
    assert rows[1]["values"][11]["userEnteredValue"] == {"stringValue": "A1B2C3D4"}
    assert result.row_number == 3


@pytest.mark.asyncio
async def test_single_submission_returns_retry_message_when_section_lock_is_busy(tmp_path):
    repository = DraftRepository(str(tmp_path / "single_section_lock_busy.db"))
    await repository.init()
    api = FakeSheetsApi()
    service = make_service(api, repository=repository)
    acquired = await repository.acquire_bulk_section_lock(
        lock_key=f"{FL_SPREADSHEET}:Срочные:urgent:daily",
        owner="bulk:RES-BUSY",
        ttl_seconds=600,
    )

    result = await service.submit(urgent_draft(ChangeType.ADD))

    assert acquired is True
    assert result.success is False
    assert "другой пользователь вносит строки" in result.message
    assert api.batch_updates == []


@pytest.mark.asyncio
async def test_urgent_chips_uses_same_daily_section_lock(tmp_path):
    repository = DraftRepository(str(tmp_path / "urgent_chips_daily_lock_busy.db"))
    await repository.init()
    api = FakeSheetsApi()
    service = make_service(api, repository=repository)
    acquired = await repository.acquire_bulk_section_lock(
        lock_key=f"{FL_SPREADSHEET}:Срочные:urgent:daily",
        owner="single:ADD-BUSY",
        ttl_seconds=600,
    )

    result = await service.submit(urgent_draft(ChangeType.CHIPS))

    assert acquired is True
    assert result.success is False
    assert "другой пользователь вносит строки" in result.message
    assert api.batch_updates == []


@pytest.mark.asyncio
async def test_non_urgent_section_is_not_blocked_by_urgent_daily_lock(tmp_path):
    repository = DraftRepository(str(tmp_path / "single_section_lock_different.db"))
    await repository.init()
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"Срочные": 42}},
        headers={(FL_SPREADSHEET, "Срочные"): SHEET_HEADERS},
        rows={
            (FL_SPREADSHEET, "Срочные"): [
                SHEET_HEADERS,
            ]
        },
    )
    service = make_service(api, repository=repository)
    acquired = await repository.acquire_bulk_section_lock(
        lock_key=f"{FL_SPREADSHEET}:Срочные:urgent:daily",
        owner="bulk:RES-BUSY",
        ttl_seconds=600,
    )

    result = await service.submit(make_draft(change_type=ChangeType.ADD.value))

    assert acquired is True
    assert result.success is True
    assert result.sheet_name == "08.06 (1)"


@pytest.mark.asyncio
async def test_single_submission_shift_rows_after_insert_dimension(tmp_path):
    repository = DraftRepository(str(tmp_path / "single_shift_rows.db"))
    await repository.init()
    await repository.save_submitted_application(
        application_id="EXISTING1",
        telegram_user_id=200,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=42,
        sheet_name="Срочные",
        last_known_status=ApplicationStatus.NEW.value,
        last_seen_row_number=5,
    )
    await repository.create_bulk_reservation(
        reservation_id="RES-BELOW",
        idempotency_key="RES-BELOW-key",
        telegram_user_id=300,
    )
    await repository.update_bulk_reservation_step(
        "RES-BELOW",
        state=BulkReservationState.CREATED,
        direction=Direction.FL.value,
        target_kind="urgent",
        change_type=ChangeType.CHIPS.value,
        requested_count=3,
    )
    await repository.complete_bulk_reservation_creation(
        "RES-BELOW",
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=42,
        sheet_name="Срочные",
        start_row=7,
        end_row=9,
        insert_url="https://example.test",
    )
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"Срочные": 42}},
        headers={(FL_SPREADSHEET, "Срочные"): SHEET_HEADERS},
        rows={
            (FL_SPREADSHEET, "Срочные"): [
                SHEET_HEADERS,
                draft_to_sheet_row(urgent_draft(ChangeType.ADD, application_id="OLDROW001")),
            ]
        },
    )
    service = make_service(api, repository=repository)

    result = await service.submit(urgent_draft(ChangeType.EDIT, application_id="NEWEDIT1"))

    assert result.success is True
    assert result.row_number == 4
    existing = await repository.get_submitted_application("EXISTING1")
    reservation = await repository.get_bulk_reservation("RES-BELOW")
    assert existing is not None
    assert existing.last_seen_row_number == 5
    assert reservation is not None
    assert reservation.start_row == 7
    assert reservation.end_row == 9


@pytest.mark.asyncio
async def test_new_urgent_chips_appends_to_dedicated_section():
    api = FakeSheetsApi()
    service = make_service(api)

    result = await service.submit(urgent_draft(ChangeType.CHIPS))

    assert result.success is True
    assert result.sheet_name == "Срочные"
    requests = _last_requests_with(api, "insertDimension", "updateCells")
    update_request = requests[1]["updateCells"]
    assert update_request["rows"][0]["values"][0]["userEnteredValue"] == {
        "stringValue": "03.06.26"
    }
    assert update_request["rows"][1]["values"][0]["userEnteredValue"] == {
        "stringValue": ChangeType.CHIPS.value
    }
    assert [
        cell["userEnteredValue"]["stringValue"]
        for cell in update_request["rows"][2]["values"]
    ] == CHIPS_WORKSHEET_HEADERS
    header_cell_format = update_request["rows"][2]["values"][0]["userEnteredFormat"]
    assert header_cell_format["backgroundColor"] == {
        "red": 0.94,
        "green": 0.94,
        "blue": 0.94,
    }
    assert header_cell_format["horizontalAlignment"] == "CENTER"
    assert header_cell_format["textFormat"] == {"bold": True}
    assert header_cell_format["wrapStrategy"] == "WRAP"
    cells = update_request["rows"][3]["values"]
    assert len(cells) == len(CHIPS_WORKSHEET_HEADERS)
    assert cells[3]["userEnteredValue"] == {"stringValue": "before"}
    assert cells[4]["userEnteredValue"] == {"stringValue": "chip"}
    assert cells[5]["userEnteredValue"] == {"stringValue": "after"}
    initialization_requests = _last_requests_with(api, "setBasicFilter")
    urgent_filter = next(
        request["setBasicFilter"]
        for request in initialization_requests
        if "setBasicFilter" in request
    )
    assert urgent_filter["filter"]["range"]["endRowIndex"] == 1
    urgent_formats = [
        request["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0][
            "userEnteredValue"
        ]
        for request in initialization_requests
        if "addConditionalFormatRule" in request
        and "$R2" in request["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"][
            "values"
        ][0]["userEnteredValue"]
    ]
    assert urgent_formats == []
    assert result.row_number == 5
    structure_reads = [
        call
        for call in api.value_get_calls
        if call["range"] == "'Срочные'!A:X"
    ]
    assert structure_reads[-1]["valueRenderOption"] == "FORMATTED_VALUE"


@pytest.mark.asyncio
async def test_urgent_add_inserts_before_same_day_chips_section():
    chips_row = chips_draft_to_sheet_row(
        urgent_draft(ChangeType.CHIPS, application_id="CHIP0001")
    )
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"Срочные": 42}},
        headers={(FL_SPREADSHEET, "Срочные"): SHEET_HEADERS},
        rows={
            (FL_SPREADSHEET, "Срочные"): [
                SHEET_HEADERS,
                ["03.06.26"],
                [ChangeType.CHIPS.value],
                CHIPS_WORKSHEET_HEADERS,
                chips_row,
            ]
        },
    )
    service = make_service(api)

    result = await service.submit(urgent_draft(ChangeType.ADD, application_id="ADDNEW01"))

    assert result.success is True
    assert result.row_number == 3
    request = _last_requests_with(api, "insertDimension", "updateCells")[1]["updateCells"]
    assert request["range"]["startRowIndex"] == 2
    assert request["rows"][0]["values"][11]["userEnteredValue"] == {
        "stringValue": "ADDNEW01"
    }


@pytest.mark.asyncio
async def test_urgent_chips_formats_header_when_added_to_existing_day():
    existing_row = draft_to_sheet_row(
        urgent_draft(ChangeType.ADD, application_id="ADD00001")
    )
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"Срочные": 42}},
        headers={(FL_SPREADSHEET, "Срочные"): SHEET_HEADERS},
        rows={
            (FL_SPREADSHEET, "Срочные"): [
                SHEET_HEADERS,
                ["03.06.26"],
                existing_row,
            ]
        },
    )
    service = make_service(api)

    result = await service.submit(urgent_draft(ChangeType.CHIPS, application_id="CHIPNEW1"))

    assert result.success is True
    assert result.row_number == 6
    update_request = _last_requests_with(api, "insertDimension", "updateCells")[1]["updateCells"]
    assert update_request["rows"][0]["values"][0]["userEnteredValue"] == {
        "stringValue": ChangeType.CHIPS.value
    }
    header_cell_format = update_request["rows"][1]["values"][0]["userEnteredFormat"]
    assert header_cell_format["backgroundColor"] == {
        "red": 0.94,
        "green": 0.94,
        "blue": 0.94,
    }
    assert header_cell_format["horizontalAlignment"] == "CENTER"
    assert header_cell_format["textFormat"] == {"bold": True}
    assert header_cell_format["wrapStrategy"] == "WRAP"


@pytest.mark.asyncio
async def test_existing_urgent_sheet_gets_chips_section_without_changing_rows():
    existing_row = draft_to_sheet_row(urgent_draft(ChangeType.ADD))
    rows = [SHEET_HEADERS, existing_row]
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"Срочные": 42}},
        headers={(FL_SPREADSHEET, "Срочные"): SHEET_HEADERS},
        rows={(FL_SPREADSHEET, "Срочные"): rows},
    )
    service = make_service(api)

    result = await service.submit(
        urgent_draft(ChangeType.EDIT, application_id="B1C2D3E4")
    )

    assert result.success is True
    assert api.rows[(FL_SPREADSHEET, "Срочные")][:2] == [
        SHEET_HEADERS,
        existing_row,
    ]
    assert len(api.rows[(FL_SPREADSHEET, "Срочные")]) == 2
    requests = _last_requests_with(api, "insertDimension", "updateCells")
    assert requests[0]["insertDimension"]["range"]["startIndex"] == 2
    assert requests[0]["insertDimension"]["range"]["endIndex"] == 4
    assert requests[1]["updateCells"]["rows"][0]["values"][0]["userEnteredValue"] == {
        "stringValue": "03.06.26"
    }
    assert result.row_number == 4


@pytest.mark.asyncio
async def test_existing_urgent_sheet_replaces_urgent_conditional_formatting():
    rows = [
        SHEET_HEADERS,
        draft_to_sheet_row(urgent_draft(ChangeType.ADD)),
        [ChangeType.CHIPS.value],
        CHIPS_WORKSHEET_HEADERS,
    ]
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {URGENT_SHEET_NAME: 42}},
        headers={(FL_SPREADSHEET, URGENT_SHEET_NAME): SHEET_HEADERS},
        rows={(FL_SPREADSHEET, URGENT_SHEET_NAME): rows},
    )
    api.sheets_by_spreadsheet[FL_SPREADSHEET][0]["conditionalFormats"] = [
        {"old": 1},
        {"old": 2},
    ]
    service = make_service(api)

    result = await service.submit(urgent_draft(ChangeType.ADD, application_id="COND0001"))

    assert result.success is True
    requests = _last_requests_with(api, "deleteConditionalFormatRule")
    deleted_indices = [
        request["deleteConditionalFormatRule"]["index"]
        for request in requests
        if "deleteConditionalFormatRule" in request
    ]
    assert deleted_indices == [1, 0]
    urgent_rules = [
        request["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0][
            "userEnteredValue"
        ]
        for request in requests
        if "addConditionalFormatRule" in request
        and "$R2" in request["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"][
            "values"
        ][0]["userEnteredValue"]
    ]
    assert urgent_rules == []


@pytest.mark.asyncio
async def test_integration_submission_creates_daily_separator():
    api = FakeSheetsApi()
    service = make_service(api)

    result = await service.submit(
        make_draft(
            answer_type=AnswerType.INTEGRATION.value,
            change_type=ChangeType.ADD.value,
            is_urgent=False,
        )
    )

    assert result.success is True
    assert result.sheet_name == "Интеграции"
    requests = _last_requests_with(api, "insertDimension", "updateCells")
    assert requests[0]["insertDimension"]["range"]["startIndex"] == 1
    assert requests[0]["insertDimension"]["range"]["endIndex"] == 3
    update_rows = requests[1]["updateCells"]["rows"]
    assert update_rows[0]["values"][0]["userEnteredValue"] == {
        "stringValue": "03.06.26"
    }
    assert update_rows[1]["values"][11]["userEnteredValue"] == {
        "stringValue": "A1B2C3D4"
    }
    assert result.row_number == 3


@pytest.mark.asyncio
async def test_daily_grouping_failure_does_not_block_submission():
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"Интеграции": 42}},
        headers={(FL_SPREADSHEET, "Интеграции"): SHEET_HEADERS},
        rows={
            (FL_SPREADSHEET, "Интеграции"): [
                SHEET_HEADERS,
                ["02.06.26"],
                ["Иван Иванов", ApplicationStatus.NEW.value],
            ]
        },
    )
    api.fail_dimension_group = True
    service = make_service(api)

    result = await service.submit(
        make_draft(
            answer_type=AnswerType.INTEGRATION.value,
            change_type=ChangeType.ADD.value,
            is_urgent=False,
        )
    )

    assert result.success is True
    assert result.row_number == 5
    critical_requests = _last_requests_with(api, "insertDimension", "updateCells")
    assert all("addDimensionGroup" not in request for request in critical_requests)
    assert "insertDimension" in critical_requests[0]
    assert "updateCells" in critical_requests[1]
    best_effort_requests = _last_requests_with(api, "addDimensionGroup")
    assert best_effort_requests == [
        {
            "addDimensionGroup": {
                "range": {
                    "sheetId": 42,
                        "dimension": "ROWS",
                        "startIndex": 2,
                        "endIndex": 3,
                    }
                }
        }
    ]


def test_previous_daily_group_excludes_date_separator_row():
    from app.submission import _previous_daily_group_request

    request = _previous_daily_group_request(
        [2, 8],
        new_separator_row=12,
        sheet_id=42,
    )

    assert request == {
        "addDimensionGroup": {
            "range": {
                "sheetId": 42,
                "dimension": "ROWS",
                "startIndex": 8,
                "endIndex": 11,
            }
        }
    }


def test_new_daily_separator_insert_does_not_inherit_previous_row_group():
    from app.submission import _daily_insert_plan

    plan = _daily_insert_plan(
        [
            SHEET_HEADERS,
            ["02.07.26"],
            ["old application"],
        ],
        label="07.07.26",
        sheet_id=42,
        section_start_row=2,
        section_end_row=4,
        column_count=len(SHEET_HEADERS),
        row_data={"values": [{"userEnteredValue": {"stringValue": "new application"}}]},
    )

    insert = plan["requests"][0]["insertDimension"]
    assert insert["inheritFromBefore"] is False
    assert plan["group_request"] == {
        "addDimensionGroup": {
            "range": {
                "sheetId": 42,
                "dimension": "ROWS",
                "startIndex": 2,
                "endIndex": 3,
            }
        }
    }


def test_existing_daily_separator_insert_keeps_inheriting_same_day_formatting():
    from app.submission import _daily_insert_plan

    plan = _daily_insert_plan(
        [
            SHEET_HEADERS,
            ["07.07.26"],
            ["same day application"],
        ],
        label="07.07.26",
        sheet_id=42,
        section_start_row=2,
        section_end_row=4,
        column_count=len(SHEET_HEADERS),
        row_data={"values": [{"userEnteredValue": {"stringValue": "new application"}}]},
    )

    insert = plan["requests"][0]["insertDimension"]
    assert insert["inheritFromBefore"] is True
    assert plan["group_request"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        LEGACY_WORKSHEET_HEADERS,
        CURRENT_WORKSHEET_HEADERS,
        PREVIOUS_WORKSHEET_HEADERS,
        SHEET_HEADERS,
    ],
)
async def test_urgent_add_supports_all_existing_base_headers(headers):
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"Срочные": 42}},
        headers={(FL_SPREADSHEET, "Срочные"): headers},
        rows={(FL_SPREADSHEET, "Срочные"): [headers]},
    )
    service = make_service(api)

    result = await service.submit(urgent_draft(ChangeType.ADD))

    assert result.success is True
    update = api.batch_updates[-1]["body"]["requests"][1]["updateCells"]
    assert update["range"]["endColumnIndex"] == len(headers)


@pytest.mark.asyncio
async def test_urgent_sheet_rejects_damaged_chips_header():
    rows = [SHEET_HEADERS, ["03.06.26"], [ChangeType.CHIPS.value], ["wrong"]]
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"Срочные": 42}},
        headers={(FL_SPREADSHEET, "Срочные"): SHEET_HEADERS},
        rows={(FL_SPREADSHEET, "Срочные"): rows},
    )
    service = make_service(api)

    result = await service.submit(urgent_draft(ChangeType.CHIPS))

    assert result.success is False
    assert "Повреждена CHIPS-шапка внутри дневного блока" in result.message


@pytest.mark.asyncio
async def test_submit_creates_week_sheet_and_appends_row():
    api = FakeSheetsApi()
    service = make_service(api)

    result = await service.submit(make_draft(created_at="2026-06-05T10:00:00+00:00"))

    assert result.success is True
    assert result.spreadsheet_id == FL_SPREADSHEET
    assert result.sheet_name == "08.06 (1)"
    assert api.headers[(FL_SPREADSHEET, "08.06 (1)")] == ["ADD"]
    assert len(api.append_cells) == 1
    spreadsheet_id, append_cells = api.append_cells[0]
    assert spreadsheet_id == FL_SPREADSHEET
    assert append_cells["sheetId"] == 100
    values = _append_cell_values(append_cells)
    assert values[0] == "Иван Иванов"
    assert values[1] == ApplicationStatus.NEW.value
    assert values[9] == "Редактор не выбран"
    assert values[10] == "intent.change_limit"
    assert values[11] == "A1B2C3D4"
    assert values[15] == Direction.FL.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change_type", "expected_insert_index", "uses_append"),
    [
        (ChangeType.ADD.value, 2, False),
        (ChangeType.EDIT.value, 4, False),
        (ChangeType.CHIPS.value, None, True),
    ],
)
async def test_sectioned_week_sheet_writes_to_selected_section(
    change_type,
    expected_insert_index,
    uses_append,
):
    rows = [
        ["ADD"],
        SHEET_HEADERS,
        ["EDIT"],
        SHEET_HEADERS,
        ["CHIPS"],
        SHEET_HEADERS,
    ]
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"08.06 (1)": 42}},
        headers={(FL_SPREADSHEET, "08.06 (1)"): ["ADD"]},
        rows={(FL_SPREADSHEET, "08.06 (1)"): rows},
    )
    service = make_service(api)

    result = await service.submit(make_draft(change_type=change_type))

    assert result.success is True
    write_requests = api.batch_updates[-1]["body"]["requests"]
    if uses_append:
        assert "appendCells" in write_requests[0]
        assert result.row_number == 7
    else:
        assert write_requests[0]["insertDimension"]["range"]["startIndex"] == expected_insert_index
        assert "updateCells" in write_requests[1]
        assert result.row_number == expected_insert_index + 1


@pytest.mark.asyncio
async def test_empty_legacy_chips_section_is_upgraded_in_place():
    rows = [
        ["ADD"],
        SHEET_HEADERS,
        ["EDIT"],
        SHEET_HEADERS,
        ["CHIPS"],
        SHEET_HEADERS,
    ]
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"08.06 (1)": 42}},
        headers={(FL_SPREADSHEET, "08.06 (1)"): ["ADD"]},
        rows={(FL_SPREADSHEET, "08.06 (1)"): rows},
    )
    service = make_service(api)

    result = await service.submit(
        make_draft(
            change_type=ChangeType.CHIPS.value,
            chip_text_before="before",
            chip_text="chip",
            chip_text_after="after",
        )
    )

    assert result.success is True
    header_updates = [
        call for call in api.value_updates if call["range"].endswith("!A6:U6")
    ]
    assert header_updates[0]["body"]["values"] == [CHIPS_WORKSHEET_HEADERS]
    assert not any(
        call["body"]["values"][0] == [CHIPS_V2_MARKER]
        for call in api.value_updates
    )


@pytest.mark.asyncio
async def test_populated_legacy_chips_section_creates_and_uses_v2():
    legacy_chips_row = ["legacy"] * len(SHEET_HEADERS)
    rows = [
        ["ADD"],
        SHEET_HEADERS,
        ["EDIT"],
        SHEET_HEADERS,
        ["CHIPS"],
        SHEET_HEADERS,
        legacy_chips_row,
    ]
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"08.06 (1)": 42}},
        headers={(FL_SPREADSHEET, "08.06 (1)"): ["ADD"]},
        rows={(FL_SPREADSHEET, "08.06 (1)"): rows},
    )
    service = make_service(api)

    result = await service.submit(
        make_draft(
            change_type=ChangeType.CHIPS.value,
            chip_text_before="before",
            chip_text="chip",
            chip_text_after="after",
        )
    )

    assert result.success is True
    assert api.rows[(FL_SPREADSHEET, "08.06 (1)")][6] == legacy_chips_row
    assert api.rows[(FL_SPREADSHEET, "08.06 (1)")][7:9] == [
        [CHIPS_V2_MARKER],
        CHIPS_WORKSHEET_HEADERS,
    ]
    assert result.row_number == 10


@pytest.mark.asyncio
async def test_submit_routes_by_submission_time_not_draft_creation_time():
    api = FakeSheetsApi()
    service = make_service(
        api,
        clock=lambda: datetime(2026, 6, 4, 11, 0, tzinfo=timezone.utc),
    )

    result = await service.submit(
        make_draft(created_at="2026-05-18T13:00:00+00:00")
    )

    assert result.success is True
    assert result.sheet_name == "15.06 (1)"
    assert result.submitted_at == "2026-06-04T11:00:00+00:00"
    date_cell = api.append_cells[0][1]["rows"][0]["values"][14]
    expected_serial = (
        datetime(2026, 6, 4, 14, 0) - GOOGLE_SHEETS_EPOCH
    ).total_seconds() / 86400
    assert date_cell["userEnteredValue"]["numberValue"] == pytest.approx(expected_serial)


@pytest.mark.asyncio
async def test_submit_retry_keeps_previously_selected_sheet():
    api = FakeSheetsApi()
    service = make_service(
        api,
        clock=lambda: datetime(2026, 6, 4, 11, 0, tzinfo=timezone.utc),
    )

    result = await service.submit(
        make_draft(
            submission_spreadsheet_id=FL_SPREADSHEET,
            submission_sheet_name="01.06 ср",
        )
    )

    assert result.success is True
    assert result.sheet_name == "01.06 ср"
    assert (FL_SPREADSHEET, "01.06 ср") in api.headers


@pytest.mark.asyncio
async def test_existing_wrong_headers_return_error_and_do_not_append():
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"08.06 (1)": 42}},
        headers={(FL_SPREADSHEET, "08.06 (1)"): ["wrong"] * len(SHEET_HEADERS)},
    )
    service = make_service(api)

    result = await service.submit(make_draft(created_at="2026-06-05T10:00:00+00:00"))

    assert result.success is False
    assert "Структура колонок" in result.message
    assert api.append_cells == []


@pytest.mark.asyncio
async def test_submit_returns_user_friendly_message_after_google_rate_limit():
    api = FailingMetadataSheetsApi(
        FakeGoogleHttpError(429, "Quota exceeded for quota metric 'Read requests'")
    )
    service = make_service_with_retry(
        api,
        GoogleApiRetryConfig(max_attempts=1, base_seconds=0, max_seconds=0),
    )

    result = await service.submit(make_draft())

    assert result.success is False
    assert "Google API временно перегружен" in result.message
    assert "Повторите отправку через 2-3 минуты" in result.message
    assert "HttpError" not in result.message
    assert "Quota exceeded" not in result.message


@pytest.mark.asyncio
async def test_submit_keeps_generic_message_for_non_rate_limit_google_error():
    api = FailingMetadataSheetsApi(FakeGoogleHttpError(400, "schema error"))
    service = make_service_with_retry(
        api,
        GoogleApiRetryConfig(max_attempts=1, base_seconds=0, max_seconds=0),
    )

    result = await service.submit(make_draft())

    assert result.success is False
    assert "Не удалось отправить заявку" in result.message
    assert "schema error" in result.message


@pytest.mark.asyncio
async def test_idempotent_retry_does_not_rewrite_existing_submission_date():
    existing_row = [""] * len(SHEET_HEADERS)
    existing_row[11] = "A1B2C3D4"
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"08.06 (1)": 42}},
        headers={(FL_SPREADSHEET, "08.06 (1)"): SHEET_HEADERS.copy()},
        rows={(FL_SPREADSHEET, "08.06 (1)"): [SHEET_HEADERS, existing_row]},
    )
    service = make_service(api)

    result = await service.submit(make_draft())

    assert result.success is True
    assert result.row_number == 2
    assert result.submitted_at is None
    assert api.append_cells == []


@pytest.mark.asyncio
async def test_existing_legacy_working_sheet_keeps_legacy_schema():
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"08.06 (1)": 42}},
        headers={(FL_SPREADSHEET, "08.06 (1)"): LEGACY_WORKSHEET_HEADERS.copy()},
    )
    service = make_service(api)

    result = await service.submit(make_draft())

    assert result.success is True
    cells = api.append_cells[0][1]["rows"][0]["values"]
    assert len(cells) == 22
    assert cells[0]["userEnteredValue"] == {"stringValue": "A1B2C3D4"}


@pytest.mark.asyncio
async def test_existing_current_working_sheet_keeps_current_schema():
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"08.06 (1)": 42}},
        headers={(FL_SPREADSHEET, "08.06 (1)"): CURRENT_WORKSHEET_HEADERS.copy()},
    )
    service = make_service(api)

    result = await service.submit(make_draft())

    assert result.success is True
    cells = api.append_cells[0][1]["rows"][0]["values"]
    assert len(cells) == 23
    assert cells[0]["userEnteredValue"] == {"stringValue": "A1B2C3D4"}
    assert cells[10]["userEnteredValue"] == {"stringValue": "Редактор не выбран"}


def test_legacy_dashboard_requires_manual_editor_column():
    api = FakeSheetsApi(
        sheets={DASHBOARD_SPREADSHEET: {DASHBOARD_SHEET_NAME: 100}},
        headers={
            (DASHBOARD_SPREADSHEET, DASHBOARD_SHEET_NAME):
            LEGACY_DASHBOARD_HEADERS.copy()
        },
    )
    service = DashboardSyncService(
        spreadsheet_id=DASHBOARD_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    with pytest.raises(SheetConfigurationError, match="Insert column J"):
        service.upsert_application(
            application=make_draft(),
            status=ApplicationStatus.NEW.value,
            row_link="row-link",
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" Да ", "Да"),
        ("дА", "Да"),
        ("YES", "Да"),
        ("true", "Да"),
        ("1", "Да"),
        ("Нет", "Нет"),
        ("nO", "Нет"),
        ("FALSE", "Нет"),
        ("0", "Нет"),
        ("Р”Р°", "Да"),
        ("РќРµС‚", "Нет"),
        ("??", "Да"),
        ("???", "Нет"),
        ("неизвестно", "неизвестно"),
    ],
)
def test_repair_sheet_bool_handles_supported_values(value, expected):
    assert _repair_sheet_bool(value) == expected


def test_repair_dashboard_headers_restores_reversible_mojibake():
    damaged = []
    for header in DASHBOARD_HEADERS:
        encoded = header.encode("utf-8")
        try:
            damaged.append(encoded.decode("cp1251"))
        except UnicodeDecodeError:
            damaged.append(encoded.decode("latin1"))

    assert _repair_dashboard_headers(damaged) == DASHBOARD_HEADERS


def test_repair_dashboard_headers_restores_exact_question_mark_fingerprints():
    damaged = [
        "".join("?" if character.isalpha() and character not in {"I", "D"} else character for character in header)
        for header in DASHBOARD_HEADERS
    ]

    assert _repair_dashboard_headers(damaged) == DASHBOARD_HEADERS


def test_repair_dashboard_headers_rejects_ambiguous_or_foreign_schema():
    ambiguous = ["?" * len(header) for header in DASHBOARD_HEADERS]
    ambiguous[0] = "?" * len(DASHBOARD_HEADERS[0])
    foreign = DASHBOARD_HEADERS.copy()
    foreign[3] = "Посторонняя колонка"

    assert _repair_dashboard_headers(ambiguous) != DASHBOARD_HEADERS
    assert _repair_dashboard_headers(foreign) != DASHBOARD_HEADERS
    assert _repair_dashboard_headers(DASHBOARD_HEADERS[:-1]) != DASHBOARD_HEADERS


@pytest.mark.asyncio
async def test_append_cells_contains_status_dropdown_and_source_rich_text():
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"08.06 (1)": 42}},
        headers={(FL_SPREADSHEET, "08.06 (1)"): SHEET_HEADERS.copy()},
        rows={(FL_SPREADSHEET, "08.06 (1)"): [SHEET_HEADERS]},
    )
    service = make_service(api)
    formatting_json = serialize_formatting_spans(
        [
            TextFormattingSpan(start=0, end=4, bold=True),
            TextFormattingSpan(start=2, end=8, strikethrough=True),
        ]
    )

    result = await service.submit(
        make_draft(
            created_at="2026-06-05T10:00:00+00:00",
            source_text="abcdefgh",
            source_text_formatting_json=formatting_json,
        )
    )

    assert result.success is True
    cells = api.append_cells[0][1]["rows"][0]["values"]
    status_cell = cells[1]
    assert status_cell["userEnteredValue"] == {"stringValue": ApplicationStatus.NEW.value}
    assert [
        value["userEnteredValue"]
        for value in status_cell["dataValidation"]["condition"]["values"]
    ] == [status.value for status in ApplicationStatus]
    editor_cell = cells[9]
    assert editor_cell["userEnteredValue"] == {"stringValue": "Редактор не выбран"}
    assert [
        value["userEnteredValue"]
        for value in editor_cell["dataValidation"]["condition"]["values"]
    ] == ["Редактор не выбран", "редактор 1", "редактор 2"]
    source_cell = cells[4]
    assert source_cell["userEnteredValue"] == {"stringValue": "abcdefgh"}
    assert source_cell["userEnteredFormat"]["wrapStrategy"] == "CLIP"
    assert source_cell["textFormatRuns"] == [
        {"startIndex": 0, "format": {"bold": True, "strikethrough": False}},
        {"startIndex": 2, "format": {"bold": True, "strikethrough": True}},
        {"startIndex": 4, "format": {"bold": False, "strikethrough": True}},
    ]


def test_new_worksheet_formatting_has_active_group_border():
    from app.submission import worksheet_formatting_requests

    requests = worksheet_formatting_requests(42, ("редактор 1", "редактор 2"))
    border = next(
        request["repeatCell"]
        for request in requests
        if request.get("repeatCell", {})
        .get("cell", {})
        .get("userEnteredFormat", {})
        .get("borders")
    )

    assert border["range"]["startColumnIndex"] == 10
    assert border["range"]["endColumnIndex"] == 11
    assert border["cell"]["userEnteredFormat"]["borders"]["right"]["style"] == "SOLID_THICK"
    conditional_formulas = [
        request["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0][
            "userEnteredValue"
        ]
        for request in requests
        if "addConditionalFormatRule" in request
    ]
    assert conditional_formulas
    assert not any("$R2" in formula for formula in conditional_formulas)


def test_chips_row_preserves_rich_text_in_all_three_fragments():
    bold = serialize_formatting_spans(
        [TextFormattingSpan(start=0, end=3, bold=True)]
    )
    strike = serialize_formatting_spans(
        [TextFormattingSpan(start=1, end=4, strikethrough=True)]
    )
    row_data = _draft_to_row_data(
        make_draft(
            change_type=ChangeType.CHIPS.value,
            chip_text_before="before",
            chip_text_before_formatting_json=bold,
            chip_text="chip",
            chip_text_formatting_json=strike,
            chip_text_after="after",
            chip_text_after_formatting_json=bold,
        ),
        schema="chips",
    )

    cells = row_data["values"]
    assert cells[3]["textFormatRuns"][0]["format"]["bold"] is True
    assert cells[4]["textFormatRuns"][0]["startIndex"] == 0
    assert cells[4]["textFormatRuns"][1]["format"]["strikethrough"] is True
    assert cells[5]["textFormatRuns"][0]["format"]["bold"] is True


def test_chips_row_status_has_no_background_fill():
    row_data = _draft_to_row_data(
        make_draft(
            change_type=ChangeType.CHIPS.value,
            chip_text_before="before",
            chip_text="chip",
            chip_text_after="after",
        ),
        schema="chips",
    )

    status_cell = row_data["values"][1]
    assert status_cell["userEnteredValue"] == {"stringValue": ApplicationStatus.NEW.value}
    assert "dataValidation" in status_cell
    assert status_cell["userEnteredFormat"]["textFormat"]["bold"] is True
    assert "backgroundColor" not in status_cell["userEnteredFormat"]


@pytest.mark.asyncio
async def test_submission_does_not_write_dashboard_directly_when_configured():
    api = FakeSheetsApi()
    service = make_service(api, dashboard=True)

    result = await service.submit(make_draft(created_at="2026-06-05T10:00:00+00:00"))

    assert result.success is True
    assert not any(
        append[0] == DASHBOARD_SPREADSHEET for append in api.append_cells
    )


def test_week_sheet_name_uses_monday():
    assert week_sheet_name("2026-06-05T10:00:00+00:00") == "01.06"


def test_dashboard_duplicate_rows_are_detected_by_application_and_batch_id():
    rows = [
        DASHBOARD_HEADERS,
        ["A1B2C3D4", ""],
        ["A1B2C3D4", ""],
        ["Пачка", "BATCH-ONE"],
        ["Пачка", "BATCH-ONE"],
    ]

    assert _dashboard_duplicate_row_numbers(rows) == [3, 5]


def test_dashboard_tracked_row_preserves_existing_unknown_fields():
    tracked = SubmittedApplication(
        application_id="A1B2C3D4",
        telegram_user_id=123,
        sheet_name="01.06",
        last_known_status=ApplicationStatus.NEW.value,
        application_type=ApplicationType.SINGLE.value,
    )
    current = type(
        "Current",
        (),
        {
            "batch_id": None,
            "direction": Direction.FL.value,
            "answer_type": AnswerType.ROLLOUT.value,
            "is_urgent": None,
            "status": ApplicationStatus.IN_PROGRESS.value,
            "editor": "редактор 1",
            "final_answer": "",
        },
    )()
    existing = [
        "A1B2C3D4",
        "",
        "2026-06-05T10:00:00+00:00",
        Direction.FL.value,
        ApplicationType.SINGLE.value,
        AnswerType.ROLLOUT.value,
        "Р”Р°",
        "РђРІС‚РѕСЂ",
        ApplicationStatus.NEW.value,
        "Редактор не выбран",
        "РќРµС‚",
        "old-link",
    ]

    row = dashboard_tracked_row(
        tracked=tracked,
        current=current,
        row_link="new-link",
        existing_row=existing,
    )

    assert row[2] == "2026-06-05T10:00:00+00:00"
    assert row[6] == "Да"
    assert row[7] == "РђРІС‚РѕСЂ"
    assert row[8] == ApplicationStatus.IN_PROGRESS.value
    assert row[9] == "редактор 1"
    assert row[11] == "new-link"


def test_dashboard_bulk_batch_upsert_updates_existing_batch_row():
    api = FakeSheetsApi(
        sheets={DASHBOARD_SPREADSHEET: {DASHBOARD_SHEET_NAME: 100}},
        headers={(DASHBOARD_SPREADSHEET, DASHBOARD_SHEET_NAME): DASHBOARD_HEADERS},
        rows={
            (DASHBOARD_SPREADSHEET, DASHBOARD_SHEET_NAME): [
                DASHBOARD_HEADERS,
                ["Пачка", "BATCH-ABC12345", "old-date", Direction.FL.value, ApplicationType.BULK.value, "", "", "Telegram 123", ApplicationStatus.NEW.value, "Редактор не выбран", "Нет", "old-link"],
            ]
        },
    )
    service = DashboardSyncService(
        spreadsheet_id=DASHBOARD_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )
    batch = BulkBatch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name="Массовый ввод",
        sheet_id=300,
        start_row=2,
        data_start_row=4,
        reserved_rows=2,
    )

    service.upsert_bulk_batch(
        batch=batch,
        status=ApplicationStatus.IN_PROGRESS.value,
        row_link="new-link",
    )

    assert api.append_cells == []
    assert api.updated_rows == [
        (
            DASHBOARD_SPREADSHEET,
            DASHBOARD_SHEET_NAME,
            ["Пачка", "BATCH-ABC12345", "old-date", Direction.FL.value, ApplicationType.BULK.value, "", "", "Telegram 123", ApplicationStatus.IN_PROGRESS.value, "Редактор не выбран", "Нет", "new-link"],
        )
    ]
    dashboard_reads = [
        call for call in api.value_get_calls if call["range"].endswith("!A:L")
    ]
    assert dashboard_reads[-1]["valueRenderOption"] == "UNFORMATTED_VALUE"


def test_dashboard_bulk_batch_upsert_finds_legacy_batch_id_in_first_column():
    api = FakeSheetsApi(
        sheets={DASHBOARD_SPREADSHEET: {DASHBOARD_SHEET_NAME: 100}},
        headers={(DASHBOARD_SPREADSHEET, DASHBOARD_SHEET_NAME): DASHBOARD_HEADERS},
        rows={
            (DASHBOARD_SPREADSHEET, DASHBOARD_SHEET_NAME): [
                DASHBOARD_HEADERS,
                ["BATCH-ABC12345", "", "old-date", Direction.FL.value, ApplicationType.BULK.value, "", "", "Telegram 123", ApplicationStatus.NEW.value, "Редактор не выбран", "Нет", "old-link"],
            ]
        },
    )
    service = DashboardSyncService(
        spreadsheet_id=DASHBOARD_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )
    batch = BulkBatch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name="Массовый ввод",
        sheet_id=300,
        start_row=2,
        data_start_row=4,
        reserved_rows=2,
    )

    service.upsert_bulk_batch(
        batch=batch,
        status=ApplicationStatus.IN_PROGRESS.value,
        row_link="new-link",
    )

    assert api.append_cells == []
    assert len(api.updated_rows) == 1


def test_dashboard_bulk_batch_aggregates_multiple_editors():
    api = FakeSheetsApi(
        sheets={DASHBOARD_SPREADSHEET: {DASHBOARD_SHEET_NAME: 100}},
        headers={(DASHBOARD_SPREADSHEET, DASHBOARD_SHEET_NAME): DASHBOARD_HEADERS},
        rows={(DASHBOARD_SPREADSHEET, DASHBOARD_SHEET_NAME): [DASHBOARD_HEADERS]},
    )
    service = DashboardSyncService(
        spreadsheet_id=DASHBOARD_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )
    batch = BulkBatch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name="Массовый ввод",
        sheet_id=300,
        start_row=2,
        data_start_row=4,
        reserved_rows=2,
    )

    service.upsert_bulk_batch(
        batch=batch,
        status=ApplicationStatus.IN_PROGRESS.value,
        row_link="new-link",
        editors=("редактор 1", "редактор 2"),
    )

    values = _append_cell_values(api.append_cells[0][1])
    assert values[9] == "Несколько редакторов"


def test_dashboard_sync_batches_updates_and_merges_duplicates():
    existing_rows = [
        DASHBOARD_HEADERS,
        [
            "A1B2C3D4",
            "",
            46100.5,
            Direction.FL.value,
            ApplicationType.SINGLE.value,
            AnswerType.ROLLOUT.value,
            "Нет",
            "Автор",
            ApplicationStatus.NEW.value,
            "Редактор не выбран",
            "Нет",
            "https://example.test/first",
        ],
        [
            "A1B2C3D4",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            ApplicationStatus.IN_PROGRESS.value,
            "редактор 2",
            "Да",
            "",
        ],
    ]
    api = FakeSheetsApi(
        sheets={DASHBOARD_SPREADSHEET: {DASHBOARD_SHEET_NAME: 200}},
        headers={(DASHBOARD_SPREADSHEET, DASHBOARD_SHEET_NAME): DASHBOARD_HEADERS},
        rows={(DASHBOARD_SPREADSHEET, DASHBOARD_SHEET_NAME): existing_rows},
    )
    service = DashboardSyncService(
        spreadsheet_id=DASHBOARD_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )
    projected = list(existing_rows[1])
    projected[8] = ApplicationStatus.ACCEPTED.value
    projected[9] = "редактор 3"
    projected[10] = "Нет"
    item = DashboardOutboxItem(
        entity_type="APPLICATION",
        entity_id="A1B2C3D4",
        snapshot_json=json.dumps(
            dashboard_projection(projected),
            ensure_ascii=False,
        ),
    )

    service.sync_projections([item])

    dashboard_reads = [
        call
        for call in api.value_get_calls
        if call["range"].endswith("!A:L")
    ]
    assert len(dashboard_reads) == 1
    requests = api.batch_updates[-1]["body"]["requests"]
    assert sum("updateCells" in request for request in requests) == 1
    assert sum("deleteDimension" in request for request in requests) == 1
    update_values = requests[0]["updateCells"]["rows"][0]["values"]
    assert update_values[8]["userEnteredValue"] == {
        "stringValue": ApplicationStatus.ACCEPTED.value
    }
    assert update_values[9]["userEnteredValue"] == {"stringValue": "редактор 3"}
    assert update_values[10]["userEnteredValue"] == {"stringValue": "Да"}


def test_dashboard_sync_deletes_application_row_for_delete_action():
    existing_rows = [
        DASHBOARD_HEADERS,
        [
            "A1B2C3D4",
            "",
            "01.07.2026 12:00",
            Direction.FL.value,
            ApplicationType.SINGLE.value,
            AnswerType.URGENT.value,
            "Да",
            "Автор",
            ApplicationStatus.NEW.value,
            "Редактор не выбран",
            "Нет",
            "https://example.test/row",
        ],
    ]
    api = FakeSheetsApi(
        sheets={DASHBOARD_SPREADSHEET: {DASHBOARD_SHEET_NAME: 200}},
        headers={(DASHBOARD_SPREADSHEET, DASHBOARD_SHEET_NAME): DASHBOARD_HEADERS},
        rows={(DASHBOARD_SPREADSHEET, DASHBOARD_SHEET_NAME): existing_rows},
    )
    service = DashboardSyncService(
        spreadsheet_id=DASHBOARD_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )
    item = DashboardOutboxItem(
        entity_type="APPLICATION",
        entity_id="A1B2C3D4",
        snapshot_json=json.dumps(
            {"action": "delete", "application_id": "A1B2C3D4"},
            ensure_ascii=False,
        ),
    )

    service.sync_projections([item])

    requests = api.batch_updates[-1]["body"]["requests"]
    assert requests == [
        {
            "deleteDimension": {
                "range": {
                    "sheetId": 200,
                    "dimension": "ROWS",
                    "startIndex": 1,
                    "endIndex": 2,
                }
            }
        }
    ]


def test_sheet_protection_requests_replace_bot_managed_ranges():
    api = FakeSheetsApi(sheets={FL_SPREADSHEET: {"Срочные": 100}})
    api.sheets_by_spreadsheet[FL_SPREADSHEET][0]["protectedRanges"] = [
        {
            "protectedRangeId": 77,
            "description": "bot:protected:first-column:100",
        },
        {
            "protectedRangeId": 88,
            "description": "manual-protection",
        },
    ]

    requests = sheet_protection_requests(
        api,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        column_count=24,
        row_numbers=[1, 5],
    )

    assert requests[0] == {"deleteProtectedRange": {"protectedRangeId": 77}}
    added = [request["addProtectedRange"]["protectedRange"] for request in requests[1:]]
    descriptions = {item["description"] for item in added}
    assert descriptions == {
        "bot:protected:technical-columns:100:24",
        "bot:protected:service-row:100:1",
        "bot:protected:service-row:100:5",
    }
    technical = next(
        item for item in added if item["description"] == "bot:protected:technical-columns:100:24"
    )
    assert technical["range"] == {
        "sheetId": 100,
        "startColumnIndex": 11,
        "endColumnIndex": 24,
    }


def _sheet_name_from_range(range_name: str) -> str:
    quoted_name = range_name.split("!", maxsplit=1)[0]
    if quoted_name.startswith("'") and quoted_name.endswith("'"):
        return quoted_name[1:-1].replace("''", "'")
    return quoted_name


def _last_requests_with(api: FakeSheetsApi, *request_keys: str) -> list[dict]:
    for update in reversed(api.batch_updates):
        requests = update["body"]["requests"]
        if all(any(key in request for request in requests) for key in request_keys):
            return requests
    raise AssertionError(f"batchUpdate with keys {request_keys!r} not found")


def _append_cell_values(append_cells: dict) -> list:
    result = []
    for cell in append_cells["rows"][0]["values"]:
        value = cell.get("userEnteredValue", {})
        if "stringValue" in value:
            result.append(value["stringValue"])
        elif "numberValue" in value:
            result.append(value["numberValue"])
        else:
            result.append("")
    return result
