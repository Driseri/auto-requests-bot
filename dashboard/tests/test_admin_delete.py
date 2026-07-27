from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from fastapi.testclient import TestClient

from backend.admin_delete import (
    _CONTAINER_SCRIPT,
    AdminDeleteService,
    build_admin_delete_command,
    confirmation_phrase,
    normalize_application_ids,
)
from backend.app import create_app
from backend.collector import DashboardCollector
from backend.config import RemoteConfig, load_config
from backend.storage import JsonStorage

from test_collector_api import FakeRunner


class FakeAdminRunner:
    """Test runner that returns deterministic admin delete JSON."""

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or sample_preview_payload()
        self.calls = 0
        self.commands: list[str] = []

    def run(self, command: str) -> str:
        self.calls += 1
        self.commands.append(command)
        return json.dumps(self.payload, ensure_ascii=False)


class SlowAdminRunner(FakeAdminRunner):
    """Runner slow enough to verify the execute overlap guard."""

    def run(self, command: str) -> str:
        time.sleep(0.15)
        return super().run(command)


class FailingAdminRunner(FakeAdminRunner):
    """Runner that simulates SSH failure for error-path tests."""

    def run(self, command: str) -> str:
        self.calls += 1
        self.commands.append(command)
        raise RuntimeError("ssh failed")


def sample_preview_payload() -> dict:
    return {
        "status": "ok",
        "mode": "preview",
        "application_ids": ["APP-1"],
        "errors": [],
        "preview": {
            "application_ids": ["APP-1"],
            "confirmation_phrase": "DELETE APP-1",
            "counts": {
                "submitted_applications": 1,
                "dashboard_outbox": 1,
                "notification_outbox": 1,
            },
            "export": {
                "submitted_applications": [{"application_id": "APP-1", "batch_id": "BATCH-1"}],
                "dashboard_outbox": [],
                "notification_outbox": [],
            },
        },
    }


def sample_execute_payload() -> dict:
    return {
        "status": "ok",
        "mode": "execute",
        "application_ids": ["APP-1"],
        "errors": [],
        "execution": {
            "deleted": {
                "submitted_applications": 1,
                "dashboard_outbox": 1,
                "notification_outbox": 1,
            },
            "remaining": {"counts": {"submitted_applications": 0}},
        },
    }


def make_config(tmp_path: Path, *, admin_enabled: bool = False):
    config_path = tmp_path / "config.local.toml"
    config_path.write_text(
        f"""
[ssh]
host = "vps"
username = "root"
password = "secret"

[collector]
auto_collect = false

[admin]
destructive_actions_enabled = {str(admin_enabled).lower()}
max_delete_ids = 20

[storage]
data_dir = "{(tmp_path / 'data').as_posix()}"
""",
        encoding="utf-8",
    )
    return load_config(config_path)


def make_client(tmp_path: Path, runner: FakeAdminRunner, *, admin_enabled: bool = False) -> TestClient:
    config = make_config(tmp_path, admin_enabled=admin_enabled)
    storage = JsonStorage(config.storage.data_dir)
    collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner())
    admin_service = AdminDeleteService(config=config, storage=storage, runner=runner)
    app = create_app(
        config_path=tmp_path / "config.local.toml",
        collector=collector,
        admin_delete_service=admin_service,
    )
    return TestClient(app)


