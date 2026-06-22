from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.collector import DashboardCollector
from backend.config import load_config
from backend.storage import JsonStorage


class FakeRunner:
    """Test SSH runner that returns deterministic remote JSON."""

    def __init__(self, payload: dict | None = None, *, fail: bool = False) -> None:
        self.payload = payload or sample_remote_payload()
        self.fail = fail
        self.calls = 0

    def run(self, command: str) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("ssh failed")
        return json.dumps(self.payload)


def sample_remote_payload() -> dict:
    return {
        "collected_at": "2026-06-18T10:00:00+00:00",
        "host": "vps",
        "app_version": "pilot",
        "errors": [],
        "container": {
            "id": "abc",
            "compose_ps": "bot running",
            "inspect": {
                "RestartCount": 0,
                "State": {
                    "Status": "running",
                    "Health": {"Status": "healthy"},
                    "StartedAt": "2026-06-18T09:00:00Z",
                    "FinishedAt": "0001-01-01T00:00:00Z",
                    "OOMKilled": False,
                },
            },
            "stats": {"MemUsage": "100MiB / 2GiB"},
        },
        "vps": {
            "uptime": "10:00 up 1 day, load average: 0.10, 0.20, 0.30",
            "free_m": "Mem: 2000 800 1200\nSwap: 0 0 0\n",
            "df_root": "Filesystem 1G-blocks Used Available Use% Mounted on\n/dev/vda1 20G 5G 15G 25% /\n",
            "nproc": "1",
            "heavy": {},
        },
        "container_payload": {
            "heartbeat": {"updated_at": "2026-06-18T09:59:30+00:00", "iteration": 10},
            "external_health": {"result": "ok", "consecutive_failures": 0},
            "sqlite": {
                "ok": True,
                "metrics": {
                    "notification_outbox_by_state": [{"state": "PENDING", "count": 0}],
                    "dashboard_outbox_by_state": [{"state": "PENDING", "count": 0}],
                    "applications_summary": {
                        "total": 1,
                        "created_today": 1,
                        "urgent_total": 0,
                        "urgent_today": 0,
                        "not_found_total": 0,
                    },
                    "applications_by_status": [{"status": "Новая", "count": 1}],
                    "applications_by_direction": [{"direction": "ФЛ", "count": 1}],
                    "bulk_summary": {"total": 0, "unfinished": 0, "registering": 0, "registered": 0},
                    "urgent_applications": [],
                    "drafts_summary": [],
                    "user_workflow_pending": [],
                },
                "errors": [],
            },
        },
        "logs": "Status polling iteration failed: consecutive_errors=1",
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


def test_api_collect_and_latest_snapshot(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner())
    app = create_app(config_path=tmp_path / "config.local.toml", collector=collector)
    client = TestClient(app)

    response = client.post("/api/collect")
    assert response.status_code == 200
    assert response.json()["snapshot"]["collection_status"] == "ok"

    latest = client.get("/api/snapshot/latest")
    assert latest.status_code == 200
    assert latest.json()["container"]["app_version"] == "pilot"
    assert latest.json()["business"]["today"]["created"] == 1
    assert latest.json()["business"]["directions"]["top"][0]["direction"] == "ФЛ"


def test_urgent_oldest_age_is_calculated(tmp_path: Path) -> None:
    payload = sample_remote_payload()
    payload["container_payload"]["sqlite"]["metrics"]["urgent_applications"] = [
        {
            "application_id": "APP-URGENT",
            "telegram_user_id": 100,
            "direction": "ФЛ",
            "created_at": "2026-06-18T09:00:00+00:00",
            "last_seen_editor": "",
            "has_final_answer": 0,
        }
    ]
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner(payload))
    app = create_app(config_path=tmp_path / "config.local.toml", collector=collector)
    client = TestClient(app)

    response = client.post("/api/collect")

    assert response.status_code == 200
    urgent = response.json()["snapshot"]["urgent"]
    assert urgent["open"] == 1
    assert urgent["oldest_created_at"] == "2026-06-18T09:00:00+00:00"
    assert urgent["oldest_age_seconds"] >= 0


def test_api_safe_config_never_returns_password(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner())
    app = create_app(config_path=tmp_path / "config.local.toml", collector=collector)
    client = TestClient(app)

    response = client.get("/api/config/safe")

    assert response.status_code == 200
    assert "secret" not in response.text


def test_failed_collection_is_history_only(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    ok_collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner())
    fail_collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner(fail=True))

    ok_response = TestClient(
        create_app(config_path=tmp_path / "config.local.toml", collector=ok_collector)
    ).post("/api/collect")
    assert ok_response.status_code == 200

    fail_response = TestClient(
        create_app(config_path=tmp_path / "config.local.toml", collector=fail_collector)
    ).post("/api/collect")
    assert fail_response.status_code == 200

    assert fail_response.json()["snapshot"]["collection_status"] == "failed"
    assert storage.load_latest().collection_status == "ok"
