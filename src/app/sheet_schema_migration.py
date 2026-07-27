from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import json
import logging
from typing import Any, Iterable

from app.config import load_settings
from app.google_api import execute_with_retry
from app.submission import (
    PREVIOUS_CHIPS_WORKSHEET_HEADERS,
    PREVIOUS_WORKSHEET_HEADERS,
    build_google_sheets_api,
    quote_sheet_name,
)


LOGGER = logging.getLogger(__name__)
MIGRATION_FIELDS = (
    "userEnteredValue,userEnteredFormat,textFormatRuns,dataValidation,note"
)
GRID_READ_FIELDS = (
    "sheets(properties(sheetId,title),"
    "data(rowData(values(userEnteredValue,userEnteredFormat,textFormatRuns,"
    "dataValidation,note,formattedValue))))"
)


@dataclass(frozen=True, slots=True)
class SheetSchemaMigration:
    spreadsheet_id: str
    sheet_id: int
    sheet_name: str
    schema: str
    header_row_number: int
    start_row_number: int
    end_row_number: int
    row_count: int


def find_sheet_schema_migrations(
    *,
    spreadsheet_id: str,
    sheet_id: int,
    sheet_name: str,
    rows: list[list[dict[str, Any]]],
) -> list[SheetSchemaMigration]:
    migrations: list[SheetSchemaMigration] = []
    marker_rows = {
        row_index
        for row_index, row in enumerate(rows)
        if _is_marker_row(row)
    }
    for row_index, row in enumerate(rows):
        headers = _row_texts(row)
        if _starts_with(headers, PREVIOUS_WORKSHEET_HEADERS):
            column_count = len(PREVIOUS_WORKSHEET_HEADERS)
            schema = "add_edit"
        elif _starts_with(headers, PREVIOUS_CHIPS_WORKSHEET_HEADERS):
            column_count = len(PREVIOUS_CHIPS_WORKSHEET_HEADERS)
            schema = "chips"
        else:
            continue

        end_row_index = _next_marker_row(marker_rows, row_index) or len(rows)
        migrations.append(
            SheetSchemaMigration(
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                sheet_name=sheet_name,
                schema=schema,
                header_row_number=row_index + 1,
                start_row_number=row_index + 1,
                end_row_number=end_row_index,
                row_count=max(end_row_index - row_index, 0),
            )
        )
        LOGGER.debug(
            "Planned sheet schema migration: spreadsheet_id=%s sheet_name=%s "
            "schema=%s header_row=%s rows=%s columns=%s",
            spreadsheet_id,
            sheet_name,
            schema,
            row_index + 1,
            max(end_row_index - row_index, 0),
            column_count,
        )
    return migrations


def build_update_cells_request(
    *,
    sheet_id: int,
    schema: str,
    start_row_index: int,
    rows: list[list[dict[str, Any]]],
) -> dict[str, Any]:
    if schema == "add_edit":
        column_count = len(PREVIOUS_WORKSHEET_HEADERS)
        reordered = [_reorder_previous_add_edit_row(row) for row in rows]
    elif schema == "chips":
        column_count = len(PREVIOUS_CHIPS_WORKSHEET_HEADERS)
        reordered = [_reorder_previous_chips_row(row) for row in rows]
    else:
        raise ValueError(f"Unknown migration schema: {schema}")
    return {
        "updateCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row_index,
                "endRowIndex": start_row_index + len(reordered),
                "startColumnIndex": 0,
                "endColumnIndex": column_count,
            },
            "rows": [{"values": row} for row in reordered],
            "fields": MIGRATION_FIELDS,
        }
    }


def _reorder_previous_add_edit_row(row: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cells = _pad_cells(row, len(PREVIOUS_WORKSHEET_HEADERS))
    return [
        *_copy_cells(cells, [0, 9, 2, 3, 4, 5, 6, 7, 8, 10, 1]),
        *_copy_cells(cells, range(11, len(PREVIOUS_WORKSHEET_HEADERS))),
    ]


def _reorder_previous_chips_row(row: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cells = _pad_cells(row, len(PREVIOUS_CHIPS_WORKSHEET_HEADERS))
    return [
        *_copy_cells(cells, [0, 9, 2, 3, 4, 5, 6, 7, 8, 10, 1]),
        *_copy_cells(cells, range(11, len(PREVIOUS_CHIPS_WORKSHEET_HEADERS))),
    ]


def migrate_spreadsheet(
    api: Any,
    *,
    spreadsheet_id: str,
    sheet_names: set[str] | None,
    execute: bool,
    retry_config: Any,
) -> dict[str, Any]:
    sheets = _read_spreadsheet_grid(api, spreadsheet_id, sheet_names, retry_config)
    report: dict[str, Any] = {
        "spreadsheet_id": spreadsheet_id,
        "execute": execute,
        "sheets": [],
        "migrations": [],
    }
    requests: list[dict[str, Any]] = []
    for sheet in sheets:
        properties = sheet.get("properties", {})
        sheet_id = int(properties["sheetId"])
        sheet_name = str(properties["title"])
        rows = _sheet_row_data(sheet)
        migrations = find_sheet_schema_migrations(
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            sheet_name=sheet_name,
            rows=rows,
        )
        report["sheets"].append(
            {
                "sheet_name": sheet_name,
                "sheet_id": sheet_id,
                "migration_count": len(migrations),
            }
        )
        for migration in migrations:
            start_index = migration.start_row_number - 1
            end_index = migration.end_row_number
            section_rows = rows[start_index:end_index]
            requests.append(
                build_update_cells_request(
                    sheet_id=sheet_id,
                    schema=migration.schema,
                    start_row_index=start_index,
                    rows=section_rows,
                )
            )
            report["migrations"].append(
                {
                    "sheet_name": migration.sheet_name,
                    "schema": migration.schema,
                    "header_row": migration.header_row_number,
                    "start_row": migration.start_row_number,
                    "end_row": migration.end_row_number,
                    "row_count": migration.row_count,
                }
            )
    if execute and requests:
        execute_with_retry(
            lambda: api.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            )
            .execute(),
            config=retry_config,
            operation_id=f"sheet-schema-migration:{spreadsheet_id}",
        )
    report["request_count"] = len(requests)
    return report


def _read_spreadsheet_grid(
    api: Any,
    spreadsheet_id: str,
    sheet_names: set[str] | None,
    retry_config: Any,
) -> list[dict[str, Any]]:
    metadata = execute_with_retry(
        lambda: api.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title))",
        )
        .execute(),
        config=retry_config,
        operation_id=f"sheet-schema-migration-metadata:{spreadsheet_id}",
    )
    result = []
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {})
        title = str(properties.get("title", ""))
        if sheet_names is not None and title not in sheet_names:
            continue
        grid = execute_with_retry(
            lambda title=title: api.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                ranges=[f"{quote_sheet_name(title)}!A:X"],
                includeGridData=True,
                fields=GRID_READ_FIELDS,
            )
            .execute(),
            config=retry_config,
            operation_id=f"sheet-schema-migration-read:{spreadsheet_id}:{title}",
        )
        result.extend(grid.get("sheets", []))
    return result