def create_sqlite_fixture(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE submitted_applications (
            application_id TEXT PRIMARY KEY,
            telegram_user_id INTEGER,
            spreadsheet_id TEXT,
            sheet_id INTEGER,
            sheet_name TEXT,
            last_seen_row_number INTEGER,
            last_known_status TEXT,
            direction TEXT,
            answer_type TEXT,
            application_type TEXT,
            change_type TEXT,
            is_urgent INTEGER,
            batch_id TEXT,
            last_seen_editor TEXT,
            submitted_at TEXT,
            polling_state TEXT,
            not_found_count INTEGER,
            last_not_found_at TEXT,
            next_status_check_at TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE dashboard_outbox (
            entity_type TEXT,
            entity_id TEXT,
            state TEXT,
            attempts INTEGER,
            created_at TEXT,
            updated_at TEXT,
            last_error TEXT
        );
        CREATE TABLE notification_outbox (
            event_id TEXT,
            dedupe_key TEXT,
            telegram_user_id INTEGER,
            event_type TEXT,
            state TEXT,
            attempts INTEGER,
            snapshot_json TEXT,
            html TEXT,
            created_at TEXT,
            updated_at TEXT,
            last_error TEXT
        );
        """
    )
    rows = [
        ("APP-1", 100, "sheet", 1, "Лист", 10, "Новая", "ФЛ", "Срочная", "Одиночная", "ADD", 1, "BATCH-1", "", "2026-01-01", "ACTIVE", 0, None, None, "2026-01-01", "2026-01-01"),
        ("APP-2", 100, "sheet", 1, "Лист", 22, "Новая", "ФЛ", "Обычная", "Одиночная", "ADD", 0, "BATCH-1", "", "2026-01-01", "ACTIVE", 0, None, None, "2026-01-01", "2026-01-01"),
    ]
    connection.executemany(
        """
        INSERT INTO submitted_applications VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        rows,
    )
    connection.execute(
        "INSERT INTO dashboard_outbox VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("APPLICATION", "APP-1", "PENDING", 0, "2026-01-01", "2026-01-01", ""),
    )
    connection.execute(
        "INSERT INTO notification_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "evt-1",
            "application-APP-1",
            100,
            "STATUS",
            "PENDING",
            0,
            '{"application_id":"APP-1"}',
            "<p>APP-1</p>",
            "2026-01-01",
            "2026-01-01",
            "",
        ),
    )
    connection.commit()
    connection.close()


