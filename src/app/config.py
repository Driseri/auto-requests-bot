from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

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
    dashboard_sync_interval_seconds: float
    bulk_reserved_rows: int
    bulk_registration_stale_seconds: int
    rollout_schedule: RolloutSchedule
    application_editors: tuple[str, ...]


def load_settings() -> Settings:
    load_dotenv()
    docker_credentials_path = Path("/run/secrets/google_credentials.json")
    default_credentials_path = (
        str(docker_credentials_path) if docker_credentials_path.exists() else "credentials.json"
    )
    status_polling_interval_seconds = float(
        os.getenv("STATUS_POLLING_INTERVAL_SECONDS", "30").strip() or "30"
    )
    dashboard_sync_interval_seconds = float(
        os.getenv("DASHBOARD_SYNC_INTERVAL_SECONDS", "300").strip() or "300"
    )
    if status_polling_interval_seconds <= 0:
        raise ValueError("STATUS_POLLING_INTERVAL_SECONDS must be greater than 0")
    if dashboard_sync_interval_seconds <= 0:
        raise ValueError("DASHBOARD_SYNC_INTERVAL_SECONDS must be greater than 0")
    bulk_reserved_rows = int(os.getenv("BULK_RESERVED_ROWS", "100").strip() or "100")
    bulk_registration_stale_seconds = int(
        os.getenv("BULK_REGISTRATION_STALE_SECONDS", "600").strip() or "600"
    )
    if bulk_reserved_rows <= 0:
        raise ValueError("BULK_RESERVED_ROWS must be greater than 0")
    if bulk_registration_stale_seconds <= 0:
        raise ValueError("BULK_REGISTRATION_STALE_SECONDS must be greater than 0")
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
            "prompts/gigachat_system.md",
        ).strip()
        or "prompts/gigachat_system.md",
        gigachat_user_prompt_path=os.getenv(
            "GIGACHAT_USER_PROMPT_PATH",
            "prompts/gigachat_user.md",
        ).strip()
        or "prompts/gigachat_user.md",
        gigachat_show_response_json=_env_bool("GIGACHAT_SHOW_RESPONSE_JSON", default=False),
        status_polling_enabled=_env_bool("STATUS_POLLING_ENABLED", default=True),
        status_polling_interval_seconds=status_polling_interval_seconds,
        dashboard_sync_interval_seconds=dashboard_sync_interval_seconds,
        bulk_reserved_rows=bulk_reserved_rows,
        bulk_registration_stale_seconds=bulk_registration_stale_seconds,
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
