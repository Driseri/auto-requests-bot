from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from app.formatting import build_text_format_runs, deserialize_formatting_spans
from app.google_api import (
    GoogleApiRetryConfig,
    execute_with_retry_async,
    is_google_rate_limit_error,
)
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
from app.repository import DraftRepository
from app.scheduling import (
    DEFAULT_TIMEZONE,
    DEFAULT_ROLLOUT_SCHEDULE,
    RolloutSchedule,
    rollout_sheet_name,
)
from app.sheet_dates import google_sheets_date_cell, utc_iso

LOGGER = logging.getLogger(__name__)
EDITOR_NOT_SELECTED = "Редактор не выбран"
SECTION_LOCK_TTL_SECONDS = 600
DAILY_SEPARATOR_FORMAT = "%d.%m.%y"

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
PREVIOUS_WORKSHEET_HEADERS = [
    "Закрепленный сценарист",
    "Интент",
    "Кейс или сообщения клиента",
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
WORKSHEET_HEADERS = [
    "Закрепленный сценарист",
    "Статус",
    "Кейс или сообщения клиента",
    "Суть изменений",
    "Исходный текст",
    "Итоговый ответ редактора",
    "Комментарий качества",
    "Вопросы/комментарии редактора",
    "Ответ сценариста",
    "Редактор",
    "Интент",
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
PREVIOUS_CHIPS_WORKSHEET_HEADERS = [
    "Закрепленный сценарист",
    "Интент",
    "Причина",
    "Текст до чипса",
    "Текст чипса",
    "Текст после чипса",
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
    "Тип изменения",
]
CHIPS_WORKSHEET_HEADERS = [
    "Закрепленный сценарист",
    "Статус",
    "Причина",
    "Текст до чипса",
    "Текст чипса",
    "Текст после чипса",
    "Комментарий качества",
    "Вопросы/комментарии редактора",
    "Ответ сценариста",
    "Редактор",
    "Интент",
    "ID заявки",
    "ID пачки",
    "Тип заявки",
    "Дата заявки",
    "Направление",
    "Тип ответа",
    "Срочная",
    "Автор заявки",
    "Telegram ID",
    "Тип изменения",
]
CHIPS_V2_MARKER = "CHIPS V2"

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
INTEGRATION_SHEET_NAME = "Интеграции"
URGENT_SHEET_NAME = "Срочные"
ROLLOUT_SECTION_MARKERS = tuple(change_type.value for change_type in ChangeType)
ALL_ROLLOUT_SECTION_MARKERS = (*ROLLOUT_SECTION_MARKERS, CHIPS_V2_MARKER)
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


@dataclass(frozen=True, slots=True)
class _SubmissionWriteResult:
    result: SubmissionResult
    shift_from_row: int | None = None
    shift_delta: int = 1


@dataclass(frozen=True, slots=True)
class _InsertedRow:
    row_number: int
    shift_from_row: int | None = None
    shift_delta: int = 1


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
        repository: DraftRepository | None = None,
        daily_sheet_grouping_enabled: bool = True,
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
        self.daily_sheet_grouping_enabled = daily_sheet_grouping_enabled
        self.rollout_schedule = rollout_schedule
        self.timezone_name = timezone_name
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.application_editors = application_editors
        self.repository = repository
        self._prepared_sheets: dict[
            tuple[str, str, str], tuple[str, str | None]
        ] = {}
        self._sheet_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._section_locks: dict[str, asyncio.Lock] = {}
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
        section_kind = sheet_section_kind(
            answer_type=application.answer_type,
            change_type=ChangeType.normalize(application.change_type),
        )
        lock_key = sheet_section_lock_key(
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            section_kind=section_kind,
        )
        lock = self._section_locks.setdefault(lock_key, asyncio.Lock())
        owner = f"single:{application.application_id or application.telegram_user_id}"
        try:
            async with lock:
                acquired = True
                if self.repository is not None:
                    acquired = await self.repository.acquire_bulk_section_lock(
                        lock_key=lock_key,
                        owner=owner,
                        ttl_seconds=SECTION_LOCK_TTL_SECONDS,
                    )
                if not acquired:
                    return SubmissionResult(
                        success=False,
                        message=(
                            "Сейчас другой пользователь вносит строки в этот раздел. "
                            "Повторите отправку через несколько секунд."
                        ),
                    )
                try:
                    write_result = await execute_with_retry_async(
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
                    if (
                        self.repository is not None
                        and write_result.result.success
                        and write_result.shift_from_row is not None
                        and write_result.result.spreadsheet_id
                        and write_result.result.sheet_id is not None
                    ):
                        await self.repository.shift_rows_after_insert(
                            spreadsheet_id=write_result.result.spreadsheet_id,
                            sheet_id=write_result.result.sheet_id,
                            from_row=write_result.shift_from_row,
                            delta=write_result.shift_delta,
                        )
                    result = write_result.result
                finally:
                    if self.repository is not None and acquired:
                        await self.repository.release_bulk_section_lock(
                            lock_key=lock_key,
                            owner=owner,
                        )
        except Exception as exc:
            if is_google_rate_limit_error(exc):
                return SubmissionResult(
                    success=False,
                    message=(
                        "Google API временно перегружен и не принял заявку.\n\n"
                        "Повторите отправку через 2-3 минуты. "
                        "Заявка сохранена, заново заполнять её не нужно."
                    ),
                )
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
    ) -> _SubmissionWriteResult:
        """Сверить application_id и добавить строку только при его отсутствии."""
        if not spreadsheet_id:
            raise SheetConfigurationError(
                "Для выбранного направления не задан ID Google-таблицы."
            )
        api = self._get_sheets_api()

        if application.answer_type == AnswerType.ROLLOUT.value:
            sheet_mode = "rollout"
        elif application.answer_type == AnswerType.URGENT.value:
            sheet_mode = "urgent"
        else:
            sheet_mode = "flat"
        change_type = ChangeType.normalize(application.change_type)
        sheet_id, layout, section_marker = self._ensure_sheet_ready(
            api,
            spreadsheet_id,
            sheet_name,
            sheet_mode=sheet_mode,
            change_type=change_type,
        )
        schema = layout.split(":", maxsplit=1)[1]
        existing_row = self._find_application_row(
            api,
            spreadsheet_id,
            sheet_name,
            application.application_id,
        )
        if existing_row is not None:
            return _SubmissionWriteResult(
                SubmissionResult(
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
            insert_result = self._insert_section_row(
                api,
                spreadsheet_id,
                sheet_id,
                sheet_name,
                change_type,
                row_data,
                target_marker=section_marker,
            )
            row_number = insert_result.row_number
            shift_from_row = insert_result.shift_from_row
            shift_delta = insert_result.shift_delta
        elif layout.startswith("urgent:"):
            insert_result = self._insert_urgent_row(
                api,
                spreadsheet_id,
                sheet_id,
                sheet_name,
                change_type,
                row_data,
            )
            row_number = insert_result.row_number
            shift_from_row = insert_result.shift_from_row
            shift_delta = insert_result.shift_delta
        elif (
            application.answer_type == AnswerType.INTEGRATION.value
            and self.daily_sheet_grouping_enabled
        ):
            insert_result = self._insert_flat_daily_row(
                api,
                spreadsheet_id,
                sheet_id,
                sheet_name,
                row_data,
            )
            row_number = insert_result.row_number
            shift_from_row = insert_result.shift_from_row
            shift_delta = insert_result.shift_delta
        else:
            row_number = self._next_row_number(api, spreadsheet_id, sheet_name)
            shift_from_row = None
            shift_delta = 1
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

        return _SubmissionWriteResult(
            SubmissionResult(
                success=True,
                message="Заявка отправлена в таблицу.",
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                sheet_name=sheet_name,
                row_number=row_number,
                row_link=row_link,
                submitted_at=utc_iso(submitted_at),
            ),
            shift_from_row=shift_from_row,
            shift_delta=shift_delta,
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
            CHIPS_WORKSHEET_HEADERS,
            PREVIOUS_CHIPS_WORKSHEET_HEADERS,
            WORKSHEET_HEADERS,
            PREVIOUS_WORKSHEET_HEADERS,
            CURRENT_WORKSHEET_HEADERS,
            LEGACY_WORKSHEET_HEADERS,
        )
        for row_number, row in enumerate(rows, start=1):
            if is_daily_separator_row(row):
                id_column_index = WORKSHEET_HEADERS.index("ID заявки")
                continue
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
        sheet_mode: str,
        change_type: ChangeType | None,
    ) -> tuple[int, str, str | None]:
        """Создать или распознать поддерживаемую схему до записи данных."""
        cache_key = (
            spreadsheet_id,
            sheet_name,
            f"{sheet_mode}:{change_type.value if change_type is not None else 'none'}",
        )
        if cache_key in self._prepared_sheets:
            layout, marker = self._prepared_sheets[cache_key]
            return (
                self._get_or_create_sheet_id(api, spreadsheet_id, sheet_name),
                layout,
                marker,
            )

        sheet_id = self._get_or_create_sheet_id(api, spreadsheet_id, sheet_name)
        rows = self._read_rows(api, spreadsheet_id, sheet_name)
        if sheet_mode == "urgent":
            if change_type is None:
                raise SheetConfigurationError(
                    "Для срочной заявки не выбран тип изменения ADD, EDIT или CHIPS."
                )
            if not rows:
                self._initialize_urgent_sheet(api, spreadsheet_id, sheet_id, sheet_name)
                schema = "chips" if change_type == ChangeType.CHIPS else "new"
            else:
                schema = self._prepare_urgent_sheet(
                    api,
                    spreadsheet_id,
                    sheet_id,
                    sheet_name,
                    rows,
                    change_type,
                )
            layout, marker = f"urgent:{schema}", ChangeType.CHIPS.value
        elif not rows:
            if sheet_mode == "rollout":
                self._initialize_sectioned_sheet(api, spreadsheet_id, sheet_id, sheet_name)
                if change_type == ChangeType.CHIPS:
                    layout, marker = "sectioned:chips", ChangeType.CHIPS.value
                else:
                    layout, marker = "sectioned:new", change_type.value if change_type else None
            else:
                self._initialize_empty_sheet(api, spreadsheet_id, sheet_id, sheet_name)
                layout, marker = "flat:new", None
        elif sheet_mode == "rollout" and _is_sectioned_working_sheet(rows):
            if change_type is None:
                raise SheetConfigurationError(
                    "Для раскатки не выбран тип изменения ADD, EDIT или CHIPS."
                )
            schema, marker = self._prepare_rollout_section(
                api,
                spreadsheet_id,
                sheet_id,
                sheet_name,
                rows,
                change_type,
            )
            layout = f"sectioned:{schema}"
        elif sheet_mode in {"flat", "rollout"}:
            schema = _working_sheet_schema(rows[0])
            if schema is None:
                raise SheetConfigurationError(
                    "Структура колонок рабочей вкладки не совпадает с поддерживаемыми схемами."
                )
            layout, marker = f"flat:{schema}", None
        else:
            raise SheetConfigurationError(
                "Структура секций недельной вкладки не совпадает с поддерживаемой схемой."
            )
        self._prepared_sheets[cache_key] = (layout, marker)
        return sheet_id, layout, marker

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

    def _insert_flat_daily_row(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_name: str,
        row_data: dict[str, Any],
    ) -> _InsertedRow:
        rows = self._read_rows(api, spreadsheet_id, sheet_name)
        return self._insert_daily_row(
            api,
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            rows=rows,
            section_start_row=2,
            section_end_row=len(rows) + 1,
            column_count=len(row_data["values"]),
            row_data=row_data,
        )

    def _insert_daily_row(
        self,
        api: Any,
        *,
        spreadsheet_id: str,
        sheet_id: int,
        rows: list[list[Any]],
        section_start_row: int,
        section_end_row: int,
        column_count: int,
        row_data: dict[str, Any],
        extra_requests: list[dict[str, Any]] | None = None,
        extra_requests_factory: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> _InsertedRow:
        label = daily_separator_label(self.clock(), self.timezone_name)
        plan = _daily_insert_plan(
            rows,
            label=label,
            sheet_id=sheet_id,
            section_start_row=section_start_row,
            section_end_row=section_end_row,
            column_count=column_count,
            row_data=row_data,
        )
        requests = [
            *plan["requests"],
            *(extra_requests or []),
            *((extra_requests_factory(plan) if extra_requests_factory else [])),
        ]
        api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
        self._apply_daily_group_best_effort(
            api,
            spreadsheet_id=spreadsheet_id,
            group_request=plan.get("group_request"),
        )
        return _InsertedRow(
            row_number=plan["row_number"],
            shift_from_row=plan["shift_from_row"],
            shift_delta=plan["shift_delta"],
        )

    async def prepare_daily_sheet_blocks_once(self) -> None:
        if not self.daily_sheet_grouping_enabled:
            return
        shifts = await execute_with_retry_async(
            self._prepare_daily_sheet_blocks_sync,
            config=self.google_api_retry,
            operation_id="daily-sheet-maintenance",
            reset_client=self._reset_sheets_api,
        )
        if self.repository is None:
            return
        for spreadsheet_id, sheet_id, from_row in shifts:
            await self.repository.shift_rows_after_insert(
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
                from_row=from_row,
                delta=1,
            )

    def _prepare_daily_sheet_blocks_sync(self) -> list[tuple[str, int, int]]:
        api = self._get_sheets_api()
        shifts: list[tuple[str, int, int]] = []
        for spreadsheet_id in self._configured_spreadsheet_ids():
            metadata = api.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.properties",
            ).execute()
            for sheet in metadata.get("sheets", []):
                properties = sheet.get("properties", {})
                sheet_name = str(properties.get("title") or "")
                sheet_id = int(properties.get("sheetId") or 0)
                if not sheet_name or not sheet_id:
                    continue
                if _is_urgent_sheet_name(sheet_name):
                    shifts.extend(
                        self._prepare_urgent_daily_blocks_sync(
                            api,
                            spreadsheet_id,
                            sheet_id,
                            sheet_name,
                        )
                    )
                elif _is_integration_sheet_name(sheet_name):
                    shift_from = self._prepare_flat_daily_block_sync(
                        api,
                        spreadsheet_id,
                        sheet_id,
                        sheet_name,
                    )
                    if shift_from is not None:
                        shifts.append((spreadsheet_id, sheet_id, shift_from))
        return shifts

    def _configured_spreadsheet_ids(self) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for spreadsheet_id in (
            self.direction_spreadsheets.fl_spreadsheet_id,
            self.direction_spreadsheets.sme_spreadsheet_id,
            self.direction_spreadsheets.ai_spreadsheet_id,
            self.direction_spreadsheets.voice_collection_spreadsheet_id,
        ):
            if spreadsheet_id and spreadsheet_id not in seen:
                seen.add(spreadsheet_id)
                result.append(spreadsheet_id)
        return result

    def _prepare_flat_daily_block_sync(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_name: str,
    ) -> int | None:
        rows = self._read_rows(api, spreadsheet_id, sheet_name)
        if not rows or _working_sheet_schema(rows[0]) is None:
            return None
        return self._insert_daily_separator_if_missing(
            api,
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            rows=rows,
            section_start_row=2,
            section_end_row=len(rows) + 1,
            column_count=SHEET_COLUMN_COUNT,
        )

    def _prepare_urgent_daily_blocks_sync(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_name: str,
    ) -> list[tuple[str, int, int]]:
        rows = self._read_rows(api, spreadsheet_id, sheet_name)
        if not rows or _working_sheet_schema(rows[0]) is None:
            return []
        shift_from = self._insert_daily_separator_if_missing(
            api,
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            rows=rows,
            section_start_row=2,
            section_end_row=len(rows) + 1,
            column_count=SHEET_COLUMN_COUNT,
        )
        return [(spreadsheet_id, sheet_id, shift_from)] if shift_from is not None else []

    def _insert_daily_separator_if_missing(
        self,
        api: Any,
        *,
        spreadsheet_id: str,
        sheet_id: int,
        rows: list[list[Any]],
        section_start_row: int,
        section_end_row: int,
        column_count: int,
    ) -> int | None:
        label = daily_separator_label(self.clock(), self.timezone_name)
        separator_rows = [
            row_number
            for row_number in range(section_start_row, min(section_end_row, len(rows) + 1))
            if is_daily_separator_row(rows[row_number - 1])
        ]
        if any(_cell(rows[row_number - 1], 0).strip() == label for row_number in separator_rows):
            return None
        insert_row = section_end_row
        group_request = _previous_daily_group_request(
            separator_rows,
            new_separator_row=insert_row,
            sheet_id=sheet_id,
        )
        requests = [
            {
                "insertDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": insert_row - 1,
                        "endIndex": insert_row,
                    },
                    "inheritFromBefore": True,
                }
            },
            {
                "updateCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": insert_row - 1,
                        "endRowIndex": insert_row,
                        "startColumnIndex": 0,
                        "endColumnIndex": column_count,
                    },
                    "rows": [_daily_separator_row_data(label, column_count)],
                    "fields": "userEnteredValue,userEnteredFormat",
                }
            },
        ]
        api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
        self._apply_daily_group_best_effort(
            api,
            spreadsheet_id=spreadsheet_id,
            group_request=group_request,
        )
        return insert_row if insert_row <= len(rows) else None

    def _apply_daily_group_best_effort(
        self,
        api: Any,
        *,
        spreadsheet_id: str,
        group_request: dict[str, Any] | None,
    ) -> None:
        if group_request is None:
            return
        try:
            api.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [group_request]},
            ).execute()
        except Exception as exc:
            LOGGER.warning(
                "Daily sheet previous block grouping failed; continuing without grouping: "
                "spreadsheet_id=%s error=%r",
                spreadsheet_id,
                exc,
            )

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
            rows.append(
                CHIPS_WORKSHEET_HEADERS
                if marker == ChangeType.CHIPS.value
                else WORKSHEET_HEADERS
            )
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

    def _initialize_urgent_sheet(
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
        requests = worksheet_formatting_requests(
            sheet_id,
            self.application_editors,
        )
        requests = [request for request in requests if "setBasicFilter" not in request]
        requests.append(
            _basic_filter_request(
                sheet_id,
                SHEET_COLUMN_COUNT,
                end_row_index=1,
            )
        )
        api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()

    def _prepare_urgent_sheet(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_name: str,
        rows: list[list[Any]],
        change_type: ChangeType,
    ) -> str:
        schema = _working_sheet_schema(rows[0])
        if schema is None:
            raise SheetConfigurationError(
                "Повреждена общая шапка листа срочных заявок."
            )
        chips_schema = "chips"
        formatting_requests: list[dict[str, Any]] = []
        formatting_requests.append(
            _basic_filter_request(
                sheet_id,
                SHEET_COLUMN_COUNT,
                end_row_index=1,
            )
        )
        formatting_requests.extend(
            _replace_urgent_conditional_formatting_requests(
                api,
                spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_id,
            )
        )
        api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": formatting_requests},
        ).execute()
        return chips_schema if change_type == ChangeType.CHIPS else schema

    def _prepare_rollout_section(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_name: str,
        rows: list[list[Any]],
        change_type: ChangeType,
    ) -> tuple[str, str]:
        positions = _rollout_marker_positions(rows)
        if change_type != ChangeType.CHIPS:
            position = positions[change_type.value]
            schema = _working_sheet_schema(rows[position + 1])
            if schema is None:
                raise SheetConfigurationError(
                    f"Повреждена шапка секции {change_type.value}."
                )
            return schema, change_type.value

        if CHIPS_V2_MARKER in positions:
            position = positions[CHIPS_V2_MARKER]
            schema = _chips_sheet_schema(rows[position + 1])
            if schema is None:
                raise SheetConfigurationError("Повреждена шапка секции CHIPS V2.")
            return schema, CHIPS_V2_MARKER

        position = positions[ChangeType.CHIPS.value]
        header = rows[position + 1]
        schema = _chips_sheet_schema(header)
        if schema is not None:
            return schema, ChangeType.CHIPS.value
        if _working_sheet_schema(header) is None:
            raise SheetConfigurationError("Повреждена шапка секции CHIPS.")

        # Legacy rollout tabs used a regular ADD/EDIT header under the CHIPS
        # marker. Empty legacy sections can be upgraded in place; non-empty
        # sections keep their old rows and receive a new CHIPS V2 section.
        following_markers = sorted(
            marker_position
            for marker_position in positions.values()
            if marker_position > position
        )
        section_end = following_markers[0] if following_markers else len(rows)
        has_data = any(
            any(str(value or "").strip() for value in row)
            for row in rows[position + 2 : section_end]
        )
        if not has_data:
            header_row = position + 2
            api.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=(
                    f"{quote_sheet_name(sheet_name)}!"
                    f"A{header_row}:U{header_row}"
                ),
                valueInputOption="USER_ENTERED",
                body={"values": [CHIPS_WORKSHEET_HEADERS]},
            ).execute()
            return "chips", ChangeType.CHIPS.value

        marker_row = len(rows) + 1
        api.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!A{marker_row}:U{marker_row + 1}",
            valueInputOption="USER_ENTERED",
            body={"values": [[CHIPS_V2_MARKER], CHIPS_WORKSHEET_HEADERS]},
        ).execute()
        api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": _section_marker_header_format_requests(
                    sheet_id,
                    marker_row - 1,
                    len(CHIPS_WORKSHEET_HEADERS),
                )
            },
        ).execute()
        return "chips", CHIPS_V2_MARKER

    def _insert_section_row(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_name: str,
        change_type: ChangeType | None,
        row_data: dict[str, Any],
        *,
        target_marker: str | None = None,
    ) -> _InsertedRow:
        if change_type is None:
            raise SheetConfigurationError(
                "Для раскатки не выбран тип изменения ADD, EDIT или CHIPS."
            )
        rows = self._read_rows(api, spreadsheet_id, sheet_name)
        if not rows:
            rows = [
                item
                for marker in ROLLOUT_SECTION_MARKERS
                for item in (
                    [marker],
                    CHIPS_WORKSHEET_HEADERS
                    if marker == ChangeType.CHIPS.value
                    else WORKSHEET_HEADERS,
                )
            ]
        marker_rows = {
            _cell(row, 0).strip(): index + 1
            for index, row in enumerate(rows)
            if _cell(row, 0).strip() in ALL_ROLLOUT_SECTION_MARKERS
        }
        if not set(ROLLOUT_SECTION_MARKERS) <= set(marker_rows):
            raise SheetConfigurationError(
                "В недельной вкладке повреждена структура секций ADD, EDIT и CHIPS."
            )

        selected_marker = target_marker or change_type.value
        selected_row = marker_rows.get(selected_marker)
        if selected_row is None:
            raise SheetConfigurationError(f"Секция {selected_marker} не найдена.")
        following_rows = sorted(row for row in marker_rows.values() if row > selected_row)
        if following_rows:
            row_number = following_rows[0]
            shift_from_row = row_number
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
            shift_from_row = None
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
        return _InsertedRow(row_number=row_number, shift_from_row=shift_from_row)

    def _insert_urgent_row(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_id: int,
        sheet_name: str,
        change_type: ChangeType | None,
        row_data: dict[str, Any],
    ) -> _InsertedRow:
        if change_type is None:
            raise SheetConfigurationError(
                "Для срочной заявки не выбран тип изменения ADD, EDIT или CHIPS."
            )
        rows = self._read_rows(api, spreadsheet_id, sheet_name)
        plan = _urgent_daily_insert_plan(
            rows,
            label=daily_separator_label(self.clock(), self.timezone_name),
            sheet_id=sheet_id,
            change_type=change_type,
            row_data=row_data,
        )
        api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": plan["requests"]},
        ).execute()
        self._apply_daily_group_best_effort(
            api,
            spreadsheet_id=spreadsheet_id,
            group_request=plan.get("group_request"),
        )
        return _InsertedRow(
            row_number=plan["row_number"],
            shift_from_row=plan["shift_from_row"],
            shift_delta=plan["shift_delta"],
        )

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
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


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


