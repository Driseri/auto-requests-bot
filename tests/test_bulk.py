from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import threading
import time

import aiosqlite
import pytest

from app.bulk import (
    BULK_STAGING_HEADERS,
    BULK_RESERVATION_REQUIRED_BACKGROUND_COLOR,
    BULK_RESERVATION_SCRIPTWRITER_BACKGROUND_COLOR,
    CURRENT_BULK_STAGING_HEADERS,
    LEGACY_BULK_STAGING_HEADERS,
    BulkApplicationRegistrar,
    BulkReservationRegistrar,
    GoogleSheetsBulkBatchService,
    GoogleSheetsBulkReservationService,
    _bulk_schema_layout,
)
from app.google_api import GoogleApiRetryConfig
from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
    BulkApplicationStatus,
    BulkBatchStatus,
    BulkRegistrationState,
    BulkReservation,
    BulkReservationState,
    BulkTargetKind,
    ChangeType,
    Direction,
    Draft,
)
from app.repository import DraftRepository
from app.scheduling import RolloutSchedule
from app.submission import (
    CHIPS_WORKSHEET_HEADERS,
    DirectionSpreadsheetConfig,
    GoogleSheetsSubmissionService,
    WORKSHEET_HEADERS,
)


def test_new_bulk_sheet_uses_client_case_header():
    assert "Кейс или сообщения клиента" in BULK_STAGING_HEADERS
    assert "Причина изменений" not in BULK_STAGING_HEADERS
    assert _bulk_schema_layout(BULK_STAGING_HEADERS)["schema"] == "new"


def test_old_bulk_sheet_headers_remain_supported():
    assert "Причина изменений" in CURRENT_BULK_STAGING_HEADERS
    assert "Причина изменений" in LEGACY_BULK_STAGING_HEADERS
    assert _bulk_schema_layout(CURRENT_BULK_STAGING_HEADERS)["schema"] == "current"
    assert _bulk_schema_layout(LEGACY_BULK_STAGING_HEADERS)["schema"] == "legacy"


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
        if range_name.endswith("!A1:X1"):
            headers = self.api.headers.get((spreadsheet_id, sheet_name))
            return FakeRequest({"values": [headers]} if headers else {})
        if (
            range_name.endswith("!A:X")
            or range_name.endswith("!A:M")
            or range_name.endswith("!A:N")
        ):
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


class FakeDeveloperMetadataResource:
    def __init__(self, api: "FakeSheetsApi") -> None:
        self.api = api

    def search(self, **kwargs):
        self.api.developer_metadata_search_calls.append(kwargs)
        if self.api.developer_metadata_error is not None:
            raise self.api.developer_metadata_error
        if self.api.developer_metadata_response is not None:
            return FakeRequest(self.api.developer_metadata_response)
        lookup = kwargs["body"]["dataFilters"][0]["developerMetadataLookup"]
        metadata_key = lookup.get("metadataKey")
        metadata_value = lookup.get("metadataValue")
        matches = [
            {"developerMetadata": item}
            for item in self.api.developer_metadata
            if item.get("metadataKey") == metadata_key
            and item.get("metadataValue") == metadata_value
        ]
        return FakeRequest({"matchedDeveloperMetadata": matches})


class FakeSpreadsheetsResource:
    def __init__(self, api: "FakeSheetsApi") -> None:
        self.api = api

    def values(self):
        return FakeValuesResource(self.api)

    def developerMetadata(self):
        return FakeDeveloperMetadataResource(self.api)

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
                elif "addDimensionGroup" in request:
                    group_range = request["addDimensionGroup"]["range"]
                    sheet_id = group_range["sheetId"]
                    for sheet in self.api.sheets_by_spreadsheet[spreadsheet_id]:
                        if sheet["properties"]["sheetId"] == sheet_id:
                            sheet.setdefault("rowGroups", []).append(
                                {"range": dict(group_range)}
                            )
                            break
                    replies.append({})
                elif "createDeveloperMetadata" in request:
                    metadata = request["createDeveloperMetadata"]["developerMetadata"]
                    self.api.developer_metadata.append(metadata)
                    replies.append({"createDeveloperMetadata": {"developerMetadata": metadata}})
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
        self.developer_metadata = []
        self.developer_metadata_search_calls = []
        self.developer_metadata_error = None
        self.developer_metadata_response = None

    def spreadsheets(self):
        return FakeSpreadsheetsResource(self)

    def sheet_name_by_id(self, spreadsheet_id: str, sheet_id: int) -> str | None:
        for sheet in self.sheets_by_spreadsheet.get(spreadsheet_id, []):
            properties = sheet["properties"]
            if properties["sheetId"] == sheet_id:
                return properties["title"]
        return None


class DelayedBulkCreationRepository(DraftRepository):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.complete_started = asyncio.Event()
        self.allow_complete = asyncio.Event()

    async def complete_bulk_reservation_creation_and_shift(self, *args, **kwargs):
        self.complete_started.set()
        await self.allow_complete.wait()
        return await super().complete_bulk_reservation_creation_and_shift(
            *args,
            **kwargs,
        )


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


def make_service(
    repository: DraftRepository,
    api: FakeSheetsApi,
    *,
    reserved_rows: int = 1,
    clock=None,
    timezone_name: str = "Europe/Moscow",
):
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
        clock=clock,
        timezone_name=timezone_name,
    )


def make_reservation_service(
    repository: DraftRepository,
    api: FakeSheetsApi,
    *,
    google_api_retry: GoogleApiRetryConfig = GoogleApiRetryConfig(),
):
    submission_service = GoogleSheetsSubmissionService(
        direction_spreadsheets=DirectionSpreadsheetConfig(
            fl_spreadsheet_id=FL_SPREADSHEET,
            sme_spreadsheet_id="sme-spreadsheet",
            ai_spreadsheet_id="ai-spreadsheet",
            voice_collection_spreadsheet_id="voice-spreadsheet",
        ),
        credentials_path="missing-for-test.json",
        rollout_schedule=RolloutSchedule.from_strings(),
        clock=lambda: datetime(2026, 6, 23, 7, 0, tzinfo=timezone.utc),
        sheets_api=api,
    )
    return GoogleSheetsBulkReservationService(
        submission_service=submission_service,
        repository=repository,
        google_api_retry=google_api_retry,
    )


