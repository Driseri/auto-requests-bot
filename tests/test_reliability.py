from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from app.flow import ApplicationFlow
from app.google_api import GoogleApiRetryConfig, execute_with_retry
from app.health import ExternalProbeError, check_health, write_heartbeat
from app.maintenance import create_backup, restore_backup, verify_database
from app.models import (
    AnswerType,
    BulkBatchStatus,
    ChangeType,
    Direction,
    LlmResult,
    SubmissionResult,
)
from app.repository import DraftRepository
from app.submission import InMemorySubmissionService


class CompleteLlm:
    async def check_change_description(self, context):
        return LlmResult(
            is_complete=True,
            blocking_problem=None,
            clarification_instruction=None,
        )


async def _ready_flow(tmp_path, submission_service=None):
    repository = DraftRepository(str(tmp_path / "app.db"))
    await repository.init()
    service = submission_service or InMemorySubmissionService()
    flow = ApplicationFlow(repository, CompleteLlm(), service)
    user_id = 100
    await flow.start_single(user_id)
    await flow.select_direction(user_id, Direction.FL)
    await flow.select_answer_type(user_id, AnswerType.ROLLOUT)
    await flow.select_change_type(user_id, ChangeType.ADD)
    await flow.handle_text(user_id, "intent")
    await flow.handle_text(user_id, "Writer")
    await flow.handle_text(user_id, "Reason")
    await flow.handle_text(user_id, "Change the answer and show the new result")
    await flow.handle_text(user_id, "Original text")
    await flow.select_urgency(user_id, False)
    return flow, repository, service, user_id


@pytest.mark.asyncio
async def test_parallel_single_submit_creates_one_row(tmp_path):
    flow, repository, service, user_id = await _ready_flow(tmp_path)

    first, second = await asyncio.gather(flow.submit(user_id), flow.submit(user_id))

    assert len(service.submitted) == 1
    assert "отправ" in first.text.lower()
    assert "отправ" in second.text.lower()
    draft = await repository.get_by_user_id(user_id)
    assert draft is not None
    assert draft.submission_state == "SENT"


@pytest.mark.asyncio
async def test_sent_submit_returns_previous_success_without_new_draft(tmp_path):
    flow, repository, service, user_id = await _ready_flow(tmp_path)

    await flow.submit(user_id)
    repeated = await flow.submit(user_id)

    assert len(service.submitted) == 1
    assert "уже отправлена" in repeated.text
    draft = await repository.get_by_user_id(user_id)
    assert draft is not None
    assert draft.submission_state == "SENT"


class AmbiguousSubmissionService:
    def __init__(self) -> None:
        self.rows: list[str] = []
        self.calls = 0
        self.targets: list[str] = []
        self.resolve_calls = 0

    def resolve_target(self, application):
        if application.submission_sheet_name:
            return ("spreadsheet", application.submission_sheet_name)
        self.resolve_calls += 1
        return ("spreadsheet", f"calculated-sheet-{self.resolve_calls}")

    async def submit(self, application):
        self.calls += 1
        self.targets.append(application.submission_sheet_name or "")
        application_id = application.application_id or ""
        if application_id in self.rows:
            return SubmissionResult(
                success=True,
                message="reconciled",
                spreadsheet_id="spreadsheet",
                sheet_id=1,
                sheet_name=application.submission_sheet_name,
                row_number=2,
            )
        self.rows.append(application_id)
        return SubmissionResult(success=False, message="broken pipe")


@pytest.mark.asyncio
async def test_ambiguous_append_is_reconciled_without_duplicate(tmp_path):
    service = AmbiguousSubmissionService()
    flow, repository, _, user_id = await _ready_flow(tmp_path, service)

    first = await flow.submit(user_id)
    second = await flow.submit(user_id)

    assert first.keyboard.value == "review"
    assert second.keyboard.value == "create_mode"
    assert len(service.rows) == 1
    assert service.targets == ["calculated-sheet-1", "calculated-sheet-1"]
    assert service.resolve_calls == 1
    saved = await repository.get_by_user_id(user_id)
    assert saved is not None
    assert saved.submission_state == "SENT"


class FakeHttpError(Exception):
    def __init__(self, status: int) -> None:
        self.resp = type("Response", (), {"status": status})()


@pytest.mark.parametrize(
    "error",
    [
        FakeHttpError(429),
        FakeHttpError(503),
        TimeoutError(),
        BrokenPipeError(),
        ConnectionResetError(),
    ],
)
def test_google_retry_retries_temporary_errors(error):
    attempts = 0

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error
        return "ok"

    result = execute_with_retry(
        operation,
        config=GoogleApiRetryConfig(max_attempts=2, base_seconds=0, max_seconds=0),
        operation_id="test",
    )

    assert result == "ok"
    assert attempts == 2


