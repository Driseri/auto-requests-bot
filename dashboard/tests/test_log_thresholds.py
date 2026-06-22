from __future__ import annotations

from backend.log_classifier import classify_logs
from backend.thresholds import summarize_snapshot


def test_log_classifier_groups_operational_errors() -> None:
    groups = classify_logs(
        """
Traceback (most recent call last)
Status polling iteration failed: consecutive_errors=1
Temporary Google API failure: 429 quota exceeded
TelegramNetworkError: Request timeout
GigaChat response validation failed: invalid JSON
Dashboard outbox delivery failed
"""
    )

    assert groups["traceback"]["count"] == 1
    assert groups["polling_failed"]["count"] == 1
    assert groups["google_429"]["count"] == 1
    assert groups["telegram_network"]["count"] == 1
    assert groups["gigachat_failed"]["count"] == 1
    assert groups["dashboard_outbox_failed"]["count"] == 1


def test_thresholds_calculate_critical_status() -> None:
    summary, details = summarize_snapshot(
        {
            "collection_status": "ok",
            "container": {"state": "running", "health": "unhealthy"},
            "polling": {"heartbeat_age_seconds": 300, "max_age_seconds": 180},
            "vps": {"disk_used_percent": 90, "disk_free_gb": 1},
            "queues": {"notification_outbox": {"failed": 1}, "dashboard_outbox": {}},
            "applications": {"not_found_total": 2},
            "bulk": {},
            "log_events": {},
        }
    )

    assert summary.status == "CRITICAL"
    assert details["heartbeat_age_seconds"] == 300
    assert any("Контейнер" in problem for problem in summary.problems)
