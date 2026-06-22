from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
from typing import Any

from app.bulk import BulkApplicationRegistrar, GoogleSheetsBulkBatchService
from app.config import load_settings
from app.flow import ApplicationFlow
from app.google_api import execute_with_retry_async
from app.models import (
    AnswerType,
    ChangeType,
    Direction,
    LlmContext,
    LlmResult,
)
from app.notifications import GoogleSheetsStatusReader, StatusNotificationService
from app.repository import DraftRepository
from app.submission import (
    DashboardSyncService,
    DirectionSpreadsheetConfig,
    GoogleSheetsSubmissionService,
    build_google_sheets_api,
    quote_sheet_name,
    spreadsheet_row_link,
)


LOADTEST_USER_ID_BASE = 9_100_000_000


@dataclass(frozen=True, slots=True)
class LoadProfile:
    name: str
    users: int
    singles_per_user: int
    bulk_batches: int
    bulk_rows: int
    polling_cycles: int


PROFILES: dict[str, LoadProfile] = {
    "baseline": LoadProfile(
        name="baseline",
        users=5,
        singles_per_user=1,
        bulk_batches=0,
        bulk_rows=0,
        polling_cycles=5,
    ),
    "pilot15": LoadProfile(
        name="pilot15",
        users=15,
        singles_per_user=10,
        bulk_batches=3,
        bulk_rows=30,
        polling_cycles=30,
    ),
    "stress": LoadProfile(
        name="stress",
        users=30,
        singles_per_user=5,
        bulk_batches=5,
        bulk_rows=30,
        polling_cycles=60,
    ),
}


@dataclass(slots=True)
class LoadtestState:
    application_ids: list[str] = field(default_factory=list)
    batch_ids: list[str] = field(default_factory=list)
    user_ids: list[int] = field(default_factory=list)
    google_ranges: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class OperationError:
    operation: str
    user_id: int | None
    message: str


@dataclass(slots=True)
class LoadtestMetrics:
    single_seconds: list[float] = field(default_factory=list)
    bulk_seconds: list[float] = field(default_factory=list)
    polling_seconds: list[float] = field(default_factory=list)
    errors: list[OperationError] = field(default_factory=list)


class CompleteFakeLlm:
    calls = 0

    async def check_change_description(self, context: LlmContext) -> LlmResult:
        self.calls += 1
        return LlmResult(
            is_complete=True,
            blocking_problem=None,
            clarification_instruction=None,
        )


class CountingNotifier:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: Any | None = None,
        link_preview_options: Any | None = None,
    ) -> Any:
        self.messages.append(
            {
                "chat_id": chat_id,
                "text_len": len(text),
                "parse_mode": parse_mode,
                "has_reply_markup": reply_markup is not None,
            }
        )
        return SimpleNamespace(message_id=len(self.messages))


def default_run_id() -> str:
    return "LOADTEST-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def load_profile(name: str, *, polling_cycles: int | None = None) -> LoadProfile:
    profile = PROFILES[name]
    if polling_cycles is None:
        return profile
    return LoadProfile(
        name=profile.name,
        users=profile.users,
        singles_per_user=profile.singles_per_user,
        bulk_batches=profile.bulk_batches,
        bulk_rows=profile.bulk_rows,
        polling_cycles=polling_cycles,
    )


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percent)))
    return ordered[index]


def timing_summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    return {
        "count": len(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values),
    }


def process_memory_mb() -> dict[str, float | None]:
    statm = Path("/proc/self/statm")
    if statm.exists():
        try:
            pages = [int(value) for value in statm.read_text(encoding="utf-8").split()]
            page_size = os.sysconf("SC_PAGE_SIZE")
            rss = pages[1] * page_size / 1024 / 1024
            vms = pages[0] * page_size / 1024 / 1024
            return {"rss_mb": round(rss, 1), "vms_mb": round(vms, 1)}
        except (OSError, ValueError, IndexError):
            pass
    return {"rss_mb": None, "vms_mb": None}


def sqlite_counts(sqlite_path: str) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        tables = [
            "drafts",
            "submitted_applications",
            "bulk_batches",
            "bulk_creation_requests",
            "notification_outbox",
            "dashboard_outbox",
        ]
        counts: dict[str, Any] = {}
        for table in tables:
            counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        counts["notification_outbox_states"] = dict(
            connection.execute(
                "SELECT state, COUNT(*) FROM notification_outbox GROUP BY state"
            ).fetchall()
        )
        counts["dashboard_outbox_states"] = dict(
            connection.execute(
                "SELECT state, COUNT(*) FROM dashboard_outbox GROUP BY state"
            ).fetchall()
        )
        return counts
    finally:
        connection.close()


