from __future__ import annotations

import pytest

from app.bulk import (
    BULK_STAGING_HEADERS,
    LEGACY_BULK_STAGING_HEADERS,
    BulkApplicationRegistrar,
    GoogleSheetsBulkBatchService,
)
from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
    BulkApplicationStatus,
    BulkBatchStatus,
    ChangeType,
    Direction,
)
from app.repository import DraftRepository
from app.submission import DirectionSpreadsheetConfig


FL_SPREADSHEET = "fl-spreadsheet"
WEEK_SHEET = "01.06"
BULK_SHEET = "\u041c\u0430\u0441\u0441\u043e\u0432\u044b\u0439 \u0432\u0432\u043e\u0434"


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
        exact_rows = self.api.rows.get((spreadsheet_id, range_name))
        if exact_rows is not None:
            return FakeRequest({"values": exact_rows})
        if range_name.endswith("!A1:W1"):
            headers = self.api.headers.get((spreadsheet_id, sheet_name))
            return FakeRequest({"values": [headers]} if headers else {})
        if range_name.endswith("!A:W") or range_name.endswith("!A:M"):
            return FakeRequest({"values": self.api.rows.get((spreadsheet_id, sheet_name), [])})
        return FakeRequest({"values": []})

    def update(self, **kwargs):
        self.api.value_updates.append(kwargs)
        spreadsheet_id = kwargs["spreadsheetId"]
        sheet_name = _sheet_name_from_range(kwargs["range"])
        header = kwargs["body"]["values"][0]
        self.api.headers[(spreadsheet_id, sheet_name)] = header
        self.api.rows[(spreadsheet_id, sheet_name)] = [header]
        return FakeRequest({"updatedRows": 1})


