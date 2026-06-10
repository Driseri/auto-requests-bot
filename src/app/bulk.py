from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.models import (
    AnswerType,
    ApplicationStatus,
    ApplicationType,
    BulkApplicationStatus,
    BulkBatch,
    BulkBatchStatus,
    ChangeType,
    Direction,
    generate_application_id,
    generate_batch_id,
    utc_now_iso,
)
from app.repository import DraftRepository
from app.submission import (
    DashboardSyncService,
    DirectionSpreadsheetConfig,
    EDITOR_NOT_SELECTED,
    SheetConfigurationError,
    build_google_sheets_api,
    quote_sheet_name,
    _editor_data_validation_rule,
)


BULK_INITIAL_INPUT_ROWS = 1
BULK_REGISTRATION_SCAN_ROWS = 1000
BULK_BATCH_SPACING_ROWS = 2
BULK_INPUT_HEADERS = [
    "Тип ответа",
    "Интент",
    "Закрепленный сценарист",
    "Причина изменений",
    "Суть изменений",
    "Исходный текст",
    "Тип изменения",
]
LEGACY_BULK_SERVICE_HEADERS = [
    "ID заявки",
    "Статус",
    "Вопросы/комментарии редактора",
    "Ответ/комментарий сценариста",
    "Итоговый ответ редактора",
]
BULK_SERVICE_HEADERS = [
    "ID заявки",
    "Статус",
    "Редактор",
    "Вопросы/комментарии редактора",
    "Ответ/комментарий сценариста",
    "Итоговый ответ редактора",
]
LEGACY_BULK_STAGING_HEADERS = [*BULK_INPUT_HEADERS, *LEGACY_BULK_SERVICE_HEADERS]
BULK_STAGING_HEADERS = [*BULK_INPUT_HEADERS, *BULK_SERVICE_HEADERS]
BULK_STAGING_COLUMN_COUNT = len(BULK_STAGING_HEADERS)
BULK_STAGING_SHEET_NAME = "Массовый ввод"


@dataclass(slots=True)
class BulkBatchCreationResult:
    success: bool
    message: str
    batch: BulkBatch | None = None
    insert_url: str | None = None


@dataclass(slots=True)
class BulkRegistrationResult:
    success: bool
    message: str
    registered_count: int = 0


class BulkBatchServiceProtocol(Protocol):
    async def create_batch(
        self,
        telegram_user_id: int,
        direction: str,
    ) -> BulkBatchCreationResult:
        ...


