from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import logging
from typing import Any
from zoneinfo import ZoneInfo

from app.config import load_settings
from app.google_api import execute_with_retry
from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
    ChangeType,
    Direction,
    generate_application_id,
)
from app.repository import DraftRepository
from app.scheduling import DEFAULT_TIMEZONE
from app.sheet_dates import utc_iso
from app.submission import (
    CHIPS_WORKSHEET_HEADERS,
    CURRENT_WORKSHEET_HEADERS,
    DASHBOARD_HEADERS,
    INTEGRATION_SHEET_NAME,
    LEGACY_WORKSHEET_HEADERS,
    PREVIOUS_CHIPS_WORKSHEET_HEADERS,
    PREVIOUS_WORKSHEET_HEADERS,
    URGENT_SHEET_NAME,
    WORKSHEET_HEADERS,
    build_google_sheets_api,
    bool_to_sheet_value,
    dashboard_projection,
    quote_sheet_name,
    spreadsheet_row_link,
)


LOGGER = logging.getLogger(__name__)
DEFAULT_AUTHOR = "Админская индексация"
DEFAULT_INDEX_RANGE = "A:X"
DATE_SEPARATOR_LENGTH = 8


@dataclass(frozen=True, slots=True)
class SheetSection:
    schema: str
    headers: list[str]
    header_row_number: int
    column_count: int

    @property
    def layout(self) -> dict[str, int]:
        return {header: index for index, header in enumerate(self.headers)}

    @property
    def is_chips(self) -> bool:
        return self.schema in {"chips", "previous_chips"}


@dataclass(frozen=True, slots=True)
class IndexCandidate:
    spreadsheet_id: str
    sheet_id: int
    sheet_name: str
    row_number: int
    schema: str
    direction: str
    answer_type: str
    change_type: str
    is_urgent: bool
    status: str
    editor: str
    scriptwriter: str
    intent: str
    reason: str
    row_link: str
    values: list[str]


@dataclass(frozen=True, slots=True)
class SkippedRow:
    row_number: int
    schema: str | None
    reason: str
    preview: dict[str, str]


def find_index_candidates(
    *,
    spreadsheet_id: str,
    sheet_id: int,
    sheet_name: str,
    rows: list[list[Any]],
    direction: str,
    answer_type: str,
    change_type_filter: str | None = None,
    from_row: int | None = None,
    to_row: int | None = None,
) -> tuple[list[IndexCandidate], list[SkippedRow]]:
    candidates: list[IndexCandidate] = []
    skipped: list[SkippedRow] = []
    section: SheetSection | None = None
    default_section: SheetSection | None = None
    for row_number, row in enumerate(rows, start=1):
        normalized = _normalize_row(row)
        detected = _detect_section(normalized)
        if detected is not None:
            section = detected
            if not detected.is_chips:
                default_section = detected
            continue
        if _outside_range(row_number, from_row, to_row):
            continue
        if is_daily_separator(normalized):
            section = default_section
            continue
        if _is_marker_or_date_row(normalized):
            continue
        if section is None or not _row_has_any_value(normalized):
            continue
        if change_type_filter and _section_change_type(section) != change_type_filter:
            continue

        candidate, skip_reason = _candidate_from_row(
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            sheet_name=sheet_name,
            row_number=row_number,
            row=normalized,
            section=section,
            direction=direction,
            answer_type=answer_type,
        )
        if candidate is not None:
            candidates.append(candidate)
        elif skip_reason is not None:
            skipped.append(skip_reason)
    return candidates, skipped