class FakeSpreadsheetsResource:
    def __init__(self, api: "FakeSheetsApi") -> None:
        self.api = api

    def values(self):
        return FakeValuesResource(self.api)

    def get(self, **kwargs):
        self.api.metadata_get_calls.append(kwargs)
        spreadsheet_id = kwargs["spreadsheetId"]
        return FakeRequest({"sheets": self.api.sheets_by_spreadsheet.get(spreadsheet_id, [])})

    def batchUpdate(self, **kwargs):
        self.api.batch_updates.append(kwargs)

        def result():
            spreadsheet_id = kwargs["spreadsheetId"]
            self.api.sheets_by_spreadsheet.setdefault(spreadsheet_id, [])
            replies = []
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
                    sheet_id = request["appendCells"]["sheetId"]
                    sheet_name = self.api.sheet_name_by_id(spreadsheet_id, sheet_id)
                    if sheet_name:
                        self.api.rows.setdefault((spreadsheet_id, sheet_name), []).extend(
                            _row_data_to_values(row_data)
                            for row_data in request["appendCells"]["rows"]
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
        self.batch_updates = []

    def spreadsheets(self):
        return FakeSpreadsheetsResource(self)

    def sheet_name_by_id(self, spreadsheet_id: str, sheet_id: int) -> str | None:
        for sheet in self.sheets_by_spreadsheet.get(spreadsheet_id, []):
            properties = sheet["properties"]
            if properties["sheetId"] == sheet_id:
                return properties["title"]
        return None


class FakeDashboardSync:
    def __init__(self) -> None:
        self.bulk_upserts = []

    def upsert_bulk_batch(
        self,
        *,
        batch,
        status,
        row_link,
        final_answer_present=False,
        editors=(),
    ):
        self.bulk_upserts.append(
            {
                "batch": batch,
                "status": status,
                "row_link": row_link,
                "final_answer_present": final_answer_present,
                "editors": editors,
            }
        )


def make_service(repository: DraftRepository, api: FakeSheetsApi, *, reserved_rows: int = 1):
    return GoogleSheetsBulkBatchService(
        direction_spreadsheets=DirectionSpreadsheetConfig(
            fl_spreadsheet_id=FL_SPREADSHEET,
            sme_spreadsheet_id="sme-spreadsheet",
            ai_spreadsheet_id="ai-spreadsheet",
            voice_collection_spreadsheet_id="voice-spreadsheet",
        ),
        credentials_path="missing-for-test.json",
        repository=repository,
        sheets_api=api,
        reserved_rows=reserved_rows,
    )


@pytest.mark.asyncio
async def test_create_bulk_batch_creates_direction_week_section(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk.db"))
    await repository.init()
    api = FakeSheetsApi()
    service = make_service(repository, api)

    result = await service.create_batch(123, Direction.FL.value)

    assert result.success is True
    assert result.batch is not None
    assert result.batch.spreadsheet_id == FL_SPREADSHEET
    assert result.batch.direction == Direction.FL.value
    assert result.batch.sheet_name == BULK_SHEET
    assert result.batch.sheet_id == 100
    assert result.batch.start_row == 1
    assert result.batch.data_start_row == 3
    assert result.insert_url == (
        "https://docs.google.com/spreadsheets/d/fl-spreadsheet/edit#gid=100&range=A3:G3"
    )

    saved = await repository.get_bulk_batch(result.batch.batch_id)
    assert saved is not None
    assert saved.telegram_user_id == 123
    assert saved.direction == Direction.FL.value
    assert saved.status_schema_version == 2

    append_request = api.batch_updates[-1]["body"]["requests"][0]["appendCells"]
    assert append_request["sheetId"] == 100
    assert len(append_request["rows"]) == 3
    batch_status_cell = append_request["rows"][0]["values"][10]
    assert batch_status_cell["userEnteredValue"] == {"stringValue": BulkBatchStatus.NEW.value}
    assert [
        value["userEnteredValue"]
        for value in batch_status_cell["dataValidation"]["condition"]["values"]
    ] == [status.value for status in BulkBatchStatus]
    assert "Заполнена" not in [
        value["userEnteredValue"]
        for value in batch_status_cell["dataValidation"]["condition"]["values"]
    ]
    assert "backgroundColor" in append_request["rows"][2]["values"][0]["userEnteredFormat"]
    assert len(append_request["rows"][1]["values"]) == 13
    editor_cell = append_request["rows"][2]["values"][9]
    assert editor_cell["userEnteredValue"] == {"stringValue": "Редактор не выбран"}
    batch_status_rules = [
        request["addConditionalFormatRule"]["rule"]
        for request in api.batch_updates[-1]["body"]["requests"]
        if "addConditionalFormatRule" in request
    ]
    assert len(batch_status_rules) == 3


@pytest.mark.asyncio
async def test_create_bulk_batch_starts_after_two_empty_rows_from_actual_sheet_end(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_spacing.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="OLD-BATCH",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=1,
        data_start_row=3,
        reserved_rows=200,
    )
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, BULK_SHEET): [
                ["Пачка OLD-BATCH"],
                ["Тип ответа"],
                [AnswerType.ROLLOUT.value, "intent.one"],
                [AnswerType.URGENT.value, "intent.two"],
            ]
        },
    )
    service = make_service(repository, api)

    result = await service.create_batch(123, Direction.FL.value)

    assert result.success is True
    assert result.batch is not None
    assert result.batch.start_row == 7
    assert result.batch.data_start_row == 9
    assert result.batch.reserved_rows == 1
    assert result.insert_url.endswith("range=A9:G9")
    appended_rows = api.rows[(FL_SPREADSHEET, BULK_SHEET)]
    assert not any(appended_rows[4])
    assert not any(appended_rows[5])
    assert appended_rows[6][0].startswith("Пачка BATCH-")


@pytest.mark.asyncio
async def test_create_bulk_batch_blocks_legacy_sheet_without_editor_column(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk_legacy.db"))
    await repository.init()
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, BULK_SHEET): [
                ["Пачка OLD-BATCH"],
                LEGACY_BULK_STAGING_HEADERS,
                [AnswerType.ROLLOUT.value],
            ]
        },
    )

    result = await make_service(repository, api).create_batch(123, Direction.FL.value)

    assert result.success is False
    assert "Вставьте колонку J" in result.message


@pytest.mark.asyncio
async def test_bulk_registrar_returns_error_for_empty_batch(tmp_path):
    repository = DraftRepository(str(tmp_path / "registrar_wait.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=2,
        data_start_row=4,
        reserved_rows=3,
    )
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:M3"): [BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:M1003"): [[]],
        },
    )
    registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=FL_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    result = await registrar.register_batch("BATCH-ABC12345", 123)

    assert result.success is False
    assert "не найдены заполненные строки" in result.message
    assert await repository.list_submitted_applications() == []
    assert api.batch_updates == []