def make_reservation(
    *,
    reservation_id: str = "RES-12345678",
    change_type: ChangeType = ChangeType.ADD,
    requested_count: int = 3,
    target_kind: BulkTargetKind = BulkTargetKind.ROLLOUT,
) -> BulkReservation:
    return BulkReservation(
        reservation_id=reservation_id,
        idempotency_key=f"{reservation_id}-key",
        telegram_user_id=123,
        state=BulkReservationState.CREATING.value,
        direction=Direction.FL.value,
        target_kind=target_kind.value,
        change_type=change_type.value,
        requested_count=requested_count,
    )


async def save_created_reservation(
    repository: DraftRepository,
    *,
    reservation_id: str,
    requested_count: int,
    start_row: int,
    end_row: int,
    change_type: ChangeType = ChangeType.ADD,
) -> None:
    await repository.create_bulk_reservation(
        reservation_id=reservation_id,
        idempotency_key=f"{reservation_id}-key",
        telegram_user_id=123,
    )
    await repository.update_bulk_reservation_step(
        reservation_id,
        state=BulkReservationState.AWAITING_CONFIRMATION,
        direction=Direction.FL.value,
        target_kind=BulkTargetKind.ROLLOUT.value,
        change_type=change_type.value,
        requested_count=requested_count,
    )
    await repository.complete_bulk_reservation_creation(
        reservation_id,
        spreadsheet_id=FL_SPREADSHEET,
        sheet_id=100,
        sheet_name="29.06 (1)",
        start_row=start_row,
        end_row=end_row,
        insert_url="https://example.test",
    )


@pytest.mark.asyncio
async def test_bulk_reservation_creation_highlights_required_columns_without_borders(tmp_path):
    repository = DraftRepository(str(tmp_path / "reservation_highlight.db"))
    await repository.init()
    api = FakeSheetsApi()
    service = make_reservation_service(repository, api)

    result = await service.create_reservation(make_reservation(requested_count=2))

    assert result.success is True
    requests = [
        request
        for update in api.batch_updates
        for request in update["body"]["requests"]
    ]
    assert not any("updateBorders" in request for request in requests)
    repeat_cells = [
        request["repeatCell"]
        for request in requests
        if "repeatCell" in request
        and request["repeatCell"].get("fields") == "userEnteredFormat.backgroundColor"
    ]
    clear_background = next(
        item
        for item in repeat_cells
        if item["range"].get("startRowIndex") == (result.reservation.start_row - 1)
        and item["range"].get("endRowIndex") == result.reservation.end_row
        and item["range"].get("startColumnIndex") == 0
        and item["range"].get("endColumnIndex") == len(WORKSHEET_HEADERS)
        and item["cell"].get("userEnteredFormat") == {}
    )
    assert clear_background["fields"] == "userEnteredFormat.backgroundColor"
    highlighted_columns = {
        item["range"]["startColumnIndex"]
        for item in repeat_cells
        if item["range"].get("startRowIndex") == (result.reservation.start_row - 1)
        and "backgroundColor" in item["cell"].get("userEnteredFormat", {})
    }
    assert highlighted_columns == {0, 2, 3, 4, 10}
    colors_by_column = {
        item["range"]["startColumnIndex"]: item["cell"]["userEnteredFormat"][
            "backgroundColor"
        ]
        for item in repeat_cells
        if item["range"]["startColumnIndex"] in highlighted_columns
        and "backgroundColor" in item["cell"].get("userEnteredFormat", {})
    }
    assert colors_by_column[0] == BULK_RESERVATION_SCRIPTWRITER_BACKGROUND_COLOR
    assert {
        column: color
        for column, color in colors_by_column.items()
        if column != 0
    } == {
        2: BULK_RESERVATION_REQUIRED_BACKGROUND_COLOR,
        3: BULK_RESERVATION_REQUIRED_BACKGROUND_COLOR,
        4: BULK_RESERVATION_REQUIRED_BACKGROUND_COLOR,
        10: BULK_RESERVATION_REQUIRED_BACKGROUND_COLOR,
    }


@pytest.mark.asyncio
async def test_urgent_bulk_reservation_creation_creates_daily_separator(tmp_path):
    repository = DraftRepository(str(tmp_path / "urgent_reservation_daily.db"))
    await repository.init()
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"Срочные": 100}},
    )
    api.rows[(FL_SPREADSHEET, "Срочные")] = [
        WORKSHEET_HEADERS,
        [ChangeType.CHIPS.value],
        CHIPS_WORKSHEET_HEADERS,
    ]
    service = make_reservation_service(repository, api)

    result = await service.create_reservation(
        make_reservation(
            reservation_id="RES-URGENT-DAY",
            requested_count=2,
            change_type=ChangeType.ADD,
            target_kind=BulkTargetKind.URGENT,
        )
    )

    assert result.success is True
    assert result.reservation is not None
    assert result.reservation.start_row == 3
    assert result.reservation.end_row == 4
    assert result.shifted_rows == 3
    requests = [
        request
        for update in api.batch_updates
        for request in update["body"]["requests"]
    ]
    update_cells = next(request["updateCells"] for request in requests if "updateCells" in request)
    assert update_cells["rows"][0]["values"][0]["userEnteredValue"] == {
        "stringValue": "23.06.26"
    }
    metadata = next(
        request["createDeveloperMetadata"]
        for request in requests
        if "createDeveloperMetadata" in request
    )
    dimension_range = metadata["developerMetadata"]["location"]["dimensionRange"]
    assert dimension_range["startIndex"] == 2
    assert dimension_range["endIndex"] == 3