class InMemoryBulkBatchService:
    def __init__(self) -> None:
        self.created: list[int] = []

    async def create_batch(
        self,
        telegram_user_id: int,
        direction: str,
    ) -> BulkBatchCreationResult:
        self.created.append(telegram_user_id)
        batch = BulkBatch(
            batch_id=generate_batch_id(),
            telegram_user_id=telegram_user_id,
            spreadsheet_id="test-spreadsheet",
            direction=direction,
            sheet_name=BULK_STAGING_SHEET_NAME,
            sheet_id=100,
            start_row=1,
            data_start_row=3,
            reserved_rows=BULK_INITIAL_INPUT_ROWS,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
        return BulkBatchCreationResult(
            success=True,
            message="Место для массовой вставки создано.",
            batch=batch,
            insert_url="https://docs.google.com/spreadsheets/d/test/edit#gid=100&range=A3:G3",
        )


class GoogleSheetsBulkBatchService:
    def __init__(
        self,
        *,
        direction_spreadsheets: DirectionSpreadsheetConfig | None = None,
        spreadsheet_id: str = "",
        sheet_name: str = "",
        credentials_path: str,
        repository: DraftRepository,
        sheets_api: Any | None = None,
        reserved_rows: int = BULK_INITIAL_INPUT_ROWS,
        application_editors: tuple[str, ...] = ("редактор 1", "редактор 2"),
    ) -> None:
        self.direction_spreadsheets = direction_spreadsheets or DirectionSpreadsheetConfig(
            fl_spreadsheet_id=spreadsheet_id,
            sme_spreadsheet_id=spreadsheet_id,
            ai_spreadsheet_id=spreadsheet_id,
            voice_collection_spreadsheet_id=spreadsheet_id,
        )
        self.legacy_sheet_name = sheet_name
        self.credentials_path = credentials_path
        self.repository = repository
        self._sheets_api = sheets_api
        self.reserved_rows = reserved_rows
        self.application_editors = application_editors

        if sheets_api is None and not Path(self.credentials_path).is_file():
            raise RuntimeError(f"Google credentials file not found: {self.credentials_path}")

    async def create_batch(
        self,
        telegram_user_id: int,
        direction: str,
    ) -> BulkBatchCreationResult:
        try:
            existing_batches = await self.repository.list_bulk_batches()
            batch, insert_url = await asyncio.to_thread(
                self._create_batch_sync,
                telegram_user_id,
                direction,
                existing_batches,
            )
            saved_batch = await self.repository.save_bulk_batch(
                batch_id=batch.batch_id,
                telegram_user_id=batch.telegram_user_id,
                spreadsheet_id=batch.spreadsheet_id,
                direction=batch.direction,
                sheet_name=batch.sheet_name,
                sheet_id=batch.sheet_id,
                start_row=batch.start_row,
                data_start_row=batch.data_start_row,
                reserved_rows=batch.reserved_rows,
                status_schema_version=batch.status_schema_version,
            )
        except Exception as exc:
            return BulkBatchCreationResult(
                success=False,
                message=f"Не удалось создать место для массовой вставки. Причина: {exc}",
            )
        return BulkBatchCreationResult(
            success=True,
            message="Место для массовой вставки создано.",
            batch=saved_batch,
            insert_url=insert_url,
        )

    def _create_batch_sync(
        self,
        telegram_user_id: int,
        direction: str,
        existing_batches: list[BulkBatch],
    ) -> tuple[BulkBatch, str]:
        api = self._get_sheets_api()
        spreadsheet_id = self.direction_spreadsheets.spreadsheet_id_for(direction)
        if not spreadsheet_id:
            raise RuntimeError("Для выбранного направления не задан ID Google-таблицы")
        sheet_name = _bulk_sheet_name(direction)
        sheet_id = self._get_or_create_sheet_id(api, spreadsheet_id, sheet_name, BULK_STAGING_COLUMN_COUNT)
        existing_rows = self._read_rows(api, spreadsheet_id, sheet_name, "A:M")
        if any(
            [str(value).strip() for value in row[: len(LEGACY_BULK_STAGING_HEADERS)]]
            == LEGACY_BULK_STAGING_HEADERS
            for row in existing_rows
        ):
            raise SheetConfigurationError(
                "Лист «Массовый ввод» использует старую схему без колонки "
                "«Редактор». Вставьте колонку J во всем листе и повторите создание."
            )
        start_row = _next_batch_start_row(
            existing_rows=existing_rows,
        )
        data_start_row = start_row + 2
        batch_id = generate_batch_id()
        now = utc_now_iso()

        rows = [
            *[_empty_row() for _ in range(_spacing_rows_before_batch(existing_rows))],
            _bulk_header_row(batch_id, telegram_user_id, now, direction),
            _bulk_column_header_row(),
            *[
                _bulk_input_row(self.application_editors)
                for _ in range(self.reserved_rows)
            ],
        ]
        api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "appendCells": {
                            "sheetId": sheet_id,
                            "rows": rows,
                            "fields": "userEnteredValue,userEnteredFormat,dataValidation",
                        }
                    },
                    *_bulk_batch_status_format_rules(sheet_id, start_row),
                ]
            },
        ).execute()

        batch = BulkBatch(
            batch_id=batch_id,
            telegram_user_id=telegram_user_id,
            spreadsheet_id=spreadsheet_id,
            direction=direction,
            sheet_name=sheet_name,
            sheet_id=sheet_id,
            start_row=start_row,
            data_start_row=data_start_row,
            reserved_rows=self.reserved_rows,
            status_schema_version=2,
            created_at=now,
            updated_at=now,
        )
        return batch, self._insert_url(spreadsheet_id, sheet_id, data_start_row)

    def _get_or_create_sheet_id(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_name: str,
        column_count: int,
    ) -> int:
        metadata = api.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties",
        ).execute()
        for sheet in metadata.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("title") == sheet_name:
                return int(properties["sheetId"])

        result = api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
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
        return int(result["replies"][0]["addSheet"]["properties"]["sheetId"])

    def _read_rows(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_name: str,
        range_suffix: str,
    ) -> list[list[Any]]:
        result = api.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{quote_sheet_name(sheet_name)}!{range_suffix}",
            majorDimension="ROWS",
        ).execute()
        return result.get("values", [])

    def _insert_url(self, spreadsheet_id: str, sheet_id: int, data_start_row: int) -> str:
        return (
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
            f"#gid={sheet_id}&range=A{data_start_row}:G{data_start_row}"
        )

    def _get_sheets_api(self) -> Any:
        if self._sheets_api is None:
            self._sheets_api = build_google_sheets_api(self.credentials_path)
        return self._sheets_api


