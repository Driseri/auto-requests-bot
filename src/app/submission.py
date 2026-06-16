from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Protocol

from app.formatting import build_text_format_runs, deserialize_formatting_spans
from app.google_api import GoogleApiRetryConfig, execute_with_retry
from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
    BulkBatch,
    ChangeType,
    DashboardOutboxItem,
    Direction,
    Draft,
    SubmittedApplication,
    SubmissionResult,
)
from app.scheduling import (
    DEFAULT_TIMEZONE,
    DEFAULT_ROLLOUT_SCHEDULE,
    RolloutSchedule,
    rollout_sheet_name,
)
from app.sheet_dates import google_sheets_date_cell, utc_iso

LOGGER = logging.getLogger(__name__)
EDITOR_NOT_SELECTED = "Редактор не выбран"

LEGACY_WORKSHEET_HEADERS = [
    "ID заявки",
    "ID пачки",
    "Тип заявки",
    "Дата заявки",
    "Направление",
    "Тип ответа",
    "Срочная",
    "Автор заявки",
    "Telegram ID",
    "Статус",
    "Интент",
    "Закрепленный сценарист",
    "Причина изменений",
    "Суть изменений",
    "Исходная суть изменений",
    "Исходный текст",
    "Вопросы/комментарии редактора",
    "Ответ сценариста",
    "Итоговый ответ редактора",
    "LLM статус",
    "LLM оценка",
    "Тип изменения",
]
CURRENT_WORKSHEET_HEADERS = [
    *LEGACY_WORKSHEET_HEADERS[:10],
    "Редактор",
    *LEGACY_WORKSHEET_HEADERS[10:],
]
WORKSHEET_HEADERS = [
    "Закрепленный сценарист",
    "Интент",
    "Причина изменений",
    "Суть изменений",
    "Исходный текст",
    "Итоговый ответ редактора",
    "Комментарий качества",
    "Вопросы/комментарии редактора",
    "Ответ сценариста",
    "Статус",
    "Редактор",
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
]

LEGACY_DASHBOARD_HEADERS = [
    "ID заявки",
    "ID пачки",
    "Дата заявки",
    "Направление",
    "Тип заявки",
    "Тип ответа",
    "Срочная",
    "Автор заявки",
    "Статус",
    "Итоговый ответ есть",
    "Ссылка на рабочую строку",
]
DASHBOARD_HEADERS = [
    *LEGACY_DASHBOARD_HEADERS[:9],
    "Редактор",
    *LEGACY_DASHBOARD_HEADERS[9:],
]

BATCH_DASHBOARD_HEADERS = [
    "ID пачки",
    "Дата создания",
    "Направление",
    "Автор заявки",
    "Статус пачки",
    "Ссылка на пачку",
]

SHEET_HEADERS = WORKSHEET_HEADERS
LEGACY_SHEET_HEADERS = LEGACY_WORKSHEET_HEADERS
LEGACY_SHEET_COLUMN_COUNT = len(LEGACY_WORKSHEET_HEADERS)
CURRENT_SHEET_COLUMN_COUNT = len(CURRENT_WORKSHEET_HEADERS)
SHEET_COLUMN_COUNT = len(WORKSHEET_HEADERS)
DASHBOARD_SHEET_NAME = "Заявки"
BATCH_DASHBOARD_SHEET_NAME = "Пачки"
INTEGRATION_SHEET_NAME = "Интеграционные"
URGENT_SHEET_NAME = "Срочные"
ROLLOUT_SECTION_MARKERS = tuple(change_type.value for change_type in ChangeType)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


@dataclass(frozen=True, slots=True)
class DirectionSpreadsheetConfig:
    fl_spreadsheet_id: str
    sme_spreadsheet_id: str
    ai_spreadsheet_id: str
    voice_collection_spreadsheet_id: str

    def spreadsheet_id_for(self, direction: str | None) -> str:
        """Вернуть рабочую таблицу, являющуюся source of truth направления."""
        if direction == Direction.FL.value:
            return self.fl_spreadsheet_id
        if direction == Direction.SME.value:
            return self.sme_spreadsheet_id
        if direction == Direction.AI.value:
            return self.ai_spreadsheet_id
        if direction in {Direction.VOICEBOT.value, Direction.COLLECTION.value}:
            return self.voice_collection_spreadsheet_id
        return ""


class SubmissionServiceProtocol(Protocol):
    def resolve_target(self, application: Draft) -> tuple[str, str]:
        ...

    async def submit(self, application: Draft) -> SubmissionResult:
        ...


class InMemorySubmissionService:
    def __init__(self) -> None:
        self.submitted: list[Draft] = []

    def resolve_target(self, application: Draft) -> tuple[str, str]:
        return ("test-spreadsheet", application.submission_sheet_name or "01.06")

    async def submit(self, application: Draft) -> SubmissionResult:
        existing = next(
            (
                item
                for item in self.submitted
                if item.application_id == application.application_id
            ),
            None,
        )
        if existing is not None:
            row_number = self.submitted.index(existing) + 2
            return SubmissionResult(
                success=True,
                message="Заявка уже отправлена в таблицу.",
                spreadsheet_id="test-spreadsheet",
                sheet_id=100,
                sheet_name=application.submission_sheet_name or "01.06",
                row_number=row_number,
                row_link=spreadsheet_row_link(
                    spreadsheet_id="test-spreadsheet",
                    sheet_id=100,
                    row_number=row_number,
                    end_column="X",
                ),
            )
        self.submitted.append(application)
        return SubmissionResult(
            success=True,
            message="Заявка отправлена в таблицу.",
            spreadsheet_id="test-spreadsheet",
            sheet_id=100,
            sheet_name="01.06",
            row_number=len(self.submitted) + 1,
            row_link="https://docs.google.com/spreadsheets/d/test-spreadsheet/edit#gid=100&range=A2:V2",
        )


class SheetConfigurationError(RuntimeError):
    pass