@pytest.mark.asyncio
async def test_bulk_reservation_chips_creation_highlights_chips_required_columns(tmp_path):
    repository = DraftRepository(str(tmp_path / "reservation_chips_highlight.db"))
    await repository.init()
    rows = [
        ["ADD"],
        WORKSHEET_HEADERS,
        ["EDIT"],
        WORKSHEET_HEADERS,
        ["CHIPS"],
        CHIPS_WORKSHEET_HEADERS,
    ]
    api = FakeSheetsApi(sheets={FL_SPREADSHEET: {"29.06 (1)": 100}})
    api.rows[(FL_SPREADSHEET, "29.06 (1)")] = rows
    service = make_reservation_service(repository, api)

    result = await service.create_reservation(
        make_reservation(change_type=ChangeType.CHIPS, requested_count=2)
    )

    assert result.success is True
    requests = [
        request
        for update in api.batch_updates
        for request in update["body"]["requests"]
    ]
    highlighted_columns = {
        request["repeatCell"]["range"]["startColumnIndex"]
        for request in requests
        if "repeatCell" in request
        and request["repeatCell"].get("fields") == "userEnteredFormat.backgroundColor"
        and request["repeatCell"]["range"].get("startRowIndex") == (result.reservation.start_row - 1)
        and "backgroundColor" in request["repeatCell"]["cell"].get("userEnteredFormat", {})
    }
    assert highlighted_columns == {0, 2, 3, 4, 5, 10}
    colors_by_column = {
        request["repeatCell"]["range"]["startColumnIndex"]: request["repeatCell"][
            "cell"
        ]["userEnteredFormat"]["backgroundColor"]
        for request in requests
        if "repeatCell" in request
        and request["repeatCell"].get("fields") == "userEnteredFormat.backgroundColor"
        and request["repeatCell"]["range"].get("startRowIndex") == (result.reservation.start_row - 1)
        and "backgroundColor" in request["repeatCell"]["cell"].get("userEnteredFormat", {})
    }
    assert colors_by_column[0] == BULK_RESERVATION_SCRIPTWRITER_BACKGROUND_COLOR
    assert all(
        color == BULK_RESERVATION_REQUIRED_BACKGROUND_COLOR
        for column, color in colors_by_column.items()
        if column != 0
    )


@pytest.mark.asyncio
async def test_bulk_reservation_creation_reuses_existing_metadata_range(tmp_path):
    repository = DraftRepository(str(tmp_path / "reservation_metadata_recovery.db"))
    await repository.init()
    api = FakeSheetsApi(sheets={FL_SPREADSHEET: {"29.06 (1)": 100}})
    api.developer_metadata.append(
        {
            "metadataKey": "bulk_reservation_id",
            "metadataValue": "RES-RECOVER",
            "visibility": "DOCUMENT",
            "location": {
                "dimensionRange": {
                    "sheetId": 100,
                    "dimension": "ROWS",
                    "startIndex": 19,
                    "endIndex": 20,
                }
            },
        }
    )
    service = make_reservation_service(repository, api)

    result = await service.create_reservation(
        make_reservation(reservation_id="RES-RECOVER", requested_count=3)
    )

    assert result.success is True
    assert result.reservation is not None
    assert result.reservation.start_row == 20
    assert result.reservation.end_row == 22
    requests = [
        request
        for update in api.batch_updates
        for request in update["body"]["requests"]
    ]
    assert not any("insertDimension" in request for request in requests)


@pytest.mark.asyncio
async def test_bulk_reservation_creation_fails_closed_when_metadata_lookup_fails(tmp_path):
    repository = DraftRepository(str(tmp_path / "reservation_metadata_fail_closed.db"))
    await repository.init()
    api = FakeSheetsApi(sheets={FL_SPREADSHEET: {"29.06 (1)": 100}})
    api.developer_metadata_error = TimeoutError("metadata timeout")
    service = make_reservation_service(
        repository,
        api,
        google_api_retry=GoogleApiRetryConfig(max_attempts=1),
    )

    result = await service.create_reservation(
        make_reservation(reservation_id="RES-METADATA-FAIL", requested_count=3)
    )

    assert result.success is False
    assert result.retry_allowed is True
    assert "безопасно проверить" in result.message
    requests = [
        request
        for update in api.batch_updates
        for request in update["body"]["requests"]
    ]
    assert not any("insertDimension" in request for request in requests)


@pytest.mark.asyncio
async def test_bulk_reservation_creation_metadata_marks_only_first_row(tmp_path):
    repository = DraftRepository(str(tmp_path / "reservation_metadata_single_row.db"))
    await repository.init()
    api = FakeSheetsApi(sheets={FL_SPREADSHEET: {"29.06 (1)": 100}})
    service = make_reservation_service(repository, api)

    result = await service.create_reservation(
        make_reservation(reservation_id="RES-SINGLE-META", requested_count=4)
    )

    assert result.success is True
    metadata_requests = [
        request["createDeveloperMetadata"]["developerMetadata"]
        for update in api.batch_updates
        for request in update["body"]["requests"]
        if "createDeveloperMetadata" in request
    ]
    assert len(metadata_requests) == 1
    dimension_range = metadata_requests[0]["location"]["dimensionRange"]
    assert dimension_range["dimension"] == "ROWS"
    assert dimension_range["endIndex"] == dimension_range["startIndex"] + 1


@pytest.mark.asyncio
async def test_bulk_reservation_creation_respects_shared_section_lock(tmp_path):
    repository = DraftRepository(str(tmp_path / "reservation_shared_section_lock.db"))
    await repository.init()
    api = FakeSheetsApi(sheets={FL_SPREADSHEET: {"29.06 (1)": 100}})
    service = make_reservation_service(repository, api)
    acquired = await repository.acquire_bulk_section_lock(
        lock_key=f"{FL_SPREADSHEET}:29.06 (1):rollout:ADD",
        owner="single:A1B2C3D4",
        ttl_seconds=600,
    )

    result = await service.create_reservation_with_lock(
        make_reservation(reservation_id="RES-LOCKED", requested_count=2)
    )

    assert acquired is True
    assert result.success is False
    assert result.retry_allowed is True
    assert "другой пользователь создаёт строки" in result.message
    requests = [
        request
        for update in api.batch_updates
        for request in update["body"]["requests"]
    ]
    assert not any("insertDimension" in request for request in requests)


