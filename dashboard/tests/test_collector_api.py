from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.collector import (
    DashboardCollector,
    _duration_stats,
    _is_open_application,
    _normalize_status_counts,
    _pilot_problem_rows,
)
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


def pilot_fixture_time(hour: int, minute: int = 0, *, days_before: int = 1) -> str:
    """Return a recent UTC timestamp so seven-day tests do not expire."""

    return (datetime.now(timezone.utc) - timedelta(days=days_before)).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    ).isoformat()


def refresh_pilot_fixture_timestamps(payload: dict) -> dict:
    """Move the static sample from 2026-07-06 to yesterday, preserving intervals."""

    source_anchor = datetime(2026, 7, 6, tzinfo=timezone.utc)
    target_anchor = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    shift = target_anchor - source_anchor

    def refresh(value):
        if isinstance(value, dict):
            return {key: refresh(item) for key, item in value.items()}
        if isinstance(value, list):
            return [refresh(item) for item in value]
        if isinstance(value, str) and value.startswith("2026-07-"):
            return (datetime.fromisoformat(value.replace("Z", "+00:00")) + shift).isoformat()
        return value

    return refresh(payload)


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
                    "bulk_summary": {
                        "total": 4,
                        "active": 0,
                        "creating": 0,
                        "ready_for_registration": 0,
                        "registering": 0,
                        "failed": 0,
                        "overdue_active": 0,
                        "registered_today": 1,
                    },
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
    return refresh_pilot_fixture_timestamps(payload)


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


def test_urgent_metrics_use_chips_specific_completion_rules(tmp_path: Path) -> None:
    """CHIPS is complete by acceptance, not by the unused final-answer field."""

    payload = sample_remote_payload()
    payload["container_payload"]["sqlite"]["metrics"]["urgent_applications"] = [
        {
            "application_id": "CHIPS-SINGLE-DONE",
            "change_type": "CHIPS",
            "application_type": "Одиночная",
            "last_known_status": "Принята",
            "last_seen_editor": "Редактор",
            "created_at": pilot_fixture_time(8),
        },
        {
            "application_id": "CHIPS-BULK-DONE",
            "change_type": "CHIPS",
            "application_type": "Массовая",
            "last_known_status": "Принято",
            "last_seen_editor": "Редактор",
            "created_at": pilot_fixture_time(8),
        },
        {
            "application_id": "CHIPS-OPEN",
            "change_type": "CHIPS",
            "application_type": "Одиночная",
            "last_known_status": "В работе",
            "last_seen_editor": "",
            "created_at": pilot_fixture_time(8),
        },
        {
            "application_id": "ADD-REJECTED",
            "change_type": "ADD",
            "last_known_status": "Отклонена",
            "last_seen_editor": "Редактор",
            "created_at": pilot_fixture_time(8),
        },
    ]
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner(payload))
    client = TestClient(create_app(config_path=tmp_path / "config.local.toml", collector=collector))

    urgent = client.post("/api/collect").json()["snapshot"]["urgent"]

    assert urgent["open"] == 1
    assert urgent["no_final_answer"] == 1
    assert urgent["no_editor"] == 1
    assert [row["application_id"] for row in urgent["rows"]] == ["CHIPS-OPEN"]


def test_problem_rows_ignore_closed_chips_and_terminal_add_edit() -> None:
    """Only genuinely open applications can receive the no-result problem flag."""

    old = pilot_fixture_time(8, days_before=5)
    applications = [
        {"application_id": "CHIPS-DONE", "change_type": "CHIPS", "last_known_status": "Принята", "last_seen_editor": "Редактор", "submitted_at": old, "updated_at": old, "polling_state": "ACTIVE", "not_found_count": 0},
        {"application_id": "ADD-REJECTED", "change_type": "ADD", "last_known_status": "Отклонена", "last_seen_editor": "Редактор", "submitted_at": old, "updated_at": old, "polling_state": "ACTIVE", "not_found_count": 0},
        {"application_id": "CHIPS-OPEN", "change_type": "CHIPS", "last_known_status": "В работе", "last_seen_editor": "Редактор", "submitted_at": old, "updated_at": old, "polling_state": "ACTIVE", "not_found_count": 0},
    ]

    rows = _pilot_problem_rows(applications, {}, [])

    assert [row["application_id"] for row in rows] == ["CHIPS-OPEN"]
    assert _is_open_application(applications[0]) is False
    assert _is_open_application(applications[1]) is False
    assert _is_open_application(applications[2]) is True