class GoogleSheetsSubmissionService:
    """Идемпотентно записывает одиночные заявки в таблицы направлений."""

    def __init__(
        self,
        *,
        direction_spreadsheets: DirectionSpreadsheetConfig | None = None,
        dashboard_spreadsheet_id: str = "",
        credentials_path: str,
        sheets_api: Any | None = None,
        rollout_schedule: RolloutSchedule = DEFAULT_ROLLOUT_SCHEDULE,
        timezone_name: str = DEFAULT_TIMEZONE,
        clock: Callable[[], datetime] | None = None,
        application_editors: tuple[str, ...] = ("редактор 1", "редактор 2"),
        dashboard_sync: DashboardSyncService | None = None,
        google_api_retry: GoogleApiRetryConfig = GoogleApiRetryConfig(),
        # Legacy constructor args kept temporarily for tests/old wiring.
        spreadsheet_id: str = "",
        high_priority_sheet_name: str = "",
        low_priority_sheet_name: str = "",
    ) -> None:
        self.direction_spreadsheets = direction_spreadsheets or DirectionSpreadsheetConfig(
            fl_spreadsheet_id=spreadsheet_id,
            sme_spreadsheet_id=spreadsheet_id,
            ai_spreadsheet_id=spreadsheet_id,
            voice_collection_spreadsheet_id=spreadsheet_id,
        )
        self.dashboard_spreadsheet_id = dashboard_spreadsheet_id
        self.credentials_path = credentials_path
        self._sheets_api = sheets_api
        self._external_sheets_api = sheets_api is not None
        self.google_api_retry = google_api_retry
        self.rollout_schedule = rollout_schedule
        self.timezone_name = timezone_name
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.application_editors = application_editors
        self._prepared_sheets: dict[tuple[str, str], str] = {}
        self._sheet_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._dashboard = dashboard_sync or DashboardSyncService(
            spreadsheet_id=dashboard_spreadsheet_id,
            credentials_path=credentials_path,
            sheets_api=sheets_api,
            application_editors=application_editors,
            timezone_name=timezone_name,
        )

        if sheets_api is None and not Path(self.credentials_path).is_file():
            raise SheetConfigurationError(
                f"Google credentials file not found: {self.credentials_path}"
            )

    def resolve_target(self, application: Draft) -> tuple[str, str]:
        """Определить таблицу и вкладку с учетом направления и окна раскатки."""
        return self._resolve_target(application, submitted_at=self.clock())

    def _resolve_target(
        self,
        application: Draft,
        *,
        submitted_at: datetime,
    ) -> tuple[str, str]:
        spreadsheet_id = (
            application.submission_spreadsheet_id
            or self.direction_spreadsheets.spreadsheet_id_for(application.direction)
        )
        sheet_name = application.submission_sheet_name or target_sheet_name(
            application,
            submitted_at=submitted_at,
            rollout_schedule=self.rollout_schedule,
        )
        return spreadsheet_id, sheet_name

    async def submit(self, application: Draft) -> SubmissionResult:
        """Записать заявку под блокировкой листа с retry временных Google-сбоев."""
        submitted_at = self.clock()
        spreadsheet_id, sheet_name = self._resolve_target(
            application,
            submitted_at=submitted_at,
        )
        lock = self._sheet_locks.setdefault(
            (spreadsheet_id, sheet_name),
            asyncio.Lock(),
        )
        try:
            async with lock:
                result = await asyncio.to_thread(
                    execute_with_retry,
                    lambda: self._submit_sync(
                        application,
                        submitted_at,
                        spreadsheet_id=spreadsheet_id,
                        sheet_name=sheet_name,
                    ),
                    config=self.google_api_retry,
                    operation_id=f"submit:{application.application_id or 'unknown'}",
                    reset_client=self._reset_sheets_api,
                )
        except Exception as exc:
            return SubmissionResult(
                success=False,
                message=(
                    "Не удалось отправить заявку в Google-таблицу. "
                    f"Причина: {exc}"
                ),
            )
        return result

    def _reset_sheets_api(self) -> None:
        if self._external_sheets_api:
            return
        self._sheets_api = None
        if self._dashboard is not None:
            self._dashboard._sheets_api = None

    def _submit_sync(
        self,
        application: Draft,
        submitted_at: datetime,
        *,
        spreadsheet_id: str,
        sheet_name: str,
    ) -> SubmissionResult:
        """Сверить application_id и добавить строку только при его отсутствии."""
        if not spreadsheet_id:
            raise SheetConfigurationError(
                "Для выбранного направления не задан ID Google-таблицы."
            )
        api = self._get_sheets_api()

        use_sections = application.answer_type == AnswerType.ROLLOUT.value
        sheet_id, layout = self._ensure_sheet_ready(
            api,
            spreadsheet_id,
            sheet_name,
            use_sections=use_sections,
        )
        schema = layout.split(":", maxsplit=1)[1]
        existing_row = self._find_application_row(
            api,
            spreadsheet_id,
            sheet_name,
            application.application_id,
        )
        if existing_row is not None:
            return SubmissionResult(
                success=True,
                message="Заявка уже отправлена в таблицу.",
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                sheet_name=sheet_name,
                row_number=existing_row,
                row_link=spreadsheet_row_link(
                    spreadsheet_id=spreadsheet_id,
                    sheet_id=sheet_id,
                    row_number=existing_row,
                    end_column=_worksheet_schema_layout(schema)["end_column"],
                ),
            )
        row_data = _draft_to_row_data(
            application,
            submitted_at=submitted_at,
            timezone_name=self.timezone_name,
            application_editors=self.application_editors,
            schema=schema,
        )
        if layout.startswith("sectioned:"):
            row_number = self._insert_section_row(
                api,
                spreadsheet_id,
                sheet_id,
                sheet_name,
                ChangeType.normalize(application.change_type),
                row_data,
            )
        else:
            row_number = self._next_row_number(api, spreadsheet_id, sheet_name)
            api.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "appendCells": {
                                "sheetId": sheet_id,
                                "rows": [row_data],
                                "fields": (
                                    "userEnteredValue,dataValidation,"
                                    "userEnteredFormat,textFormatRuns"
                                ),
                            }
                        }
                    ]
                },
            ).execute()
        row_link = spreadsheet_row_link(
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            row_number=row_number,
            end_column=_worksheet_schema_layout(schema)["end_column"],
        )

        return SubmissionResult(
            success=True,
            message="Заявка отправлена в таблицу.",
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            sheet_name=sheet_name,
            row_number=row_number,
            row_link=row_link,
            submitted_at=utc_iso(submitted_at),
        )

    def _find_application_row(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_name: str,
        application_id: str | None,
    ) -> int | None:
        if not application_id:
            return None
        rows = self._read_rows(api, spreadsheet_id, sheet_name)
        id_column_index: int | None = None
        known_headers = (
            WORKSHEET_HEADERS,
            CURRENT_WORKSHEET_HEADERS,
            LEGACY_WORKSHEET_HEADERS,
        )
        for row_number, row in enumerate(rows, start=1):
            normalized = [str(value).strip() for value in row]
            matched_headers = next(
                (
                    headers
                    for headers in known_headers
                    if normalized[: len(headers)] == headers
                ),
                None,
            )
            if matched_headers is not None:
                id_column_index = matched_headers.index("ID заявки")
                continue
            if (
                id_column_index is not None
                and len(normalized) > id_column_index
                and normalized[id_column_index] == application_id
            ):
                return row_number
        return None

    def _ensure_sheet_ready(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_name: str,
        *,
        use_sections: bool,
    ) -> tuple[int, str]:
        """Создать или распознать поддерживаемую схему до записи данных."""
        cache_key = (spreadsheet_id, sheet_name)
        if cache_key in self._prepared_sheets:
            return (
                self._get_or_create_sheet_id(api, spreadsheet_id, sheet_name),
                self._prepared_sheets[cache_key],
            )

        sheet_id = self._get_or_create_sheet_id(api, spreadsheet_id, sheet_name)
        rows = self._read_rows(api, spreadsheet_id, sheet_name)
        if not rows:
            if use_sections:
                self._initialize_sectioned_sheet(api, spreadsheet_id, sheet_id, sheet_name)
                layout = "sectioned:new"
            else:
                self._initialize_empty_sheet(api, spreadsheet_id, sheet_id, sheet_name)
                layout = "flat:new"
        elif sectioned_schema := _sectioned_working_sheet_schema(rows):
            layout = f"sectioned:{sectioned_schema}"
        else:
            schema = _working_sheet_schema(rows[0])
            if schema is None:
                raise SheetConfigurationError(
                    "Структура колонок рабочей вкладки не совпадает с поддерживаемыми схемами."
                )
            layout = f"flat:{schema}"
        self._prepared_sheets[cache_key] = layout
        return sheet_id, layout

    def _get_or_create_sheet_id(self, api: Any, spreadsheet_id: str, sheet_name: str) -> int:
        metadata = api.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties",
        ).execute()
        for sheet in metadata.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("title") == sheet_name:
                return int(properties["sheetId"])

        add_result = api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": sheet_name,
                                "gridProperties": {
                                    "rowCount": 1000,
                                    "columnCount": SHEET_COLUMN_COUNT,
                                },
                            }
                        }
                    }
                ]
            },
        ).execute()
        return int(add_result["replies"][0]["addSheet"]["properties"]["sheetId"])

    def _read_header(self, api: Any, spreadsheet_id: str, sheet_name: str) -> list[Any]:
        result = api.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A1:X1",
            majorDimension="ROWS",
        ).execute()
        values = result.get("values", [])
        return values[0] if values else []

    def _read_rows(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_name: str,
    ) -> list[list[Any]]:
        result = api.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A:X",
            majorDimension="ROWS",
        ).execute()
        return result.get("values", [])

    def _initialize_empty_sheet(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_name: str,
    ) -> None:
        api.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A1:X1",
            valueInputOption="USER_ENTERED",
            body={"values": [WORKSHEET_HEADERS]},
        ).execute()

        api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": worksheet_formatting_requests(
                    sheet_id,
                    self.application_editors,
                )
            },
        ).execute()

    def _next_row_number(self, api: Any, spreadsheet_id: str, sheet_name: str) -> int:
        result = api.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A:A",
            majorDimension="ROWS",
        ).execute()
        return len(result.get("values", [])) + 1

    def _initialize_sectioned_sheet(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_name: str,
    ) -> None:
        rows: list[list[Any]] = []
        for marker in ROLLOUT_SECTION_MARKERS:
            rows.append([marker])
            rows.append(WORKSHEET_HEADERS)
        api.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A1:X6",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        ).execute()
        api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": sectioned_worksheet_formatting_requests(
                    sheet_id,
                    self.application_editors,
                )
            },
        ).execute()

    def _insert_section_row(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_name: str,
        change_type: ChangeType | None,
        row_data: dict[str, Any],
    ) -> int:
        if change_type is None:
            raise SheetConfigurationError(
                "Для раскатки не выбран тип изменения ADD, EDIT или CHIPS."
            )
        rows = self._read_rows(api, spreadsheet_id, sheet_name)
        if not rows:
            rows = [
                item
                for marker in ROLLOUT_SECTION_MARKERS
                for item in ([marker], WORKSHEET_HEADERS)
            ]
        marker_rows = {
            _cell(row, 0).strip(): index + 1
            for index, row in enumerate(rows)
            if _cell(row, 0).strip() in ROLLOUT_SECTION_MARKERS
        }
        if set(marker_rows) != set(ROLLOUT_SECTION_MARKERS):
            raise SheetConfigurationError(
                "В недельной вкладке повреждена структура секций ADD, EDIT и CHIPS."
            )

        marker_index = ROLLOUT_SECTION_MARKERS.index(change_type.value)
        if marker_index + 1 < len(ROLLOUT_SECTION_MARKERS):
            next_marker = ROLLOUT_SECTION_MARKERS[marker_index + 1]
            row_number = marker_rows[next_marker]
            insert_index = row_number - 1
            requests = [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": insert_index,
                            "endIndex": insert_index + 1,
                        },
                        "inheritFromBefore": True,
                    }
                },
                {
                    "updateCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": insert_index,
                            "endRowIndex": insert_index + 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(row_data["values"]),
                        },
                        "rows": [row_data],
                        "fields": (
                            "userEnteredValue,dataValidation,"
                            "userEnteredFormat,textFormatRuns"
                        ),
                    }
                },
            ]
        else:
            row_number = len(rows) + 1
            requests = [
                {
                    "appendCells": {
                        "sheetId": sheet_id,
                        "rows": [row_data],
                        "fields": (
                            "userEnteredValue,dataValidation,"
                            "userEnteredFormat,textFormatRuns"
                        ),
                    }
                }
            ]
        api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
        return row_number

    def _get_sheets_api(self) -> Any:
        if self._sheets_api is None:
            self._sheets_api = build_google_sheets_api(self.credentials_path)
            self._dashboard._sheets_api = self._sheets_api
        return self._sheets_api