def sheet_section_kind(
    *,
    answer_type: str | None,
    change_type: ChangeType | None,
) -> str:
    if answer_type == AnswerType.ROLLOUT.value:
        return f"rollout:{(change_type or ChangeType.ADD).value}"
    if answer_type == AnswerType.URGENT.value:
        return "urgent:daily"
    return "flat:main"


def sheet_section_lock_key(
    *,
    spreadsheet_id: str,
    sheet_name: str,
    section_kind: str,
) -> str:
    return f"{spreadsheet_id}:{sheet_name}:{section_kind}"


def _is_urgent_sheet_name(sheet_name: str) -> bool:
    return sheet_name == URGENT_SHEET_NAME or sheet_name.endswith(f" {URGENT_SHEET_NAME}")


def _is_integration_sheet_name(sheet_name: str) -> bool:
    return (
        sheet_name == INTEGRATION_SHEET_NAME
        or sheet_name.endswith(f" {INTEGRATION_SHEET_NAME}")
    )


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
        ApplicationStatus.NEW.value,
        draft.reason or "",
        draft.formatted_change_description or draft.raw_change_description or "",
        draft.source_text or "",
        "",
        "",
        "",
        "",
        EDITOR_NOT_SELECTED,
        draft.intent or "",
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


def chips_draft_to_sheet_row(
    draft: Draft,
    *,
    batch_id: str = "",
    submitted_at: datetime | str | None = None,
) -> list[Any]:
    return [
        draft.scriptwriter or "",
        ApplicationStatus.NEW.value,
        draft.reason or "",
        draft.chip_text_before or "",
        draft.chip_text or "",
        draft.chip_text_after or "",
        "",
        "",
        "",
        EDITOR_NOT_SELECTED,
        draft.intent or "",
        draft.application_id or "",
        batch_id,
        draft.application_type or ApplicationType.SINGLE.value,
        submitted_at or draft.created_at,
        draft.direction or "",
        draft.answer_type or "",
        bool_to_sheet_value(draft.is_urgent),
        draft.author_name or draft.scriptwriter or "",
        draft.telegram_user_id,
        ChangeType.CHIPS.value,
    ]


