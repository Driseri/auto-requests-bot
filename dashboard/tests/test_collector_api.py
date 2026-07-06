from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.collector import DashboardCollector, _duration_stats, _normalize_status_counts
from backend.config import RemoteConfig, load_config
from backend.remote_script import build_remote_command
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
                        "with_final_answer_today": 1,
                        "with_final_answer": 9,
                    },
                    "applications_by_status": [{"status": "Новая", "count": 1}],
                    "applications_by_direction": [{"direction": "ФЛ", "count": 1}],
                    "bulk_summary": {"total": 4, "unfinished": 0, "registering": 0, "registered": 4, "registered_today": 1},
                    "urgent_applications": [],
                    "drafts_summary": [],
                    "user_workflow_pending": [],
                },
                "errors": [],
            },
        },
        "logs": "Status polling iteration failed: consecutive_errors=1",
    }


def sample_pilot_payload() -> dict:
    payload = sample_remote_payload()
    payload["collected_at"] = "2026-07-06T10:00:00+00:00"
    metrics = payload["container_payload"]["sqlite"]["metrics"]
    metrics["applications_by_status"] = [
        {"status": "Новая", "count": 2},
        {"status": "cashback_osago", "count": 3},
    ]
    metrics["pilot_metrics_raw"] = {
        "applications": [
            {
                "application_id": "APP-1",
                "telegram_user_id": 100,
                "direction": "ФЛ",
                "answer_type": "Раскатка",
                "application_type": "Одиночная",
                "change_type": "ADD",
                "is_urgent": 0,
                "batch_id": None,
                "sheet_name": "06.07",
                "last_seen_row_number": 10,
                "last_known_status": "Итоговый ответ готов",
                "last_seen_editor": "Редактор",
                "has_editor_comment": 1,
                "has_final_answer": 1,
                "has_scriptwriter_response": 1,
                "submitted_at": "2026-07-06T08:00:00+00:00",
                "created_at": "2026-07-06T07:55:00+00:00",
                "updated_at": "2026-07-06T09:00:00+00:00",
                "polling_state": "ACTIVE",
                "not_found_count": 0,
                "last_not_found_at": None,
                "deletion_seen_count": 0,
                "deletion_last_seen_at": None,
            },
            {
                "application_id": "APP-2",
                "telegram_user_id": 200,
                "direction": "ФЛ",
                "answer_type": "Срочные",
                "application_type": "Одиночная",
                "change_type": None,
                "is_urgent": 1,
                "batch_id": None,
                "sheet_name": "06.07",
                "last_seen_row_number": 11,
                "last_known_status": "Новая",
                "last_seen_editor": "",
                "has_editor_comment": 0,
                "has_final_answer": 0,
                "has_scriptwriter_response": 0,
                "submitted_at": "2026-07-05T08:00:00+00:00",
                "created_at": "2026-07-05T07:55:00+00:00",
                "updated_at": "2026-07-05T08:00:00+00:00",
                "polling_state": "NOT_FOUND",
                "not_found_count": 2,
                "last_not_found_at": "2026-07-06T08:00:00+00:00",
                "deletion_seen_count": 0,
                "deletion_last_seen_at": None,
            },
        ],
        "events": [
            {
                "application_id": "APP-1",
                "telegram_user_id": 100,
                "event_type": "draft_started",
                "event_at": "2026-07-06T07:50:00+00:00",
                "old_value": None,
                "new_value": None,
            },
            {
                "application_id": "APP-1",
                "telegram_user_id": 100,
                "event_type": "application_submitted",
                "event_at": "2026-07-06T08:00:00+00:00",
                "old_value": None,
                "new_value": None,
            },
            {
                "application_id": "APP-1",
                "telegram_user_id": 100,
                "event_type": "application_indexed",
                "event_at": "2026-07-06T08:05:00+00:00",
                "old_value": None,
                "new_value": None,
            },
            {
                "application_id": "APP-1",
                "telegram_user_id": 100,
                "event_type": "editor_changed",
                "event_at": "2026-07-06T08:30:00+00:00",
                "old_value": "",
                "new_value": "Редактор",
            },
            {
                "application_id": "APP-1",
                "telegram_user_id": 100,
                "event_type": "final_answer_added",
                "event_at": "2026-07-06T09:00:00+00:00",
                "old_value": "",
                "new_value": "present",
            },
            {
                "application_id": "APP-1",
                "telegram_user_id": 100,
                "event_type": "application_deletion_error",
                "event_at": "2026-07-06T09:30:00+00:00",
                "old_value": None,
                "new_value": "busy",
            },
        ],
        "notification_errors": [
            {
                "event_type": "status",
                "telegram_user_id": 100,
                "state": "FAILED",
                "attempts": 3,
                "created_at": "2026-07-06T09:05:00+00:00",
                "updated_at": "2026-07-06T09:06:00+00:00",
                "last_error": "timeout",
            }
        ],
    }
    return payload


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
    assert latest.json()["business"]["today"]["final_answers"] == 1
    assert latest.json()["business"]["today"]["bulk_registered"] == 1
    assert latest.json()["business"]["directions"]["top"][0]["direction"] == "ФЛ"
    assert "pilot" in latest.json()


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