class BulkApplicationRegistrar:
    def __init__(
        self,
        *,
        repository: DraftRepository,
        spreadsheet_id: str,
        credentials_path: str,
        sheets_api: Any | None = None,
        dashboard_sync: DashboardSyncService | None = None,
        application_editors: tuple[str, ...] = ("редактор 1", "редактор 2"),
    ) -> None:
        self.repository = repository
        self.spreadsheet_id = spreadsheet_id
        self.credentials_path = credentials_path
        self._sheets_api = sheets_api
        self.dashboard_sync = dashboard_sync
        self.application_editors = application_editors

    async def register_batch(self, batch_id: str, telegram_user_id: int) -> BulkRegistrationResult:
        batch = await self.repository.get_bulk_batch(batch_id)
        if batch is None:
            return BulkRegistrationResult(
                success=False,
                message="Массовая заявка не найдена.",
            )
        if batch.telegram_user_id != telegram_user_id:
            return BulkRegistrationResult(
                success=False,
                message="Эту массовую заявку может подтвердить только ее автор.",
            )
        api = self._get_sheets_api()
        schema_error = await asyncio.to_thread(self._validate_batch_schema, api, batch)
        if schema_error:
            return BulkRegistrationResult(success=False, message=schema_error)
        registered = 0
        user_rows = 0
        actual_rows = 0
        rows = await asyncio.to_thread(self._read_batch_rows, api, batch)
        invalid_change_type_rows: list[int] = []
        normalized_change_types: list[tuple[int, str]] = []
        for offset, row in enumerate(rows):
            if not _has_user_bulk_data(row):
                continue
            row_number = batch.data_start_row + offset
            raw_change_type = _cell(row, 6)
            normalized_change_type = ChangeType.normalize(raw_change_type)
            if normalized_change_type is None:
                invalid_change_type_rows.append(row_number)
                continue
            row.extend([""] * (7 - len(row)))
            row[6] = normalized_change_type.value
            if raw_change_type.strip() != normalized_change_type.value:
                normalized_change_types.append((row_number, normalized_change_type.value))

        if invalid_change_type_rows:
            row_list = ", ".join(str(row_number) for row_number in invalid_change_type_rows)
            return BulkRegistrationResult(
                success=False,
                message=(
                    "Массовая заявка не зарегистрирована. "
                    "Укажите ADD, EDIT или CHIPS в колонке «Тип изменения» "
                    f"для строк: {row_list}."
                ),
            )
        if normalized_change_types:
            await asyncio.to_thread(
                self._write_normalized_change_types,
                api,
                batch,
                normalized_change_types,
            )
        for offset, row in enumerate(rows):
            staging_row_number = batch.data_start_row + offset
            if not _has_user_bulk_data(row):
                continue
            user_rows += 1
            actual_rows = offset + 1
            application_id = _cell(row, 7).strip()
            if application_id:
                existing = await self.repository.get_submitted_application(application_id)
                if existing is None:
                    await self._save_existing_application(batch, row, staging_row_number, application_id)
                    registered += 1
                continue

            application_id = generate_application_id()
            await asyncio.to_thread(
                self._write_registration_cells,
                api,
                batch,
                staging_row_number,
                application_id,
            )
            await self._save_new_application(batch, row, staging_row_number, application_id)
            registered += 1

        if user_rows == 0:
            return BulkRegistrationResult(
                success=False,
                message=(
                    "В массовой заявке не найдены заполненные строки. "
                    "Заполните данные в Google Sheets и нажмите «Заявка заполнена» еще раз."
                ),
            )
        if registered:
            await self.repository.update_bulk_batch_reserved_rows(
                batch.batch_id,
                reserved_rows=actual_rows,
            )
            await asyncio.to_thread(self._group_batch_rows, api, batch, actual_rows)
            await self._sync_dashboard(
                batch,
                final_answer_present=_has_final_answer(rows, final_answer_index=12),
            )
            return BulkRegistrationResult(
                success=True,
                message=f"Массовая заявка зарегистрирована. Строк зарегистрировано: {registered}.",
                registered_count=registered,
            )
        await self.repository.update_bulk_batch_reserved_rows(
            batch.batch_id,
            reserved_rows=actual_rows,
        )
        await self._sync_dashboard(
            batch,
            final_answer_present=_has_final_answer(rows, final_answer_index=12),
        )
        return BulkRegistrationResult(
            success=True,
            message="Новых строк для регистрации не найдено: все заполненные строки уже зарегистрированы.",
            registered_count=0,
        )

    async def _sync_dashboard(self, batch: BulkBatch, *, final_answer_present: bool) -> None:
        if self.dashboard_sync is None:
            return
        try:
            await asyncio.to_thread(
                self.dashboard_sync.upsert_bulk_batch,
                batch=batch,
                status=batch.batch_status,
                row_link=self._batch_row_link(batch),
                final_answer_present=final_answer_present,
                editors=await self._batch_editors(batch.batch_id),
            )
        except SheetConfigurationError:
            return

    async def _batch_editors(self, batch_id: str) -> tuple[str, ...]:
        applications = await self.repository.list_submitted_applications()
        return tuple(
            application.last_seen_editor or EDITOR_NOT_SELECTED
            for application in applications
            if application.batch_id == batch_id
        )

    @staticmethod
    def _batch_row_link(batch: BulkBatch) -> str:
        return (
            f"https://docs.google.com/spreadsheets/d/{batch.spreadsheet_id}/edit"
            f"#gid={batch.sheet_id}&range=A{batch.start_row}:M{batch.start_row}"
        )

    async def _save_existing_application(
        self,
        batch: BulkBatch,
        row: list[Any],
        row_number: int,
        application_id: str,
    ) -> None:
        await self.repository.save_submitted_application(
            application_id=application_id,
            telegram_user_id=batch.telegram_user_id,
            spreadsheet_id=batch.spreadsheet_id,
            sheet_id=batch.sheet_id,
            sheet_name=batch.sheet_name,
            last_known_status=_cell(row, 8).strip() or ApplicationStatus.NEW.value,
            direction=batch.direction,
            answer_type=_normalize_answer_type(_cell(row, 0)),
            application_type=ApplicationType.BULK.value,
            is_urgent=_bulk_row_is_urgent(row),
            batch_id=batch.batch_id,
            last_seen_row_number=row_number,
            last_seen_editor=_cell(row, 9).strip() or EDITOR_NOT_SELECTED,
            last_seen_editor_comment=_cell(row, 10).strip(),
            last_seen_final_answer=_cell(row, 12).strip(),
        )

    async def _save_new_application(
        self,
        batch: BulkBatch,
        row: list[Any],
        row_number: int,
        application_id: str,
    ) -> None:
        await self.repository.save_submitted_application(
            application_id=application_id,
            telegram_user_id=batch.telegram_user_id,
            spreadsheet_id=batch.spreadsheet_id,
            sheet_id=batch.sheet_id,
            sheet_name=batch.sheet_name,
            last_known_status=ApplicationStatus.NEW.value,
            direction=batch.direction,
            answer_type=_normalize_answer_type(_cell(row, 0)),
            application_type=ApplicationType.BULK.value,
            is_urgent=_bulk_row_is_urgent(row),
            batch_id=batch.batch_id,
            last_seen_row_number=row_number,
            last_seen_editor=EDITOR_NOT_SELECTED,
            last_seen_editor_comment="",
            last_seen_final_answer="",
        )

    def _validate_batch_schema(
        self,
        api: Any,
        batch: BulkBatch,
    ) -> str | None:
        header_row = batch.data_start_row - 1
        result = api.spreadsheets().values().get(
            spreadsheetId=batch.spreadsheet_id or self.spreadsheet_id,
            range=(
                f"{quote_sheet_name(batch.sheet_name)}!"
                f"A{header_row}:M{header_row}"
            ),
            majorDimension="ROWS",
        ).execute()
        rows = result.get("values", [])
        headers = [str(value).strip() for value in (rows[0] if rows else [])]
        if headers[: len(BULK_STAGING_HEADERS)] == BULK_STAGING_HEADERS:
            return None
        if headers[: len(LEGACY_BULK_STAGING_HEADERS)] == LEGACY_BULK_STAGING_HEADERS:
            return (
                "Массовая заявка использует старую схему без колонки «Редактор». "
                "Вставьте колонку J во всем листе «Массовый ввод» и повторите."
            )
        return "Структура колонок массовой заявки не соответствует ожидаемой схеме."

    def _get_or_create_sheet_id(
        self,
        api: Any,
        spreadsheet_id: str,
        sheet_name: str,
        column_count: int,
    ) -> int:
        metadata = api.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties",
        ).execute()
        for sheet in metadata.get("sheets", []):
            properties = sheet.get("properties", {})
            if properties.get("title") == sheet_name:
                return int(properties["sheetId"])

        result = api.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
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
        return int(result["replies"][0]["addSheet"]["properties"]["sheetId"])

    def _read_batch_rows(self, api: Any, batch: BulkBatch) -> list[list[Any]]:
        end_row = batch.data_start_row + max(
            batch.reserved_rows,
            BULK_REGISTRATION_SCAN_ROWS,
        ) - 1
        result = api.spreadsheets().values().get(
            spreadsheetId=batch.spreadsheet_id or self.spreadsheet_id,
            range=f"{quote_sheet_name(batch.sheet_name)}!A{batch.data_start_row}:M{end_row}",
            majorDimension="ROWS",
        ).execute()
        return result.get("values", [])

    def _write_registration_cells(
        self,
        api: Any,
        batch: BulkBatch,
        row_number: int,
        application_id: str,
    ) -> None:
        api.spreadsheets().batchUpdate(
            spreadsheetId=batch.spreadsheet_id or self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateCells": {
                            "range": {
                                "sheetId": batch.sheet_id,
                                "startRowIndex": row_number - 1,
                                "endRowIndex": row_number,
                                "startColumnIndex": 7,
                                "endColumnIndex": 10,
                            },
                            "rows": [
                                {
                                    "values": [
                                        _string_cell(application_id),
                                        _status_cell(batch.status_schema_version),
                                        _editor_cell(self.application_editors),
                                    ]
                                }
                            ],
                            "fields": "userEnteredValue,dataValidation,userEnteredFormat",
                        }
                    },
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": batch.sheet_id,
                                "startRowIndex": row_number - 1,
                                "endRowIndex": row_number,
                                "startColumnIndex": 0,
                                "endColumnIndex": 7,
                            },
                            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}},
                            "fields": "userEnteredFormat.backgroundColor",
                        }
                    },
                    *(
                        _bulk_application_status_format_rules(batch.sheet_id, row_number)
                        if batch.status_schema_version >= 2
                        else []
                    ),
                ]
            },
        ).execute()

    def _write_normalized_change_types(
        self,
        api: Any,
        batch: BulkBatch,
        values: list[tuple[int, str]],
    ) -> None:
        requests = [
            {
                "updateCells": {
                    "range": {
                        "sheetId": batch.sheet_id,
                        "startRowIndex": row_number - 1,
                        "endRowIndex": row_number,
                        "startColumnIndex": 6,
                        "endColumnIndex": 7,
                    },
                    "rows": [{"values": [_string_cell(value)]}],
                    "fields": "userEnteredValue",
                }
            }
            for row_number, value in values
        ]
        api.spreadsheets().batchUpdate(
            spreadsheetId=batch.spreadsheet_id or self.spreadsheet_id,
            body={"requests": requests},
        ).execute()

    def _group_batch_rows(self, api: Any, batch: BulkBatch, actual_rows: int) -> None:
        if actual_rows <= 0:
            return
        api.spreadsheets().batchUpdate(
            spreadsheetId=batch.spreadsheet_id or self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "addDimensionGroup": {
                            "range": {
                                "sheetId": batch.sheet_id,
                                "dimension": "ROWS",
                                "startIndex": batch.data_start_row - 1,
                                "endIndex": batch.data_start_row + actual_rows - 1,
                            }
                        }
                    }
                ]
            },
        ).execute()

    def _get_sheets_api(self) -> Any:
        if self._sheets_api is None:
            self._sheets_api = build_google_sheets_api(self.credentials_path)
        return self._sheets_api