def _previous_draft_to_sheet_row(
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


def _previous_chips_draft_to_sheet_row(
    draft: Draft,
    *,
    batch_id: str = "",
    submitted_at: datetime | str | None = None,
) -> list[Any]:
    return [
        draft.scriptwriter or "",
        draft.intent or "",
        draft.reason or "",
        draft.chip_text_before or "",
        draft.chip_text or "",
        draft.chip_text_after or "",
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
        ChangeType.CHIPS.value,
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
    final_answer_present = bool(current.final_answer)
    if (
        ChangeType.normalize(getattr(current, "change_type", None) or tracked.change_type)
        == ChangeType.CHIPS
    ):
        final_answer_present = False
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
        bool_to_sheet_value(final_answer_present),
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
    if schema == "chips":
        row = chips_draft_to_sheet_row(
            draft,
            batch_id=batch_id,
            submitted_at=submitted_at,
        )
    elif schema == "previous_chips":
        row = _previous_chips_draft_to_sheet_row(
            draft,
            batch_id=batch_id,
            submitted_at=submitted_at,
        )
    elif schema == "new":
        row = draft_to_sheet_row(
            draft,
            batch_id=batch_id,
            submitted_at=submitted_at,
        )
    elif schema == "previous_new":
        row = _previous_draft_to_sheet_row(
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
            cells.append(
                _status_cell_data(
                    ApplicationStatus.NEW.value,
                    include_background=schema not in {"chips", "previous_chips"},
                )
            )
        elif index == layout["source_text"] or (
            isinstance(layout["source_text"], tuple)
            and index in layout["source_text"]
        ):
            if schema in {"chips", "previous_chips"}:
                formatting_by_index = {
                    3: draft.chip_text_before_formatting_json,
                    4: draft.chip_text_formatting_json,
                    5: draft.chip_text_after_formatting_json,
                }
                cells.append(
                    _formatted_text_cell_data(
                        str(value or ""),
                        formatting_by_index[index],
                    )
                )
            else:
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
            "status": 1,
            "editor": 9,
            "source_text": 4,
            "telegram_id": 19,
            "llm_score": 22,
            "end_column": "X",
        },
        "chips": {
            "date": 14,
            "status": 1,
            "editor": 9,
            "source_text": (3, 4, 5),
            "telegram_id": 19,
            "llm_score": -1,
            "end_column": "U",
        },
        "previous_new": {
            "date": 14,
            "status": 9,
            "editor": 10,
            "source_text": 4,
            "telegram_id": 19,
            "llm_score": 22,
            "end_column": "X",
        },
        "previous_chips": {
            "date": 14,
            "status": 9,
            "editor": 10,
            "source_text": (3, 4, 5),
            "telegram_id": 19,
            "llm_score": -1,
            "end_column": "U",
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


def _status_cell_data(status: str, *, include_background: bool = True) -> dict[str, Any]:
    cell = {
        "userEnteredValue": {"stringValue": status},
        "dataValidation": _status_data_validation_rule(),
        "userEnteredFormat": {"textFormat": {"bold": True}},
    }
    if include_background:
        cell["userEnteredFormat"]["backgroundColor"] = _status_color(status)
    return cell


def _editor_cell_data(
    editor: str = EDITOR_NOT_SELECTED,
    application_editors: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "userEnteredValue": {"stringValue": editor or EDITOR_NOT_SELECTED},
        "dataValidation": _editor_data_validation_rule(application_editors),
    }


def _source_text_cell_data(draft: Draft) -> dict[str, Any]:
    return _formatted_text_cell_data(
        draft.source_text or "",
        draft.source_text_formatting_json,
    )


def _formatted_text_cell_data(value: str, formatting_json: str | None) -> dict[str, Any]:
    cell = _cell_data(value)
    cell["userEnteredFormat"] = {"wrapStrategy": "CLIP"}
    runs = build_text_format_runs(
        value,
        deserialize_formatting_spans(formatting_json),
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
        _status_dropdown_request(sheet_id, status_column_index=1),
        _editor_dropdown_request(
            sheet_id,
            editor_column_index=9,
            application_editors=application_editors,
        ),
        _right_border_request(sheet_id, column_index=10),
    ]
    requests.extend(_status_conditional_formatting_requests(sheet_id, status_column_index=1))
    requests.append(_urgent_conditional_formatting_request(sheet_id))
    return requests


def sectioned_worksheet_formatting_requests(
    sheet_id: int,
    application_editors: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = [
        _column_width_request(sheet_id, 0, SHEET_COLUMN_COUNT, 170),
        _column_width_request(sheet_id, 10, 19, 300),
        _status_dropdown_request(sheet_id, status_column_index=1),
        _editor_dropdown_request(
            sheet_id,
            editor_column_index=9,
            application_editors=application_editors,
        ),
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
    requests.extend(_status_conditional_formatting_requests(sheet_id, status_column_index=1))
    requests.append(_urgent_conditional_formatting_request(sheet_id))
    return requests


def _section_marker_header_format_requests(
    sheet_id: int,
    marker_row_index: int,
    column_count: int,
) -> list[dict[str, Any]]:
    return [
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": marker_row_index,
                    "endRowIndex": marker_row_index + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": column_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.82, "green": 0.86, "blue": 0.91},
                        "textFormat": {"bold": True},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": marker_row_index + 1,
                    "endRowIndex": marker_row_index + 2,
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
                "fields": (
                    "userEnteredFormat(backgroundColor,horizontalAlignment,"
                    "textFormat,wrapStrategy)"
                ),
            }
        },
    ]


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


def _basic_filter_request(
    sheet_id: int,
    column_count: int,
    *,
    end_row_index: int | None = None,
) -> dict[str, Any]:
    range_config = {
        "sheetId": sheet_id,
        "startRowIndex": 0,
        "startColumnIndex": 0,
        "endColumnIndex": column_count,
    }
    if end_row_index is not None:
        range_config["endRowIndex"] = end_row_index
    return {
        "setBasicFilter": {
            "filter": {
                "range": range_config
            }
        }
    }


def daily_separator_label(moment: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> str:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    return moment.astimezone(tz).strftime(DAILY_SEPARATOR_FORMAT)


def is_daily_separator_row(row: list[Any]) -> bool:
    first_cell = _cell(row, 0).strip()
    if not first_cell:
        return False
    try:
        datetime.strptime(first_cell, DAILY_SEPARATOR_FORMAT)
    except ValueError:
        return False
    return all(not str(value or "").strip() for value in row[1:])


def _daily_insert_plan(
    rows: list[list[Any]],
    *,
    label: str,
    sheet_id: int,
    section_start_row: int,
    section_end_row: int,
    column_count: int,
    row_data: dict[str, Any],
) -> dict[str, Any]:
    section_start_row = max(section_start_row, 1)
    section_end_row = max(section_end_row, section_start_row)
    separator_rows = [
        row_number
        for row_number in range(section_start_row, min(section_end_row, len(rows) + 1))
        if is_daily_separator_row(rows[row_number - 1])
    ]
    today_rows = [
        row_number
        for row_number in separator_rows
        if _cell(rows[row_number - 1], 0).strip() == label
    ]
    group_request: dict[str, Any] | None = None
    requests: list[dict[str, Any]] = []
    if today_rows:
        today_row = today_rows[-1]
        next_separator = next(
            (row_number for row_number in separator_rows if row_number > today_row),
            None,
        )
        insert_row = next_separator or section_end_row
        inserted_rows = 1
        update_rows = [row_data]
    else:
        insert_row = section_end_row
        inserted_rows = 2
        update_rows = [
            _daily_separator_row_data(label, column_count),
            row_data,
        ]
        group_request = _previous_daily_group_request(
            separator_rows,
            new_separator_row=insert_row,
            sheet_id=sheet_id,
        )

    insert_index = insert_row - 1
    shift_from_row = insert_row if insert_row <= len(rows) else None
    requests.extend(
        [
            {
                "insertDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": insert_index,
                        "endIndex": insert_index + inserted_rows,
                    },
                    "inheritFromBefore": True,
                }
            },
            {
                "updateCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": insert_index,
                        "endRowIndex": insert_index + inserted_rows,
                        "startColumnIndex": 0,
                        "endColumnIndex": column_count,
                    },
                    "rows": update_rows,
                    "fields": (
                        "userEnteredValue,dataValidation,"
                        "userEnteredFormat,textFormatRuns"
                    ),
                }
            },
        ]
    )
    return {
        "row_number": insert_row + inserted_rows - 1,
        "shift_from_row": shift_from_row,
        "shift_delta": inserted_rows,
        "requests": requests,
        "group_request": group_request,
    }