def cleanup_sqlite(sqlite_path: str, run_id: str, state: LoadtestState) -> dict[str, int]:
    connection = sqlite3.connect(sqlite_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        result: dict[str, int] = {}
        application_ids = _unique(
            [
                *state.application_ids,
                *_select_in(
                    connection,
                    "submitted_applications",
                    "application_id",
                    "telegram_user_id",
                    state.user_ids,
                ),
            ]
        )
        batch_ids = _unique(
            [
                *state.batch_ids,
                *_select_in(
                    connection,
                    "bulk_batches",
                    "batch_id",
                    "telegram_user_id",
                    state.user_ids,
                ),
                *_select_in(
                    connection,
                    "submitted_applications",
                    "batch_id",
                    "telegram_user_id",
                    state.user_ids,
                ),
            ]
        )

        result["dashboard_outbox_applications"] = _delete_in(
            connection,
            "DELETE FROM dashboard_outbox WHERE entity_type = 'APPLICATION' AND entity_id IN ({})",
            application_ids,
        )
        result["dashboard_outbox_batches"] = _delete_in(
            connection,
            "DELETE FROM dashboard_outbox WHERE entity_type = 'BULK_BATCH' AND entity_id IN ({})",
            batch_ids,
        )
        result["submitted_applications"] = _delete_in(
            connection,
            "DELETE FROM submitted_applications WHERE application_id IN ({})",
            application_ids,
        )
        result["submitted_applications_by_batch"] = _delete_in(
            connection,
            "DELETE FROM submitted_applications WHERE batch_id IN ({})",
            batch_ids,
        )
        result["bulk_creation_requests"] = _delete_in(
            connection,
            "DELETE FROM bulk_creation_requests WHERE batch_id IN ({})",
            batch_ids,
        )
        result["bulk_creation_requests_by_user"] = _delete_in_if_column(
            connection,
            "bulk_creation_requests",
            "telegram_user_id",
            state.user_ids,
        )
        result["bulk_batches"] = _delete_in(
            connection,
            "DELETE FROM bulk_batches WHERE batch_id IN ({})",
            batch_ids,
        )
        result["drafts"] = _delete_in(
            connection,
            "DELETE FROM drafts WHERE telegram_user_id IN ({})",
            state.user_ids,
        )
        result["user_settings_pending"] = _delete_in(
            connection,
            "DELETE FROM user_settings WHERE telegram_user_id IN ({})",
            state.user_ids,
        )
        result["notification_outbox_by_run_id"] = connection.execute(
            """
            DELETE FROM notification_outbox
            WHERE snapshot_json LIKE ? OR html LIKE ?
            """,
            (f"%{run_id}%", f"%{run_id}%"),
        ).rowcount
        result["dashboard_outbox_by_run_id"] = connection.execute(
            """
            DELETE FROM dashboard_outbox
            WHERE snapshot_json LIKE ?
            """,
            (f"%{run_id}%",),
        ).rowcount

        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _delete_in(connection: sqlite3.Connection, sql_template: str, values: list[Any]) -> int:
    if not values:
        return 0
    placeholders = ",".join("?" for _ in values)
    return connection.execute(sql_template.format(placeholders), values).rowcount


def _delete_in_if_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    values: list[Any],
) -> int:
    if not values or not _has_column(connection, table, column):
        return 0
    return _delete_in(
        connection,
        f"DELETE FROM {table} WHERE {column} IN ({{}})",
        values,
    )


def _select_in(
    connection: sqlite3.Connection,
    table: str,
    select_column: str,
    where_column: str,
    values: list[Any],
) -> list[Any]:
    if not values or not _has_column(connection, table, select_column):
        return []
    if not _has_column(connection, table, where_column):
        return []
    placeholders = ",".join("?" for _ in values)
    rows = connection.execute(
        f"""
        SELECT DISTINCT {select_column}
        FROM {table}
        WHERE {where_column} IN ({placeholders})
          AND {select_column} IS NOT NULL
        """,
        values,
    ).fetchall()
    return [row[0] for row in rows if row[0]]


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def _unique(values: list[Any]) -> list[Any]:
    result = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