def _bulk_header_row(
    batch_id: str,
    telegram_user_id: int,
    created_at: str,
    direction: str,
) -> dict[str, Any]:
    values = [{} for _ in range(BULK_STAGING_COLUMN_COUNT)]
    values[0] = _formatted_string_cell(f"Пачка {batch_id}", bold=True)
    values[1] = _formatted_string_cell(batch_id, bold=True)
    values[2] = _formatted_string_cell(created_at, bold=True)
    values[3] = _formatted_string_cell(direction, bold=True)
    values[4] = _formatted_string_cell(f"Telegram ID: {telegram_user_id}", bold=True)
    values[6] = _formatted_string_cell("Заполняйте строки ниже в колонках A:G", bold=True)
    values[10] = _bulk_batch_status_cell(BulkBatchStatus.NEW.value, status_schema_version=2)
    return {"values": values}


def _bulk_column_header_row() -> dict[str, Any]:
    return {"values": [_formatted_string_cell(header, bold=True) for header in BULK_STAGING_HEADERS]}


def _bulk_input_row(application_editors: tuple[str, ...]) -> dict[str, Any]:
    values = [_input_cell() for _ in BULK_INPUT_HEADERS]
    values[6]["dataValidation"] = {
        "condition": {
            "type": "ONE_OF_LIST",
            "values": [
                {"userEnteredValue": change_type.value}
                for change_type in ChangeType
            ],
        },
        "strict": True,
        "showCustomUi": True,
    }
    values.extend(
        [
            _service_cell(),
            _service_cell(),
            _editor_cell(application_editors),
            _service_cell(),
            _service_cell(),
            _service_cell(),
        ]
    )
    return {"values": values}