def _urgent_daily_insert_plan(
    rows: list[list[Any]],
    *,
    label: str,
    sheet_id: int,
    change_type: ChangeType,
    row_data: dict[str, Any],
) -> dict[str, Any]:
    separator_rows = [
        row_number
        for row_number in range(2, len(rows) + 1)
        if is_daily_separator_row(rows[row_number - 1])
    ]
    today_row = next(
        (
            row_number
            for row_number in reversed(separator_rows)
            if _cell(rows[row_number - 1], 0).strip() == label
        ),
        None,
    )
    group_request: dict[str, Any] | None = None
    if today_row is None:
        day_start = len(rows) + 1
        day_end = len(rows) + 1
        group_request = _previous_daily_group_request(
            separator_rows,
            new_separator_row=day_start,
            sheet_id=sheet_id,
        )
    else:
        day_start = today_row
        day_end = next(
            (row_number for row_number in separator_rows if row_number > today_row),
            len(rows) + 1,
        )

    chips_marker = _chips_marker_row_in_day(rows, day_start=day_start, day_end=day_end)
    requests: list[dict[str, Any]] = []
    if today_row is None:
        prefix_column_count = len(row_data["values"])
        prefix_rows = [_daily_separator_row_data(label, prefix_column_count)]
        if change_type == ChangeType.CHIPS:
            prefix_rows.extend(
                [
                    _section_marker_row_data(ChangeType.CHIPS.value, len(CHIPS_WORKSHEET_HEADERS)),
                    {"values": [_cell_data(value) for value in CHIPS_WORKSHEET_HEADERS]},
                ]
            )
        update_rows = [*prefix_rows, row_data]
        insert_row = day_start
        row_number = day_start + len(update_rows) - 1
        inserted_rows = len(update_rows)
    elif change_type == ChangeType.CHIPS:
        if chips_marker is None:
            update_rows = [
                _section_marker_row_data(ChangeType.CHIPS.value, len(CHIPS_WORKSHEET_HEADERS)),
                {"values": [_cell_data(value) for value in CHIPS_WORKSHEET_HEADERS]},
                row_data,
            ]
            insert_row = day_end
            row_number = day_end + 2
            inserted_rows = 3
        else:
            insert_row = day_end
            row_number = day_end
            inserted_rows = 1
            update_rows = [row_data]
    else:
        insert_row = chips_marker or day_end
        row_number = insert_row
        inserted_rows = 1
        update_rows = [row_data]

    insert_index = insert_row - 1
    column_count = max(len(item.get("values", [])) for item in update_rows)
    requests.extend(
        [
            {
                "insertDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": insert_index,
                        "endIndex": insert_index + inserted_rows,
                    },
                    "inheritFromBefore": True,
                }
            },
            {
                "updateCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": insert_index,
                        "endRowIndex": insert_index + inserted_rows,
                        "startColumnIndex": 0,
                        "endColumnIndex": column_count,
                    },
                    "rows": update_rows,
                    "fields": (
                        "userEnteredValue,dataValidation,"
                        "userEnteredFormat,textFormatRuns"
                    ),
                }
            },
        ]
    )
    return {
        "row_number": row_number,
        "shift_from_row": insert_row if insert_row <= len(rows) else None,
        "shift_delta": inserted_rows,
        "requests": requests,
        "group_request": group_request,
    }