class DashboardSyncService:
    """Поддерживает read-only дашборд как проекцию рабочих таблиц."""

    def __init__(
        self,
        *,
        spreadsheet_id: str,
        credentials_path: str,
        sheets_api: Any | None = None,
        application_editors: tuple[str, ...] = ("редактор 1", "редактор 2"),
        timezone_name: str = DEFAULT_TIMEZONE,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.credentials_path = credentials_path
        self._sheets_api = sheets_api
        self._external_sheets_api = sheets_api is not None
        self._prepared_sheets: set[str] = set()
        self._prepared_sheet_ids: dict[str, int] = {}
        self._lock = RLock()
        self.application_editors = application_editors
        self.timezone_name = timezone_name

    def upsert_application(
        self,
        *,
        application: Draft,
        status: str,
        row_link: str,
        final_answer_present: bool = False,
        submitted_at: datetime | str | None = None,
    ) -> None:
        """Создать или обновить строку одиночной заявки по application_id."""
        with self._lock:
            self._upsert_application(
                application=application,
                status=status,
                row_link=row_link,
                final_answer_present=final_answer_present,
                submitted_at=submitted_at,
            )

    def sync_projections(self, items: list[DashboardOutboxItem]) -> None:
        """Одним чтением и batchUpdate применить последние проекции outbox."""
        if not items or not self.spreadsheet_id:
            return
        with self._lock:
            self._sync_projections(items)

    def reset_client(self) -> None:
        if self._external_sheets_api:
            return
        with self._lock:
            self._sheets_api = None
            self._prepared_sheets.clear()
            self._prepared_sheet_ids.clear()

    def _sync_projections(self, items: list[DashboardOutboxItem]) -> None:
        api = self._get_sheets_api()
        sheet_id = self._ensure_dashboard_sheet(
            api,
            DASHBOARD_SHEET_NAME,
            DASHBOARD_HEADERS,
        )
        rows = self._read_rows(api, DASHBOARD_SHEET_NAME, "A:L")
        groups = _dashboard_row_groups(rows)
        requests: list[dict[str, Any]] = []
        projected_keys: set[tuple[str, str]] = set()

        for item in items:
            snapshot = json.loads(item.snapshot_json)
            projected = list(snapshot["row"])
            projected.extend([""] * (len(DASHBOARD_HEADERS) - len(projected)))
            key = _dashboard_entity_key(projected)
            if key is None:
                key = (
                    "batch" if item.entity_type == "BULK_BATCH" else "application",
                    item.entity_id,
                )
            projected_keys.add(key)
            matches = groups.get(key, [])
            if matches:
                canonical_row_number, canonical = matches[0]
                merged = _merge_dashboard_rows([row for _, row in matches])
                row = _apply_dashboard_projection(merged, projected)
                requests.append(
                    _dashboard_update_row_request(
                        sheet_id,
                        canonical_row_number,
                        row,
                        timezone_name=self.timezone_name,
                    )
                )
            else:
                requests.append(
                    {
                        "appendCells": {
                            "sheetId": sheet_id,
                            "rows": [
                                {
                                    "values": _dashboard_row_cells(
                                        projected,
                                        date_value=projected[2] or None,
                                        timezone_name=self.timezone_name,
                                    )
                                }
                            ],
                            "fields": (
                                "userEnteredValue,"
                                "userEnteredFormat.numberFormat"
                            ),
                        }
                    }
                )

        duplicate_rows: list[int] = []
        for key, matches in groups.items():
            if len(matches) < 2:
                continue
            canonical_row_number, _ = matches[0]
            if key not in projected_keys:
                merged = _merge_dashboard_rows([row for _, row in matches])
                requests.append(
                    _dashboard_update_row_request(
                        sheet_id,
                        canonical_row_number,
                        merged,
                        timezone_name=self.timezone_name,
                    )
                )
            duplicate_rows.extend(row_number for row_number, _ in matches[1:])

        requests.extend(
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": row_number - 1,
                        "endIndex": row_number,
                    }
                }
            }
            for row_number in sorted(duplicate_rows, reverse=True)
        )
        if requests:
            api.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": requests},
            ).execute()

    def _upsert_application(
        self,
        *,
        application: Draft,
        status: str,
        row_link: str,
        final_answer_present: bool = False,
        submitted_at: datetime | str | None = None,
    ) -> None:
        if not self.spreadsheet_id:
            return
        api = self._get_sheets_api()
        sheet_id = self._ensure_dashboard_sheet(api, DASHBOARD_SHEET_NAME, DASHBOARD_HEADERS)
        rows = self._read_dashboard_rows(api, sheet_id)
        date_value = submitted_at or application.created_at
        row = dashboard_row(
            application,
            status,
            final_answer_present,
            row_link,
            submitted_at=date_value,
        )
        row_number = _find_row_by_id(rows, application.application_id or "")
        if row_number is None:
            api.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "appendCells": {
                                "sheetId": sheet_id,
                                "rows": [
                                    {
                                        "values": _dashboard_row_cells(
                                            row,
                                            date_value=date_value,
                                            timezone_name=self.timezone_name,
                                        )
                                    }
                                ],
                                "fields": (
                                    "userEnteredValue,"
                                    "userEnteredFormat.numberFormat"
                                ),
                            }
                        }
                    ]
                },
            ).execute()
            return
        api.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"{quote_sheet_name(DASHBOARD_SHEET_NAME)}!A{row_number}:L{row_number}",
            valueInputOption="USER_ENTERED",
            body={"values": [row]},
        ).execute()

    def upsert_tracked_application(
        self,
        *,
        tracked: SubmittedApplication,
        current: Any,
        row_link: str,
    ) -> None:
        with self._lock:
            self._upsert_tracked_application(
                tracked=tracked,
                current=current,
                row_link=row_link,
            )

    def _upsert_tracked_application(
        self,
        *,
        tracked: SubmittedApplication,
        current: Any,
        row_link: str,
    ) -> None:
        if not self.spreadsheet_id:
            return
        api = self._get_sheets_api()
        sheet_id = self._ensure_dashboard_sheet(api, DASHBOARD_SHEET_NAME, DASHBOARD_HEADERS)
        rows = self._read_dashboard_rows(api, sheet_id)
        row_number = _find_row_by_id(rows, tracked.application_id)
        existing_row = rows[row_number - 1] if row_number is not None and row_number - 1 < len(rows) else []
        row = dashboard_tracked_row(
            tracked=tracked,
            current=current,
            row_link=row_link,
            existing_row=existing_row,
        )
        if row_number is None:
            date_value = tracked.submitted_at or tracked.created_at
            api.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "appendCells": {
                                "sheetId": sheet_id,
                                "rows": [
                                    {
                                        "values": _dashboard_row_cells(
                                            row,
                                            date_value=date_value,
                                            timezone_name=self.timezone_name,
                                        )
                                    }
                                ],
                                "fields": (
                                    "userEnteredValue,"
                                    "userEnteredFormat.numberFormat"
                                ),
                            }
                        }
                    ]
                },
            ).execute()
            return
        api.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"{quote_sheet_name(DASHBOARD_SHEET_NAME)}!A{row_number}:L{row_number}",
            valueInputOption="USER_ENTERED",
            body={"values": [row]},
        ).execute()

    def upsert_bulk_batch(
        self,
        *,
        batch: BulkBatch,
        status: str,
        row_link: str,
        final_answer_present: bool = False,
        editors: tuple[str, ...] = (),
    ) -> None:
        """Создать или обновить единственную строку пачки по batch_id."""
        with self._lock:
            self._upsert_bulk_batch(
                batch=batch,
                status=status,
                row_link=row_link,
                final_answer_present=final_answer_present,
                editors=editors,
            )

    def _upsert_bulk_batch(
        self,
        *,
        batch: BulkBatch,
        status: str,
        row_link: str,
        final_answer_present: bool = False,
        editors: tuple[str, ...] = (),
    ) -> None:
        if not self.spreadsheet_id:
            return
        api = self._get_sheets_api()
        sheet_id = self._ensure_dashboard_sheet(api, DASHBOARD_SHEET_NAME, DASHBOARD_HEADERS)
        rows = self._read_dashboard_rows(api, sheet_id)
        row_number = _find_row_by_batch_id(rows, batch.batch_id)
        existing_row = rows[row_number - 1] if row_number is not None and row_number - 1 < len(rows) else []
        row = dashboard_bulk_batch_row(
            batch=batch,
            status=status,
            row_link=row_link,
            final_answer_present=final_answer_present,
            editors=editors,
            existing_row=existing_row,
        )
        if row_number is None:
            date_value = batch.created_at
            api.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [
                        {
                            "appendCells": {
                                "sheetId": sheet_id,
                                "rows": [
                                    {
                                        "values": _dashboard_row_cells(
                                            row,
                                            date_value=date_value,
                                            timezone_name=self.timezone_name,
                                        )
                                    }
                                ],
                                "fields": (
                                    "userEnteredValue,"
                                    "userEnteredFormat.numberFormat"
                                ),
                            }
                        }
                    ]
                },
            ).execute()
            return
        api.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"{quote_sheet_name(DASHBOARD_SHEET_NAME)}!A{row_number}:L{row_number}",
            valueInputOption="USER_ENTERED",
            body={"values": [row]},
        ).execute()

    def _ensure_dashboard_sheet(self, api: Any, sheet_name: str, headers: list[str]) -> int:
        if sheet_name in self._prepared_sheets:
            return self._prepared_sheet_ids[sheet_name]
        sheet_id = self._get_or_create_sheet_id(api, sheet_name, len(headers))
        result = api.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A1:{_column_letter(len(headers))}1",
            majorDimension="ROWS",
        ).execute()
        values = result.get("values", [])
        current_headers = values[0] if values else []
        if not current_headers:
            api.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{quote_sheet_name(sheet_name)}!A1:{_column_letter(len(headers))}1",
                valueInputOption="USER_ENTERED",
                body={"values": [headers]},
            ).execute()
            api.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": dashboard_formatting_requests(
                        sheet_id,
                        len(headers),
                        self.application_editors,
                    )
                },
            ).execute()
        elif [str(value).strip() for value in current_headers[: len(headers)]] != headers:
            legacy_headers = [
                str(value).strip()
                for value in current_headers[: len(LEGACY_DASHBOARD_HEADERS)]
            ]
            if legacy_headers == LEGACY_DASHBOARD_HEADERS:
                raise SheetConfigurationError(
                    "Dashboard sheet uses old schema without «Редактор» column. "
                    "Insert column J before restarting dashboard sync."
                )
            repaired_headers = _repair_dashboard_headers(current_headers[: len(headers)])
            if repaired_headers == headers:
                api.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{quote_sheet_name(sheet_name)}!A1:{_column_letter(len(headers))}1",
                    valueInputOption="USER_ENTERED",
                    body={"values": [headers]},
                ).execute()
                api.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={"requests": [_dashboard_final_answer_note_request(sheet_id)]},
                ).execute()
            else:
                raise SheetConfigurationError(
                    f"Dashboard sheet {sheet_name} headers do not match the expected schema."
                )
        self._prepared_sheets.add(sheet_name)
        self._prepared_sheet_ids[sheet_name] = sheet_id
        return sheet_id

    def _get_or_create_sheet_id(self, api: Any, sheet_name: str, column_count: int) -> int:
        if sheet_name in self._prepared_sheet_ids:
            return self._prepared_sheet_ids[sheet_name]
        metadata = api.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            fields="sheets.properties",
        ).execute()
        for sheet in metadata.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("title") == sheet_name:
                sheet_id = int(properties["sheetId"])
                self._prepared_sheet_ids[sheet_name] = sheet_id
                return sheet_id
        result = api.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": sheet_name,
                                "gridProperties": {
                                    "rowCount": 1000,
                                    "columnCount": column_count,
                                },
                            }
                        }
                    }
                ]
            },
        ).execute()
        sheet_id = int(result["replies"][0]["addSheet"]["properties"]["sheetId"])
        self._prepared_sheet_ids[sheet_name] = sheet_id
        return sheet_id

    def _read_rows(self, api: Any, sheet_name: str, range_suffix: str) -> list[list[Any]]:
        result = api.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!{range_suffix}",
            majorDimension="ROWS",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
        return result.get("values", [])

    def _read_dashboard_rows(self, api: Any, sheet_id: int) -> list[list[Any]]:
        return self._read_rows(api, DASHBOARD_SHEET_NAME, "A:L")

    def _get_sheets_api(self) -> Any:
        if self._sheets_api is None:
            self._sheets_api = build_google_sheets_api(self.credentials_path)
        return self._sheets_api