@pytest.mark.parametrize("error", [FakeHttpError(403), ValueError("schema")])
def test_google_retry_does_not_retry_permanent_errors(error):
    attempts = 0

    def operation():
        nonlocal attempts
        attempts += 1
        raise error

    with pytest.raises(type(error)):
        execute_with_retry(
            operation,
            config=GoogleApiRetryConfig(max_attempts=4, base_seconds=0, max_seconds=0),
            operation_id="test",
        )
    assert attempts == 1


@pytest.mark.asyncio
async def test_sqlite_pragmas_and_backup_restore(tmp_path):
    database = tmp_path / "app.db"
    repository = DraftRepository(str(database))
    await repository.init()
    await repository.get_or_create(42)

    async with repository._connection() as connection:
        journal_cursor = await connection.execute("PRAGMA journal_mode")
        synchronous_cursor = await connection.execute("PRAGMA synchronous")
        assert (await journal_cursor.fetchone())[0] == "wal"
        assert (await synchronous_cursor.fetchone())[0] == 1

    backup = create_backup(str(database), str(tmp_path / "backups"))
    verify_database(backup)
    restored = restore_backup(str(backup), str(tmp_path / "restored.db"))
    with sqlite3.connect(restored) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM drafts WHERE telegram_user_id = 42"
        ).fetchone()[0] == 1


def test_healthcheck_rejects_stale_heartbeat(tmp_path):
    database = tmp_path / "app.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE marker (id INTEGER)")
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(
        json.dumps(
            {
                "updated_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=10)
                ).isoformat(),
                "iteration": 1,
            }
        ),
        encoding="utf-8",
    )

    healthy, message = check_health(
        polling_enabled=True,
        heartbeat_path=str(heartbeat),
        sqlite_path=str(database),
        max_age_seconds=60,
    )

    assert not healthy
    assert "stale" in message


def test_healthcheck_accepts_fresh_heartbeat(tmp_path):
    database = tmp_path / "app.db"
    repository_connection = sqlite3.connect(database)
    repository_connection.execute("CREATE TABLE marker (id INTEGER)")
    repository_connection.commit()
    repository_connection.close()
    heartbeat = tmp_path / "heartbeat.json"
    write_heartbeat(str(heartbeat), iteration=2)

    healthy, message = check_health(
        polling_enabled=True,
        heartbeat_path=str(heartbeat),
        sqlite_path=str(database),
        max_age_seconds=60,
    )

    assert healthy
    assert message == "ok"


def _health_files(tmp_path):
    database = tmp_path / "app.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE marker (id INTEGER)")
    heartbeat = tmp_path / "heartbeat.json"
    write_heartbeat(str(heartbeat), iteration=1)
    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "bot@example.test",
                "private_key": "test-private-key",
            }
        ),
        encoding="utf-8",
    )
    return database, heartbeat, credentials


def _external_health_kwargs(tmp_path):
    database, heartbeat, credentials = _health_files(tmp_path)
    return {
        "polling_enabled": True,
        "heartbeat_path": str(heartbeat),
        "sqlite_path": str(database),
        "max_age_seconds": 60,
        "telegram_bot_token": "test-token",
        "google_credentials_path": str(credentials),
        "required_spreadsheet_ids": {
            "FL": "fl-sheet",
            "SME": "sme-sheet",
            "AI": "ai-sheet",
            "VOICE": "voice-sheet",
        },
        "optional_spreadsheet_ids": {"DASHBOARD": "dashboard-sheet"},
        "external_cache_path": str(tmp_path / "external-health.json"),
        "external_check_interval_seconds": 300,
        "external_failure_threshold": 2,
        "external_timeout_seconds": 10,
    }


def test_healthcheck_rejects_missing_required_spreadsheet_id(tmp_path):
    kwargs = _external_health_kwargs(tmp_path)
    kwargs["required_spreadsheet_ids"]["SME"] = ""

    healthy, message = check_health(
        **kwargs,
        telegram_probe=lambda *_: None,
        google_probe=lambda *_: None,
    )

    assert not healthy
    assert "SME" in message


@pytest.mark.parametrize(
    ("contents", "message_fragment"),
    [
        ("not-json", "JSON is invalid"),
        (json.dumps({"type": "service_account"}), "fields are missing"),
        (
            json.dumps(
                {
                    "type": "authorized_user",
                    "client_email": "bot@example.test",
                    "private_key": "key",
                }
            ),
            "must be a service account",
        ),
    ],
)
def test_healthcheck_rejects_invalid_google_credentials(tmp_path, contents, message_fragment):
    kwargs = _external_health_kwargs(tmp_path)
    Path(kwargs["google_credentials_path"]).write_text(contents, encoding="utf-8")

    healthy, message = check_health(
        **kwargs,
        telegram_probe=lambda *_: None,
        google_probe=lambda *_: None,
    )

    assert not healthy
    assert message_fragment in message