async def run_loadtest(
    *,
    profile: LoadProfile,
    run_id: str,
    concurrency: int,
    cleanup: bool,
    report_path: str | None,
    google_throttle_seconds: float = 0.2,
) -> dict[str, Any]:
    settings = load_settings()
    repository = DraftRepository(settings.sqlite_path)
    await repository.init()

    direction_spreadsheets = DirectionSpreadsheetConfig(
        fl_spreadsheet_id=settings.google_fl_spreadsheet_id,
        sme_spreadsheet_id=settings.google_sme_spreadsheet_id,
        ai_spreadsheet_id=settings.google_ai_spreadsheet_id,
        voice_collection_spreadsheet_id=settings.google_voice_collection_spreadsheet_id,
    )
    dashboard_sync = (
        DashboardSyncService(
            spreadsheet_id=settings.google_dashboard_spreadsheet_id,
            credentials_path=settings.google_credentials_path,
            application_editors=settings.application_editors,
            timezone_name=settings.rollout_schedule.timezone_name,
        )
        if settings.google_dashboard_spreadsheet_id
        else None
    )
    submission_service = GoogleSheetsSubmissionService(
        direction_spreadsheets=direction_spreadsheets,
        dashboard_spreadsheet_id=settings.google_dashboard_spreadsheet_id,
        credentials_path=settings.google_credentials_path,
        rollout_schedule=settings.rollout_schedule,
        timezone_name=settings.rollout_schedule.timezone_name,
        application_editors=settings.application_editors,
        dashboard_sync=dashboard_sync,
        google_api_retry=settings.google_api_retry,
    )
    bulk_service = GoogleSheetsBulkBatchService(
        direction_spreadsheets=direction_spreadsheets,
        credentials_path=settings.google_credentials_path,
        repository=repository,
        application_editors=settings.application_editors,
        reserved_rows=settings.bulk_reserved_rows,
        google_api_retry=settings.google_api_retry,
        timezone_name=settings.rollout_schedule.timezone_name,
    )
    bulk_registrar = BulkApplicationRegistrar(
        repository=repository,
        spreadsheet_id=settings.google_fl_spreadsheet_id,
        credentials_path=settings.google_credentials_path,
        dashboard_sync=dashboard_sync,
        application_editors=settings.application_editors,
        registration_stale_seconds=settings.bulk_registration_stale_seconds,
        google_api_retry=settings.google_api_retry,
    )
    flow = ApplicationFlow(
        repository=repository,
        llm_client=CompleteFakeLlm(),  # type: ignore[arg-type]
        submission_service=submission_service,
        bulk_service=bulk_service,
        bulk_registrar=bulk_registrar,
        bulk_reserved_rows=settings.bulk_reserved_rows,
        bulk_creation_stale_seconds=settings.bulk_creation_stale_seconds,
        dashboard_enabled=bool(settings.google_dashboard_spreadsheet_id),
    )
    notifier = CountingNotifier()
    status_service = StatusNotificationService(
        repository=repository,
        status_reader=GoogleSheetsStatusReader(
            direction_spreadsheets=direction_spreadsheets,
            credentials_path=settings.google_credentials_path,
            google_api_retry=settings.google_api_retry,
        ),
        dashboard_sync=dashboard_sync,
        dashboard_sync_interval_seconds=settings.dashboard_sync_interval_seconds,
        status_not_found_threshold=settings.status_not_found_threshold,
        status_not_found_recheck_seconds=settings.status_not_found_recheck_seconds,
        completed_bulk_dashboard_scan_interval_seconds=(
            settings.completed_bulk_dashboard_scan_interval_seconds
        ),
        dashboard_outbox_retry_base_seconds=settings.dashboard_outbox_retry_base_seconds,
        dashboard_outbox_retry_max_seconds=settings.dashboard_outbox_retry_max_seconds,
        dashboard_outbox_sending_stale_seconds=settings.dashboard_outbox_sending_stale_seconds,
        google_api_retry=settings.google_api_retry,
        notifier=notifier,
        fallback_spreadsheet_id=settings.google_dashboard_spreadsheet_id,
        notification_max_attempts=settings.notification_max_attempts,
        notification_retry_base_seconds=settings.notification_retry_base_seconds,
        notification_sending_stale_seconds=settings.notification_sending_stale_seconds,
        notification_message_max_chars=settings.notification_message_max_chars,
    )

    state = LoadtestState(
        user_ids=[LOADTEST_USER_ID_BASE + index for index in range(1, profile.users + 1)]
    )
    metrics = LoadtestMetrics()
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    memory_checkpoints: dict[str, dict[str, float | None]] = {
        "before": process_memory_mb(),
    }
    counts_before = sqlite_counts(settings.sqlite_path)

    semaphore = asyncio.Semaphore(concurrency)
    single_tasks = [
        _guarded_user_singles(
            semaphore,
            flow,
            state,
            metrics,
            run_id,
            user_id,
            profile.singles_per_user,
            google_throttle_seconds=google_throttle_seconds,
        )
        for user_id in state.user_ids
    ]
    if single_tasks:
        await asyncio.gather(*single_tasks)
    memory_checkpoints["after_singles"] = memory_cleanup()

    bulk_tasks = [
        _guarded_bulk(
            semaphore,
            flow,
            repository,
            state,
            metrics,
            run_id,
            user_id=state.user_ids[index % len(state.user_ids)],
            batch_index=index + 1,
            rows=profile.bulk_rows,
            credentials_path=settings.google_credentials_path,
            google_api_retry=settings.google_api_retry,
            google_throttle_seconds=google_throttle_seconds,
        )
        for index in range(profile.bulk_batches)
    ]
    if bulk_tasks:
        await asyncio.gather(*bulk_tasks)
    memory_checkpoints["after_bulk"] = memory_cleanup()

    for _ in range(profile.polling_cycles):
        cycle_started = time.perf_counter()
        try:
            await status_service.run_once()
        except Exception as exc:
            metrics.errors.append(
                OperationError(
                    operation="polling",
                    user_id=None,
                    message=_safe_error(exc),
                )
            )
        metrics.polling_seconds.append(time.perf_counter() - cycle_started)
        memory_cleanup()
        await _throttle(google_throttle_seconds)
    memory_checkpoints["after_polling"] = memory_cleanup()

    counts_after_run = sqlite_counts(settings.sqlite_path)
    cleanup_result: dict[str, int] = {}
    if cleanup:
        cleanup_result = cleanup_sqlite(settings.sqlite_path, run_id, state)
    counts_after_cleanup = sqlite_counts(settings.sqlite_path)
    memory_checkpoints["after_sqlite_cleanup"] = memory_cleanup()
    finished = time.perf_counter()
    memory_checkpoints["after_final_cleanup"] = memory_cleanup()

    report = {
        "profile": asdict(profile),
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "single_created": len(state.application_ids)
            - sum(1 for item in state.application_ids if item.startswith("BULK:")),
            "bulk_batches_created": len(state.batch_ids),
            "bulk_rows_registered": sum(
                1 for item in state.application_ids if item.startswith("BULK:")
            ),
            "polling_cycles": profile.polling_cycles,
            "fake_telegram_messages": len(notifier.messages),
        },
        "timings": {
            "total_seconds": finished - started,
            "single": timing_summary(metrics.single_seconds),
            "bulk": timing_summary(metrics.bulk_seconds),
            "polling": timing_summary(metrics.polling_seconds),
        },
        "sqlite": {
            "before": counts_before,
            "after_run": counts_after_run,
            "after_cleanup": counts_after_cleanup,
            "cleanup": cleanup_result,
        },
        "memory": {
            "before": memory_checkpoints["before"],
            "after": memory_checkpoints["after_final_cleanup"],
            "checkpoints": memory_checkpoints,
        },
        "created": {
            "user_ids": state.user_ids,
            "application_ids": [
                item for item in state.application_ids if not item.startswith("BULK:")
            ],
            "bulk_application_ids": [
                item.removeprefix("BULK:") for item in state.application_ids if item.startswith("BULK:")
            ],
            "batch_ids": state.batch_ids,
            "google_ranges": state.google_ranges,
        },
        "errors": [asdict(error) for error in metrics.errors],
        "manual_google_cleanup": {
            "required": True,
            "run_id": run_id,
            "note": "SQLite cleanup does not remove rows from Google Sheets.",
            "ranges": state.google_ranges,
        },
    }
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