@pytest.mark.asyncio
async def test_bulk_reservation_holds_section_lock_until_sqlite_shift_completes(tmp_path):
    repository = DelayedBulkCreationRepository(str(tmp_path / "reservation_lock_until_shift.db"))
    await repository.init()
    await repository.create_bulk_reservation(
        reservation_id="RES-RACE",
        idempotency_key="RES-RACE-key",
        telegram_user_id=123,
    )
    await repository.update_bulk_reservation_step(
        "RES-RACE",
        state=BulkReservationState.CREATING,
        direction=Direction.FL.value,
        target_kind=BulkTargetKind.ROLLOUT.value,
        change_type=ChangeType.ADD.value,
        requested_count=2,
    )
    api = FakeSheetsApi(sheets={FL_SPREADSHEET: {"29.06 (1)": 100}})
    bulk_service = make_reservation_service(repository, api)
    single_service = GoogleSheetsSubmissionService(
        direction_spreadsheets=DirectionSpreadsheetConfig(
            fl_spreadsheet_id=FL_SPREADSHEET,
            sme_spreadsheet_id="sme-spreadsheet",
            ai_spreadsheet_id="ai-spreadsheet",
            voice_collection_spreadsheet_id="voice-spreadsheet",
        ),
        credentials_path="missing-for-test.json",
        rollout_schedule=RolloutSchedule.from_strings(),
        clock=lambda: datetime(2026, 6, 23, 7, 0, tzinfo=timezone.utc),
        sheets_api=api,
        repository=repository,
    )

    create_task = asyncio.create_task(
        bulk_service.create_reservation_with_lock(
            make_reservation(reservation_id="RES-RACE", requested_count=2)
        )
    )
    await asyncio.wait_for(repository.complete_started.wait(), timeout=1)

    single_result = await single_service.submit(
        Draft(
            telegram_user_id=456,
            current_step="completed",
            application_id="SINGLE01",
            direction=Direction.FL.value,
            answer_type=AnswerType.ROLLOUT.value,
            application_type=ApplicationType.SINGLE.value,
            change_type=ChangeType.ADD.value,
            created_at="2026-06-23T07:00:00+00:00",
            updated_at="2026-06-23T07:00:00+00:00",
        )
    )

    assert single_result.success is False
    assert "другой пользователь вносит строки" in single_result.message
    insert_requests_before_release = [
        request
        for update in api.batch_updates
        for request in update["body"]["requests"]
        if "insertDimension" in request
    ]
    assert len(insert_requests_before_release) == 1

    repository.allow_complete.set()
    bulk_result = await create_task

    assert bulk_result.success is True
    saved = await repository.get_bulk_reservation("RES-RACE")
    assert saved is not None
    assert saved.state == BulkReservationState.CREATED.value
    assert saved.start_row == bulk_result.reservation.start_row
    acquired_after_complete = await repository.acquire_bulk_section_lock(
        lock_key=f"{FL_SPREADSHEET}:29.06 (1):rollout:ADD",
        owner="single:SINGLE02",
        ttl_seconds=600,
    )
    assert acquired_after_complete is True


@pytest.mark.asyncio
async def test_bulk_reservation_registration_adds_borders_after_success(tmp_path):
    repository = DraftRepository(str(tmp_path / "reservation_registration_borders.db"))
    await repository.init()
    api = FakeSheetsApi(sheets={FL_SPREADSHEET: {"29.06 (1)": 100}})
    rows = [[""] * len(WORKSHEET_HEADERS) for _ in range(5)]
    rows[0][0] = "writer 1"
    rows[0][2] = "case 1"
    rows[0][3] = "change 1"
    rows[0][4] = "source 1"
    rows[0][10] = "intent 1"
    rows[2][0] = "writer 2"
    rows[2][2] = "case 2"
    rows[2][3] = "change 2"
    rows[2][4] = "source 2"
    rows[2][10] = "intent 2"
    api.rows[(FL_SPREADSHEET, "'29.06 (1)'!A10:X14")] = rows
    await save_created_reservation(
        repository,
        reservation_id="RES-READY",
        requested_count=5,
        start_row=10,
        end_row=14,
    )
    registrar = BulkReservationRegistrar(
        repository=repository,
        credentials_path="missing-for-test.json",
        sheets_api=api,
        application_editors=("editor 1", "editor 2"),
    )

    result = await registrar.register_reservation("RES-READY", 123)

    assert result.success is True
    assert len(api.batch_updates) == 1
    requests = [
        request
        for update in api.batch_updates
        for request in update["body"]["requests"]
    ]
    update_cells = [request["updateCells"] for request in requests if "updateCells" in request]
    assert len(update_cells) == 2
    assert all(
        update_cell["fields"] == "userEnteredValue,userEnteredFormat"
        for update_cell in update_cells
    )
    assert all(
        update_cell["rows"][0]["values"][0]["userEnteredFormat"]["backgroundColor"]
        == BULK_RESERVATION_SCRIPTWRITER_BACKGROUND_COLOR
        for update_cell in update_cells
    )
    editor_validation_requests = [
        request["repeatCell"]
        for request in requests
        if "repeatCell" in request
        and request["repeatCell"].get("fields") == "dataValidation"
    ]
    assert len(editor_validation_requests) == 4
    assert {
        item["range"]["startColumnIndex"]
        for item in editor_validation_requests
    } == {1, 9}
    status_validation_requests = [
        item for item in editor_validation_requests if item["range"]["startColumnIndex"] == 1
    ]
    editor_validation_requests = [
        item for item in editor_validation_requests if item["range"]["startColumnIndex"] == 9
    ]
    assert len(status_validation_requests) == 2
    assert len(editor_validation_requests) == 2
    assert [
        value["userEnteredValue"]
        for value in editor_validation_requests[0]["cell"]["dataValidation"]["condition"][
            "values"
        ]
    ] == ["Редактор не выбран", "editor 1", "editor 2"]
    assert [
        value["userEnteredValue"]
        for value in status_validation_requests[0]["cell"]["dataValidation"]["condition"][
            "values"
        ]
    ] == [status.value for status in ApplicationStatus]
    border_requests = [request["updateBorders"] for request in requests if "updateBorders" in request]
    assert len(border_requests) == 2
    assert border_requests[0]["range"]["startRowIndex"] == 9
    assert border_requests[0]["range"]["endRowIndex"] == 10
    assert border_requests[1]["range"]["startRowIndex"] == 11
    assert border_requests[1]["range"]["endRowIndex"] == 12
    clear_requests = [
        request["repeatCell"]
        for request in requests
        if "repeatCell" in request
        and request["repeatCell"].get("fields") == "userEnteredFormat.backgroundColor"
    ]
    cleared_rows = {
        request["range"]["startRowIndex"]
        for request in clear_requests
        if request["cell"].get("userEnteredFormat") == {}
    }
    assert cleared_rows == {10, 12, 13}