def test_healthcheck_rejects_missing_google_credentials_file(tmp_path):
    kwargs = _external_health_kwargs(tmp_path)
    Path(kwargs["google_credentials_path"]).unlink()

    healthy, message = check_health(
        **kwargs,
        telegram_probe=lambda *_: None,
        google_probe=lambda *_: None,
    )

    assert not healthy
    assert "credentials file is unavailable" in message


def test_healthcheck_caches_successful_external_checks(tmp_path):
    kwargs = _external_health_kwargs(tmp_path)
    calls = {"telegram": 0, "google": 0}
    checked_ids = []

    def telegram_probe(*_):
        calls["telegram"] += 1

    def google_probe(_, spreadsheet_ids, __):
        calls["google"] += 1
        checked_ids.extend(spreadsheet_ids)

    first_time = datetime(2026, 6, 14, 10, 0, tzinfo=timezone.utc)
    first = check_health(
        **kwargs,
        telegram_probe=telegram_probe,
        google_probe=google_probe,
        now=first_time,
    )
    second = check_health(
        **kwargs,
        telegram_probe=telegram_probe,
        google_probe=google_probe,
        now=first_time + timedelta(seconds=299),
    )

    assert first == (True, "ok")
    assert second == (True, "ok")
    assert calls == {"telegram": 1, "google": 1}
    assert checked_ids == [
        "fl-sheet",
        "sme-sheet",
        "ai-sheet",
        "voice-sheet",
        "dashboard-sheet",
    ]
    assert not Path(f"{kwargs['external_cache_path']}.tmp").exists()


def test_healthcheck_marks_second_consecutive_external_failure_unhealthy(tmp_path):
    kwargs = _external_health_kwargs(tmp_path)
    current = datetime(2026, 6, 14, 10, 0, tzinfo=timezone.utc)
    assert check_health(
        **kwargs,
        telegram_probe=lambda *_: None,
        google_probe=lambda *_: None,
        now=current,
    ) == (True, "ok")

    def failing_probe(*_):
        raise ExternalProbeError("telegram", status=503)

    first_failure = check_health(
        **kwargs,
        telegram_probe=failing_probe,
        google_probe=lambda *_: None,
        now=current + timedelta(seconds=301),
    )
    second_failure = check_health(
        **kwargs,
        telegram_probe=failing_probe,
        google_probe=lambda *_: None,
        now=current + timedelta(seconds=602),
    )

    assert first_failure == (True, "ok (external checks degraded: telegram: HTTP 503)")
    assert second_failure == (False, "external checks failed: telegram: HTTP 503")


def test_healthcheck_first_external_failure_without_success_is_unhealthy(tmp_path):
    kwargs = _external_health_kwargs(tmp_path)

    def failing_probe(*_):
        raise TimeoutError("secret details must not be exposed")

    healthy, message = check_health(
        **kwargs,
        telegram_probe=failing_probe,
        google_probe=lambda *_: None,
        now=datetime(2026, 6, 14, 10, 0, tzinfo=timezone.utc),
    )

    assert not healthy
    assert message == "external checks failed: external: TimeoutError"
    assert "secret details" not in message


def test_healthcheck_recovers_after_external_failure(tmp_path):
    kwargs = _external_health_kwargs(tmp_path)
    current = datetime(2026, 6, 14, 10, 0, tzinfo=timezone.utc)

    def failing_probe(*_):
        raise ExternalProbeError("google", status=403)

    assert not check_health(
        **kwargs,
        telegram_probe=lambda *_: None,
        google_probe=failing_probe,
        now=current,
    )[0]
    assert check_health(
        **kwargs,
        telegram_probe=lambda *_: None,
        google_probe=lambda *_: None,
        now=current + timedelta(seconds=301),
    ) == (True, "ok")


@pytest.mark.asyncio
async def test_completed_bulk_batches_are_excluded_from_active_polling(tmp_path):
    repository = DraftRepository(str(tmp_path / "active_batches.db"))
    await repository.init()
    for batch_id in ("BATCH-AAAABBBB", "BATCH-CCCCDDDD"):
        await repository.save_bulk_batch(
            batch_id=batch_id,
            telegram_user_id=100,
            spreadsheet_id="spreadsheet",
            direction=Direction.FL.value,
            sheet_name="Массовый ввод",
            sheet_id=1,
            start_row=1,
            data_start_row=3,
            reserved_rows=10,
        )
    await repository.update_bulk_batch_status(
        "BATCH-AAAABBBB",
        batch_status=BulkBatchStatus.DONE.value,
        last_known_batch_status=BulkBatchStatus.DONE.value,
    )

    active = await repository.list_active_bulk_batches()

    assert [batch.batch_id for batch in active] == ["BATCH-CCCCDDDD"]