@pytest.mark.asyncio
async def test_bulk_registrar_registers_rows_by_batch_button_and_tracks_metadata(tmp_path):
    repository = DraftRepository(str(tmp_path / "registrar.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=2,
        data_start_row=4,
        reserved_rows=3,
    )
    row_to_register = [""] * 13
    row_to_register[0] = AnswerType.ROLLOUT.value
    row_to_register[1] = "intent.one"
    row_to_register[2] = "Writer"
    row_to_register[3] = "Reason"
    row_to_register[4] = "Change"
    row_to_register[5] = "Source"
    row_to_register[6] = ChangeType.ADD.value
    existing_row = [""] * 13
    existing_row[0] = AnswerType.URGENT.value
    existing_row[1] = "intent.two"
    existing_row[6] = ChangeType.EDIT.value
    existing_row[7] = "EXISTING1"
    existing_row[8] = ApplicationStatus.NEW.value
    existing_row[9] = "редактор 1"
    existing_row[10] = "Editor comment"
    existing_row[12] = "Final answer"
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:M3"): [BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:M1003"): [
                row_to_register,
                [],
                existing_row,
            ],
        },
    )
    registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=FL_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    result = await registrar.register_batch("BATCH-ABC12345", 123)
    tracked = await repository.list_submitted_applications()

    assert result.success is True
    assert result.registered_count == 2
    assert len(tracked) == 2
    generated = [item for item in tracked if item.application_id != "EXISTING1"][0]
    existing = [item for item in tracked if item.application_id == "EXISTING1"][0]
    assert generated.batch_id == "BATCH-ABC12345"
    assert generated.telegram_user_id == 123
    assert generated.spreadsheet_id == FL_SPREADSHEET
    assert generated.direction == Direction.FL.value
    assert generated.application_type == ApplicationType.BULK.value
    assert generated.answer_type == AnswerType.ROLLOUT.value
    assert generated.is_urgent is False
    assert generated.sheet_name == BULK_SHEET
    assert generated.sheet_id == 100
    assert generated.last_seen_row_number == 4
    assert existing.batch_id == "BATCH-ABC12345"
    assert existing.answer_type == AnswerType.URGENT.value
    assert existing.last_seen_row_number == 6
    assert existing.last_seen_editor == "редактор 1"
    assert existing.last_seen_editor_comment == "Editor comment"
    assert existing.last_seen_final_answer == "Final answer"
    saved_batch = await repository.get_bulk_batch("BATCH-ABC12345")
    assert saved_batch is not None
    assert saved_batch.reserved_rows == 3

    update_requests = [
        request
        for batch in api.batch_updates
        for request in batch["body"]["requests"]
        if "updateCells" in request
    ]
    assert len(update_requests) == 1
    registration_values = update_requests[0]["updateCells"]["rows"][0]["values"]
    assert registration_values[0]["userEnteredValue"]["stringValue"]
    assert registration_values[1]["userEnteredValue"] == {"stringValue": ApplicationStatus.NEW.value}
    assert "dataValidation" in registration_values[1]
    assert [
        item["userEnteredValue"]
        for item in registration_values[1]["dataValidation"]["condition"]["values"]
    ] == [status.value for status in BulkApplicationStatus]
    assert registration_values[2]["userEnteredValue"] == {
        "stringValue": "Редактор не выбран"
    }
    assert "dataValidation" in registration_values[2]
    assert update_requests[0]["updateCells"]["range"]["endColumnIndex"] == 10
    row_status_rules = [
        request["addConditionalFormatRule"]["rule"]
        for batch_update in api.batch_updates
        for request in batch_update["body"]["requests"]
        if "addConditionalFormatRule" in request
    ]
    assert len(row_status_rules) == 3

    hide_requests = [
        request
        for batch in api.batch_updates
        for request in batch["body"]["requests"]
        if "updateDimensionProperties" in request
    ]
    assert hide_requests == []
    group_requests = [
        request
        for batch in api.batch_updates
        for request in batch["body"]["requests"]
        if "addDimensionGroup" in request
    ]
    assert len(group_requests) == 1
    group_range = group_requests[0]["addDimensionGroup"]["range"]
    assert group_range["startIndex"] == 3
    assert group_range["endIndex"] == 6