@pytest.mark.asyncio
async def test_bulk_reservation_registration_uses_current_metadata_range(tmp_path):
    repository = DraftRepository(str(tmp_path / "reservation_registration_metadata_range.db"))
    await repository.init()
    api = FakeSheetsApi(sheets={FL_SPREADSHEET: {"29.06 (1)": 100}})
    api.developer_metadata.append(
        {
            "metadataKey": "bulk_reservation_id",
            "metadataValue": "RES-MOVED",
            "visibility": "DOCUMENT",
            "location": {
                "dimensionRange": {
                    "sheetId": 100,
                    "dimension": "ROWS",
                    "startIndex": 19,
                    "endIndex": 20,
                }
            },
        }
    )
    rows = [[""] * len(WORKSHEET_HEADERS) for _ in range(3)]
    rows[0][0] = "writer moved"
    rows[0][2] = "case moved"
    rows[0][3] = "change moved"
    rows[0][4] = "source moved"
    rows[0][10] = "intent moved"
    api.rows[(FL_SPREADSHEET, "'29.06 (1)'!A20:X22")] = rows
    await save_created_reservation(
        repository,
        reservation_id="RES-MOVED",
        requested_count=3,
        start_row=10,
        end_row=12,
    )
    registrar = BulkReservationRegistrar(
        repository=repository,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    result = await registrar.register_reservation("RES-MOVED", 123)

    assert result.success is True
    assert result.insert_url is not None
    assert "range=A20" in result.insert_url
    assert api.value_get_calls[-1]["range"] == "'29.06 (1)'!A20:X22"
    tracked = await repository.list_submitted_applications()
    assert len(tracked) == 1
    assert tracked[0].last_seen_row_number == 20
    reservation = await repository.get_bulk_reservation("RES-MOVED")
    assert reservation is not None
    assert reservation.start_row == 20
    assert reservation.end_row == 22
    assert reservation.insert_url is not None
    assert "range=A20" in reservation.insert_url


@pytest.mark.asyncio
async def test_bulk_reservation_registration_fails_closed_when_metadata_lookup_fails(tmp_path):
    repository = DraftRepository(str(tmp_path / "reservation_registration_metadata_fail.db"))
    await repository.init()
    api = FakeSheetsApi(sheets={FL_SPREADSHEET: {"29.06 (1)": 100}})
    api.developer_metadata_error = TimeoutError("metadata timeout")
    rows = [[""] * len(WORKSHEET_HEADERS)]
    rows[0][0] = "writer"
    rows[0][2] = "case"
    rows[0][3] = "change"
    rows[0][4] = "source"
    rows[0][10] = "intent"
    api.rows[(FL_SPREADSHEET, "'29.06 (1)'!A10:X10")] = rows
    await save_created_reservation(
        repository,
        reservation_id="RES-META-REGISTER-FAIL",
        requested_count=1,
        start_row=10,
        end_row=10,
    )
    registrar = BulkReservationRegistrar(
        repository=repository,
        credentials_path="missing-for-test.json",
        sheets_api=api,
        google_api_retry=GoogleApiRetryConfig(max_attempts=1),
    )

    result = await registrar.register_reservation("RES-META-REGISTER-FAIL", 123)

    assert result.success is False
    assert "актуальное расположение" in result.message
    assert api.batch_updates == []
    reservation = await repository.get_bulk_reservation("RES-META-REGISTER-FAIL")
    assert reservation is not None
    assert reservation.state == BulkReservationState.CREATED.value


@pytest.mark.asyncio
async def test_bulk_reservation_registration_error_does_not_add_borders(tmp_path):
    repository = DraftRepository(str(tmp_path / "reservation_registration_no_borders.db"))
    await repository.init()
    api = FakeSheetsApi(sheets={FL_SPREADSHEET: {"29.06 (1)": 100}})
    partial = [""] * len(WORKSHEET_HEADERS)
    partial[0] = "writer"
    partial[2] = "case"
    api.rows[(FL_SPREADSHEET, "'29.06 (1)'!A10:X10")] = [partial]
    await save_created_reservation(
        repository,
        reservation_id="RES-PARTIAL",
        requested_count=1,
        start_row=10,
        end_row=10,
    )
    registrar = BulkReservationRegistrar(
        repository=repository,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    result = await registrar.register_reservation("RES-PARTIAL", 123)

    assert result.success is False
    requests = [
        request
        for update in api.batch_updates
        for request in update["body"]["requests"]
    ]
    assert not any("updateBorders" in request for request in requests)


@pytest.mark.asyncio
async def test_bulk_reservation_registration_enqueues_dashboard_application_projection(tmp_path):
    repository = DraftRepository(str(tmp_path / "reservation_dashboard.db"))
    await repository.init()
    api = FakeSheetsApi(sheets={FL_SPREADSHEET: {"29.06 (1)": 100}})
    row = [""] * len(WORKSHEET_HEADERS)
    row[0] = "writer"
    row[2] = "case text that must not become dashboard date"
    row[3] = "change description"
    row[4] = "source text"
    row[10] = "dashboard.intent"
    api.rows[(FL_SPREADSHEET, "'29.06 (1)'!A10:X10")] = [row]
    await save_created_reservation(
        repository,
        reservation_id="RES-DASH",
        requested_count=1,
        start_row=10,
        end_row=10,
    )
    registrar = BulkReservationRegistrar(
        repository=repository,
        credentials_path="missing-for-test.json",
        sheets_api=api,
        clock=lambda: datetime(2026, 6, 30, 9, 15, tzinfo=timezone.utc),
    )

    result = await registrar.register_reservation("RES-DASH", 123)

    assert result.success is True
    outbox = await repository.list_dashboard_outbox()
    assert len(outbox) == 1
    dashboard_row = json.loads(outbox[0].snapshot_json)["row"]
    assert dashboard_row[0] == outbox[0].entity_id
    assert dashboard_row[1] == ""
    assert dashboard_row[2] == "2026-06-30T09:15:00+00:00"
    assert dashboard_row[3] == Direction.FL.value
    assert dashboard_row[4] == ApplicationType.SINGLE.value
    assert dashboard_row[5] == AnswerType.ROLLOUT.value
    assert dashboard_row[8] == ApplicationStatus.NEW.value
    assert dashboard_row[9] == "Редактор не выбран"
    assert dashboard_row[10] == "Нет"
    assert dashboard_row[11].endswith("gid=100&range=A10:X10")


@pytest.mark.asyncio
async def test_bulk_reservation_chips_dashboard_projection_uses_dashboard_layout(tmp_path):
    repository = DraftRepository(str(tmp_path / "reservation_chips_dashboard.db"))
    await repository.init()
    api = FakeSheetsApi(sheets={FL_SPREADSHEET: {"29.06 (1)": 100}})
    row = [""] * len(CHIPS_WORKSHEET_HEADERS)
    row[0] = "writer"
    row[2] = "chips reason that must not become dashboard date"
    row[3] = "text before"
    row[4] = "chip text"
    row[5] = "text after"
    row[10] = "chips.intent"
    api.rows[(FL_SPREADSHEET, "'29.06 (1)'!A10:U10")] = [row]
    await save_created_reservation(
        repository,
        reservation_id="RES-CHIPS-DASH",
        requested_count=1,
        start_row=10,
        end_row=10,
        change_type=ChangeType.CHIPS,
    )
    registrar = BulkReservationRegistrar(
        repository=repository,
        credentials_path="missing-for-test.json",
        sheets_api=api,
        clock=lambda: datetime(2026, 6, 30, 9, 20, tzinfo=timezone.utc),
    )

    result = await registrar.register_reservation("RES-CHIPS-DASH", 123)

    assert result.success is True
    outbox = await repository.list_dashboard_outbox()
    assert len(outbox) == 1
    dashboard_row = json.loads(outbox[0].snapshot_json)["row"]
    assert dashboard_row[0] == outbox[0].entity_id
    assert dashboard_row[1] == ""
    assert dashboard_row[2] == "2026-06-30T09:20:00+00:00"
    assert dashboard_row[3] == Direction.FL.value
    assert dashboard_row[4] == ApplicationType.SINGLE.value
    assert dashboard_row[5] == AnswerType.ROLLOUT.value
    assert dashboard_row[8] == ApplicationStatus.NEW.value
    assert dashboard_row[10] == "Нет"
    assert dashboard_row[11].endswith("gid=100&range=A10:U10")


@pytest.mark.asyncio
async def test_create_bulk_batch_creates_direction_week_section(tmp_path):
    repository = DraftRepository(str(tmp_path / "bulk.db"))
    await repository.init()
    api = FakeSheetsApi()
    service = make_service(
        repository,
        api,
        clock=lambda: datetime(2026, 6, 15, 10, 30, tzinfo=timezone.utc),
    )

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
    batch_status_cell = append_request["rows"][0]["values"][11]
    date_cell = append_request["rows"][0]["values"][2]
    assert "numberValue" in date_cell["userEnteredValue"]
    assert date_cell["userEnteredFormat"]["numberFormat"] == {
        "type": "DATE_TIME",
        "pattern": "dd.MM.yyyy hh:mm",
    }
    assert saved.created_at == "2026-06-15T10:30:00+00:00"
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
    assert len(append_request["rows"][1]["values"]) == 14
    assert _row_data_to_values(append_request["rows"][1]) == BULK_STAGING_HEADERS
    batch_status_rules = [
        request["addConditionalFormatRule"]["rule"]
        for request in api.batch_updates[-1]["body"]["requests"]
        if "addConditionalFormatRule" in request
    ]
    assert len(batch_status_rules) == 3


@pytest.mark.asyncio
async def test_create_bulk_batch_starts_after_previous_allocated_range(tmp_path):
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
        data_end_row=4,
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
    assert result.batch.start_row == 205
    assert result.batch.data_start_row == 207
    assert result.batch.reserved_rows == 1
    assert result.insert_url.endswith("range=A207:G207")
    appended_rows = api.rows[(FL_SPREADSHEET, BULK_SHEET)]
    assert not any(appended_rows[202])
    assert not any(appended_rows[203])
    assert appended_rows[204][0].startswith("Пачка BATCH-")


@pytest.mark.asyncio
async def test_create_bulk_batch_allows_new_section_after_legacy_section(tmp_path):
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

    assert result.success is True
    assert result.batch is not None
    assert result.batch.start_row > 3


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
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:N3"): [CURRENT_BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:N6"): [[]],
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
        reserved_rows=5,
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
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:N3"): [CURRENT_BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:N8"): [
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
    assert saved_batch.reserved_rows == 5
    assert saved_batch.data_end_row == 6

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

    dimension_requests = [
        request
        for batch in api.batch_updates
        for request in batch["body"]["requests"]
        if "updateDimensionProperties" in request
    ]
    assert len(dimension_requests) == 2
    visible_request = next(
        request
        for request in dimension_requests
        if request["updateDimensionProperties"]["properties"]["hiddenByUser"] is False
    )
    visible_range = visible_request["updateDimensionProperties"]["range"]
    assert visible_range["startIndex"] == 3
    assert visible_range["endIndex"] == 6
    hidden_request = next(
        request
        for request in dimension_requests
        if request["updateDimensionProperties"]["properties"]["hiddenByUser"] is True
    )
    hidden_range = hidden_request["updateDimensionProperties"]["range"]
    assert hidden_range["startIndex"] == 6
    assert hidden_range["endIndex"] == 8
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

    registrar._organize_registered_batch_rows(api, saved_batch, actual_rows=3)
    repeated_group_requests = [
        request
        for batch in api.batch_updates
        for request in batch["body"]["requests"]
        if "addDimensionGroup" in request
    ]
    assert len(repeated_group_requests) == 1


def test_bulk_input_source_text_uses_clip_wrapping():
    from app.bulk import _bulk_input_row

    row = _bulk_input_row(("редактор 1", "редактор 2"))

    assert row["values"][6]["userEnteredFormat"]["wrapStrategy"] == "CLIP"
    assert row["values"][5]["userEnteredFormat"]["wrapStrategy"] == "WRAP"


@pytest.mark.asyncio
async def test_new_bulk_schema_registers_status_editor_and_id_in_l_to_n(tmp_path):
    repository = DraftRepository(str(tmp_path / "new_bulk_schema.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-NEW00001",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=2,
        data_start_row=4,
        reserved_rows=1,
    )
    row = [""] * 14
    row[0] = AnswerType.ROLLOUT.value
    row[1] = ChangeType.ADD.value
    row[2] = "Writer"
    row[3] = "intent.one"
    row[4] = "Reason"
    row[5] = "Change"
    row[6] = "Source"
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:N3"): [BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:N4"): [row],
        },
    )
    registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=FL_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    result = await registrar.register_batch("BATCH-NEW00001", 123)

    assert result.success is True
    update_request = next(
        request["updateCells"]
        for update in api.batch_updates
        for request in update["body"]["requests"]
        if "updateCells" in request
    )
    assert update_request["range"]["startColumnIndex"] == 11
    assert update_request["range"]["endColumnIndex"] == 14
    values = update_request["rows"][0]["values"]
    assert values[0]["userEnteredValue"] == {"stringValue": "Новая"}
    assert values[1]["userEnteredValue"] == {"stringValue": "Редактор не выбран"}
    assert values[2]["userEnteredValue"]["stringValue"]


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
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:N3"): [CURRENT_BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:N4"): [row],
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
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:N3"): [CURRENT_BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:N5"): [valid_row, invalid_row],
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
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:N3"): [CURRENT_BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:N6"): [row_to_register],
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
    assert dashboard.bulk_upserts == []
    outbox = await repository.list_dashboard_outbox()
    assert len(outbox) == 1
    assert outbox[0].entity_id == "BATCH-ABC12345"
    row = json.loads(outbox[0].snapshot_json)["row"]
    assert row[8] == BulkBatchStatus.NEW.value
    assert row[10] == "Да"
    assert row[11].endswith("gid=100&range=A2:N2")


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