async def _guarded_single(
    semaphore: asyncio.Semaphore,
    flow: ApplicationFlow,
    state: LoadtestState,
    metrics: LoadtestMetrics,
    run_id: str,
    user_id: int,
    item_index: int,
) -> None:
    async with semaphore:
        started = time.perf_counter()
        try:
            application = await _create_single_application(flow, run_id, user_id, item_index)
            state.application_ids.append(application["application_id"])
            state.google_ranges.append(application["google_range"])
        except Exception as exc:
            metrics.errors.append(
                OperationError(
                    operation="single",
                    user_id=user_id,
                    message=_safe_error(exc),
                )
            )
        metrics.single_seconds.append(time.perf_counter() - started)


async def _guarded_user_singles(
    semaphore: asyncio.Semaphore,
    flow: ApplicationFlow,
    state: LoadtestState,
    metrics: LoadtestMetrics,
    run_id: str,
    user_id: int,
    singles_per_user: int,
    *,
    google_throttle_seconds: float,
) -> None:
    async with semaphore:
        for item_index in range(1, singles_per_user + 1):
            await _guarded_single(
                _NoopSemaphore(),
                flow,
                state,
                metrics,
                run_id,
                user_id,
                item_index,
            )
            memory_cleanup()
            await _throttle(google_throttle_seconds)