def build_google_sheets_api(credentials_path: str) -> Any:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def target_sheet_name(
    draft: Draft,
    *,
    submitted_at: datetime | None = None,
    rollout_schedule: RolloutSchedule = DEFAULT_ROLLOUT_SCHEDULE,
) -> str:
    """Выбрать вкладку по типу ответа, срочности, времени и направлению."""
    direction = draft.direction or ""
    if draft.answer_type == AnswerType.INTEGRATION.value:
        if direction in {Direction.VOICEBOT.value, Direction.COLLECTION.value}:
            return f"{direction} {INTEGRATION_SHEET_NAME}"
        return INTEGRATION_SHEET_NAME

    if draft.is_urgent:
        if direction in {Direction.VOICEBOT.value, Direction.COLLECTION.value}:
            return f"{direction} {URGENT_SHEET_NAME}"
        return URGENT_SHEET_NAME

    if draft.answer_type == AnswerType.ROLLOUT.value:
        week_name = rollout_sheet_name(
            submitted_at or datetime.now(timezone.utc),
            rollout_schedule,
        )
        if direction in {Direction.VOICEBOT.value, Direction.COLLECTION.value}:
            return f"{direction} {week_name}"
        return week_name

    week_name = week_sheet_name(draft.created_at)
    if direction in {Direction.VOICEBOT.value, Direction.COLLECTION.value}:
        return f"{direction} {week_name}"
    return week_name


