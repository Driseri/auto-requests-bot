from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.google_api import GoogleApiRetryConfig
from app.scheduling import (
    DEFAULT_THURSDAY_CUTOFF,
    DEFAULT_TIMEZONE,
    DEFAULT_WEDNESDAY_CUTOFF,
    RolloutSchedule,
)


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    telegram_proxy_url: str | None
    telegram_request_timeout: float
    sqlite_path: str
    google_dashboard_spreadsheet_id: str
    google_fl_spreadsheet_id: str
    google_sme_spreadsheet_id: str
    google_ai_spreadsheet_id: str
    google_voice_collection_spreadsheet_id: str
    google_spreadsheet_id: str
    google_sheet_name: str
    google_high_priority_sheet_name: str
    google_low_priority_sheet_name: str
    google_bulk_sheet_name: str
    google_credentials_path: str
    gigachat_credentials: str
    gigachat_base_url: str
    gigachat_auth_url: str
    gigachat_scope: str
    gigachat_model: str
    gigachat_verify_ssl_certs: bool
    gigachat_ca_bundle_file: str | None
    gigachat_timeout: float
    gigachat_max_retries: int
    gigachat_retry_backoff_factor: float
    gigachat_system_prompt_path: str
    gigachat_user_prompt_path: str
    gigachat_show_response_json: bool
    status_polling_enabled: bool
    status_polling_interval_seconds: float
    status_polling_memory_log_interval: int
    status_not_found_threshold: int
    status_not_found_recheck_seconds: int
    dashboard_sync_interval_seconds: float
    completed_bulk_dashboard_scan_interval_seconds: float
    bulk_relocation_search_interval_seconds: int
    dashboard_outbox_retry_base_seconds: int
    dashboard_outbox_retry_max_seconds: int
    dashboard_outbox_sending_stale_seconds: int
    bulk_reserved_rows: int
    bulk_max_rows: int
    bulk_registration_stale_seconds: int
    bulk_creation_stale_seconds: int
    notification_max_attempts: int
    notification_retry_base_seconds: int
    notification_sending_stale_seconds: int
    notification_message_max_chars: int
    urgent_editor_notifications_enabled: bool
    editor_urgent_chat_id: int | None
    rollout_schedule: RolloutSchedule
    application_editors: tuple[str, ...]
    google_api_retry: GoogleApiRetryConfig
    status_polling_heartbeat_path: str


