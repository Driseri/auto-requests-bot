from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

from app.config import load_settings
from app.submission import build_google_sheets_api, quote_sheet_name

INVENTORY_SCHEMA_VERSION = 1


CURRENT_BULK_INPUT_HEADERS = [
    "Тип ответа",
    "Интент",
    "Закрепленный сценарист",
    "Причина изменений",
    "Суть изменений",
    "Исходный текст",
    "Тип изменения",
]


CURRENT_BULK_SERVICE_HEADERS = [
    "ID заявки",
    "Статус",
    "Редактор",
    "Вопросы/комментарии редактора",
    "Ответ/комментарий сценариста",
    "Итоговый ответ редактора",
]


LEGACY_BULK_SERVICE_HEADERS = [
    "ID заявки",
    "Статус",
    "Вопросы/комментарии редактора",
    "Ответ/комментарий сценариста",
    "Итоговый ответ редактора",
]


BULK_INPUT_HEADERS = [
    "Тип ответа",
    "Тип изменения",
    "Закрепленный сценарист",
    "Интент",
    "Кейс или сообщения клиента",
    "Суть изменений",
    "Исходный текст",
]


BULK_SERVICE_HEADERS = [
    "Итоговый ответ редактора",
    "Комментарий качества",
    "Вопросы/комментарии редактора",
    "Ответ сценариста",
    "Статус",
    "Редактор",
    "ID заявки",
]


LEGACY_BULK_STAGING_HEADERS = [
    *CURRENT_BULK_INPUT_HEADERS,
    *LEGACY_BULK_SERVICE_HEADERS,
]


CURRENT_BULK_STAGING_HEADERS = [
    *CURRENT_BULK_INPUT_HEADERS,
    *CURRENT_BULK_SERVICE_HEADERS,
]


BULK_STAGING_HEADERS = [*BULK_INPUT_HEADERS, *BULK_SERVICE_HEADERS]


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



def build_legacy_inventory(
    sqlite_path: str,
    *,
    with_google: bool = False,
    credentials_path: str | None = None,
    code_version: str | None = None,
) -> dict[str, Any]:
    """Build a read-only legacy inventory from SQLite and optionally Google Sheets."""
    connection = _connect_read_only(sqlite_path)
    connection.row_factory = sqlite3.Row
    try:
        tables = _table_names(connection)
        report: dict[str, Any] = {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "code_version": code_version or os.getenv("APP_VERSION") or "unknown",
            "database": {
                "user_version": _scalar(connection, "PRAGMA user_version"),
                "integrity_check": _scalar(connection, "PRAGMA integrity_check"),
                "tables": {
                    table: _table_count(connection, table)
                    for table in (
                        "drafts",
                        "user_settings",
                        "submitted_applications",
                        "bulk_batches",
                        "bulk_creation_requests",
                        "bulk_reservations",
                        "notification_outbox",
                        "dashboard_outbox",
                        "application_events",
                        "schema_migrations",
                    )
                    if table in tables
                },
            },
            "legacy": _legacy_database_report(connection, tables),
        }
    finally:
        connection.close()

    if with_google:
        effective_credentials = credentials_path
        if not effective_credentials:
            effective_credentials = load_settings().google_credentials_path
        report["google"] = _legacy_google_report(
            report["legacy"]["bulk_applications"]["items"],
            credentials_path=effective_credentials,
        )

    report["blockers"] = _inventory_blockers(report)
    return report