async def _create_single_application(
    flow: ApplicationFlow,
    run_id: str,
    user_id: int,
    item_index: int,
) -> dict[str, Any]:
    await flow.start_single(user_id, force=True, author_name=f"{run_id} author {user_id}")
    await flow.select_direction(user_id, Direction.FL)
    await flow.select_answer_type(user_id, AnswerType.ROLLOUT)
    await flow.select_change_type(user_id, ChangeType.ADD)
    await flow.handle_text(user_id, f"{run_id}.intent.single.{user_id}.{item_index}")
    await flow.handle_text(user_id, f"{run_id} scriptwriter {user_id}")
    await flow.handle_text(user_id, f"{run_id} reason single {item_index}")
    await flow.handle_text(
        user_id,
        (
            f"{run_id} change single {item_index}: добавить тестовую информацию, "
            "чтобы проверить нагрузку пилота."
        ),
    )
    await flow.handle_text(user_id, f"{run_id} source text single {item_index}")
    result = await flow.submit(user_id)
    if result.draft is None or not result.draft.application_id:
        raise RuntimeError(f"single submit did not return draft/application_id: {result.text[:200]}")
    if "Не удалось" in result.text:
        raise RuntimeError(result.text[:300])
    row_link = None
    if (
        result.draft.submission_spreadsheet_id
        and result.draft.submission_sheet_id is not None
        and result.draft.submission_row_number is not None
    ):
        row_link = spreadsheet_row_link(
            spreadsheet_id=result.draft.submission_spreadsheet_id,
            sheet_id=result.draft.submission_sheet_id,
            row_number=result.draft.submission_row_number,
            end_column="X",
        )
    return {
        "application_id": result.draft.application_id,
        "google_range": {
            "kind": "single",
            "application_id": result.draft.application_id,
            "spreadsheet_id": result.draft.submission_spreadsheet_id,
            "sheet_id": result.draft.submission_sheet_id,
            "sheet_name": result.draft.submission_sheet_name,
            "row_number": result.draft.submission_row_number,
            "row_link": row_link,
        },
    }


async def _guarded_bulk(
    semaphore: asyncio.Semaphore,
    flow: ApplicationFlow,
    repository: DraftRepository,
    state: LoadtestState,
    metrics: LoadtestMetrics,
    run_id: str,
    *,
    user_id: int,
    batch_index: int,
    rows: int,
    credentials_path: str,
    google_api_retry: Any,
    google_throttle_seconds: float,
) -> None:
    async with semaphore:
        started = time.perf_counter()
        try:
            batch_id, registered_ids, google_range = await _create_bulk_batch(
                flow,
                repository,
                run_id,
                user_id=user_id,
                batch_index=batch_index,
                rows=rows,
                credentials_path=credentials_path,
                google_api_retry=google_api_retry,
                google_throttle_seconds=google_throttle_seconds,
            )
            state.batch_ids.append(batch_id)
            state.application_ids.extend([f"BULK:{item}" for item in registered_ids])
            state.google_ranges.append(google_range)
        except Exception as exc:
            metrics.errors.append(
                OperationError(
                    operation="bulk",
                    user_id=user_id,
                    message=_safe_error(exc),
                )
            )
        metrics.bulk_seconds.append(time.perf_counter() - started)


