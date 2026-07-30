from __future__ import annotations

import pytest

import app.config as config_module
from app.config import load_settings
from app.scheduling import cutoff_to_string


def test_load_settings_uses_default_rollout_schedule(monkeypatch):
    monkeypatch.setenv("BOT_TIMEZONE", "")
    monkeypatch.setenv("ROLLOUT_WEDNESDAY_CUTOFF", "")
    monkeypatch.setenv("ROLLOUT_THURSDAY_CUTOFF", "")
    monkeypatch.setenv("URGENT_EDITOR_NOTIFICATIONS_ENABLED", "")
    monkeypatch.setenv("EDITOR_URGENT_CHAT_ID", "")

    settings = load_settings()

    assert settings.rollout_schedule.timezone_name == "Europe/Moscow"
    assert cutoff_to_string(settings.rollout_schedule.wednesday_cutoff) == "14:00"
    assert cutoff_to_string(settings.rollout_schedule.thursday_cutoff) == "14:00"
    assert settings.application_editors == ("редактор 1", "редактор 2")
    assert settings.dashboard_sync_interval_seconds == 300
    assert settings.status_polling_memory_log_interval == 40
    assert settings.status_not_found_threshold == 20
    assert settings.status_not_found_recheck_seconds == 3600
    assert settings.bulk_registration_stale_seconds == 600
    assert settings.bulk_creation_stale_seconds == 600
    assert settings.daily_sheet_grouping_enabled is True
    assert settings.daily_sheet_maintenance_enabled is False
    assert settings.daily_sheet_maintenance_time == "00:01"
    assert settings.notification_max_attempts == 10
    assert settings.notification_retry_base_seconds == 30
    assert settings.notification_sending_stale_seconds == 300
    assert settings.notification_message_max_chars == 3500
    assert settings.urgent_editor_notifications_enabled is False
    assert settings.editor_urgent_chat_id is None


def test_load_settings_uses_v62_system_and_v61_user_prompts_by_default(monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda: None)
    monkeypatch.delenv("GIGACHAT_MODEL", raising=False)
    monkeypatch.delenv("GIGACHAT_SYSTEM_PROMPT_PATH", raising=False)
    monkeypatch.delenv("GIGACHAT_USER_PROMPT_PATH", raising=False)
    monkeypatch.delenv("GIGACHAT_LOG_FULL_REQUEST", raising=False)

    settings = load_settings()

    assert settings.gigachat_model == "GigaChat-2-Pro"
    assert (
        settings.gigachat_system_prompt_path
        == "prompts/gigachat_system_v6.2_recommendation.md"
    )
    assert (
        settings.gigachat_user_prompt_path
        == "prompts/gigachat_user_v6.1_recommendation.md"
    )
    assert settings.gigachat_log_full_request is False


def test_load_settings_can_enable_full_gigachat_request_log(monkeypatch):
    monkeypatch.setenv("GIGACHAT_LOG_FULL_REQUEST", "true")

    settings = load_settings()

    assert settings.gigachat_log_full_request is True


def test_load_settings_uses_custom_dashboard_sync_interval(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SYNC_INTERVAL_SECONDS", "120")

    settings = load_settings()

    assert settings.dashboard_sync_interval_seconds == 120


def test_load_settings_uses_custom_status_polling_memory_log_interval(monkeypatch):
    monkeypatch.setenv("STATUS_POLLING_MEMORY_LOG_INTERVAL", "0")

    settings = load_settings()

    assert settings.status_polling_memory_log_interval == 0


def test_load_settings_uses_editor_urgent_chat_id(monkeypatch):
    monkeypatch.setenv("URGENT_EDITOR_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("EDITOR_URGENT_CHAT_ID", "-100123456")

    settings = load_settings()

    assert settings.urgent_editor_notifications_enabled is True
    assert settings.editor_urgent_chat_id == -100123456


def test_load_settings_rejects_enabled_editor_notifications_without_chat(monkeypatch):
    monkeypatch.setenv("URGENT_EDITOR_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("EDITOR_URGENT_CHAT_ID", "")

    with pytest.raises(ValueError, match="EDITOR_URGENT_CHAT_ID"):
        load_settings()


def test_load_settings_rejects_invalid_editor_urgent_chat_id(monkeypatch):
    monkeypatch.setenv("EDITOR_URGENT_CHAT_ID", "not-a-number")

    with pytest.raises(ValueError, match="EDITOR_URGENT_CHAT_ID"):
        load_settings()


def test_load_settings_uses_custom_bulk_limits(monkeypatch):
    monkeypatch.setenv("BULK_REGISTRATION_STALE_SECONDS", "900")
    monkeypatch.setenv("BULK_CREATION_STALE_SECONDS", "700")

    settings = load_settings()

    assert settings.bulk_registration_stale_seconds == 900
    assert settings.bulk_creation_stale_seconds == 700


def test_load_settings_uses_custom_daily_sheet_grouping(monkeypatch):
    monkeypatch.setenv("DAILY_SHEET_GROUPING_ENABLED", "false")
    monkeypatch.setenv("DAILY_SHEET_MAINTENANCE_ENABLED", "false")
    monkeypatch.setenv("DAILY_SHEET_MAINTENANCE_TIME", "01:15")

    settings = load_settings()

    assert settings.daily_sheet_grouping_enabled is False
    assert settings.daily_sheet_maintenance_enabled is False
    assert settings.daily_sheet_maintenance_time == "01:15"


def test_load_settings_rejects_invalid_daily_sheet_maintenance_time(monkeypatch):
    monkeypatch.setenv("DAILY_SHEET_MAINTENANCE_TIME", "25:99")

    with pytest.raises(ValueError, match="DAILY_SHEET_MAINTENANCE_TIME"):
        load_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BULK_REGISTRATION_STALE_SECONDS", "0"),
        ("BULK_REGISTRATION_STALE_SECONDS", "-1"),
        ("BULK_CREATION_STALE_SECONDS", "0"),
        ("NOTIFICATION_MAX_ATTEMPTS", "0"),
        ("NOTIFICATION_RETRY_BASE_SECONDS", "0"),
        ("NOTIFICATION_SENDING_STALE_SECONDS", "0"),
        ("NOTIFICATION_MESSAGE_MAX_CHARS", "0"),
        ("STATUS_NOT_FOUND_THRESHOLD", "0"),
        ("STATUS_NOT_FOUND_RECHECK_SECONDS", "0"),
        ("STATUS_POLLING_MEMORY_LOG_INTERVAL", "-1"),
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