@pytest.mark.asyncio
async def test_legacy_bulk_batch_keeps_legacy_row_status_validation(tmp_path):
    repository = DraftRepository(str(tmp_path / "legacy_statuses.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-LEGACY",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=2,
        data_start_row=4,
        reserved_rows=1,
        status_schema_version=1,
    )
    row = [AnswerType.ROLLOUT.value, "intent", "", "", "", "", ChangeType.ADD.value]
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:M3"): [BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:M1003"): [row],
        },
    )
    registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=FL_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    result = await registrar.register_batch("BATCH-LEGACY", 123)

    assert result.success is True
    update_request = next(
        request
        for batch_update in api.batch_updates
        for request in batch_update["body"]["requests"]
        if "updateCells" in request
    )
    status_cell = update_request["updateCells"]["rows"][0]["values"][1]
    assert [
        item["userEnteredValue"]
        for item in status_cell["dataValidation"]["condition"]["values"]
    ] == [status.value for status in ApplicationStatus]


@pytest.mark.asyncio
async def test_bulk_registration_rejects_all_rows_when_change_type_is_missing(tmp_path):
    repository = DraftRepository(str(tmp_path / "invalid_change_type.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-INVALID",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=2,
        data_start_row=4,
        reserved_rows=2,
    )
    valid_row = [AnswerType.ROLLOUT.value, "intent.one", "", "", "", "", "ADD"]
    invalid_row = [AnswerType.ROLLOUT.value, "intent.two"]
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:M3"): [BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:M1003"): [valid_row, invalid_row],
        },
    )
    registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=FL_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    result = await registrar.register_batch("BATCH-INVALID", 123)

    assert result.success is False
    assert "строк: 5" in result.message
    assert await repository.list_submitted_applications() == []


@pytest.mark.asyncio
async def test_bulk_registrar_syncs_dashboard_after_registration(tmp_path):
    repository = DraftRepository(str(tmp_path / "registrar_dashboard.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=2,
        data_start_row=4,
        reserved_rows=3,
    )
    row_to_register = [""] * 13
    row_to_register[0] = AnswerType.ROLLOUT.value
    row_to_register[1] = "intent.one"
    row_to_register[5] = "Source"
    row_to_register[6] = ChangeType.ADD.value
    row_to_register[12] = "Final answer"
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:M3"): [BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:M1003"): [row_to_register],
        },
    )
    dashboard = FakeDashboardSync()
    registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=FL_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
        dashboard_sync=dashboard,
    )

    result = await registrar.register_batch("BATCH-ABC12345", 123)

    assert result.success is True
    assert len(dashboard.bulk_upserts) == 1
    upsert = dashboard.bulk_upserts[0]
    assert upsert["batch"].batch_id == "BATCH-ABC12345"
    assert upsert["status"] == BulkBatchStatus.NEW.value
    assert upsert["row_link"].endswith("gid=100&range=A2:M2")
    assert upsert["final_answer_present"] is True


@pytest.mark.asyncio
async def test_bulk_registrar_rejects_non_author(tmp_path):
    repository = DraftRepository(str(tmp_path / "registrar_author.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-ABC12345",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=2,
        data_start_row=4,
        reserved_rows=3,
    )
    registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=FL_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=FakeSheetsApi(),
    )

    result = await registrar.register_batch("BATCH-ABC12345", 999)

    assert result.success is False
    assert "только ее автор" in result.message


def _sheet_name_from_range(range_name: str) -> str:
    quoted_name = range_name.split("!", maxsplit=1)[0]
    if quoted_name.startswith("'") and quoted_name.endswith("'"):
        return quoted_name[1:-1].replace("''", "'")
    return quoted_name


def _row_data_to_values(row_data: dict) -> list[str]:
    values = []
    for cell in row_data.get("values", []):
        entered = cell.get("userEnteredValue", {})
        values.append(str(entered.get("stringValue", entered.get("numberValue", ""))))
    return values