async def _create_bulk_batch(
    flow: ApplicationFlow,
    repository: DraftRepository,
    run_id: str,
    *,
    user_id: int,
    batch_index: int,
    rows: int,
    credentials_path: str,
    google_api_retry: Any,
    google_throttle_seconds: float,
) -> tuple[str, list[str], dict[str, Any]]:
    response = await flow.create_bulk_batch(user_id)
    if not response.keyboard_payload:
        raise RuntimeError(f"bulk create did not return idempotency key: {response.text[:200]}")
    response = await flow.select_bulk_direction(
        user_id,
        response.keyboard_payload,
        Direction.FL,
    )
    if not response.keyboard_payload:
        raise RuntimeError(f"bulk direction did not return batch id: {response.text[:200]}")
    batch_id = response.keyboard_payload
    batch = await repository.get_bulk_batch(batch_id)
    if batch is None:
        raise RuntimeError(f"bulk batch was not saved: {batch_id}")

    await _fill_bulk_rows(
        batch,
        run_id=run_id,
        batch_index=batch_index,
        rows=rows,
        credentials_path=credentials_path,
        google_api_retry=google_api_retry,
    )
    await _throttle(google_throttle_seconds)
    response = await flow.confirm_bulk_batch_filled(user_id, batch_id)
    if "Не удалось" in response.text or "не зарегистрирована" in response.text:
        raise RuntimeError(response.text[:300])
    applications = await repository.list_submitted_applications(include_deferred=True)
    registered_ids = [
        application.application_id
        for application in applications
        if application.batch_id == batch_id
    ]
    return (
        batch_id,
        registered_ids,
        {
            "spreadsheet_id": batch.spreadsheet_id,
            "sheet_name": batch.sheet_name,
            "range": f"A{batch.start_row}:N{batch.data_start_row + rows - 1}",
            "batch_id": batch_id,
        },
    )


async def _fill_bulk_rows(
    batch: Any,
    *,
    run_id: str,
    batch_index: int,
    rows: int,
    credentials_path: str,
    google_api_retry: Any,
) -> None:
    values = [
        [
            AnswerType.ROLLOUT.value,
            ChangeType.ADD.value,
            f"{run_id} scriptwriter bulk {batch_index}",
            f"{run_id}.intent.bulk.{batch_index}.{row_index}",
            f"{run_id} reason bulk {batch_index}.{row_index}",
            f"{run_id} change bulk {batch_index}.{row_index}",
            f"{run_id} source bulk {batch_index}.{row_index}",
        ]
        for row_index in range(1, rows + 1)
    ]

    def operation() -> Any:
        api = build_google_sheets_api(credentials_path)
        return api.spreadsheets().values().update(
            spreadsheetId=batch.spreadsheet_id,
            range=(
                f"{quote_sheet_name(batch.sheet_name)}!"
                f"A{batch.data_start_row}:G{batch.data_start_row + rows - 1}"
            ),
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()

    await execute_with_retry_async(
        operation,
        config=google_api_retry,
        operation_id=f"loadtest-fill-bulk:{batch.batch_id}",
    )


class _NoopSemaphore:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


async def _throttle(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


def memory_cleanup() -> dict[str, float | None]:
    gc.collect()
    _malloc_trim()
    return process_memory_mb()


def _malloc_trim() -> None:
    if os.name != "posix":
        return
    try:
        libc = ctypes.CDLL("libc.so.6")
        trim = getattr(libc, "malloc_trim", None)
        if trim is not None:
            trim(0)
    except Exception:
        return


def _safe_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:500]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run scenario load test for the bot.")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="baseline")
    parser.add_argument("--run-id", default=default_run_id())
    parser.add_argument("--cleanup-sqlite", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--google-throttle-seconds", type=float, default=0.2)
    parser.add_argument("--polling-cycles", type=int)
    parser.add_argument("--report-path")
    return parser.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.concurrency <= 0:
        raise SystemExit("--concurrency must be greater than 0")
    if args.google_throttle_seconds < 0:
        raise SystemExit("--google-throttle-seconds must be zero or greater")
    profile = load_profile(args.profile, polling_cycles=args.polling_cycles)
    report = await run_loadtest(
        profile=profile,
        run_id=args.run_id,
        concurrency=args.concurrency,
        cleanup=args.cleanup_sqlite,
        report_path=args.report_path,
        google_throttle_seconds=args.google_throttle_seconds,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["errors"] else 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