def week_sheet_name(created_at: str | None = None) -> str:
    moment = _parse_datetime(created_at) if created_at else datetime.now(timezone.utc)
    monday = moment.date() - timedelta(days=moment.weekday())
    return monday.strftime("%d.%m")


def draft_to_sheet_row(
    draft: Draft,
    *,
    batch_id: str = "",
    submitted_at: datetime | str | None = None,
) -> list[Any]:
    return [
        draft.scriptwriter or "",
        draft.intent or "",
        draft.reason or "",
        draft.formatted_change_description or draft.raw_change_description or "",
        draft.source_text or "",
        "",
        "",
        "",
        "",
        ApplicationStatus.NEW.value,
        EDITOR_NOT_SELECTED,
        draft.application_id or "",
        batch_id,
        draft.application_type or ApplicationType.SINGLE.value,
        submitted_at or draft.created_at,
        draft.direction or "",
        draft.answer_type or "",
        bool_to_sheet_value(draft.is_urgent),
        draft.author_name or draft.scriptwriter or "",
        draft.telegram_user_id,
        draft.raw_change_description or "",
        draft.llm_check_status or "",
        draft.llm_score if draft.llm_score is not None else "",
        draft.change_type or "",
    ]


def _current_draft_to_sheet_row(
    draft: Draft,
    *,
    batch_id: str = "",
    submitted_at: datetime | str | None = None,
) -> list[Any]:
    return [
        draft.application_id or "",
        batch_id,
        draft.application_type or ApplicationType.SINGLE.value,
        submitted_at or draft.created_at,
        draft.direction or "",
        draft.answer_type or "",
        bool_to_sheet_value(draft.is_urgent),
        draft.author_name or draft.scriptwriter or "",
        draft.telegram_user_id,
        ApplicationStatus.NEW.value,
        EDITOR_NOT_SELECTED,
        draft.intent or "",
        draft.scriptwriter or "",
        draft.reason or "",
        draft.formatted_change_description or draft.raw_change_description or "",
        draft.raw_change_description or "",
        draft.source_text or "",
        "",
        "",
        "",
        draft.llm_check_status or "",
        draft.llm_score if draft.llm_score is not None else "",
        draft.change_type or "",
    ]


def _legacy_draft_to_sheet_row(
    draft: Draft,
    *,
    batch_id: str = "",
    submitted_at: datetime | str | None = None,
) -> list[Any]:
    row = _current_draft_to_sheet_row(
        draft,
        batch_id=batch_id,
        submitted_at=submitted_at,
    )
    del row[10]
    return row


def dashboard_row(
    draft: Draft,
    status: str,
    final_answer_present: bool,
    row_link: str,
    *,
    submitted_at: datetime | str | None = None,
) -> list[Any]:
    return [
        draft.application_id or "",
        "",
        submitted_at or draft.created_at,
        draft.direction or "",
        draft.application_type or ApplicationType.SINGLE.value,
        draft.answer_type or "",
        bool_to_sheet_value(draft.is_urgent),
        draft.author_name or draft.scriptwriter or "",
        status,
        EDITOR_NOT_SELECTED,
        "Да" if final_answer_present else "Нет",
        row_link,
    ]


def dashboard_tracked_row(
    *,
    tracked: SubmittedApplication,
    current: Any,
    row_link: str,
    existing_row: list[Any] | None = None,
) -> list[Any]:
    existing = list(existing_row or [])
    existing.extend([""] * (len(DASHBOARD_HEADERS) - len(existing)))
    urgent_value = current.is_urgent if current.is_urgent is not None else tracked.is_urgent
    return [
        tracked.application_id,
        current.batch_id or tracked.batch_id or existing[1],
        existing[2] or tracked.submitted_at or tracked.created_at,
        current.direction or tracked.direction or existing[3],
        tracked.application_type or existing[4],
        current.answer_type or tracked.answer_type or existing[5],
        bool_to_sheet_value(urgent_value) if urgent_value is not None else _repair_sheet_bool(existing[6]),
        existing[7],
        current.status or tracked.last_known_status,
        getattr(current, "editor", "")
        or tracked.last_seen_editor
        or existing[9]
        or EDITOR_NOT_SELECTED,
        bool_to_sheet_value(bool(current.final_answer)),
        row_link,
    ]


