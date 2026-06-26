from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.application_report import (
    ApplicationReportCollector,
    build_application_report_command,
    normalize_application_report,
)
from backend.collector import DashboardCollector
from backend.config import RemoteConfig, load_config
from backend.storage import JsonStorage

from test_collector_api import FakeRunner


class FakeReportRunner:
    """Test runner that returns deterministic application report JSON."""

    def __init__(self, payload: dict | None = None, *, fail: bool = False) -> None:
        self.payload = payload or sample_report_payload()
        self.fail = fail
        self.calls = 0
        self.commands: list[str] = []

    def run(self, command: str) -> str:
        self.calls += 1
        self.commands.append(command)
        if self.fail:
            raise RuntimeError("ssh failed")
        return json.dumps(self.payload, ensure_ascii=False)


class SlowReportRunner(FakeReportRunner):
    """Runner slow enough to verify the collector overlap guard."""

    def run(self, command: str) -> str:
        import time

        time.sleep(0.15)
        return super().run(command)


def sample_report_payload() -> dict:
    application = {
        "application_id": "APP-1",
        "telegram_user_id": 100,
        "spreadsheet_id": "sheet-id",
        "sheet_id": 456,
        "sheet_name": "Срочные",
        "last_seen_row_number": 12,
        "last_known_status": "Новая",
        "direction": "ФЛ",
        "answer_type": "Срочные",
        "application_type": "Одиночная",
        "change_type": "ADD",
        "is_urgent": 1,
        "batch_id": None,
        "last_seen_editor": "",
        "has_final_answer": 0,
        "submitted_at": "2026-06-24T10:00:00+00:00",
        "polling_state": "NOT_FOUND",
        "not_found_count": 3,
        "last_not_found_at": "2026-06-25T08:00:00+00:00",
        "next_status_check_at": "2026-06-25T09:00:00+00:00",
        "created_at": "2026-06-24T10:00:00+00:00",
        "updated_at": "2026-06-25T08:10:00+00:00",
    }
    return {
        "collected_at": "2026-06-25T09:00:00+00:00",
        "errors": [],
        "container_payload": {
            "errors": [],
            "report": {
                "lost": [application],
                "urgent_without_final_answer": [application],
                "without_owner": [application],
                "needs_clarification": [],
                "stale_without_movement": [application],
                "problematic_bulk_batches": [
                    {
                        "batch_id": "BATCH-1",
                        "telegram_user_id": 100,
                        "spreadsheet_id": "sheet-id",
                        "sheet_id": 789,
                        "sheet_name": "Массовый ввод",
                        "start_row": 20,
                        "registration_state": "REGISTERING",
                        "location_state": "MISSING",
                        "location_miss_count": 2,
                        "updated_at": "2026-06-25T08:00:00+00:00",
                    }
                ],
                "unfinished_workflows": [
                    {
                        "telegram_user_id": 100,
                        "current_step": "review",
                        "submission_state": "DRAFT",
                        "application_id": "APP-1",
                        "updated_at": "2026-06-25T08:30:00+00:00",
                    }
                ],
            },
        },
    }


def make_config(tmp_path: Path):
    config_path = tmp_path / "config.local.toml"
    config_path.write_text(
        f"""
[ssh]
host = "vps"
username = "root"
password = "secret"

[collector]
auto_collect = false

[storage]
data_dir = "{(tmp_path / 'data').as_posix()}"
""",
        encoding="utf-8",
    )
    return load_config(config_path)


def make_app(tmp_path: Path, runner: FakeReportRunner) -> TestClient:
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    snapshot_collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner())
    report_collector = ApplicationReportCollector(config=config, storage=storage, runner=runner)
    app = create_app(
        config_path=tmp_path / "config.local.toml",
        collector=snapshot_collector,
        application_report_collector=report_collector,
    )
    return TestClient(app)


def test_latest_application_report_returns_404_when_empty(tmp_path: Path) -> None:
    client = make_app(tmp_path, FakeReportRunner())

    response = client.get("/api/applications/report/latest")

    assert response.status_code == 404


def test_application_report_collects_and_stores_latest(tmp_path: Path) -> None:
    runner = FakeReportRunner()
    client = make_app(tmp_path, runner)

    response = client.post("/api/applications/report")

    assert response.status_code == 200
    report = response.json()["report"]
    assert report["collection_status"] == "ok"
    assert report["summary"]["lost"] == 1
    assert report["lost"][0]["problem"] == "не найдена polling"
    assert report["lost"][0]["row_link"].endswith("/edit#gid=456&range=A12")
    assert "source_text" not in json.dumps(report, ensure_ascii=False)
    assert "raw_change_description" not in json.dumps(report, ensure_ascii=False)
    assert "file:/data/app.db?mode=ro" in runner.commands[0]
    assert "PRAGMA query_only=ON" in runner.commands[0]

    latest = client.get("/api/applications/report/latest")
    assert latest.status_code == 200
    assert latest.json()["lost"][0]["application_id"] == "APP-1"


def test_failed_application_report_keeps_previous_latest(tmp_path: Path) -> None:
    ok_client = make_app(tmp_path, FakeReportRunner())
    assert ok_client.post("/api/applications/report").status_code == 200

    fail_client = make_app(tmp_path, FakeReportRunner(fail=True))
    failed = fail_client.post("/api/applications/report")

    assert failed.status_code == 200
    assert failed.json()["report"]["collection_status"] == "failed"
    latest = fail_client.get("/api/applications/report/latest")
    assert latest.status_code == 200
    assert latest.json()["collection_status"] == "ok"


def test_application_report_collector_prevents_overlapping_runs(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    collector = ApplicationReportCollector(
        config=config,
        storage=storage,
        runner=SlowReportRunner(),
    )

    async def run_two() -> tuple[object, object]:
        first = asyncio.create_task(collector.collect())
        await asyncio.sleep(0.01)
        second = asyncio.create_task(collector.collect())
        return await first, await second

    first, second = asyncio.run(run_two())

    assert first.report is not None
    assert second.skipped is True


def test_application_report_normalizes_repeat_not_found_label() -> None:
    payload = sample_report_payload()
    row = payload["container_payload"]["report"]["lost"][0]
    row["polling_state"] = "ACTIVE"
    row["not_found_count"] = 2

    report = normalize_application_report(payload, source_host="vps")

    assert report.lost[0]["problem"] == "повторные not_found"


def test_application_report_command_is_read_only() -> None:
    command = build_application_report_command(RemoteConfig())

    assert "file:/data/app.db?mode=ro" in command
    assert "PRAGMA query_only=ON" in command
    assert "DELETE FROM" not in command.upper()
    assert "UPDATE submitted_applications".upper() not in command.upper()