def _legacy_database_report(
    connection: sqlite3.Connection,
    tables: set[str],
) -> dict[str, Any]:
    bulk_items: list[dict[str, Any]] = []
    if "submitted_applications" in tables:
        bulk_items = [
            {
                "application_id": row["application_id"],
                "batch_id": row["batch_id"],
                "spreadsheet_id": row["spreadsheet_id"],
                "sheet_id": row["sheet_id"],
                "sheet_name": row["sheet_name"],
                "row_number": row["last_seen_row_number"],
                "status": row["last_known_status"],
                "polling_state": row["polling_state"],
                "not_found_count": row["not_found_count"],
            }
            for row in connection.execute(
                """
                SELECT application_id, batch_id, spreadsheet_id, sheet_id,
                       sheet_name, last_seen_row_number, last_known_status,
                       polling_state, not_found_count
                FROM submitted_applications
                WHERE batch_id IS NOT NULL AND TRIM(batch_id) <> ''
                ORDER BY batch_id, last_seen_row_number, application_id
                """
            )
        ]

    report = {
        "bulk_applications": {
            "count": len(bulk_items),
            "by_status": _counter_dict(item["status"] for item in bulk_items),
            "by_polling_state": _counter_dict(
                item["polling_state"] for item in bulk_items
            ),
            "items": bulk_items,
        },
        "bulk_batches": _grouped_table_report(
            connection,
            tables,
            table="bulk_batches",
            group_column="registration_state",
        ),
        "bulk_creation_requests": _grouped_table_report(
            connection,
            tables,
            table="bulk_creation_requests",
            group_column="state",
        ),
        "bulk_dashboard_outbox": 0,
        "legacy_priority_drafts": {"count": 0, "by_step": {}},
        "retired_pending_actions": {"count": 0, "by_action": {}},
    }

    if "dashboard_outbox" in tables:
        report["bulk_dashboard_outbox"] = connection.execute(
            "SELECT COUNT(*) FROM dashboard_outbox WHERE entity_type = 'BULK_BATCH'"
        ).fetchone()[0]

    if "drafts" in tables:
        priority_rows = connection.execute(
            """
            SELECT current_step, COUNT(*) AS count
            FROM drafts
            WHERE current_step IN ('priority', 'edit_priority')
               OR (priority IS NOT NULL AND TRIM(priority) <> '')
            GROUP BY current_step
            """
        ).fetchall()
        report["legacy_priority_drafts"] = {
            "count": sum(row["count"] for row in priority_rows),
            "by_step": {row["current_step"]: row["count"] for row in priority_rows},
        }

    if "user_settings" in tables:
        action_rows = connection.execute(
            """
            SELECT pending_action, COUNT(*) AS count
            FROM user_settings
            WHERE pending_action LIKE 'create_bulk_direction:%'
            GROUP BY pending_action
            """
        ).fetchall()
        report["retired_pending_actions"] = {
            "count": sum(row["count"] for row in action_rows),
            "by_action": {row["pending_action"]: row["count"] for row in action_rows},
        }
    return report