def _chips_marker_row_in_day(
    rows: list[list[Any]],
    *,
    day_start: int,
    day_end: int,
) -> int | None:
    for row_number in range(day_start + 1, min(day_end, len(rows) + 1)):
        if not _is_exact_marker_row(rows[row_number - 1], ChangeType.CHIPS.value):
            continue
        header_row_number = row_number + 1
        if header_row_number >= day_end or header_row_number > len(rows):
            raise SheetConfigurationError("Повреждена CHIPS-секция внутри дневного блока.")
        if not _is_chips_header(rows[header_row_number - 1]):
            raise SheetConfigurationError("Повреждена CHIPS-шапка внутри дневного блока.")
        return row_number
    return None


def _section_marker_row_data(marker: str, column_count: int) -> dict[str, Any]:
    values = [_cell_data(marker if index == 0 else "") for index in range(column_count)]
    for cell in values:
        cell["userEnteredFormat"] = {
            "backgroundColor": {"red": 0.90, "green": 0.90, "blue": 0.90},
            "textFormat": {"bold": True},
        }
    return {"values": values}


def _daily_separator_row_data(label: str, column_count: int) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    for index in range(column_count):
        cell = _cell_data(label if index == 0 else "")
        cell["userEnteredFormat"] = {
            "backgroundColor": {"red": 0.82, "green": 0.82, "blue": 0.82},
            "textFormat": {"bold": True},
        }
        values.append(cell)
    return {"values": values}