def _sheet_row_data(sheet: dict[str, Any]) -> list[list[dict[str, Any]]]:
    data = sheet.get("data") or []
    if not data:
        return []
    return [
        row.get("values", [])
        for row in (data[0].get("rowData") or [])
    ]


def _row_texts(row: list[dict[str, Any]]) -> list[str]:
    return [_cell_text(cell).strip() for cell in row]


def _cell_text(cell: dict[str, Any]) -> str:
    value = cell.get("userEnteredValue") or {}
    for key in ("stringValue", "numberValue", "boolValue", "formulaValue"):
        if key in value:
            return str(value[key])
    formatted = cell.get("formattedValue")
    return str(formatted) if formatted is not None else ""


def _starts_with(values: list[str], expected: list[str]) -> bool:
    return values[: len(expected)] == expected


def _is_marker_row(row: list[dict[str, Any]]) -> bool:
    first = _cell_text(row[0]).strip() if row else ""
    if first not in {"ADD", "EDIT", "CHIPS", "CHIPS V2"}:
        return False
    return all(not _cell_text(cell).strip() for cell in row[1:])


def _next_marker_row(marker_rows: set[int], start_row_index: int) -> int | None:
    candidates = [row_index for row_index in marker_rows if row_index > start_row_index]
    return min(candidates) if candidates else None


def _pad_cells(row: list[dict[str, Any]], length: int) -> list[dict[str, Any]]:
    return [*row[:length], *({} for _ in range(max(length - len(row), 0)))]


def _copy_cells(cells: list[dict[str, Any]], indexes: Iterable[int]) -> list[dict[str, Any]]:
    return [deepcopy(cells[index]) for index in indexes]


def _configured_spreadsheets(settings: Any) -> dict[str, str]:
    return {
        "fl": settings.google_fl_spreadsheet_id,
        "sme": settings.google_sme_spreadsheet_id,
        "ai": settings.google_ai_spreadsheet_id,
        "voice_collection": settings.google_voice_collection_spreadsheet_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate single-application Google Sheets from the previous column "
            "order to the current order with Status in B and Intent in K."
        )
    )
    parser.add_argument("--execute", action="store_true", help="Apply changes.")
    parser.add_argument(
        "--spreadsheet-id",
        action="append",
        default=[],
        help="Spreadsheet ID to migrate. Can be specified multiple times.",
    )
    parser.add_argument(
        "--direction",
        action="append",
        choices=("fl", "sme", "ai", "voice_collection"),
        default=[],
        help="Configured direction spreadsheet to migrate.",
    )
    parser.add_argument(
        "--sheet-name",
        action="append",
        default=[],
        help="Limit migration to a sheet/tab name. Can be specified multiple times.",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON output indentation.",
    )
    logging.basicConfig(level=logging.INFO)
    args = parser.parse_args()
    settings = load_settings()
    spreadsheet_ids = list(args.spreadsheet_id)
    configured = _configured_spreadsheets(settings)
    for direction in args.direction:
        spreadsheet_id = configured.get(direction, "")
        if spreadsheet_id:
            spreadsheet_ids.append(spreadsheet_id)
    if not spreadsheet_ids:
        spreadsheet_ids = [value for value in configured.values() if value]
    spreadsheet_ids = list(dict.fromkeys(spreadsheet_ids))
    sheet_names = set(args.sheet_name) if args.sheet_name else None
    api = build_google_sheets_api(settings.google_credentials_path)

    reports = [
        migrate_spreadsheet(
            api,
            spreadsheet_id=spreadsheet_id,
            sheet_names=sheet_names,
            execute=args.execute,
            retry_config=settings.google_api_retry,
        )
        for spreadsheet_id in spreadsheet_ids
    ]
    output = {
        "execute": args.execute,
        "spreadsheet_count": len(spreadsheet_ids),
        "migration_count": sum(len(report["migrations"]) for report in reports),
        "request_count": sum(report["request_count"] for report in reports),
        "reports": reports,
    }
    print(json.dumps(output, ensure_ascii=False, indent=args.indent))


if __name__ == "__main__":
    main()