@pytest.mark.asyncio
async def test_first_batch_registration_stops_before_next_batch_header(tmp_path):
    repository = DraftRepository(str(tmp_path / "bounded_batches.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-FIRST111",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=2,
        data_start_row=4,
        reserved_rows=7,
        data_end_row=10,
    )
    first_row = [AnswerType.ROLLOUT.value, "first", "", "", "", "", ChangeType.ADD.value]
    second_row = [AnswerType.ROLLOUT.value, "second", "", "", "", "", ChangeType.EDIT.value]
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:N3"): [CURRENT_BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:N10"): [
                first_row,
                [],
                [],
                ["Пачка BATCH-AABBCC22", "BATCH-AABBCC22"],
                CURRENT_BULK_STAGING_HEADERS,
                second_row,
            ],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A11:N17"): [],
        },
    )
    registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=FL_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    result = await registrar.register_batch("BATCH-FIRST111", 123)
    tracked = await repository.list_submitted_applications()

    assert result.success is True
    assert len(tracked) == 1
    assert tracked[0].last_seen_row_number == 4
    saved = await repository.get_bulk_batch("BATCH-FIRST111")
    assert saved is not None
    assert saved.data_end_row == 4
    assert saved.registration_state == BulkRegistrationState.REGISTERED.value