def test_urgent_final_status_is_not_counted_without_final(tmp_path: Path) -> None:
    payload = sample_remote_payload()
    payload["container_payload"]["sqlite"]["metrics"]["urgent_applications"] = [
        {
            "application_id": "APP-FINAL",
            "telegram_user_id": 100,
            "direction": "ФЛ",
            "created_at": "2026-06-18T09:00:00+00:00",
            "last_seen_editor": "Редактор",
            "last_known_status": "Итоговый ответ готов",
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
    assert urgent["no_final_answer"] == 0


def test_urgent_editor_not_selected_is_counted_without_owner(tmp_path: Path) -> None:
    payload = sample_remote_payload()
    payload["container_payload"]["sqlite"]["metrics"]["urgent_applications"] = [
        {
            "application_id": "APP-NO-OWNER",
            "telegram_user_id": 100,
            "direction": "ФЛ",
            "created_at": "2026-06-18T09:00:00+00:00",
            "last_seen_editor": "Редактор не выбран",
            "last_known_status": "Новая",
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
    assert response.json()["snapshot"]["urgent"]["no_editor"] == 1


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


def test_pilot_metrics_are_normalized_from_snapshot(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner(sample_pilot_payload()))
    app = create_app(config_path=tmp_path / "config.local.toml", collector=collector)
    client = TestClient(app)

    response = client.post("/api/collect")

    assert response.status_code == 200
    snapshot = response.json()["snapshot"]
    period = snapshot["pilot"]["periods"]["7d"]
    assert period["kpi"]["total_applications"] == 2
    assert period["kpi"]["active_users"] == 2
    assert period["kpi"]["creation_time_seconds"]["median_seconds"] == 600
    assert period["kpi"]["first_editor_action_seconds"]["median_seconds"] == 1800
    assert period["kpi"]["full_cycle_seconds"]["median_seconds"] == 3600
    assert period["kpi"]["not_found_or_tracking_errors"] == 1
    assert period["problem_rows"]
    funnel = {item["key"]: item for item in period["funnel"]}
    assert funnel["user_response"]["count"] == 1
    assert funnel["submitted"]["average_transition_seconds"] == 600
    assert funnel["sheets_visible"]["average_transition_seconds"] == 300
    assert funnel["status_changed"]["average_transition_seconds"] == 1500
    assert funnel["submitted"]["transition_sample_size"] == 1
    assert snapshot["applications"]["by_status"] == [
        {"status": "Некорректные статусы/интенты", "count": 3},
        {"status": "Новая", "count": 2},
    ]


def test_pilot_metrics_handle_missing_events(tmp_path: Path) -> None:
    payload = sample_pilot_payload()
    payload["container_payload"]["sqlite"]["metrics"]["pilot_metrics_raw"]["events"] = []
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner(payload))
    app = create_app(config_path=tmp_path / "config.local.toml", collector=collector)
    client = TestClient(app)

    response = client.post("/api/collect")

    pilot = response.json()["snapshot"]["pilot"]
    assert pilot["data_quality"]["status"] == "missing_events"
    assert pilot["periods"]["7d"]["kpi"]["creation_time_seconds"]["median_seconds"] is None


def test_pilot_metrics_match_actual_application_event_names(tmp_path: Path) -> None:
    payload = sample_pilot_payload()
    events = payload["container_payload"]["sqlite"]["metrics"]["pilot_metrics_raw"]["events"]
    events[:] = [
        event for event in events if event["event_type"] not in {"draft_started", "application_submitted", "application_indexed"}
    ]
    events.extend(
        [
            {
                "application_id": "APP-2",
                "telegram_user_id": 200,
                "event_type": "application_indexed",
                "event_at": "2026-07-05T08:01:00+00:00",
                "old_value": None,
                "new_value": None,
            },
            {
                "application_id": "APP-2",
                "telegram_user_id": 200,
                "event_type": "application_deleted",
                "event_at": "2026-07-05T09:00:00+00:00",
                "old_value": None,
                "new_value": None,
            },
        ]
    )
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner(payload))
    app = create_app(config_path=tmp_path / "config.local.toml", collector=collector)
    client = TestClient(app)

    response = client.post("/api/collect")

    pilot = response.json()["snapshot"]["pilot"]
    period = pilot["periods"]["7d"]
    funnel = {item["key"]: item["count"] for item in period["funnel"]}
    assert pilot["data_quality"]["event_count_by_type"]["application_indexed"] == 1
    assert any("draft_started" in note for note in pilot["data_quality"]["notes"])
    assert funnel["deletion"] == 1


def test_duration_stats_calculates_median_for_odd_and_even_samples() -> None:
    assert _duration_stats([10, 30, 20])["median_seconds"] == 20
    assert _duration_stats([10, 30])["median_seconds"] == 20


def test_status_counts_aggregate_intents() -> None:
    rows = _normalize_status_counts(
        [
            {"status": "Новая", "count": 1},
            {"status": "account_packageservice_info", "count": 2},
            {"status": "cashback_osago", "count": 3},
        ]
    )

    assert {"status": "Некорректные статусы/интенты", "count": 5} in rows
    assert {"status": "Новая", "count": 1} in rows


def test_remote_command_collects_pilot_metrics_read_only() -> None:
    command = build_remote_command(RemoteConfig(), include_logs=False, include_heavy=False)

    assert "pilot_metrics_raw" in command
    assert "file:/data/app.db?mode=ro" in command
    assert "PRAGMA query_only=ON" in command
    assert "LIMIT 2000" in command
    assert "LIMIT 5000" in command
    assert "COALESCE(last_known_status" in command
    assert "Итоговый ответ готов" in command
    assert "Редактор не выбран" in command
    assert "+3 hours" in command
    assert "with_final_answer_today" in command
    assert "final_answer_added" in command
    assert "status_final_answer_ready" in command
    assert "registered_today" in command
    assert "VACUUM" not in command
    assert "BEGIN IMMEDIATE" not in command