def _legacy_google_report(
    bulk_items: list[dict[str, Any]],
    *,
    credentials_path: str,
) -> dict[str, Any]:
    api = build_google_sheets_api(credentials_path)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in bulk_items:
        if item["spreadsheet_id"] and item["sheet_name"]:
            grouped[(item["spreadsheet_id"], item["sheet_name"])].append(item)

    schema_layouts = {
        "bulk_new": _bulk_row_layout(BULK_STAGING_HEADERS),
        "bulk_current": _bulk_row_layout(CURRENT_BULK_STAGING_HEADERS),
        "bulk_legacy": _bulk_row_layout(LEGACY_BULK_STAGING_HEADERS),
    }
    result: dict[str, Any] = {
        "checked": 0,
        "found": 0,
        "missing": [],
        "coordinate_mismatches": [],
        "schema_counts": {},
        "source_errors": [],
    }
    schema_counts: Counter[str] = Counter()

    for (spreadsheet_id, sheet_name), items in grouped.items():
        try:
            response = (
                api.spreadsheets()
                .values()
                .get(
                    spreadsheetId=spreadsheet_id,
                    range=f"{quote_sheet_name(sheet_name)}!A:N",
                    majorDimension="ROWS",
                    valueRenderOption="FORMATTED_VALUE",
                )
                .execute()
            )
            rows = response.get("values", [])
        except Exception as exc:
            result["source_errors"].append(
                {
                    "spreadsheet_id": spreadsheet_id,
                    "sheet_name": sheet_name,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
            )
            continue

        found_rows: dict[str, tuple[int, str]] = {}
        active_layout: dict[str, Any] | None = None
        active_schema = "unknown"
        for row_number, row in enumerate(rows, start=1):
            layout = _bulk_row_layout(row)
            if layout is not None:
                active_layout = layout
                active_schema = next(
                    (
                        name
                        for name, expected_layout in schema_layouts.items()
                        if layout == expected_layout
                    ),
                    "unknown",
                )
                continue
            if active_layout is None:
                continue
            application_id_index = active_layout["application_id"]
            application_id = (
                str(row[application_id_index]).strip()
                if application_id_index < len(row)
                else ""
            )
            if application_id:
                found_rows[application_id] = (row_number, active_schema)

        for item in items:
            result["checked"] += 1
            found = found_rows.get(item["application_id"])
            if found is None:
                result["missing"].append(
                    {
                        "application_id": item["application_id"],
                        "batch_id": item["batch_id"],
                        "sheet_name": sheet_name,
                    }
                )
                continue
            actual_row, schema = found
            result["found"] += 1
            schema_counts[schema] += 1
            if item["row_number"] != actual_row:
                result["coordinate_mismatches"].append(
                    {
                        "application_id": item["application_id"],
                        "expected_row": item["row_number"],
                        "actual_row": actual_row,
                        "sheet_name": sheet_name,
                    }
                )

    result["schema_counts"] = dict(sorted(schema_counts.items()))
    return result


def _inventory_blockers(report: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    legacy = report["legacy"]
    if legacy["bulk_applications"]["count"]:
        blockers.append("active_legacy_bulk_applications")
    if legacy["bulk_batches"]["count"]:
        blockers.append("legacy_bulk_batches_present")
    if legacy["bulk_creation_requests"]["count"]:
        blockers.append("legacy_bulk_creation_requests_present")
    if legacy["bulk_dashboard_outbox"]:
        blockers.append("legacy_bulk_dashboard_outbox_present")
    if legacy["legacy_priority_drafts"]["count"]:
        blockers.append("legacy_priority_drafts_present")
    if legacy["retired_pending_actions"]["count"]:
        blockers.append("retired_pending_actions_present")
    google = report.get("google")
    if google:
        if google["missing"]:
            blockers.append("legacy_bulk_rows_missing_in_google")
        if google["coordinate_mismatches"]:
            blockers.append("legacy_bulk_coordinate_mismatches")
        if google["source_errors"]:
            blockers.append("legacy_google_sources_unavailable")
    return blockers


def _grouped_table_report(
    connection: sqlite3.Connection,
    tables: set[str],
    *,
    table: str,
    group_column: str,
) -> dict[str, Any]:
    if table not in tables:
        return {"count": 0, "by_state": {}}
    rows = connection.execute(
        f"SELECT {group_column} AS state, COUNT(*) AS count "
        f"FROM {table} GROUP BY {group_column} ORDER BY {group_column}"
    ).fetchall()
    return {
        "count": sum(row["count"] for row in rows),
        "by_state": {row["state"]: row["count"] for row in rows},
    }


def _connect_read_only(path: str) -> sqlite3.Connection:
    database = Path(path).resolve()
    if not database.exists():
        raise FileNotFoundError(database)
    return sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=10)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _scalar(connection: sqlite3.Connection, query: str) -> Any:
    row = connection.execute(query).fetchone()
    return row[0] if row else None


def _counter_dict(values: Any) -> dict[str, int]:
    return dict(sorted(Counter(str(value or "") for value in values).items()))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only inventory for legacy application state",
    )
    parser.add_argument("--sqlite-path", default=os.getenv("SQLITE_PATH", "/data/app.db"))
    parser.add_argument("--with-google", action="store_true")
    parser.add_argument("--credentials-path")
    parser.add_argument("--output")
    parser.add_argument("--fail-on-blockers", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_legacy_inventory(
        args.sqlite_path,
        with_google=args.with_google,
        credentials_path=args.credentials_path,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)
    return 2 if args.fail_on_blockers and report["blockers"] else 0


if __name__ == "__main__":
    sys.exit(main())