def run_container_script(db_path: Path, *, mode: str, ids: list[str], confirmation: str = "") -> dict:
    env = {
        **os.environ,
        "DASHBOARD_DELETE_MODE": mode,
        "DASHBOARD_APPLICATION_IDS": json.dumps(ids),
        "DASHBOARD_CONFIRMATION": confirmation,
        "DASHBOARD_SQLITE_PATH": str(db_path),
    }
    completed = subprocess.run(
        [sys.executable, "-c", _CONTAINER_SCRIPT],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def count_rows(db_path: Path, table: str) -> int:
    connection = sqlite3.connect(db_path)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def test_normalizes_ids_and_builds_confirmation_phrase() -> None:
    ids = normalize_application_ids([" app-1 ", "APP-1", "app_2"], max_ids=20)

    assert ids == ["APP-1", "APP_2"]
    assert confirmation_phrase(ids) == "DELETE APP-1 APP_2"


def test_admin_delete_command_keeps_operation_bounded() -> None:
    command = build_admin_delete_command(
        RemoteConfig(),
        mode="execute",
        application_ids=["APP-1"],
        confirmation="DELETE APP-1",
    )

    assert "BEGIN IMMEDIATE" in command
    assert "PRAGMA busy_timeout=2000" in command
    assert "timeout=30" in command
    assert '"docker", "compose"' in command
    assert "VACUUM" not in command
    assert "checkpoint" not in command.lower()


def test_preview_endpoint_is_read_only_and_returns_counts(tmp_path: Path) -> None:
    runner = FakeAdminRunner(sample_preview_payload())
    client = make_client(tmp_path, runner)

    response = client.post("/api/admin/delete/preview", json={"application_ids": ["APP-1"]})

    assert response.status_code == 200
    assert response.json()["result"]["preview"]["counts"]["submitted_applications"] == 1
    assert runner.calls == 1


def test_preview_endpoint_returns_failed_result_on_ssh_error(tmp_path: Path) -> None:
    client = make_client(tmp_path, FailingAdminRunner())

    response = client.post("/api/admin/delete/preview", json={"application_ids": ["APP-1"]})

    assert response.status_code == 200
    assert response.json()["result"]["status"] == "failed"
    assert "ssh failed" in response.text


def test_execute_is_disabled_by_default(tmp_path: Path) -> None:
    client = make_client(tmp_path, FakeAdminRunner(sample_execute_payload()))

    response = client.post(
        "/api/admin/delete/execute",
        json={"application_ids": ["APP-1"], "confirmation": "DELETE APP-1"},
    )

    assert response.status_code == 400
    assert "disabled" in response.json()["detail"]


def test_execute_requires_exact_confirmation(tmp_path: Path) -> None:
    client = make_client(tmp_path, FakeAdminRunner(sample_execute_payload()), admin_enabled=True)

    response = client.post(
        "/api/admin/delete/execute",
        json={"application_ids": ["APP-1"], "confirmation": "DELETE WRONG"},
    )

    assert response.status_code == 400
    assert "confirmation" in response.json()["detail"]


def test_execute_stores_local_audit(tmp_path: Path) -> None:
    runner = FakeAdminRunner(sample_execute_payload())
    client = make_client(tmp_path, runner, admin_enabled=True)

    response = client.post(
        "/api/admin/delete/execute",
        json={"application_ids": ["APP-1"], "confirmation": "DELETE APP-1"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["audit_id"]
    assert body["result"]["execution"]["deleted"]["submitted_applications"] == 1
    audit_files = list((tmp_path / "data" / "admin-deletes").glob("*.jsonl"))
    assert audit_files
    assert "APP-1" in audit_files[0].read_text(encoding="utf-8")


def test_parallel_execute_is_blocked(tmp_path: Path) -> None:
    config = make_config(tmp_path, admin_enabled=True)
    storage = JsonStorage(config.storage.data_dir)
    service = AdminDeleteService(config=config, storage=storage, runner=SlowAdminRunner(sample_execute_payload()))

    async def run_two() -> tuple[object, object]:
        first = asyncio.create_task(service.execute(application_ids=["APP-1"], confirmation="DELETE APP-1"))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(service.execute(application_ids=["APP-1"], confirmation="DELETE APP-1"))
        return await first, await second

    first, second = asyncio.run(run_two())

    assert first.result["status"] == "ok"
    assert second.skipped is True


def test_container_preview_does_not_write(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    create_sqlite_fixture(db_path)

    result = run_container_script(db_path, mode="preview", ids=["APP-1"])

    assert result["status"] == "ok"
    assert result["preview"]["counts"]["submitted_applications"] == 1
    assert count_rows(db_path, "submitted_applications") == 2
    assert count_rows(db_path, "dashboard_outbox") == 1


def test_container_execute_deletes_related_rows_without_legacy_bulk_updates(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    create_sqlite_fixture(db_path)

    result = run_container_script(db_path, mode="execute", ids=["APP-1"], confirmation="DELETE APP-1")

    connection = sqlite3.connect(db_path)
    try:
        remaining_ids = [row[0] for row in connection.execute("SELECT application_id FROM submitted_applications")]
    finally:
        connection.close()
    assert result["status"] == "ok"
    assert result["execution"]["deleted"]["submitted_applications"] == 1
    assert result["execution"]["deleted"]["dashboard_outbox"] == 1
    assert result["execution"]["deleted"]["notification_outbox"] == 1
    assert remaining_ids == ["APP-2"]


def test_container_execute_bad_confirmation_rolls_back(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    create_sqlite_fixture(db_path)

    result = run_container_script(db_path, mode="execute", ids=["APP-1"], confirmation="DELETE WRONG")

    assert result["status"] == "failed"
    assert count_rows(db_path, "submitted_applications") == 2


def test_admin_delete_has_no_legacy_bulk_query() -> None:
    assert "bulk_batches" not in _CONTAINER_SCRIPT
    assert "bulk_creation_requests" not in _CONTAINER_SCRIPT