@pytest.mark.asyncio
async def test_bulk_registration_rejects_data_after_allocated_boundary(tmp_path):
    repository = DraftRepository(str(tmp_path / "overflow.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-OVERFLOW",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=2,
        data_start_row=4,
        reserved_rows=1,
        data_end_row=4,
    )
    valid_row = [AnswerType.ROLLOUT.value, "inside", "", "", "", "", ChangeType.ADD.value]
    overflow_row = [AnswerType.ROLLOUT.value, "outside", "", "", "", "", ChangeType.ADD.value]
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:N3"): [CURRENT_BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:N4"): [valid_row],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A5:N5"): [overflow_row],
        },
    )
    registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=FL_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    result = await registrar.register_batch("BATCH-OVERFLOW", 123)

    assert result.success is False
    assert "Допустимые строки: 4-4" in result.message
    assert await repository.list_submitted_applications() == []
    saved = await repository.get_bulk_batch("BATCH-OVERFLOW")
    assert saved is not None
    assert saved.registration_state == BulkRegistrationState.DRAFT.value


@pytest.mark.asyncio
async def test_bulk_registration_overflow_check_stops_before_known_next_batch(tmp_path):
    repository = DraftRepository(str(tmp_path / "overflow_next.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-CURRENT1",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=2,
        data_start_row=4,
        reserved_rows=7,
        data_end_row=10,
    )
    await repository.save_bulk_batch(
        batch_id="BATCH-NEXT2222",
        telegram_user_id=456,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=13,
        data_start_row=15,
        reserved_rows=7,
        data_end_row=21,
    )
    valid_row = [AnswerType.ROLLOUT.value, "inside", "", "", "", "", ChangeType.ADD.value]
    overflow_row = [AnswerType.ROLLOUT.value, "outside", "", "", "", "", ChangeType.ADD.value]
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:N3"): [CURRENT_BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:N10"): [valid_row],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A11:N12"): [overflow_row],
        },
    )
    registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=FL_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    result = await registrar.register_batch("BATCH-CURRENT1", 123)

    assert result.success is False
    assert any(
        call["range"] == f"'{BULK_SHEET}'!A11:N12"
        for call in api.value_get_calls
    )


@pytest.mark.asyncio
async def test_bulk_registration_skips_overflow_read_when_next_batch_is_adjacent(tmp_path):
    repository = DraftRepository(str(tmp_path / "overflow_adjacent.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-CURRENT1",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=2,
        data_start_row=4,
        reserved_rows=7,
        data_end_row=10,
    )
    await repository.save_bulk_batch(
        batch_id="BATCH-NEXT2222",
        telegram_user_id=456,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=11,
        data_start_row=13,
        reserved_rows=7,
        data_end_row=19,
    )
    row = [AnswerType.ROLLOUT.value, "inside", "", "", "", "", ChangeType.ADD.value]
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:N3"): [CURRENT_BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:N10"): [row],
        },
    )
    registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=FL_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    result = await registrar.register_batch("BATCH-CURRENT1", 123)

    assert result.success is True
    assert all(call["range"] != f"'{BULK_SHEET}'!A11:N10" for call in api.value_get_calls)
    assert all(call["range"] != f"'{BULK_SHEET}'!A11:N11" for call in api.value_get_calls)


@pytest.mark.asyncio
async def test_concurrent_bulk_registration_is_idempotent(tmp_path):
    repository = DraftRepository(str(tmp_path / "concurrent_registration.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-CONCURR1",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=2,
        data_start_row=4,
        reserved_rows=1,
    )
    row = [AnswerType.ROLLOUT.value, "intent", "", "", "", "", ChangeType.ADD.value]
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:N3"): [CURRENT_BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:N4"): [row],
        },
    )
    registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=FL_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    first, second = await asyncio.gather(
        registrar.register_batch("BATCH-CONCURR1", 123),
        registrar.register_batch("BATCH-CONCURR1", 123),
    )

    assert first.success is True
    assert second.success is True
    assert first.registered_count == second.registered_count == 1
    update_requests = [
        request
        for update in api.batch_updates
        for request in update["body"]["requests"]
        if "updateCells" in request
    ]
    group_requests = [
        request
        for update in api.batch_updates
        for request in update["body"]["requests"]
        if "addDimensionGroup" in request
    ]
    hide_requests = [
        request
        for update in api.batch_updates
        for request in update["body"]["requests"]
        if "updateDimensionProperties" in request
    ]
    assert len(update_requests) == 1
    assert len(group_requests) == 1
    assert (
        group_requests[0]["addDimensionGroup"]["range"]["startIndex"],
        group_requests[0]["addDimensionGroup"]["range"]["endIndex"],
    ) == (3, 4)
    assert all(
        request["updateDimensionProperties"]["properties"]["hiddenByUser"] is False
        for request in hide_requests
    )