def test_remote_collection_uses_only_bulk_reservations() -> None:
    command = build_remote_command(RemoteConfig(), include_logs=False, include_heavy=False)

    assert "bulk_reservations" in command
    assert "bulk_batches" not in command
    assert "bulk_creation_requests" not in command


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
    assert period["kpi"]["first_editor_action_seconds_by_urgency"]["regular"]["median_seconds"] == 1800
    assert period["kpi"]["first_editor_action_seconds_by_urgency"]["urgent"]["median_seconds"] is None
    assert period["kpi"]["full_cycle_seconds_by_urgency"]["regular"]["median_seconds"] == 3600
    assert period["kpi"]["full_cycle_seconds_by_urgency"]["urgent"]["median_seconds"] is None
    assert period["kpi"]["not_found_or_tracking_errors"] == 1
    assert snapshot["pilot"]["stickiness"]["wau"] == 2
    assert snapshot["pilot"]["stickiness"]["mau"] == 2
    assert period["problem_rows"]
    funnel = {item["key"]: item for item in period["funnel"]}
    assert funnel["submitted"]["count"] == 1
    assert funnel["editor_assigned"]["count"] == 1
    assert funnel["status_changed"]["count"] == 0
    assert funnel["final_answer"]["count"] == 1
    assert funnel["scriptwriter_response"]["count"] == 0
    assert funnel["submitted"]["average_transition_seconds"] == 600
    assert funnel["editor_assigned"]["average_transition_seconds"] == 1800
    assert funnel["status_changed"]["average_transition_seconds"] is None
    assert funnel["submitted"]["transition_sample_size"] == 1
    assert snapshot["applications"]["by_status"] == [
        {"status": "Некорректные статусы/интенты", "count": 3},
        {"status": "Новая", "count": 2},
    ]


def test_full_cycle_counts_exact_completion_when_submission_precedes_period(tmp_path: Path) -> None:
    """A cycle completes in the selected period even when it started earlier."""

    payload = sample_pilot_payload()
    raw = payload["container_payload"]["sqlite"]["metrics"]["pilot_metrics_raw"]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    submitted_at = now - timedelta(days=10)
    final_at = now - timedelta(days=1)
    raw["applications"] = [
        {
            **raw["applications"][0],
            "application_id": "APP-CROSS-PERIOD",
            "is_urgent": 0,
            "submitted_at": submitted_at.isoformat(),
            "created_at": submitted_at.isoformat(),
        }
    ]
    raw["events"] = [
        {
            "application_id": "APP-CROSS-PERIOD",
            "telegram_user_id": 100,
            "event_type": "application_submitted",
            "event_at": submitted_at.isoformat(),
            "old_value": None,
            "new_value": None,
        },
        {
            "application_id": "APP-CROSS-PERIOD",
            "telegram_user_id": 100,
            "event_type": "final_answer_added",
            "event_at": final_at.isoformat(),
            "old_value": "",
            "new_value": "Готово",
        },
    ]
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner(payload))
    client = TestClient(create_app(config_path=tmp_path / "config.local.toml", collector=collector))

    period = client.post("/api/collect").json()["snapshot"]["pilot"]["periods"]["7d"]

    assert period["kpi"]["full_cycle_seconds"] == {
        "average_seconds": 9 * 24 * 60 * 60,
        "median_seconds": 9 * 24 * 60 * 60,
        "sample_size": 1,
    }
    assert period["kpi"]["full_cycle_seconds_by_urgency"]["regular"]["sample_size"] == 1


