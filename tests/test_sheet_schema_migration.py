from __future__ import annotations

from app.models import ApplicationStatus
from app.sheet_schema_migration import (
    build_update_cells_request,
    find_sheet_schema_migrations,
)
from app.submission import (
    CHIPS_WORKSHEET_HEADERS,
    PREVIOUS_CHIPS_WORKSHEET_HEADERS,
    PREVIOUS_WORKSHEET_HEADERS,
    WORKSHEET_HEADERS,
)


def cell(value: str) -> dict:
    return {"userEnteredValue": {"stringValue": value}}


def row(values: list[str]) -> list[dict]:
    return [cell(value) for value in values]


def values(cells: list[dict]) -> list[str]:
    return [item.get("userEnteredValue", {}).get("stringValue", "") for item in cells]


def test_find_sheet_schema_migrations_detects_previous_add_edit_and_chips_sections():
    rows = [
        row(["ADD"]),
        row(PREVIOUS_WORKSHEET_HEADERS),
        row(["writer", "intent", "case", "change"]),
        row(["CHIPS"]),
        row(PREVIOUS_CHIPS_WORKSHEET_HEADERS),
        row(["writer", "intent", "reason"]),
        row(["EDIT"]),
        row(WORKSHEET_HEADERS),
    ]

    migrations = find_sheet_schema_migrations(
        spreadsheet_id="spreadsheet",
        sheet_id=42,
        sheet_name="29.06 (1)",
        rows=rows,
    )

    assert [(item.schema, item.start_row_number, item.end_row_number) for item in migrations] == [
        ("add_edit", 2, 3),
        ("chips", 5, 6),
    ]


def test_find_sheet_schema_migrations_ignores_current_headers():
    migrations = find_sheet_schema_migrations(
        spreadsheet_id="spreadsheet",
        sheet_id=42,
        sheet_name="29.06 (1)",
        rows=[row(WORKSHEET_HEADERS), row(CHIPS_WORKSHEET_HEADERS)],
    )

    assert migrations == []


def test_build_update_cells_request_reorders_previous_add_edit_cells():
    data = [""] * len(PREVIOUS_WORKSHEET_HEADERS)
    data[0] = "writer"
    data[1] = "intent"
    data[2] = "case"
    data[3] = "change"
    data[4] = "source"
    data[5] = "final"
    data[6] = "quality"
    data[7] = "question"
    data[8] = "answer"
    data[9] = ApplicationStatus.ACCEPTED.value
    data[10] = "editor"
    data[11] = "APPID"

    request = build_update_cells_request(
        sheet_id=42,
        schema="add_edit",
        start_row_index=1,
        rows=[row(data)],
    )

    reordered = values(request["updateCells"]["rows"][0]["values"])
    assert reordered[:12] == [
        "writer",
        ApplicationStatus.ACCEPTED.value,
        "case",
        "change",
        "source",
        "final",
        "quality",
        "question",
        "answer",
        "editor",
        "intent",
        "APPID",
    ]
    assert request["updateCells"]["range"] == {
        "sheetId": 42,
        "startRowIndex": 1,
        "endRowIndex": 2,
        "startColumnIndex": 0,
        "endColumnIndex": len(PREVIOUS_WORKSHEET_HEADERS),
    }


def test_build_update_cells_request_reorders_previous_chips_cells():
    data = [""] * len(PREVIOUS_CHIPS_WORKSHEET_HEADERS)
    data[0] = "writer"
    data[1] = "intent"
    data[2] = "reason"
    data[3] = "before"
    data[4] = "chip"
    data[5] = "after"
    data[9] = ApplicationStatus.NEW.value
    data[10] = "editor"
    data[11] = "APPID"

    request = build_update_cells_request(
        sheet_id=42,
        schema="chips",
        start_row_index=4,
        rows=[row(data)],
    )

    reordered = values(request["updateCells"]["rows"][0]["values"])
    assert reordered[:12] == [
        "writer",
        ApplicationStatus.NEW.value,
        "reason",
        "before",
        "chip",
        "after",
        "",
        "",
        "",
        "editor",
        "intent",
        "APPID",
    ]
    assert request["updateCells"]["range"]["endColumnIndex"] == len(
        PREVIOUS_CHIPS_WORKSHEET_HEADERS
    )