def _empty_row() -> dict[str, Any]:
    return {"values": [{} for _ in range(BULK_STAGING_COLUMN_COUNT)]}


def _bulk_batch_status_cell(
    status: str,
    *,
    status_schema_version: int,
) -> dict[str, Any]:
    statuses = (
        [status.value for status in BulkBatchStatus]
        if status_schema_version >= 2
        else [
            "Новая пачка",
            "В работе",
            "Нужны пояснения",
            "Частично готова",
            "Готова",
            "Отклонена",
            "Отложена",
        ]
    )
    return {
        "userEnteredValue": {"stringValue": status},
        "dataValidation": {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [{"userEnteredValue": value} for value in statuses],
            },
            "strict": True,
            "showCustomUi": True,
        },
        "userEnteredFormat": {
            "backgroundColor": _bulk_status_color(status),
            "textFormat": {"bold": True},
        },
    }


def _bulk_status_color(status: str) -> dict[str, float]:
    if status == BulkBatchStatus.NEW.value:
        return {"red": 0.78, "green": 0.88, "blue": 1.0}
    if status == BulkBatchStatus.IN_PROGRESS.value:
        return {"red": 1.0, "green": 0.93, "blue": 0.62}
    if status == BulkBatchStatus.DONE.value:
        return {"red": 0.74, "green": 0.93, "blue": 0.76}
    return {"red": 0.86, "green": 0.86, "blue": 0.86}