def load_settings() -> Settings:
    """Загрузить и проверить конфигурацию приложения из переменных окружения."""
    load_dotenv()
    docker_credentials_path = Path("/run/secrets/google_credentials.json")
    default_credentials_path = (
        str(docker_credentials_path) if docker_credentials_path.exists() else "credentials.json"
    )
    status_polling_interval_seconds = float(
        os.getenv("STATUS_POLLING_INTERVAL_SECONDS", "30").strip() or "30"
    )
    status_polling_memory_log_interval = int(
        os.getenv("STATUS_POLLING_MEMORY_LOG_INTERVAL", "10").strip() or "10"
    )
    status_not_found_threshold = int(
        os.getenv("STATUS_NOT_FOUND_THRESHOLD", "20").strip() or "20"
    )
    status_not_found_recheck_seconds = int(
        os.getenv("STATUS_NOT_FOUND_RECHECK_SECONDS", "3600").strip() or "3600"
    )
    dashboard_sync_interval_seconds = float(
        os.getenv("DASHBOARD_SYNC_INTERVAL_SECONDS", "300").strip() or "300"
    )
    completed_bulk_dashboard_scan_interval_seconds = float(
        os.getenv(
            "COMPLETED_BULK_DASHBOARD_SCAN_INTERVAL_SECONDS",
            "3600",
        ).strip()
        or "3600"
    )
    bulk_relocation_search_interval_seconds = int(
        os.getenv("BULK_RELOCATION_SEARCH_INTERVAL_SECONDS", "3600").strip() or "3600"
    )
    dashboard_outbox_retry_base_seconds = int(
        os.getenv("DASHBOARD_OUTBOX_RETRY_BASE_SECONDS", "60").strip() or "60"
    )
    dashboard_outbox_retry_max_seconds = int(
        os.getenv("DASHBOARD_OUTBOX_RETRY_MAX_SECONDS", "3600").strip() or "3600"
    )
    dashboard_outbox_sending_stale_seconds = int(
        os.getenv("DASHBOARD_OUTBOX_SENDING_STALE_SECONDS", "300").strip() or "300"
    )
    if status_polling_interval_seconds <= 0:
        raise ValueError("STATUS_POLLING_INTERVAL_SECONDS must be greater than 0")
    if status_polling_memory_log_interval < 0:
        raise ValueError("STATUS_POLLING_MEMORY_LOG_INTERVAL must be greater than or equal to 0")
    if status_not_found_threshold <= 0:
        raise ValueError("STATUS_NOT_FOUND_THRESHOLD must be greater than 0")
    if status_not_found_recheck_seconds <= 0:
        raise ValueError("STATUS_NOT_FOUND_RECHECK_SECONDS must be greater than 0")
    if dashboard_sync_interval_seconds <= 0:
        raise ValueError("DASHBOARD_SYNC_INTERVAL_SECONDS must be greater than 0")
    if completed_bulk_dashboard_scan_interval_seconds <= 0:
        raise ValueError(
            "COMPLETED_BULK_DASHBOARD_SCAN_INTERVAL_SECONDS must be greater than 0"
        )
    if bulk_relocation_search_interval_seconds <= 0:
        raise ValueError("BULK_RELOCATION_SEARCH_INTERVAL_SECONDS must be greater than 0")
    bulk_reserved_rows = int(os.getenv("BULK_RESERVED_ROWS", "100").strip() or "100")
    bulk_max_rows = int(os.getenv("BULK_MAX_ROWS", "50").strip() or "50")
    bulk_registration_stale_seconds = int(
        os.getenv("BULK_REGISTRATION_STALE_SECONDS", "600").strip() or "600"
    )
    bulk_creation_stale_seconds = int(
        os.getenv("BULK_CREATION_STALE_SECONDS", "600").strip() or "600"
    )
    notification_max_attempts = int(
        os.getenv("NOTIFICATION_MAX_ATTEMPTS", "10").strip() or "10"
    )
    notification_retry_base_seconds = int(
        os.getenv("NOTIFICATION_RETRY_BASE_SECONDS", "30").strip() or "30"
    )
    notification_sending_stale_seconds = int(
        os.getenv("NOTIFICATION_SENDING_STALE_SECONDS", "300").strip() or "300"
    )
    notification_message_max_chars = int(
        os.getenv("NOTIFICATION_MESSAGE_MAX_CHARS", "3500").strip() or "3500"
    )
    urgent_editor_notifications_enabled = _env_bool(
        "URGENT_EDITOR_NOTIFICATIONS_ENABLED",
        default=False,
    )
    editor_urgent_chat_id_raw = os.getenv("EDITOR_URGENT_CHAT_ID", "").strip()
    editor_urgent_chat_id: int | None = None
    if editor_urgent_chat_id_raw:
        try:
            editor_urgent_chat_id = int(editor_urgent_chat_id_raw)
        except ValueError as exc:
            raise ValueError("EDITOR_URGENT_CHAT_ID must be an integer") from exc
    if urgent_editor_notifications_enabled and editor_urgent_chat_id is None:
        raise ValueError(
            "EDITOR_URGENT_CHAT_ID must be set when "
            "URGENT_EDITOR_NOTIFICATIONS_ENABLED=true"
        )
    if bulk_reserved_rows <= 0:
        raise ValueError("BULK_RESERVED_ROWS must be greater than 0")
    if bulk_max_rows <= 0:
        raise ValueError("BULK_MAX_ROWS must be greater than 0")
    positive_values = {
        "BULK_REGISTRATION_STALE_SECONDS": bulk_registration_stale_seconds,
        "BULK_CREATION_STALE_SECONDS": bulk_creation_stale_seconds,
        "NOTIFICATION_MAX_ATTEMPTS": notification_max_attempts,
        "NOTIFICATION_RETRY_BASE_SECONDS": notification_retry_base_seconds,
        "NOTIFICATION_SENDING_STALE_SECONDS": notification_sending_stale_seconds,
        "NOTIFICATION_MESSAGE_MAX_CHARS": notification_message_max_chars,
        "DASHBOARD_OUTBOX_RETRY_BASE_SECONDS": dashboard_outbox_retry_base_seconds,
        "DASHBOARD_OUTBOX_RETRY_MAX_SECONDS": dashboard_outbox_retry_max_seconds,
        "DASHBOARD_OUTBOX_SENDING_STALE_SECONDS": (
            dashboard_outbox_sending_stale_seconds
        ),
        "STATUS_NOT_FOUND_THRESHOLD": status_not_found_threshold,
        "STATUS_NOT_FOUND_RECHECK_SECONDS": status_not_found_recheck_seconds,
    }
    for name, value in positive_values.items():
        if value <= 0:
            raise ValueError(f"{name} must be greater than 0")
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_proxy_url=os.getenv("TELEGRAM_PROXY_URL", "").strip() or None,
        telegram_request_timeout=float(os.getenv("TELEGRAM_REQUEST_TIMEOUT", "60").strip() or "60"),
        sqlite_path=os.getenv("SQLITE_PATH", "/data/app.db").strip() or "/data/app.db",
        google_dashboard_spreadsheet_id=os.getenv(
            "GOOGLE_DASHBOARD_SPREADSHEET_ID",
            "",
        ).strip(),
        google_fl_spreadsheet_id=os.getenv("GOOGLE_FL_SPREADSHEET_ID", "").strip(),
        google_sme_spreadsheet_id=os.getenv("GOOGLE_SME_SPREADSHEET_ID", "").strip(),
        google_ai_spreadsheet_id=os.getenv("GOOGLE_AI_SPREADSHEET_ID", "").strip(),
        google_voice_collection_spreadsheet_id=os.getenv(
            "GOOGLE_VOICE_COLLECTION_SPREADSHEET_ID",
            "",
        ).strip(),
        google_spreadsheet_id=os.getenv("GOOGLE_SPREADSHEET_ID", "").strip(),
        google_sheet_name=os.getenv("GOOGLE_SHEET_NAME", "Заявки").strip() or "Заявки",
        google_high_priority_sheet_name=os.getenv(
            "GOOGLE_HIGH_PRIORITY_SHEET_NAME",
            "Высокий",
        ).strip()
        or "Высокий",
        google_low_priority_sheet_name=os.getenv(
            "GOOGLE_LOW_PRIORITY_SHEET_NAME",
            "Низкий",
        ).strip()
        or "Низкий",
        google_bulk_sheet_name=os.getenv(
            "GOOGLE_BULK_SHEET_NAME",
            "Массовые",
        ).strip()
        or "Массовые",
        google_credentials_path=os.getenv(
            "GOOGLE_CREDENTIALS_PATH",
            default_credentials_path,
        ).strip()
        or default_credentials_path,
        gigachat_credentials=os.getenv("GIGACHAT_CREDENTIALS", "").strip(),
        gigachat_base_url=os.getenv(
            "GIGACHAT_BASE_URL",
            "https://gigachat.devices.sberbank.ru/api/v1",
        ).strip()
        or "https://gigachat.devices.sberbank.ru/api/v1",
        gigachat_auth_url=os.getenv(
            "GIGACHAT_AUTH_URL",
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        ).strip()
        or "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        gigachat_scope=os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
        or "GIGACHAT_API_PERS",
        gigachat_model=os.getenv("GIGACHAT_MODEL", "GigaChat").strip() or "GigaChat",
        gigachat_verify_ssl_certs=_env_bool("GIGACHAT_VERIFY_SSL_CERTS", default=True),
        gigachat_ca_bundle_file=os.getenv("GIGACHAT_CA_BUNDLE_FILE", "").strip() or None,
        gigachat_timeout=float(os.getenv("GIGACHAT_TIMEOUT", "60").strip() or "60"),
        gigachat_max_retries=int(os.getenv("GIGACHAT_MAX_RETRIES", "3").strip() or "3"),
        gigachat_retry_backoff_factor=float(
            os.getenv("GIGACHAT_RETRY_BACKOFF_FACTOR", "1").strip() or "1"
        ),
        gigachat_system_prompt_path=os.getenv(
            "GIGACHAT_SYSTEM_PROMPT_PATH",
            "prompts/gigachat_system_v2.md",
        ).strip()
        or "prompts/gigachat_system_v2.md",
        gigachat_user_prompt_path=os.getenv(
            "GIGACHAT_USER_PROMPT_PATH",
            "prompts/gigachat_user_v2.md",
        ).strip()
        or "prompts/gigachat_user_v2.md",
        gigachat_show_response_json=_env_bool("GIGACHAT_SHOW_RESPONSE_JSON", default=False),
        status_polling_enabled=_env_bool("STATUS_POLLING_ENABLED", default=True),
        status_polling_interval_seconds=status_polling_interval_seconds,
        status_polling_memory_log_interval=status_polling_memory_log_interval,
        status_not_found_threshold=status_not_found_threshold,
        status_not_found_recheck_seconds=status_not_found_recheck_seconds,
        dashboard_sync_interval_seconds=dashboard_sync_interval_seconds,
        completed_bulk_dashboard_scan_interval_seconds=(
            completed_bulk_dashboard_scan_interval_seconds
        ),
        bulk_relocation_search_interval_seconds=bulk_relocation_search_interval_seconds,
        dashboard_outbox_retry_base_seconds=dashboard_outbox_retry_base_seconds,
        dashboard_outbox_retry_max_seconds=dashboard_outbox_retry_max_seconds,
        dashboard_outbox_sending_stale_seconds=dashboard_outbox_sending_stale_seconds,
        bulk_reserved_rows=bulk_reserved_rows,
        bulk_max_rows=bulk_max_rows,
        bulk_registration_stale_seconds=bulk_registration_stale_seconds,
        bulk_creation_stale_seconds=bulk_creation_stale_seconds,
        notification_max_attempts=notification_max_attempts,
        notification_retry_base_seconds=notification_retry_base_seconds,
        notification_sending_stale_seconds=notification_sending_stale_seconds,
        notification_message_max_chars=notification_message_max_chars,
        urgent_editor_notifications_enabled=urgent_editor_notifications_enabled,
        editor_urgent_chat_id=editor_urgent_chat_id,
        rollout_schedule=RolloutSchedule.from_strings(
            timezone_name=os.getenv("BOT_TIMEZONE", DEFAULT_TIMEZONE).strip()
            or DEFAULT_TIMEZONE,
            wednesday_cutoff=(
                os.getenv("ROLLOUT_WEDNESDAY_CUTOFF", DEFAULT_WEDNESDAY_CUTOFF).strip()
                or DEFAULT_WEDNESDAY_CUTOFF
            ),
            thursday_cutoff=(
                os.getenv("ROLLOUT_THURSDAY_CUTOFF", DEFAULT_THURSDAY_CUTOFF).strip()
                or DEFAULT_THURSDAY_CUTOFF
            ),
        ),
        application_editors=_env_editors("APPLICATION_EDITORS"),
        google_api_retry=GoogleApiRetryConfig(
            max_attempts=int(os.getenv("GOOGLE_API_MAX_ATTEMPTS", "4").strip() or "4"),
            base_seconds=float(
                os.getenv("GOOGLE_API_RETRY_BASE_SECONDS", "1").strip() or "1"
            ),
            max_seconds=float(
                os.getenv("GOOGLE_API_RETRY_MAX_SECONDS", "8").strip() or "8"
            ),
        ),
        status_polling_heartbeat_path=os.getenv(
            "STATUS_POLLING_HEARTBEAT_PATH",
            "/data/status-polling-heartbeat.json",
        ).strip()
        or "/data/status-polling-heartbeat.json",
    )


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_editors(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "редактор 1,редактор 2")
    editors = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not editors:
        raise ValueError(f"{name} must contain at least one editor")
    if "Редактор не выбран" in editors:
        raise ValueError(f"{name} must not contain reserved value 'Редактор не выбран'")
    return editors