async def index_candidates(
    *,
    repository: DraftRepository,
    api: Any,
    candidates: list[IndexCandidate],
    rows_to_execute: set[int],
    telegram_user_id: int,
    author: str,
    timezone_name: str,
    retry_config: Any,
) -> list[dict[str, Any]]:
    indexed: list[dict[str, Any]] = []
    now = datetime.now(ZoneInfo(timezone_name))
    submitted_at = utc_iso(now)
    value_updates: list[dict[str, Any]] = []
    planned: list[tuple[IndexCandidate, str]] = []
    for candidate in candidates:
        if candidate.row_number not in rows_to_execute:
            continue
        application_id = generate_application_id()
        planned.append((candidate, application_id))
        value_updates.extend(
            _technical_value_updates(
                candidate,
                application_id=application_id,
                submitted_at=now.strftime("%d.%m.%Y %H:%M"),
                author=author,
                telegram_user_id=telegram_user_id,
            )
        )

    if value_updates:
        execute_with_retry(
            lambda: api.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=planned[0][0].spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": value_updates},
            )
            .execute(),
            config=retry_config,
            operation_id=f"sheet-indexer-write:{planned[0][0].spreadsheet_id}",
        )

    for candidate, application_id in planned:
        dashboard_row = _dashboard_row(
            candidate,
            application_id=application_id,
            submitted_at=submitted_at,
            author=author,
        )
        await repository.index_submitted_application(
            application_id=application_id,
            telegram_user_id=telegram_user_id,
            spreadsheet_id=candidate.spreadsheet_id,
            sheet_id=candidate.sheet_id,
            sheet_name=candidate.sheet_name,
            last_known_status=candidate.status,
            direction=candidate.direction,
            answer_type=candidate.answer_type,
            application_type=ApplicationType.SINGLE.value,
            change_type=candidate.change_type,
            is_urgent=candidate.is_urgent,
            last_seen_row_number=candidate.row_number,
            last_seen_editor=candidate.editor or None,
            last_seen_editor_comment=_value_by_header(
                candidate.values,
                _headers_for_schema(candidate.schema),
                "Вопросы/комментарии редактора",
            )
            or None,
            last_seen_final_answer=_value_by_header(
                candidate.values,
                _headers_for_schema(candidate.schema),
                "Итоговый ответ редактора",
            )
            or None,
            submitted_at=submitted_at,
            dashboard_projection=dashboard_projection(dashboard_row),
        )
        indexed.append(
            {
                "application_id": application_id,
                "row_number": candidate.row_number,
                "schema": candidate.schema,
                "direction": candidate.direction,
                "answer_type": candidate.answer_type,
                "change_type": candidate.change_type,
                "row_link": candidate.row_link,
            }
        )
    return indexed


async def migrate_urgent_chips_layout(
    *,
    repository: DraftRepository,
    api: Any,
    spreadsheet_id: str,
    sheet_id: int,
    sheet_name: str,
    rows: list[list[Any]],
    execute: bool,
    retry_config: Any,
) -> dict[str, Any]:
    migration = plan_urgent_chips_layout_migration(
        spreadsheet_id=spreadsheet_id,
        sheet_id=sheet_id,
        sheet_name=sheet_name,
        rows=rows,
    )
    if execute and migration["migrated"]:
        execute_with_retry(
            lambda: api.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=f"{quote_sheet_name(sheet_name)}!A1:X{len(migration['rows'])}",
                valueInputOption="USER_ENTERED",
                body={"values": migration["rows"]},
            )
            .execute(),
            config=retry_config,
            operation_id=f"urgent-chips-layout-migration:{spreadsheet_id}:{sheet_name}",
        )
        if len(rows) > len(migration["rows"]):
            execute_with_retry(
                lambda: api.spreadsheets()
                .values()
                .clear(
                    spreadsheetId=spreadsheet_id,
                    range=(
                        f"{quote_sheet_name(sheet_name)}!"
                        f"A{len(migration['rows']) + 1}:X{len(rows)}"
                    ),
                    body={},
                )
                .execute(),
                config=retry_config,
                operation_id=(
                    f"urgent-chips-layout-migration-clear:{spreadsheet_id}:{sheet_name}"
                ),
            )
        await repository.update_application_tracking_batch(
            migration["tracking_updates"],
            dashboard_projections=migration["dashboard_projections"],
        )
    report = {
        "execute": execute,
        "spreadsheet_id": spreadsheet_id,
        "sheet_id": sheet_id,
        "sheet_name": sheet_name,
        "migrated_count": len(migration["migrated"]),
        "skipped_count": len(migration["skipped"]),
        "migrated": migration["migrated"],
        "skipped": migration["skipped"],
    }
    return report