def dashboard_bulk_batch_row(
    *,
    batch: BulkBatch,
    status: str,
    row_link: str,
    final_answer_present: bool,
    editors: tuple[str, ...] = (),
    existing_row: list[Any] | None = None,
) -> list[Any]:
    existing = list(existing_row or [])
    existing.extend([""] * (len(DASHBOARD_HEADERS) - len(existing)))
    selected_editors = {
        editor
        for editor in editors
        if editor and editor != EDITOR_NOT_SELECTED
    }
    if len(selected_editors) > 1:
        editor_value = "Несколько редакторов"
    elif selected_editors:
        editor_value = next(iter(selected_editors))
    else:
        editor_value = EDITOR_NOT_SELECTED
    return [
        "\u041f\u0430\u0447\u043a\u0430",
        batch.batch_id,
        existing[2] or batch.created_at,
        batch.direction or existing[3],
        ApplicationType.BULK.value,
        existing[5],
        _repair_sheet_bool(existing[6]),
        existing[7] or f"Telegram {batch.telegram_user_id}",
        status or batch.last_known_batch_status,
        editor_value,
        bool_to_sheet_value(final_answer_present or _sheet_value_is_yes(existing[10])),
        row_link,
    ]


def bool_to_sheet_value(value: bool | None) -> str:
    return "\u0414\u0430" if value else "\u041d\u0435\u0442"


def _sheet_value_is_yes(value: Any) -> bool:
    yes = "\u0414\u0430"
    return _repair_sheet_bool(value).strip().lower() in {yes.lower(), "yes", "true", "1"}


def _repair_sheet_bool(value: Any) -> str:
    text = str(value or "").strip()
    normalized_candidates = {
        candidate.casefold()
        for candidate in _mojibake_candidates(text)
    }
    if normalized_candidates & {"да", "yes", "true", "1"} or text == "??":
        return "Да"
    if normalized_candidates & {"нет", "no", "false", "0"} or text == "???":
        return "Нет"
    return text


def _repair_dashboard_headers(values: list[Any]) -> list[str]:
    if len(values) != len(DASHBOARD_HEADERS):
        return [str(value or "").strip() for value in values]

    repaired: list[str] = []
    for value, expected in zip(values, DASHBOARD_HEADERS, strict=True):
        text = str(value or "").strip()
        if expected in _mojibake_candidates(text) or _is_question_mark_fingerprint(text, expected):
            repaired.append(expected)
        else:
            repaired.append(text)
    return repaired


def _mojibake_candidates(text: str) -> set[str]:
    candidates = {text}
    frontier = {text}
    for _ in range(2):
        next_frontier: set[str] = set()
        for candidate in frontier:
            for encoding in ("cp1251", "latin1"):
                try:
                    repaired = candidate.encode(encoding).decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    continue
                if repaired not in candidates:
                    candidates.add(repaired)
                    next_frontier.add(repaired)
        frontier = next_frontier
        if not frontier:
            break
    return candidates


def _is_question_mark_fingerprint(value: str, expected: str) -> bool:
    if "?" not in value:
        return False
    if any(character not in {"?", " ", "/", "I", "D", "L", "M"} for character in value):
        return False
    if len(value) != len(expected):
        return False
    return all(
        actual == wanted
        or (actual == "?" and wanted.isalpha())
        for actual, wanted in zip(value, expected, strict=True)
    )


def _draft_to_row_data(
    draft: Draft,
    *,
    batch_id: str = "",
    application_editors: tuple[str, ...] = (),
    schema: str = "new",
    submitted_at: datetime | str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    layout = _worksheet_schema_layout(schema)
    if schema == "new":
        row = draft_to_sheet_row(
            draft,
            batch_id=batch_id,
            submitted_at=submitted_at,
        )
    elif schema == "current":
        row = _current_draft_to_sheet_row(
            draft,
            batch_id=batch_id,
            submitted_at=submitted_at,
        )
    else:
        row = _legacy_draft_to_sheet_row(
            draft,
            batch_id=batch_id,
            submitted_at=submitted_at,
        )
    cells: list[dict[str, Any]] = []
    for index, value in enumerate(row):
        if index == layout["telegram_id"]:
            cells.append(_number_cell_data(value))
        elif index == layout["date"]:
            cells.append(
                google_sheets_date_cell(
                    value,
                    timezone_name=timezone_name,
                )
            )
        elif index == layout["status"]:
            cells.append(_status_cell_data(ApplicationStatus.NEW.value))
        elif index == layout["source_text"]:
            cells.append(_source_text_cell_data(draft))
        elif index == layout["editor"]:
            cells.append(_editor_cell_data(value, application_editors))
        elif index == layout["llm_score"] and value != "":
            cells.append(_number_cell_data(value))
        else:
            cells.append(_cell_data(value))
    return {"values": cells}


def _worksheet_schema_layout(schema: str) -> dict[str, Any]:
    layouts = {
        "new": {
            "date": 14,
            "status": 9,
            "editor": 10,
            "source_text": 4,
            "telegram_id": 19,
            "llm_score": 22,
            "end_column": "X",
        },
        "current": {
            "date": 3,
            "status": 9,
            "editor": 10,
            "source_text": 16,
            "telegram_id": 8,
            "llm_score": 21,
            "end_column": "W",
        },
        "legacy": {
            "date": 3,
            "status": 9,
            "editor": -1,
            "source_text": 15,
            "telegram_id": 8,
            "llm_score": 20,
            "end_column": "V",
        },
    }
    return layouts[schema]


def _status_cell_data(status: str) -> dict[str, Any]:
    return {
        "userEnteredValue": {"stringValue": status},
        "dataValidation": _status_data_validation_rule(),
        "userEnteredFormat": {
            "backgroundColor": _status_color(status),
            "textFormat": {"bold": True},
        },
    }


def _editor_cell_data(
    editor: str = EDITOR_NOT_SELECTED,
    application_editors: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "userEnteredValue": {"stringValue": editor or EDITOR_NOT_SELECTED},
        "dataValidation": _editor_data_validation_rule(application_editors),
    }


def _source_text_cell_data(draft: Draft) -> dict[str, Any]:
    cell = _cell_data(draft.source_text or "")
    cell["userEnteredFormat"] = {"wrapStrategy": "CLIP"}
    runs = build_text_format_runs(
        draft.source_text or "",
        deserialize_formatting_spans(draft.source_text_formatting_json),
    )
    if runs:
        cell["textFormatRuns"] = runs
    return cell


def _cell_data(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _number_cell_data(value)
    return {"userEnteredValue": {"stringValue": str(value)}}


def _dashboard_row_cells(
    row: list[Any],
    *,
    date_value: datetime | str | None,
    timezone_name: str,
) -> list[dict[str, Any]]:
    cells = [_cell_data(value) for value in row]
    if isinstance(date_value, (int, float)):
        cells[2] = {
            "userEnteredValue": {"numberValue": date_value},
            "userEnteredFormat": {
                "numberFormat": {
                    "type": "DATE_TIME",
                    "pattern": "dd.MM.yyyy hh:mm",
                }
            },
        }
    elif date_value:
        cells[2] = google_sheets_date_cell(date_value, timezone_name=timezone_name)
    return cells


def dashboard_projection(row: list[Any]) -> dict[str, Any]:
    return {"row": list(row[: len(DASHBOARD_HEADERS)])}


def _dashboard_entity_key(row: list[Any]) -> tuple[str, str] | None:
    application_id = _cell(row, 0).strip()
    batch_id = _cell(row, 1).strip()
    if batch_id and application_id in {"", "Пачка"}:
        return "batch", batch_id
    if application_id:
        return "application", application_id
    return None


def _dashboard_row_groups(
    rows: list[list[Any]],
) -> dict[tuple[str, str], list[tuple[int, list[Any]]]]:
    groups: dict[tuple[str, str], list[tuple[int, list[Any]]]] = {}
    for row_number, row in enumerate(rows[1:], start=2):
        key = _dashboard_entity_key(row)
        if key is not None:
            groups.setdefault(key, []).append((row_number, list(row)))
    return groups


def _merge_dashboard_rows(rows: list[list[Any]]) -> list[Any]:
    normalized = [list(row) + [""] * (len(DASHBOARD_HEADERS) - len(row)) for row in rows]
    merged = ["" for _ in DASHBOARD_HEADERS]
    for index in (0, 1, 2, 3, 4, 5, 6, 7, 11):
        merged[index] = next(
            (row[index] for row in normalized if str(row[index]).strip()),
            "",
        )
    for index in (8, 9):
        merged[index] = next(
            (row[index] for row in reversed(normalized) if str(row[index]).strip()),
            "",
        )
    merged[10] = (
        "Да"
        if any(_sheet_value_is_yes(row[10]) for row in normalized)
        else next(
            (row[10] for row in reversed(normalized) if str(row[10]).strip()),
            "Нет",
        )
    )
    return merged


def _apply_dashboard_projection(existing: list[Any], projected: list[Any]) -> list[Any]:
    result = list(existing) + [""] * (len(DASHBOARD_HEADERS) - len(existing))
    for index, value in enumerate(projected[: len(DASHBOARD_HEADERS)]):
        if index == 10:
            result[index] = bool_to_sheet_value(
                _sheet_value_is_yes(result[index]) or _sheet_value_is_yes(value)
            )
            continue
        if value not in (None, "") or index in {8, 9, 10, 11}:
            result[index] = value
    return result


def _dashboard_update_row_request(
    sheet_id: int,
    row_number: int,
    row: list[Any],
    *,
    timezone_name: str,
) -> dict[str, Any]:
    return {
        "updateCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": row_number - 1,
                "endRowIndex": row_number,
                "startColumnIndex": 0,
                "endColumnIndex": len(DASHBOARD_HEADERS),
            },
            "rows": [
                {
                    "values": _dashboard_row_cells(
                        row,
                        date_value=row[2] or None,
                        timezone_name=timezone_name,
                    )
                }
            ],
            "fields": "userEnteredValue,userEnteredFormat.numberFormat",
        }
    }


