from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json

import pytest

from app.bulk import (
    BULK_RESERVATION_REQUIRED_BACKGROUND_COLOR,
    BULK_RESERVATION_SCRIPTWRITER_BACKGROUND_COLOR,
    BulkReservationRegistrar,
    GoogleSheetsBulkReservationService,
    _daily_bulk_reservation_insert_plan,
    _urgent_daily_bulk_reservation_insert_plan,
)
from app.google_api import GoogleApiRetryConfig
from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
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






def test_new_daily_bulk_reservation_does_not_inherit_previous_row_group():
    plan = _daily_bulk_reservation_insert_plan(
        [
            WORKSHEET_HEADERS,
            ["02.07.26"],
            ["old application"],
        ],
        sheet_id=42,
        label="07.07.26",
        section_start_row=2,
        section_end_row=4,
        column_count=len(WORKSHEET_HEADERS),
        count=2,
    )

    assert plan["inherit_from_before"] is False
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


def test_existing_daily_bulk_reservation_keeps_inheriting_same_day_formatting():
    plan = _urgent_daily_bulk_reservation_insert_plan(
        [
            WORKSHEET_HEADERS,
            ["07.07.26"],
            ["same day application"],
        ],
        sheet_id=42,
        label="07.07.26",
        change_type=ChangeType.ADD,
        column_count=len(WORKSHEET_HEADERS),
        count=2,
    )

    assert plan["inherit_from_before"] is True
    assert plan["group_request"] is None


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




def make_reservation_service(
    repository: DraftRepository,
    api: FakeSheetsApi,
    *,
    google_api_retry: GoogleApiRetryConfig = GoogleApiRetryConfig(),
    daily_sheet_grouping_enabled: bool = True,
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
        daily_sheet_grouping_enabled=daily_sheet_grouping_enabled,
    )
    return GoogleSheetsBulkReservationService(
        submission_service=submission_service,
        repository=repository,
        google_api_retry=google_api_retry,
        daily_sheet_grouping_enabled=daily_sheet_grouping_enabled,
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
    assert result.shifted_rows == 0
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
async def test_urgent_bulk_reservation_always_uses_daily_structure(tmp_path):
    repository = DraftRepository(str(tmp_path / "urgent_reservation_daily_forced.db"))
    await repository.init()
    api = FakeSheetsApi(
        sheets={FL_SPREADSHEET: {"Срочные": 100}},
    )
    api.rows[(FL_SPREADSHEET, "Срочные")] = [
        WORKSHEET_HEADERS,
    ]
    service = make_reservation_service(
        repository,
        api,
        daily_sheet_grouping_enabled=False,
    )

    result = await service.create_reservation(
        make_reservation(
            reservation_id="RES-URGENT-FORCED-DAY",
            requested_count=2,
            change_type=ChangeType.CHIPS,
            target_kind=BulkTargetKind.URGENT,
        )
    )

    assert result.success is True
    assert result.reservation is not None
    assert result.reservation.start_row == 5
    requests = [
        request
        for update in api.batch_updates
        for request in update["body"]["requests"]
    ]
    inserted_values = [
        cell["userEnteredValue"]
        for request in requests
        if "updateCells" in request
        for row in request["updateCells"].get("rows", [])
        for cell in row.get("values", [])
        if cell.get("userEnteredValue")
    ]
    assert {"stringValue": "23.06.26"} in inserted_values
    assert {"stringValue": ChangeType.CHIPS.value} in inserted_values
    assert {"stringValue": CHIPS_WORKSHEET_HEADERS[0]} in inserted_values
    update_cells = next(request["updateCells"] for request in requests if "updateCells" in request)
    header_cell_format = update_cells["rows"][2]["values"][0]["userEnteredFormat"]
    assert header_cell_format["backgroundColor"] == {
        "red": 0.94,
        "green": 0.94,
        "blue": 0.94,
    }
    assert header_cell_format["horizontalAlignment"] == "CENTER"
    assert header_cell_format["textFormat"] == {"bold": True}
    assert header_cell_format["wrapStrategy"] == "WRAP"


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
    submitted_events = await repository.list_application_events(
        application_id=outbox[0].entity_id,
        event_type="application_submitted",
    )
    assert len(submitted_events) == 1
    assert submitted_events[0].event_at == "2026-06-30T09:15:00+00:00"
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