def plan_urgent_chips_layout_migration(
    *,
    spreadsheet_id: str,
    sheet_id: int,
    sheet_name: str,
    rows: list[list[Any]],
) -> dict[str, Any]:
    global_marker_index = _legacy_global_chips_marker_index(rows)
    if global_marker_index is None:
        return {
            "rows": rows,
            "migrated": [],
            "skipped": [{"reason": "legacy_global_chips_section_not_found"}],
            "tracking_updates": [],
            "dashboard_projections": [],
        }
    top_rows = [list(row) for row in rows[:global_marker_index]]
    chips_rows = rows[global_marker_index + 2 :]
    chips_layout = {header: index for index, header in enumerate(CHIPS_WORKSHEET_HEADERS)}
    by_date: dict[str, list[tuple[int, list[Any]]]] = {}
    skipped: list[dict[str, Any]] = []
    active_date = ""
    for source_index, source_row in enumerate(chips_rows, start=global_marker_index + 3):
        row = _normalize_row(list(source_row))
        if is_daily_separator(row):
            active_date = row[0]
            continue
        if _detect_section(row) is not None or _is_marker_or_date_row(row):
            continue
        if not _row_has_any_value(row):
            continue
        application_id = _value(row, chips_layout, "ID заявки")
        if not application_id or application_id == "ID заявки":
            skipped.append({"source_row": source_index, "reason": "missing_application_id"})
            continue
        row_date = active_date or _date_label_from_technical_value(
            _value(row, chips_layout, "Дата заявки")
        )
        if not row_date:
            skipped.append(
                {
                    "source_row": source_index,
                    "application_id": application_id,
                    "reason": "date_not_found",
                }
            )
            continue
        by_date.setdefault(row_date, []).append((source_index, list(source_row)))

    migrated: list[dict[str, Any]] = []
    tracking_updates: list[dict[str, Any]] = []
    dashboard_projections: list[dict[str, Any]] = []
    for date_label, entries in by_date.items():
        date_row = _find_date_row(top_rows, date_label)
        if date_row is None:
            top_rows.append([date_label])
            date_row = len(top_rows)
        day_end = _day_end_row(top_rows, date_row)
        chips_marker = _chips_marker_row(top_rows, date_row, day_end)
        if chips_marker is None:
            insert_at = day_end - 1
            top_rows[insert_at:insert_at] = [
                [ChangeType.CHIPS.value],
                list(CHIPS_WORKSHEET_HEADERS),
            ]
            chips_marker = insert_at + 1
            day_end += 2
        insert_at = day_end - 1
        copied_rows = [list(row) for _, row in entries]
        top_rows[insert_at:insert_at] = copied_rows
        for offset, (source_row, row) in enumerate(entries):
            new_row_number = insert_at + offset + 1
            application_id = _value(
                _normalize_row(row),
                chips_layout,
                "ID заявки",
            )
            status = _value(_normalize_row(row), chips_layout, "Статус") or ApplicationStatus.NEW.value
            editor = _value(_normalize_row(row), chips_layout, "Редактор")
            editor_comment = _value(
                _normalize_row(row),
                chips_layout,
                "Вопросы/комментарии редактора",
            )
            row_link = spreadsheet_row_link(
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                row_number=new_row_number,
                end_column="U",
            )
            migrated.append(
                {
                    "application_id": application_id,
                    "source_row": source_row,
                    "target_row": new_row_number,
                    "date": date_label,
                    "row_link": row_link,
                }
            )
            tracking_updates.append(
                {
                    "application_id": application_id,
                    "spreadsheet_id": spreadsheet_id,
                    "sheet_id": sheet_id,
                    "sheet_name": sheet_name,
                    "last_known_status": status,
                    "last_seen_row_number": new_row_number,
                    "last_seen_editor": editor or None,
                    "last_seen_editor_comment": editor_comment or None,
                    "last_seen_final_answer": None,
                    "last_seen_scriptwriter_response": _value(
                        _normalize_row(row),
                        chips_layout,
                        "Ответ сценариста",
                    )
                    or None,
                    "change_type": ChangeType.CHIPS.value,
                }
            )
            dashboard_projections.append(
                {
                    "entity_type": "APPLICATION",
                    "entity_id": application_id,
                    "snapshot": dashboard_projection(
                        [
                            application_id,
                            "",
                            _value(_normalize_row(row), chips_layout, "Дата заявки"),
                            _value(_normalize_row(row), chips_layout, "Направление"),
                            _value(_normalize_row(row), chips_layout, "Тип заявки")
                            or ApplicationType.SINGLE.value,
                            _value(_normalize_row(row), chips_layout, "Тип ответа")
                            or AnswerType.URGENT.value,
                            _value(_normalize_row(row), chips_layout, "Срочная") or "Да",
                            _value(_normalize_row(row), chips_layout, "Автор заявки"),
                            status,
                            editor,
                            "Нет",
                            row_link,
                        ]
                    ),
                }
            )
    all_tracking_updates, all_dashboard_projections = _tracking_updates_for_sheet_rows(
        spreadsheet_id=spreadsheet_id,
        sheet_id=sheet_id,
        sheet_name=sheet_name,
        rows=top_rows,
    )
    return {
        "rows": top_rows,
        "migrated": migrated,
        "skipped": skipped,
        "tracking_updates": _merge_tracking_updates(
            all_tracking_updates,
            tracking_updates,
        ),
        "dashboard_projections": _merge_dashboard_projections(
            all_dashboard_projections,
            dashboard_projections,
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Index manually inserted Google Sheets rows into SQLite tracking."
    )
    parser.add_argument("--spreadsheet-id", required=True)
    parser.add_argument("--sheet-name", required=True)
    parser.add_argument("--direction", choices=[item.value for item in Direction])
    parser.add_argument(
        "--answer-type",
        choices=[item.value for item in AnswerType],
        help="By default inferred from sheet name: Срочные/Интеграции/Раскатка.",
    )
    parser.add_argument("--change-type", choices=[item.value for item in ChangeType])
    parser.add_argument("--from-row", type=int)
    parser.add_argument("--to-row", type=int)
    parser.add_argument("--rows", help="Comma-separated row numbers to execute.")
    parser.add_argument("--telegram-user-id", type=int)
    parser.add_argument("--allow-unknown-user", action="store_true")
    parser.add_argument("--author", default=DEFAULT_AUTHOR)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--migrate-urgent-chips-layout",
        action="store_true",
        help="Move legacy global urgent CHIPS rows into per-day CHIPS sections.",
    )
    parser.add_argument("--output-json", help="Optional path for JSON report.")
    return parser.parse_args(argv)