def _bulk_application_status_color(status: str) -> dict[str, float]:
    if status == BulkApplicationStatus.NEW.value:
        return {"red": 0.78, "green": 0.88, "blue": 1.0}
    if status == BulkApplicationStatus.NEEDS_CLARIFICATION.value:
        return {"red": 1.0, "green": 0.80, "blue": 0.55}
    return {"red": 0.74, "green": 0.93, "blue": 0.76}


def _bulk_batch_status_format_rules(sheet_id: int, row_number: int) -> list[dict[str, Any]]:
    return _status_format_rules(
        sheet_id=sheet_id,
        row_number=row_number,
        column_index=10,
        statuses=[item.value for item in BulkBatchStatus],
        color_getter=_bulk_status_color,
    )


def _bulk_application_status_format_rules(
    sheet_id: int,
    row_number: int,
) -> list[dict[str, Any]]:
    return _status_format_rules(
        sheet_id=sheet_id,
        row_number=row_number,
        column_index=8,
        statuses=[item.value for item in BulkApplicationStatus],
        color_getter=_bulk_application_status_color,
    )


def _status_format_rules(
    *,
    sheet_id: int,
    row_number: int,
    column_index: int,
    statuses: list[str],
    color_getter,
) -> list[dict[str, Any]]:
    return [
        {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [
                        {
                            "sheetId": sheet_id,
                            "startRowIndex": row_number - 1,
                            "endRowIndex": row_number,
                            "startColumnIndex": column_index,
                            "endColumnIndex": column_index + 1,
                        }
                    ],
                    "booleanRule": {
                        "condition": {
                            "type": "TEXT_EQ",
                            "values": [{"userEnteredValue": status}],
                        },
                        "format": {
                            "backgroundColor": color_getter(status),
                            "textFormat": {"bold": True},
                        },
                    },
                },
                "index": 0,
            }
        }
        for status in statuses
    ]