def _number_cell_data(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    return {"userEnteredValue": {"numberValue": value}}


def quote_sheet_name(sheet_name: str) -> str:
    return "'" + sheet_name.replace("'", "''") + "'"


def spreadsheet_row_link(
    *,
    spreadsheet_id: str,
    sheet_id: int,
    row_number: int,
    end_column: str,
) -> str:
    return (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        f"#gid={sheet_id}&range=A{row_number}:{end_column}{row_number}"
    )


def worksheet_formatting_requests(
    sheet_id: int,
    application_editors: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = [
        _freeze_first_row_request(sheet_id),
        _header_format_request(sheet_id, SHEET_COLUMN_COUNT),
        _basic_filter_request(sheet_id, SHEET_COLUMN_COUNT),
        _column_width_request(sheet_id, 0, SHEET_COLUMN_COUNT, 170),
        _column_width_request(sheet_id, 10, 19, 300),
        _status_dropdown_request(sheet_id, status_column_index=9),
        _editor_dropdown_request(
            sheet_id,
            editor_column_index=10,
            application_editors=application_editors,
        ),
        _right_border_request(sheet_id, column_index=10),
    ]
    requests.extend(_status_conditional_formatting_requests(sheet_id, status_column_index=9))
    requests.append(_urgent_conditional_formatting_request(sheet_id))
    return requests


def sectioned_worksheet_formatting_requests(
    sheet_id: int,
    application_editors: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = [
        _column_width_request(sheet_id, 0, SHEET_COLUMN_COUNT, 170),
        _column_width_request(sheet_id, 10, 19, 300),
        _right_border_request(sheet_id, column_index=10),
    ]
    for marker_row_index in (0, 2, 4):
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": marker_row_index,
                        "endRowIndex": marker_row_index + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": SHEET_COLUMN_COUNT,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {
                                "red": 0.82,
                                "green": 0.86,
                                "blue": 0.91,
                            },
                            "textFormat": {"bold": True},
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            }
        )
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": marker_row_index + 1,
                        "endRowIndex": marker_row_index + 2,
                        "startColumnIndex": 0,
                        "endColumnIndex": SHEET_COLUMN_COUNT,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {
                                "red": 0.94,
                                "green": 0.94,
                                "blue": 0.94,
                            },
                            "horizontalAlignment": "CENTER",
                            "textFormat": {"bold": True},
                            "wrapStrategy": "WRAP",
                        }
                    },
                    "fields": (
                        "userEnteredFormat(backgroundColor,horizontalAlignment,"
                        "textFormat,wrapStrategy)"
                    ),
                }
            }
        )
    requests.extend(_status_conditional_formatting_requests(sheet_id, status_column_index=9))
    requests.append(_urgent_conditional_formatting_request(sheet_id))
    return requests


def dashboard_formatting_requests(
    sheet_id: int,
    column_count: int,
    application_editors: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    return [
        _freeze_first_row_request(sheet_id),
        _header_format_request(sheet_id, column_count),
        _dashboard_final_answer_note_request(sheet_id),
        _basic_filter_request(sheet_id, column_count),
        _column_width_request(sheet_id, 0, column_count, 180),
        _urgent_dashboard_conditional_formatting_request(sheet_id),
    ]


def _freeze_first_row_request(sheet_id: int) -> dict[str, Any]:
    return {
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }
    }


def _header_format_request(sheet_id: int, column_count: int) -> dict[str, Any]:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": column_count,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.94},
                    "horizontalAlignment": "CENTER",
                    "textFormat": {"bold": True},
                    "wrapStrategy": "WRAP",
                }
            },
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat,wrapStrategy)",
        }
    }


def _dashboard_final_answer_note_request(sheet_id: int) -> dict[str, Any]:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 10,
                "endColumnIndex": 11,
            },
            "cell": {
                "note": (
                    "Да, если есть итоговый ответ редактора: для одиночной заявки "
                    "в рабочей строке, для пачки хотя бы в одной строке пачки."
                )
            },
            "fields": "note",
        }
    }


def _basic_filter_request(sheet_id: int, column_count: int) -> dict[str, Any]:
    return {
        "setBasicFilter": {
            "filter": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "startColumnIndex": 0,
                    "endColumnIndex": column_count,
                }
            }
        }
    }


def _column_width_request(
    sheet_id: int,
    start_index: int,
    end_index: int,
    pixel_size: int,
) -> dict[str, Any]:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": start_index,
                "endIndex": end_index,
            },
            "properties": {"pixelSize": pixel_size},
            "fields": "pixelSize",
        }
    }


def _right_border_request(sheet_id: int, *, column_index: int) -> dict[str, Any]:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startColumnIndex": column_index,
                "endColumnIndex": column_index + 1,
            },
            "cell": {
                "userEnteredFormat": {
                    "borders": {
                        "right": {
                            "style": "SOLID_THICK",
                            "color": {"red": 0.35, "green": 0.35, "blue": 0.35},
                        }
                    }
                }
            },
            "fields": "userEnteredFormat.borders.right",
        }
    }