async def run(argv: list[str] | None = None) -> dict[str, Any]:
    args = parse_args(argv)
    settings = load_settings()
    repository = DraftRepository(settings.sqlite_path)
    await repository.init()
    api = build_google_sheets_api(settings.google_credentials_path)
    sheet_id, rows = _read_sheet(
        api,
        spreadsheet_id=args.spreadsheet_id,
        sheet_name=args.sheet_name,
        retry_config=settings.google_api_retry,
    )
    if args.migrate_urgent_chips_layout:
        report = await migrate_urgent_chips_layout(
            repository=repository,
            api=api,
            spreadsheet_id=args.spreadsheet_id,
            sheet_id=sheet_id,
            sheet_name=args.sheet_name,
            rows=rows,
            execute=args.execute,
            retry_config=settings.google_api_retry,
        )
        if args.output_json:
            with open(args.output_json, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
        return report
    direction = args.direction or _infer_direction(args.spreadsheet_id, settings)
    answer_type = args.answer_type or _infer_answer_type(args.sheet_name)
    candidates, skipped = find_index_candidates(
        spreadsheet_id=args.spreadsheet_id,
        sheet_id=sheet_id,
        sheet_name=args.sheet_name,
        rows=rows,
        direction=direction,
        answer_type=answer_type,
        change_type_filter=args.change_type,
        from_row=args.from_row,
        to_row=args.to_row,
    )
    selected_rows = _parse_rows(args.rows)
    indexed: list[dict[str, Any]] = []
    if args.execute:
        if args.telegram_user_id is None and not args.allow_unknown_user:
            raise SystemExit(
                "--execute requires --telegram-user-id or explicit --allow-unknown-user"
            )
        if not selected_rows:
            raise SystemExit("--execute requires explicit --rows")
        valid_rows = {candidate.row_number for candidate in candidates}
        unknown_rows = selected_rows - valid_rows
        if unknown_rows:
            raise SystemExit(f"Rows are not safe index candidates: {sorted(unknown_rows)}")
        indexed = await index_candidates(
            repository=repository,
            api=api,
            candidates=candidates,
            rows_to_execute=selected_rows,
            telegram_user_id=args.telegram_user_id or 0,
            author=args.author,
            timezone_name=settings.rollout_schedule.timezone_name or DEFAULT_TIMEZONE,
            retry_config=settings.google_api_retry,
        )
    report = {
        "execute": args.execute,
        "spreadsheet_id": args.spreadsheet_id,
        "sheet_name": args.sheet_name,
        "sheet_id": sheet_id,
        "direction": direction,
        "answer_type": answer_type,
        "candidate_count": len(candidates),
        "skipped_count": len(skipped),
        "indexed_count": len(indexed),
        "candidates": [_candidate_report(candidate) for candidate in candidates],
        "skipped": [_skipped_report(item) for item in skipped],
        "indexed": indexed,
    }
    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    report = asyncio.run(run())
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _read_sheet(
    api: Any,
    *,
    spreadsheet_id: str,
    sheet_name: str,
    retry_config: Any,
) -> tuple[int, list[list[Any]]]:
    metadata = execute_with_retry(
        lambda: api.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets(properties(sheetId,title))")
        .execute(),
        config=retry_config,
        operation_id=f"sheet-indexer-metadata:{spreadsheet_id}",
    )
    sheet_id: int | None = None
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("title") == sheet_name:
            sheet_id = int(properties["sheetId"])
            break
    if sheet_id is None:
        raise SystemExit(f"Sheet not found: {sheet_name}")
    values = execute_with_retry(
        lambda: api.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!{DEFAULT_INDEX_RANGE}",
            majorDimension="ROWS",
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute(),
        config=retry_config,
        operation_id=f"sheet-indexer-read:{spreadsheet_id}:{sheet_name}",
    )
    return sheet_id, values.get("values", [])


def _detect_section(row: list[str]) -> SheetSection | None:
    for schema, headers in (
        ("new", WORKSHEET_HEADERS),
        ("previous_new", PREVIOUS_WORKSHEET_HEADERS),
        ("current", CURRENT_WORKSHEET_HEADERS),
        ("legacy", LEGACY_WORKSHEET_HEADERS),
        ("chips", CHIPS_WORKSHEET_HEADERS),
        ("previous_chips", PREVIOUS_CHIPS_WORKSHEET_HEADERS),
    ):
        if row[: len(headers)] == headers:
            return SheetSection(
                schema=schema,
                headers=list(headers),
                header_row_number=0,
                column_count=len(headers),
            )
    return None


def _candidate_from_row(
    *,
    spreadsheet_id: str,
    sheet_id: int,
    sheet_name: str,
    row_number: int,
    row: list[str],
    section: SheetSection,
    direction: str,
    answer_type: str,
) -> tuple[IndexCandidate | None, SkippedRow | None]:
    headers = section.headers
    values = row + [""] * (len(headers) - len(row))
    layout = section.layout
    application_id = _value(values, layout, "ID заявки")
    if application_id:
        return None, None
    nonempty_tech = [
        header
        for header in _technical_headers(headers)
        if header != "ID заявки" and _value(values, layout, header)
    ]
    if nonempty_tech:
        return None, _skip(row_number, section.schema, "technical_fields_not_empty", values, headers)
    required = _required_headers(section)
    missing = [header for header in required if not _value(values, layout, header)]
    if missing:
        if any(_value(values, layout, header) for header in required):
            return None, _skip(
                row_number,
                section.schema,
                f"missing_required_fields:{','.join(missing)}",
                values,
                headers,
            )
        return None, None
    status = _value(values, layout, "Статус") or ApplicationStatus.NEW.value
    editor = _value(values, layout, "Редактор")
    intent = _value(values, layout, "Интент")
    reason = _value(values, layout, "Причина") or _value(
        values, layout, "Кейс или сообщения клиента"
    ) or _value(values, layout, "Причина изменений")
    change_type = _value(values, layout, "Тип изменения") or _section_change_type(section)
    is_urgent = answer_type == AnswerType.URGENT.value
    end_column = _column_letter(section.column_count)
    return (
        IndexCandidate(
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            sheet_name=sheet_name,
            row_number=row_number,
            schema=section.schema,
            direction=direction,
            answer_type=answer_type,
            change_type=change_type,
            is_urgent=is_urgent,
            status=status,
            editor=editor,
            scriptwriter=_value(values, layout, "Закрепленный сценарист"),
            intent=intent,
            reason=reason,
            row_link=spreadsheet_row_link(
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                row_number=row_number,
                end_column=end_column,
            ),
            values=values[: section.column_count],
        ),
        None,
    )


def _technical_value_updates(
    candidate: IndexCandidate,
    *,
    application_id: str,
    submitted_at: str,
    author: str,
    telegram_user_id: int,
) -> list[dict[str, Any]]:
    headers = _headers_for_schema(candidate.schema)
    values = {
        "ID заявки": application_id,
        "ID пачки": "",
        "Тип заявки": ApplicationType.SINGLE.value,
        "Дата заявки": submitted_at,
        "Направление": candidate.direction,
        "Тип ответа": candidate.answer_type,
        "Срочная": bool_to_sheet_value(candidate.is_urgent),
        "Автор заявки": author,
        "Telegram ID": telegram_user_id if telegram_user_id else "",
        "Тип изменения": candidate.change_type,
    }
    updates = []
    for header, value in values.items():
        if header not in headers:
            continue
        column = _column_letter(headers.index(header) + 1)
        updates.append(
            {
                "range": (
                    f"{quote_sheet_name(candidate.sheet_name)}!"
                    f"{column}{candidate.row_number}"
                ),
                "values": [[value]],
            }
        )
    return updates


def _dashboard_row(
    candidate: IndexCandidate,
    *,
    application_id: str,
    submitted_at: str,
    author: str,
) -> list[Any]:
    final_answer = _value_by_header(
        candidate.values,
        _headers_for_schema(candidate.schema),
        "Итоговый ответ редактора",
    )
    return [
        application_id,
        "",
        submitted_at,
        candidate.direction,
        ApplicationType.SINGLE.value,
        candidate.answer_type,
        bool_to_sheet_value(candidate.is_urgent),
        author,
        candidate.status,
        candidate.editor,
        bool_to_sheet_value(bool(final_answer.strip())),
        candidate.row_link,
    ][: len(DASHBOARD_HEADERS)]


def _candidate_report(candidate: IndexCandidate) -> dict[str, Any]:
    return {
        "row_number": candidate.row_number,
        "schema": candidate.schema,
        "direction": candidate.direction,
        "answer_type": candidate.answer_type,
        "change_type": candidate.change_type,
        "status": candidate.status,
        "scriptwriter": candidate.scriptwriter,
        "intent": candidate.intent,
        "reason": candidate.reason,
        "row_link": candidate.row_link,
    }


def _skipped_report(item: SkippedRow) -> dict[str, Any]:
    return {
        "row_number": item.row_number,
        "schema": item.schema,
        "reason": item.reason,
        "preview": item.preview,
    }


def _headers_for_schema(schema: str) -> list[str]:
    return {
        "new": WORKSHEET_HEADERS,
        "previous_new": PREVIOUS_WORKSHEET_HEADERS,
        "current": CURRENT_WORKSHEET_HEADERS,
        "legacy": LEGACY_WORKSHEET_HEADERS,
        "chips": CHIPS_WORKSHEET_HEADERS,
        "previous_chips": PREVIOUS_CHIPS_WORKSHEET_HEADERS,
    }[schema]


def _tracking_updates_for_sheet_rows(
    *,
    spreadsheet_id: str,
    sheet_id: int,
    sheet_name: str,
    rows: list[list[Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    updates: list[dict[str, Any]] = []
    projections: list[dict[str, Any]] = []
    section: SheetSection | None = None
    default_section: SheetSection | None = None
    for row_number, row in enumerate(rows, start=1):
        normalized = _normalize_row(row)
        detected = _detect_section(normalized)
        if detected is not None:
            section = detected
            if not detected.is_chips:
                default_section = detected
            continue
        if is_daily_separator(normalized):
            section = default_section
            continue
        if _is_marker_or_date_row(normalized) or section is None:
            continue
        layout = section.layout
        application_id = _value(normalized, layout, "ID заявки")
        if not application_id:
            continue
        status = _value(normalized, layout, "Статус") or ApplicationStatus.NEW.value
        editor = _value(normalized, layout, "Редактор")
        editor_comment = _value(
            normalized,
            layout,
            "Вопросы/комментарии редактора",
        )
        final_answer = _value(normalized, layout, "Итоговый ответ редактора")
        row_link = spreadsheet_row_link(
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            row_number=row_number,
            end_column="U" if section.is_chips else "X",
        )
        updates.append(
            {
                "application_id": application_id,
                "spreadsheet_id": spreadsheet_id,
                "sheet_id": sheet_id,
                "sheet_name": sheet_name,
                "last_known_status": status,
                "last_seen_row_number": row_number,
                "last_seen_editor": editor or None,
                "last_seen_editor_comment": editor_comment or None,
                "last_seen_final_answer": final_answer or None,
                "last_seen_scriptwriter_response": _value(
                    normalized,
                    layout,
                    "Ответ сценариста",
                )
                or None,
                "change_type": _value(normalized, layout, "Тип изменения")
                or (ChangeType.CHIPS.value if section.is_chips else None),
            }
        )
        projections.append(
            {
                "entity_type": "APPLICATION",
                "entity_id": application_id,
                "snapshot": dashboard_projection(
                    [
                        application_id,
                        _value(normalized, layout, "ID пачки"),
                        _value(normalized, layout, "Дата заявки"),
                        _value(normalized, layout, "Направление"),
                        _value(normalized, layout, "Тип заявки")
                        or ApplicationType.SINGLE.value,
                        _value(normalized, layout, "Тип ответа") or AnswerType.URGENT.value,
                        _value(normalized, layout, "Срочная"),
                        _value(normalized, layout, "Автор заявки"),
                        status,
                        editor,
                        "Нет" if section.is_chips else bool_to_sheet_value(bool(final_answer)),
                        row_link,
                    ]
                ),
            }
        )
    return updates, projections


def _merge_tracking_updates(
    base: list[dict[str, Any]],
    override: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = {item["application_id"]: item for item in base}
    for item in override:
        merged[item["application_id"]] = {**merged.get(item["application_id"], {}), **item}
    return list(merged.values())


def _merge_dashboard_projections(
    base: list[dict[str, Any]],
    override: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = {item["entity_id"]: item for item in base}
    for item in override:
        merged[item["entity_id"]] = item
    return list(merged.values())


def _legacy_global_chips_marker_index(rows: list[list[Any]]) -> int | None:
    for index, row in enumerate(rows[:-2]):
        if not _is_exact_marker(row, ChangeType.CHIPS.value):
            continue
        next_row = _normalize_row(rows[index + 1])
        if next_row[: len(CHIPS_WORKSHEET_HEADERS)] != CHIPS_WORKSHEET_HEADERS:
            continue
        following_rows = rows[index + 2 :]
        if any(is_daily_separator(_normalize_row(item)) for item in following_rows):
            return index
    return None


def _is_exact_marker(row: list[Any], marker: str) -> bool:
    normalized = _normalize_row(row)
    return bool(normalized) and normalized[0] == marker and all(
        not value for value in normalized[1:]
    )


def _date_label_from_technical_value(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    for fmt in ("%d.%m.%y", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.strftime("%d.%m.%y")
    return ""


def _find_date_row(rows: list[list[Any]], label: str) -> int | None:
    for row_number, row in enumerate(rows, start=1):
        if _normalize_row(row)[0:1] == [label] and is_daily_separator(_normalize_row(row)):
            return row_number
    return None


def _day_end_row(rows: list[list[Any]], date_row: int) -> int:
    next_date = next(
        (
            row_number
            for row_number in range(date_row + 1, len(rows) + 1)
            if is_daily_separator(_normalize_row(rows[row_number - 1]))
        ),
        None,
    )
    return next_date or len(rows) + 1


def _chips_marker_row(rows: list[list[Any]], date_row: int, day_end: int) -> int | None:
    for row_number in range(date_row + 1, min(day_end, len(rows) + 1)):
        if not _is_exact_marker(rows[row_number - 1], ChangeType.CHIPS.value):
            continue
        header_row_number = row_number + 1
        if header_row_number >= day_end or header_row_number > len(rows):
            return None
        header = _normalize_row(rows[header_row_number - 1])
        if header[: len(CHIPS_WORKSHEET_HEADERS)] == CHIPS_WORKSHEET_HEADERS:
            return row_number
    return None


def _required_headers(section: SheetSection) -> list[str]:
    if section.is_chips:
        return [
            "Закрепленный сценарист",
            "Причина",
            "Текст до чипса",
            "Текст чипса",
            "Текст после чипса",
            "Интент",
        ]
    reason_header = (
        "Кейс или сообщения клиента"
        if "Кейс или сообщения клиента" in section.headers
        else "Причина изменений"
    )
    return [
        "Закрепленный сценарист",
        reason_header,
        "Суть изменений",
        "Исходный текст",
        "Интент",
    ]


def _technical_headers(headers: list[str]) -> list[str]:
    return [
        header
        for header in (
            "ID заявки",
            "ID пачки",
            "Тип заявки",
            "Дата заявки",
            "Направление",
            "Тип ответа",
            "Срочная",
            "Автор заявки",
            "Telegram ID",
            "Исходная суть изменений",
            "LLM статус",
            "LLM оценка",
            "Тип изменения",
        )
        if header in headers
    ]


def _section_change_type(section: SheetSection) -> str:
    if section.is_chips:
        return ChangeType.CHIPS.value
    return ChangeType.ADD.value


def _infer_direction(spreadsheet_id: str, settings: Any) -> str:
    mapping = {
        settings.google_fl_spreadsheet_id: Direction.FL.value,
        settings.google_sme_spreadsheet_id: Direction.SME.value,
        settings.google_ai_spreadsheet_id: Direction.AI.value,
        settings.google_voice_collection_spreadsheet_id: Direction.VOICEBOT.value,
    }
    return mapping.get(spreadsheet_id, "")


def _infer_answer_type(sheet_name: str) -> str:
    if sheet_name == URGENT_SHEET_NAME or sheet_name.endswith(f" {URGENT_SHEET_NAME}"):
        return AnswerType.URGENT.value
    if sheet_name == INTEGRATION_SHEET_NAME or sheet_name.endswith(f" {INTEGRATION_SHEET_NAME}"):
        return AnswerType.INTEGRATION.value
    return AnswerType.ROLLOUT.value


def _parse_rows(value: str | None) -> set[int]:
    if not value:
        return set()
    return {int(item.strip()) for item in value.split(",") if item.strip()}


def _normalize_row(row: list[Any]) -> list[str]:
    return [str(value).strip() if value is not None else "" for value in row]


def _outside_range(row_number: int, from_row: int | None, to_row: int | None) -> bool:
    return (from_row is not None and row_number < from_row) or (
        to_row is not None and row_number > to_row
    )


def _row_has_any_value(row: list[str]) -> bool:
    return any(value.strip() for value in row)


def _is_marker_or_date_row(row: list[str]) -> bool:
    first = row[0].strip() if row else ""
    if first in {item.value for item in ChangeType} | {"CHIPS V2"}:
        return True
    return is_daily_separator(row)


def is_daily_separator(row: list[str]) -> bool:
    first = row[0].strip() if row else ""
    if len(first) == DATE_SEPARATOR_LENGTH:
        try:
            datetime.strptime(first, "%d.%m.%y")
        except ValueError:
            return False
        return all(not value.strip() for value in row[1:])
    return False


def _value(values: list[str], layout: dict[str, int], header: str) -> str:
    index = layout.get(header)
    if index is None or index >= len(values):
        return ""
    return str(values[index]).strip()


def _value_by_header(values: list[str], headers: list[str], header: str) -> str:
    if header not in headers:
        return ""
    index = headers.index(header)
    if index >= len(values):
        return ""
    return str(values[index]).strip()


def _skip(
    row_number: int,
    schema: str | None,
    reason: str,
    values: list[str],
    headers: list[str],
) -> SkippedRow:
    preview_headers = [
        "Закрепленный сценарист",
        "Статус",
        "Интент",
        "Кейс или сообщения клиента",
        "Причина",
        "Суть изменений",
        "Исходный текст",
        "ID заявки",
    ]
    preview = {
        header: _value_by_header(values, headers, header)
        for header in preview_headers
        if header in headers
    }
    return SkippedRow(row_number=row_number, schema=schema, reason=reason, preview=preview)


def _column_letter(column_number: int) -> str:
    result = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        result = chr(65 + remainder) + result
    return result


if __name__ == "__main__":
    main()