def _has_user_bulk_data(row: list[Any]) -> bool:
    return any(_cell(row, index).strip() for index in range(0, len(BULK_INPUT_HEADERS)))


def _has_final_answer(
    rows: list[list[Any]],
    *,
    final_answer_index: int = 12,
) -> bool:
    return any(
        _cell(row, final_answer_index).strip()
        for row in rows
        if _has_user_bulk_data(row)
    )


def _normalize_answer_type(value: str) -> str:
    text = value.strip()
    valid_values = {answer_type.value for answer_type in AnswerType}
    return text if text in valid_values else ""


def _bulk_row_is_urgent(row: list[Any]) -> bool:
    return _normalize_answer_type(_cell(row, 0)) == AnswerType.URGENT.value


def _cell(row: list[Any], index: int) -> str:
    if index >= len(row):
        return ""
    return str(row[index])


def _bulk_sheet_name(direction: str) -> str:
    if direction in {Direction.VOICEBOT.value, Direction.COLLECTION.value}:
        return f"{direction} {BULK_STAGING_SHEET_NAME}"
    return BULK_STAGING_SHEET_NAME


def _next_batch_start_row(
    *,
    existing_rows: list[list[Any]],
) -> int:
    last_non_empty_row = _last_non_empty_row_number(existing_rows)
    if last_non_empty_row == 0:
        return 1
    next_row = last_non_empty_row + BULK_BATCH_SPACING_ROWS + 1
    return next_row


def _spacing_rows_before_batch(existing_rows: list[list[Any]]) -> int:
    return 0 if _last_non_empty_row_number(existing_rows) == 0 else BULK_BATCH_SPACING_ROWS


def _last_non_empty_row_number(rows: list[list[Any]]) -> int:
    for index in range(len(rows), 0, -1):
        if any(str(value).strip() for value in rows[index - 1]):
            return index
    return 0


def _string_cell(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    return {"userEnteredValue": {"stringValue": str(value)}}


def _status_cell(status_schema_version: int) -> dict[str, Any]:
    if status_schema_version >= 2:
        status = BulkApplicationStatus.NEW.value
        allowed_statuses = [item.value for item in BulkApplicationStatus]
        color = _bulk_application_status_color(status)
    else:
        status = ApplicationStatus.NEW.value
        allowed_statuses = [item.value for item in ApplicationStatus]
        color = {"red": 0.78, "green": 0.88, "blue": 1.0}
    return {
        "userEnteredValue": {"stringValue": status},
        "dataValidation": {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [
                    {"userEnteredValue": allowed_status}
                    for allowed_status in allowed_statuses
                ],
            },
            "strict": True,
            "showCustomUi": True,
        },
        "userEnteredFormat": {
            "backgroundColor": color,
            "textFormat": {"bold": True},
        },
    }


def _editor_cell(application_editors: tuple[str, ...]) -> dict[str, Any]:
    cell = _service_cell()
    cell["userEnteredValue"] = {"stringValue": EDITOR_NOT_SELECTED}
    cell["dataValidation"] = _editor_data_validation_rule(application_editors)
    return cell


def _formatted_string_cell(value: Any, *, bold: bool = False) -> dict[str, Any]:
    cell = _string_cell(value)
    cell["userEnteredFormat"] = {
        "backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.94},
        "textFormat": {"bold": bold},
        "wrapStrategy": "WRAP",
    }
    return cell


def _input_cell() -> dict[str, Any]:
    return {
        "userEnteredFormat": {
            "backgroundColor": {"red": 1.0, "green": 0.97, "blue": 0.80},
            "wrapStrategy": "WRAP",
        }
    }


def _service_cell() -> dict[str, Any]:
    return {
        "userEnteredFormat": {
            "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
            "wrapStrategy": "WRAP",
        }
    }