def _status_dropdown_request(
    sheet_id: int,
    *,
    status_column_index: int,
    start_row_index: int = 1,
    end_row_index: int | None = None,
) -> dict[str, Any]:
    range_payload = {
        "sheetId": sheet_id,
        "startRowIndex": start_row_index,
        "startColumnIndex": status_column_index,
        "endColumnIndex": status_column_index + 1,
    }
    if end_row_index is not None:
        range_payload["endRowIndex"] = end_row_index
    return {
        "setDataValidation": {
            "range": range_payload,
            "rule": _status_data_validation_rule(),
        }
    }


def _status_data_validation_rule() -> dict[str, Any]:
    return {
        "condition": {
            "type": "ONE_OF_LIST",
            "values": [{"userEnteredValue": status.value} for status in ApplicationStatus],
        },
        "strict": True,
        "showCustomUi": True,
    }


def _editor_dropdown_request(
    sheet_id: int,
    *,
    editor_column_index: int,
    application_editors: tuple[str, ...],
    start_row_index: int = 1,
    end_row_index: int | None = None,
) -> dict[str, Any]:
    range_payload = {
        "sheetId": sheet_id,
        "startRowIndex": start_row_index,
        "startColumnIndex": editor_column_index,
        "endColumnIndex": editor_column_index + 1,
    }
    if end_row_index is not None:
        range_payload["endRowIndex"] = end_row_index
    return {
        "setDataValidation": {
            "range": range_payload,
            "rule": _editor_data_validation_rule(application_editors),
        }
    }


def _editor_data_validation_rule(
    application_editors: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "condition": {
            "type": "ONE_OF_LIST",
            "values": [
                {"userEnteredValue": value}
                for value in (EDITOR_NOT_SELECTED, *application_editors)
            ],
        },
        "strict": True,
        "showCustomUi": True,
    }


def _status_conditional_formatting_requests(
    sheet_id: int,
    *,
    status_column_index: int,
) -> list[dict[str, Any]]:
    requests = []
    status_column_letter = _column_letter(status_column_index + 1)
    for index, status in enumerate(ApplicationStatus):
        requests.append(
            {
                "addConditionalFormatRule": {
                    "index": index,
                    "rule": {
                        "ranges": [
                            {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "startColumnIndex": status_column_index,
                                "endColumnIndex": status_column_index + 1,
                            }
                        ],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [
                                    {
                                        "userEnteredValue": (
                                            f'=${status_column_letter}2="{status.value}"'
                                        )
                                    }
                                ],
                            },
                            "format": {"backgroundColor": _status_color(status.value)},
                        },
                    },
                }
            }
        )
    return requests


def _urgent_conditional_formatting_request(sheet_id: int) -> dict[str, Any]:
    return {
        "addConditionalFormatRule": {
            "index": 100,
            "rule": {
                "ranges": [{"sheetId": sheet_id, "startRowIndex": 1}],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": '=$R2="Да"'}],
                    },
                    "format": {"backgroundColor": {"red": 1.0, "green": 0.90, "blue": 0.82}},
                },
            },
        }
    }


def _urgent_dashboard_conditional_formatting_request(sheet_id: int) -> dict[str, Any]:
    return {
        "addConditionalFormatRule": {
            "index": 100,
            "rule": {
                "ranges": [{"sheetId": sheet_id, "startRowIndex": 1}],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": '=$G2="Да"'}],
                    },
                    "format": {"backgroundColor": {"red": 1.0, "green": 0.90, "blue": 0.82}},
                },
            },
        }
    }


def _status_color(status: str) -> dict[str, float]:
    colors = {
        ApplicationStatus.NEW.value: {"red": 0.78, "green": 0.86, "blue": 1.0},
        ApplicationStatus.IN_PROGRESS.value: {"red": 1.0, "green": 0.92, "blue": 0.60},
        ApplicationStatus.NEEDS_CLARIFICATION.value: {
            "red": 1.0,
            "green": 0.80,
            "blue": 0.55,
        },
        ApplicationStatus.FINAL_ANSWER_READY.value: {
            "red": 0.88,
            "green": 0.78,
            "blue": 1.0,
        },
        ApplicationStatus.ACCEPTED.value: {"red": 0.75, "green": 0.92, "blue": 0.75},
        ApplicationStatus.REJECTED.value: {"red": 1.0, "green": 0.75, "blue": 0.75},
        ApplicationStatus.POSTPONED.value: {"red": 0.86, "green": 0.86, "blue": 0.86},
    }
    return colors.get(status, {"red": 1.0, "green": 1.0, "blue": 1.0})


def _column_letter(column_number: int) -> str:
    result = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell(row: list[Any], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return str(row[index])


def _working_sheet_schema(header_row: list[Any]) -> str | None:
    headers = [str(value).strip() for value in header_row]
    if headers[: len(WORKSHEET_HEADERS)] == WORKSHEET_HEADERS:
        return "new"
    if headers[: len(CURRENT_WORKSHEET_HEADERS)] == CURRENT_WORKSHEET_HEADERS:
        return "current"
    if headers[: len(LEGACY_WORKSHEET_HEADERS)] == LEGACY_WORKSHEET_HEADERS:
        return "legacy"
    return None


def _sectioned_working_sheet_schema(rows: list[list[Any]]) -> str | None:
    marker_positions: dict[str, int] = {}
    for index, row in enumerate(rows):
        marker = _cell(row, 0).strip()
        if marker in ROLLOUT_SECTION_MARKERS:
            marker_positions[marker] = index
    if set(marker_positions) != set(ROLLOUT_SECTION_MARKERS):
        return False
    positions = [marker_positions[marker] for marker in ROLLOUT_SECTION_MARKERS]
    if positions != sorted(positions):
        return None
    schemas: set[str] = set()
    for position in positions:
        if position + 1 >= len(rows):
            return None
        schema = _working_sheet_schema(rows[position + 1])
        if schema is None:
            return None
        schemas.add(schema)
    return schemas.pop() if len(schemas) == 1 else None


def _is_sectioned_working_sheet(rows: list[list[Any]]) -> bool:
    return _sectioned_working_sheet_schema(rows) is not None


def _find_row_by_id(rows: list[list[Any]], application_id: str) -> int | None:
    if not application_id:
        return None
    for index, row in enumerate(rows[1:], start=2):
        if row and str(row[0]).strip() == application_id:
            return index
    return None


def _dashboard_duplicate_row_numbers(rows: list[list[Any]]) -> list[int]:
    seen: set[tuple[str, str]] = set()
    duplicates: list[int] = []
    for row_number, row in enumerate(rows[1:], start=2):
        application_id = _cell(row, 0).strip()
        batch_id = _cell(row, 1).strip()
        if batch_id and application_id in {"", "Пачка"}:
            key = ("batch", batch_id)
        elif application_id:
            key = ("application", application_id)
        else:
            continue
        if key in seen:
            duplicates.append(row_number)
        else:
            seen.add(key)
    return duplicates


def _find_row_by_batch_id(rows: list[list[Any]], batch_id: str) -> int | None:
    if not batch_id:
        return None
    for index, row in enumerate(rows[1:], start=2):
        first_cell = str(row[0]).strip() if row else ""
        second_cell = str(row[1]).strip() if len(row) > 1 else ""
        if second_cell == batch_id or first_cell == batch_id:
            return index
    return None


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


# Backward-compatible fake service name used by older tests.
SubmissionService = InMemorySubmissionService