def test_pilot_stickiness_counts_unique_submission_users(tmp_path: Path) -> None:
    payload = sample_pilot_payload()
    now = datetime.now(timezone.utc)
    today = now.isoformat()
    yesterday = (now - timedelta(days=1)).isoformat()
    old = (now - timedelta(days=20)).isoformat()
    apps = payload["container_payload"]["sqlite"]["metrics"]["pilot_metrics_raw"]["applications"]
    apps[:] = [
        {**apps[0], "application_id": "APP-TODAY-1", "telegram_user_id": 100, "submitted_at": today, "created_at": today},
        {**apps[0], "application_id": "APP-TODAY-2", "telegram_user_id": 100, "submitted_at": today, "created_at": today},
        {**apps[0], "application_id": "APP-WEEK", "telegram_user_id": 200, "submitted_at": yesterday, "created_at": yesterday},
        {**apps[0], "application_id": "APP-MONTH", "telegram_user_id": 300, "submitted_at": old, "created_at": old},
    ]
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner(payload))
    app = create_app(config_path=tmp_path / "config.local.toml", collector=collector)
    client = TestClient(app)

    response = client.post("/api/collect")

    stickiness = response.json()["snapshot"]["pilot"]["stickiness"]
    assert stickiness["dau"] == 1
    assert stickiness["wau"] == 2
    assert stickiness["mau"] == 3
    assert stickiness["dau_wau_percent"] == 50
    assert stickiness["dau_mau_percent"] == 33
    assert stickiness["wau_mau_percent"] == 67


def test_pilot_funnel_counts_unique_applications_for_repeated_events(tmp_path: Path) -> None:
    payload = sample_pilot_payload()
    events = payload["container_payload"]["sqlite"]["metrics"]["pilot_metrics_raw"]["events"]
    events.extend(
        [
            {
                "application_id": "APP-1",
                "telegram_user_id": 100,
                "event_type": "final_answer_added",
                "event_at": pilot_fixture_time(9, 5),
                "old_value": "",
                "new_value": "present again",
            },
            {
                "application_id": "APP-1",
                "telegram_user_id": 100,
                "event_type": "editor_changed",
                "event_at": pilot_fixture_time(8, 40),
                "old_value": "",
                "new_value": "Редактор 2",
            },
        ]
    )
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner(payload))
    app = create_app(config_path=tmp_path / "config.local.toml", collector=collector)
    client = TestClient(app)

    response = client.post("/api/collect")

    funnel = {item["key"]: item["count"] for item in response.json()["snapshot"]["pilot"]["periods"]["7d"]["funnel"]}
    assert funnel["final_answer"] == 1
    assert funnel["editor_assigned"] == 1
    assert funnel["status_changed"] == 0


def test_pilot_funnel_uses_only_actual_stage_events(tmp_path: Path) -> None:
    """Keep Sheets indexing and unsupported user events out of the funnel."""

    payload = sample_pilot_payload()
    events = payload["container_payload"]["sqlite"]["metrics"]["pilot_metrics_raw"]["events"]
    events.extend(
        [
            {
                "application_id": "APP-1",
                "telegram_user_id": 100,
                "event_type": "status_changed",
                "event_at": pilot_fixture_time(8, 20),
                "old_value": "Новая",
                "new_value": "В работе",
            },
            {
                "application_id": "APP-1",
                "telegram_user_id": 100,
                "event_type": "editor_comment_added",
                "event_at": pilot_fixture_time(8, 30),
                "old_value": "",
                "new_value": "Нужны пояснения",
            },
            {
                "application_id": "APP-1",
                "telegram_user_id": 100,
                "event_type": "user_comment_added",
                "event_at": pilot_fixture_time(8, 40),
                "old_value": "",
                "new_value": "Не используется ботом",
            },
            {
                "application_id": "APP-1",
                "telegram_user_id": 100,
                "event_type": "scriptwriter_response_added",
                "event_at": pilot_fixture_time(8, 50),
                "old_value": "",
                "new_value": "Ответ сценариста",
            },
        ]
    )
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner(payload))
    client = TestClient(create_app(config_path=tmp_path / "config.local.toml", collector=collector))

    funnel = {
        item["key"]: item
        for item in client.post("/api/collect").json()["snapshot"]["pilot"]["periods"]["7d"]["funnel"]
    }

    assert "sheets_visible" not in funnel
    assert "user_response" not in funnel
    assert funnel["editor_assigned"]["count"] == 1
    assert funnel["status_changed"]["count"] == 1
    assert funnel["editor_comment"]["count"] == 1
    assert funnel["scriptwriter_response"]["count"] == 1
    assert funnel["scriptwriter_response"]["average_transition_seconds"] == 1200
    assert funnel["scriptwriter_response"]["transition_sample_size"] == 1