@pytest.mark.asyncio
async def test_concurrent_bulk_registrations_collapse_only_their_own_tails(tmp_path):
    repository = DraftRepository(str(tmp_path / "parallel_batch_registration.db"))
    await repository.init()
    for batch_id, start_row, data_start_row in (
        ("BATCH-PARALLEL1", 2, 4),
        ("BATCH-PARALLEL2", 8, 10),
    ):
        await repository.save_bulk_batch(
            batch_id=batch_id,
            telegram_user_id=123,
            spreadsheet_id=FL_SPREADSHEET,
            direction=Direction.FL.value,
            sheet_name=BULK_SHEET,
            sheet_id=100,
            start_row=start_row,
            data_start_row=data_start_row,
            reserved_rows=3,
        )

    row = [AnswerType.ROLLOUT.value, "intent", "", "", "", "", ChangeType.ADD.value]
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:N3"): [CURRENT_BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:N6"): [row],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A9:N9"): [CURRENT_BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A10:N12"): [row],
        },
    )
    registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=FL_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    first, second = await asyncio.gather(
        registrar.register_batch("BATCH-PARALLEL1", 123),
        registrar.register_batch("BATCH-PARALLEL2", 123),
    )

    assert first.success is True
    assert second.success is True
    hidden_ranges = {
        (
            request["updateDimensionProperties"]["range"]["startIndex"],
            request["updateDimensionProperties"]["range"]["endIndex"],
        )
        for update in api.batch_updates
        for request in update["body"]["requests"]
        if "updateDimensionProperties" in request
        and request["updateDimensionProperties"]["properties"]["hiddenByUser"] is True
    }
    assert hidden_ranges == {(4, 6), (10, 12)}
    grouped_ranges = {
        (
            request["addDimensionGroup"]["range"]["startIndex"],
            request["addDimensionGroup"]["range"]["endIndex"],
        )
        for update in api.batch_updates
        for request in update["body"]["requests"]
        if "addDimensionGroup" in request
    }
    assert grouped_ranges == {(3, 4), (9, 10)}


@pytest.mark.asyncio
async def test_stale_bulk_registration_claim_can_be_recovered(tmp_path):
    db_path = str(tmp_path / "stale_registration.db")
    repository = DraftRepository(db_path)
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-STALE001",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=2,
        data_start_row=4,
        reserved_rows=1,
    )

    first_claim = await repository.claim_bulk_batch_registration(
        "BATCH-STALE001",
        stale_after_seconds=600,
    )
    active_claim = await repository.claim_bulk_batch_registration(
        "BATCH-STALE001",
        stale_after_seconds=600,
    )
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE bulk_batches
            SET registration_started_at = '2020-01-01T00:00:00+00:00'
            WHERE batch_id = 'BATCH-STALE001'
            """
        )
        await db.commit()
    recovered_claim = await repository.claim_bulk_batch_registration(
        "BATCH-STALE001",
        stale_after_seconds=600,
    )

    assert first_claim == "ACQUIRED"
    assert active_claim == BulkRegistrationState.REGISTERING.value
    assert recovered_claim == "ACQUIRED"


@pytest.mark.asyncio
async def test_google_error_releases_bulk_registration_for_retry(tmp_path):
    repository = DraftRepository(str(tmp_path / "registration_error.db"))
    await repository.init()
    await repository.save_bulk_batch(
        batch_id="BATCH-FA11ED01",
        telegram_user_id=123,
        spreadsheet_id=FL_SPREADSHEET,
        direction=Direction.FL.value,
        sheet_name=BULK_SHEET,
        sheet_id=100,
        start_row=2,
        data_start_row=4,
        reserved_rows=1,
    )
    row = [AnswerType.ROLLOUT.value, "intent", "", "", "", "", ChangeType.ADD.value]

    class FailingSpreadsheetsResource(FakeSpreadsheetsResource):
        def batchUpdate(self, **kwargs):
            if any("updateCells" in request for request in kwargs["body"]["requests"]):
                raise BrokenPipeError(32, "Broken pipe")
            return super().batchUpdate(**kwargs)

    class FailingSheetsApi(FakeSheetsApi):
        def spreadsheets(self):
            return FailingSpreadsheetsResource(self)

    api = FailingSheetsApi(
        sheets={FL_SPREADSHEET: {BULK_SHEET: 100}},
        rows={
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A3:N3"): [CURRENT_BULK_STAGING_HEADERS],
            (FL_SPREADSHEET, f"'{BULK_SHEET}'!A4:N4"): [row],
        },
    )
    registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=FL_SPREADSHEET,
        credentials_path="missing-for-test.json",
        sheets_api=api,
    )

    result = await registrar.register_batch("BATCH-FA11ED01", 123)
    saved = await repository.get_bulk_batch("BATCH-FA11ED01")

    assert result.success is False
    assert "Broken pipe" in result.message
    assert saved is not None
    assert saved.registration_state == BulkRegistrationState.DRAFT.value


@pytest.mark.asyncio
async def test_concurrent_bulk_creation_is_serialized_per_sheet(tmp_path):
    repository = DraftRepository(str(tmp_path / "concurrent_creation.db"))
    await repository.init()
    api = FakeSheetsApi()

    class InstrumentedService(GoogleSheetsBulkBatchService):
        active = 0
        max_active = 0
        counter_lock = threading.Lock()

        def _create_batch_sync(self, *args, **kwargs):
            with self.counter_lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.05)
                return super()._create_batch_sync(*args, **kwargs)
            finally:
                with self.counter_lock:
                    self.active -= 1

    service = InstrumentedService(
        direction_spreadsheets=DirectionSpreadsheetConfig(
            fl_spreadsheet_id=FL_SPREADSHEET,
            sme_spreadsheet_id="sme",
            ai_spreadsheet_id="ai",
            voice_collection_spreadsheet_id="voice",
        ),
        credentials_path="missing-for-test.json",
        repository=repository,
        sheets_api=api,
        reserved_rows=2,
    )

    first, second = await asyncio.gather(
        service.create_batch(101, Direction.FL.value),
        service.create_batch(102, Direction.FL.value),
    )

    assert first.success is True
    assert second.success is True
    assert service.max_active == 1
    assert first.batch is not None and second.batch is not None
    assert first.batch.data_end_row is not None
    assert second.batch.start_row > first.batch.data_end_row


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