def _previous_daily_group_request(
    separator_rows: list[int],
    *,
    new_separator_row: int,
    sheet_id: int,
) -> dict[str, Any] | None:
    previous_rows = [row_number for row_number in separator_rows if row_number < new_separator_row]
    if not previous_rows or not sheet_id:
        return None
    previous_separator = previous_rows[-1]
    start_row = previous_separator
    end_row = new_separator_row - 1
    if start_row > end_row:
        return None
    return {
        "addDimensionGroup": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": start_row - 1,
                "endIndex": end_row,
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
                        "values": [{"userEnteredValue": '=AND($R2="Да",$U2<>"CHIPS")'}],
                    },
                    "format": {"backgroundColor": {"red": 1.0, "green": 0.90, "blue": 0.82}},
                },
            },
        }
    }


def _replace_urgent_conditional_formatting_requests(
    api: Any,
    *,
    spreadsheet_id: str,
    sheet_id: int,
) -> list[dict[str, Any]]:
    metadata = api.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId),conditionalFormats)",
    ).execute()
    conditional_count = 0
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("sheetId") == sheet_id:
            conditional_count = len(sheet.get("conditionalFormats", []))
            break
    requests: list[dict[str, Any]] = [
        {
            "deleteConditionalFormatRule": {
                "sheetId": sheet_id,
                "index": index,
            }
        }
        for index in range(conditional_count - 1, -1, -1)
    ]
    requests.extend(_status_conditional_formatting_requests(sheet_id, status_column_index=1))
    requests.append(_urgent_conditional_formatting_request(sheet_id))
    return requests


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
    if headers[: len(PREVIOUS_WORKSHEET_HEADERS)] == PREVIOUS_WORKSHEET_HEADERS:
        return "previous_new"
    if headers[: len(CURRENT_WORKSHEET_HEADERS)] == CURRENT_WORKSHEET_HEADERS:
        return "current"
    if headers[: len(LEGACY_WORKSHEET_HEADERS)] == LEGACY_WORKSHEET_HEADERS:
        return "legacy"
    return None