def test_pilot_metrics_do_not_use_current_state_fallbacks(tmp_path: Path) -> None:
    payload = sample_pilot_payload()
    raw = payload["container_payload"]["sqlite"]["metrics"]["pilot_metrics_raw"]
    raw["applications"] = [
        {
            **raw["applications"][0],
            "application_id": "APP-FALLBACK",
            "is_urgent": 1,
            "last_seen_row_number": 44,
            "last_known_status": "Итоговый ответ готов",
            "last_seen_editor": "Редактор",
            "has_editor_comment": 1,
            "has_final_answer": 1,
            "has_scriptwriter_response": 1,
            "submitted_at": pilot_fixture_time(8),
            "updated_at": pilot_fixture_time(12),
        }
    ]
    raw["events"] = [
        {
            "application_id": "APP-FALLBACK",
            "telegram_user_id": 100,
            "event_type": "application_submitted",
            "event_at": pilot_fixture_time(8),
            "old_value": None,
            "new_value": None,
        }
    ]
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner(payload))
    app = create_app(config_path=tmp_path / "config.local.toml", collector=collector)
    client = TestClient(app)

    response = client.post("/api/collect")

    period = response.json()["snapshot"]["pilot"]["periods"]["7d"]
    kpi = period["kpi"]
    assert kpi["first_editor_action_seconds"]["sample_size"] == 0
    assert kpi["full_cycle_seconds"]["sample_size"] == 0
    assert kpi["first_editor_action_seconds_by_urgency"]["urgent"]["sample_size"] == 0
    assert kpi["full_cycle_seconds_by_urgency"]["urgent"]["sample_size"] == 0
    funnel = {item["key"]: item for item in period["funnel"]}
    assert funnel["submitted"]["count"] == 1
    assert funnel["editor_assigned"]["count"] == 0
    assert funnel["editor_comment"]["count"] == 0
    assert funnel["scriptwriter_response"]["count"] == 0
    assert funnel["final_answer"]["count"] == 0
    assert funnel["status_changed"]["count"] == 0


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


def test_pilot_metrics_mark_partial_events(tmp_path: Path) -> None:
    payload = sample_pilot_payload()
    raw = payload["container_payload"]["sqlite"]["metrics"]["pilot_metrics_raw"]
    raw["events"] = [event for event in raw["events"] if event["event_type"] == "application_submitted"]
    config = make_config(tmp_path)
    storage = JsonStorage(config.storage.data_dir)
    collector = DashboardCollector(config=config, storage=storage, runner=FakeRunner(payload))
    app = create_app(config_path=tmp_path / "config.local.toml", collector=collector)
    client = TestClient(app)

    response = client.post("/api/collect")

    pilot = response.json()["snapshot"]["pilot"]
    assert pilot["data_quality"]["status"] == "partial_events"
    assert any("fallback" in note for note in pilot["data_quality"]["notes"])


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
                "event_at": pilot_fixture_time(8, 1, days_before=2),
                "old_value": None,
                "new_value": None,
            },
            {
                "application_id": "APP-2",
                "telegram_user_id": 200,
                "event_type": "application_deleted",
                "event_at": pilot_fixture_time(9, days_before=2),
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
