from __future__ import annotations

import pytest

from app.config import load_settings
from app.scheduling import cutoff_to_string


def test_load_settings_uses_default_rollout_schedule(monkeypatch):
    monkeypatch.setenv("BOT_TIMEZONE", "")
    monkeypatch.setenv("ROLLOUT_WEDNESDAY_CUTOFF", "")
    monkeypatch.setenv("ROLLOUT_THURSDAY_CUTOFF", "")

    settings = load_settings()

    assert settings.rollout_schedule.timezone_name == "Europe/Moscow"
    assert cutoff_to_string(settings.rollout_schedule.wednesday_cutoff) == "14:00"
    assert cutoff_to_string(settings.rollout_schedule.thursday_cutoff) == "14:00"
    assert settings.application_editors == ("редактор 1", "редактор 2")
    assert settings.dashboard_sync_interval_seconds == 300
    assert settings.bulk_reserved_rows == 100
    assert settings.bulk_registration_stale_seconds == 600


def test_load_settings_uses_custom_dashboard_sync_interval(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SYNC_INTERVAL_SECONDS", "120")

    settings = load_settings()

    assert settings.dashboard_sync_interval_seconds == 120


def test_load_settings_uses_custom_bulk_limits(monkeypatch):
    monkeypatch.setenv("BULK_RESERVED_ROWS", "75")
    monkeypatch.setenv("BULK_REGISTRATION_STALE_SECONDS", "900")

    settings = load_settings()

    assert settings.bulk_reserved_rows == 75
    assert settings.bulk_registration_stale_seconds == 900


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BULK_RESERVED_ROWS", "0"),
        ("BULK_RESERVED_ROWS", "-1"),
        ("BULK_REGISTRATION_STALE_SECONDS", "0"),
        ("BULK_REGISTRATION_STALE_SECONDS", "-1"),
    ],
)
def test_load_settings_rejects_invalid_bulk_limits(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        load_settings()


@pytest.mark.parametrize("value", ["0", "-1"])
def test_load_settings_rejects_invalid_dashboard_sync_interval(monkeypatch, value):
    monkeypatch.setenv("DASHBOARD_SYNC_INTERVAL_SECONDS", value)

    with pytest.raises(ValueError, match="DASHBOARD_SYNC_INTERVAL_SECONDS"):
        load_settings()


def test_load_settings_uses_custom_rollout_schedule(monkeypatch):
    monkeypatch.setenv("BOT_TIMEZONE", "Asia/Yekaterinburg")
    monkeypatch.setenv("ROLLOUT_WEDNESDAY_CUTOFF", "13:30")
    monkeypatch.setenv("ROLLOUT_THURSDAY_CUTOFF", "15:00")

    settings = load_settings()

    assert settings.rollout_schedule.timezone_name == "Asia/Yekaterinburg"
    assert cutoff_to_string(settings.rollout_schedule.wednesday_cutoff) == "13:30"
    assert cutoff_to_string(settings.rollout_schedule.thursday_cutoff) == "15:00"


def test_load_settings_rejects_unknown_timezone(monkeypatch):
    monkeypatch.setenv("BOT_TIMEZONE", "Unknown/Timezone")

    with pytest.raises(ValueError, match="Unknown BOT_TIMEZONE"):
        load_settings()


@pytest.mark.parametrize("value", ["14", "2:00", "14:00:00", "24:00", "not-a-time"])
def test_load_settings_rejects_invalid_cutoff(monkeypatch, value):
    monkeypatch.setenv("ROLLOUT_WEDNESDAY_CUTOFF", value)

    with pytest.raises(ValueError, match="ROLLOUT_WEDNESDAY_CUTOFF"):
        load_settings()


def test_load_settings_parses_unique_application_editors(monkeypatch):
    monkeypatch.setenv(
        "APPLICATION_EDITORS",
        " редактор 1,редактор 2,редактор 1 ",
    )

    settings = load_settings()

    assert settings.application_editors == ("редактор 1", "редактор 2")


@pytest.mark.parametrize("value", ["", " , ", "Редактор не выбран"])
def test_load_settings_rejects_invalid_application_editors(monkeypatch, value):
    monkeypatch.setenv("APPLICATION_EDITORS", value)

    with pytest.raises(ValueError, match="APPLICATION_EDITORS"):
        load_settings()