def _is_chips_header(header_row: list[Any]) -> bool:
    return _chips_sheet_schema(header_row) is not None


def _chips_sheet_schema(header_row: list[Any]) -> str | None:
    headers = [str(value).strip() for value in header_row]
    if headers[: len(CHIPS_WORKSHEET_HEADERS)] == CHIPS_WORKSHEET_HEADERS:
        return "chips"
    if headers[: len(PREVIOUS_CHIPS_WORKSHEET_HEADERS)] == PREVIOUS_CHIPS_WORKSHEET_HEADERS:
        return "previous_chips"
    return None


def _rollout_marker_positions(rows: list[list[Any]]) -> dict[str, int]:
    return {
        _cell(row, 0).strip(): index
        for index, row in enumerate(rows)
        if _cell(row, 0).strip() in ALL_ROLLOUT_SECTION_MARKERS
    }


def _is_exact_marker_row(row: list[Any], marker: str) -> bool:
    return _cell(row, 0).strip() == marker and all(
        not str(value or "").strip() for value in row[1:]
    )


def _sectioned_working_sheet_schema(rows: list[list[Any]]) -> str | None:
    marker_positions = _rollout_marker_positions(rows)
    if not set(ROLLOUT_SECTION_MARKERS) <= set(marker_positions):
        return None
    positions = [marker_positions[marker] for marker in ROLLOUT_SECTION_MARKERS]
    if positions != sorted(positions):
        return None
    for marker in (ChangeType.ADD.value, ChangeType.EDIT.value):
        position = marker_positions[marker]
        if position + 1 >= len(rows) or _working_sheet_schema(rows[position + 1]) is None:
            return None
    chips_position = marker_positions[ChangeType.CHIPS.value]
    if chips_position + 1 >= len(rows):
        return None
    chips_header = rows[chips_position + 1]
    if _working_sheet_schema(chips_header) is None and not _is_chips_header(chips_header):
        return None
    if CHIPS_V2_MARKER in marker_positions:
        v2_position = marker_positions[CHIPS_V2_MARKER]
        if v2_position <= chips_position or v2_position + 1 >= len(rows):
            return None
        if not _is_chips_header(rows[v2_position + 1]):
            return None
    return "mixed"


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

