from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any


EXTERNAL_HEALTH_CACHE_VERSION = 1
TelegramProbe = Callable[[str, str | None, float], None]
GoogleProbe = Callable[[str, Sequence[str], float], None]


def write_heartbeat(path: str, *, iteration: int) -> None:
    """Атомарно записать heartbeat успешного цикла фонового polling."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "iteration": iteration,
            }
        ),
        encoding="utf-8",
    )
    temporary.replace(target)


def check_health(
    *,
    polling_enabled: bool,
    heartbeat_path: str,
    sqlite_path: str,
    max_age_seconds: float,
    telegram_bot_token: str | None = None,
    telegram_proxy_url: str | None = None,
    google_credentials_path: str | None = None,
    required_spreadsheet_ids: Mapping[str, str] | None = None,
    optional_spreadsheet_ids: Mapping[str, str] | None = None,
    external_cache_path: str = "/data/external-health.json",
    external_check_interval_seconds: float = 300,
    external_failure_threshold: int = 2,
    external_timeout_seconds: float = 10,
    telegram_probe: TelegramProbe | None = None,
    google_probe: GoogleProbe | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Проверить локальное состояние и, если настроено, доступность внешних API."""
    local_result = _check_local_health(
        polling_enabled=polling_enabled,
        heartbeat_path=heartbeat_path,
        sqlite_path=sqlite_path,
        max_age_seconds=max_age_seconds,
    )
    if not local_result[0]:
        return local_result

    external_configured = any(
        value is not None
        for value in (
            telegram_bot_token,
            google_credentials_path,
            required_spreadsheet_ids,
            optional_spreadsheet_ids,
        )
    )
    if not external_configured:
        return True, "ok"

    config_error, spreadsheet_ids = _validate_external_configuration(
        telegram_bot_token=telegram_bot_token,
        google_credentials_path=google_credentials_path,
        required_spreadsheet_ids=required_spreadsheet_ids or {},
        optional_spreadsheet_ids=optional_spreadsheet_ids or {},
        external_check_interval_seconds=external_check_interval_seconds,
        external_failure_threshold=external_failure_threshold,
        external_timeout_seconds=external_timeout_seconds,
    )
    if config_error:
        return False, config_error

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cache = _read_external_cache(external_cache_path)
    if _external_cache_is_fresh(
        cache,
        now=current_time,
        interval_seconds=external_check_interval_seconds,
    ):
        return _external_cache_result(cache, external_failure_threshold)

    telegram_check = telegram_probe or probe_telegram
    google_check = google_probe or probe_google_spreadsheets
    try:
        telegram_check(
            telegram_bot_token or "",
            telegram_proxy_url,
            external_timeout_seconds,
        )
        google_check(
            google_credentials_path or "",
            spreadsheet_ids,
            external_timeout_seconds,
        )
    except Exception as exc:
        cache = _failed_external_cache(cache, current_time, exc)
    else:
        cache = {
            "version": EXTERNAL_HEALTH_CACHE_VERSION,
            "last_attempt_at": current_time.isoformat(),
            "last_success_at": current_time.isoformat(),
            "consecutive_failures": 0,
            "result": "ok",
        }
    _write_external_cache(external_cache_path, cache)
    return _external_cache_result(cache, external_failure_threshold)


def _check_local_health(
    *,
    polling_enabled: bool,
    heartbeat_path: str,
    sqlite_path: str,
    max_age_seconds: float,
) -> tuple[bool, str]:
    if polling_enabled:
        try:
            payload = json.loads(Path(heartbeat_path).read_text(encoding="utf-8"))
            updated_at = datetime.fromisoformat(payload["updated_at"])
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - updated_at).total_seconds()
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return False, f"heartbeat unavailable: {exc}"
        if age > max_age_seconds:
            return False, f"heartbeat is stale: {age:.1f}s"

    try:
        with sqlite3.connect(sqlite_path, timeout=10) as connection:
            connection.execute("PRAGMA busy_timeout=10000")
            result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        return False, f"sqlite unavailable: {exc}"
    if not result or result[0] != "ok":
        return False, f"sqlite quick_check failed: {result}"
    return True, "ok"


def _validate_external_configuration(
    *,
    telegram_bot_token: str | None,
    google_credentials_path: str | None,
    required_spreadsheet_ids: Mapping[str, str],
    optional_spreadsheet_ids: Mapping[str, str],
    external_check_interval_seconds: float,
    external_failure_threshold: int,
    external_timeout_seconds: float,
) -> tuple[str | None, list[str]]:
    if not (telegram_bot_token or "").strip():
        return "configuration invalid: TELEGRAM_BOT_TOKEN is required", []
    if external_check_interval_seconds <= 0:
        return "configuration invalid: HEALTH_EXTERNAL_CHECK_INTERVAL_SECONDS must be positive", []
    if external_failure_threshold < 1:
        return "configuration invalid: HEALTH_EXTERNAL_FAILURE_THRESHOLD must be at least 1", []
    if external_timeout_seconds <= 0:
        return "configuration invalid: HEALTH_EXTERNAL_TIMEOUT_SECONDS must be positive", []

    missing_ids = sorted(name for name, value in required_spreadsheet_ids.items() if not value.strip())
    if missing_ids:
        return f"configuration invalid: missing spreadsheet IDs: {', '.join(missing_ids)}", []

    credentials_path = Path(google_credentials_path or "")
    if not credentials_path.is_file():
        return "configuration invalid: Google credentials file is unavailable", []
    try:
        credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "configuration invalid: Google credentials JSON is invalid", []
    if not isinstance(credentials, dict) or any(
        not str(credentials.get(field, "")).strip()
        for field in ("type", "client_email", "private_key")
    ):
        return "configuration invalid: Google service account fields are missing", []
    if credentials.get("type") != "service_account":
        return "configuration invalid: Google credentials must be a service account", []

    spreadsheet_ids = [
        value.strip()
        for value in (*required_spreadsheet_ids.values(), *optional_spreadsheet_ids.values())
        if value.strip()
    ]
    return None, list(dict.fromkeys(spreadsheet_ids))


def _read_external_cache(path: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != EXTERNAL_HEALTH_CACHE_VERSION:
        return {}
    return payload


def _write_external_cache(path: str, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    temporary.replace(target)


def _external_cache_is_fresh(
    cache: Mapping[str, Any],
    *,
    now: datetime,
    interval_seconds: float,
) -> bool:
    try:
        last_attempt = datetime.fromisoformat(str(cache["last_attempt_at"]))
    except (KeyError, ValueError):
        return False
    if last_attempt.tzinfo is None:
        last_attempt = last_attempt.replace(tzinfo=timezone.utc)
    age = (now - last_attempt.astimezone(timezone.utc)).total_seconds()
    return 0 <= age < interval_seconds


def _external_cache_result(
    cache: Mapping[str, Any],
    failure_threshold: int,
) -> tuple[bool, str]:
    failures = int(cache.get("consecutive_failures", 0) or 0)
    last_success = str(cache.get("last_success_at", "") or "")
    result = str(cache.get("result", "external checks unavailable") or "external checks unavailable")
    if failures == 0 and last_success:
        return True, "ok"
    if not last_success or failures >= failure_threshold:
        return False, f"external checks failed: {result}"
    return True, f"ok (external checks degraded: {result})"


def _failed_external_cache(
    previous: Mapping[str, Any],
    now: datetime,
    exc: Exception,
) -> dict[str, Any]:
    return {
        "version": EXTERNAL_HEALTH_CACHE_VERSION,
        "last_attempt_at": now.isoformat(),
        "last_success_at": str(previous.get("last_success_at", "") or ""),
        "consecutive_failures": int(previous.get("consecutive_failures", 0) or 0) + 1,
        "result": _safe_external_error(exc),
    }


def _safe_external_error(exc: Exception) -> str:
    service = getattr(exc, "service", "external")
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return f"{service}: HTTP {status}"
    return f"{service}: {type(exc).__name__}"


class ExternalProbeError(RuntimeError):
    def __init__(self, service: str, *, status: int | None = None) -> None:
        super().__init__(service)
        self.service = service
        self.status = status


def probe_telegram(token: str, proxy_url: str | None, timeout_seconds: float) -> None:
    asyncio.run(_probe_telegram_async(token, proxy_url, timeout_seconds))


async def _probe_telegram_async(
    token: str,
    proxy_url: str | None,
    timeout_seconds: float,
) -> None:
    from aiohttp import ClientSession, ClientTimeout

    connector = None
    if proxy_url:
        from aiohttp_socks import ProxyConnector

        connector = ProxyConnector.from_url(proxy_url)
    timeout = ClientTimeout(total=timeout_seconds)
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        async with ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise ExternalProbeError("telegram", status=response.status)
                payload = await response.json()
                if not payload.get("ok"):
                    raise ExternalProbeError("telegram")
    except ExternalProbeError:
        raise
    except Exception as exc:
        raise ExternalProbeError("telegram") from exc


def probe_google_spreadsheets(
    credentials_path: str,
    spreadsheet_ids: Sequence[str],
    timeout_seconds: float,
) -> None:
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.service_account import Credentials

    credentials = Credentials.from_service_account_file(
        credentials_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    session = AuthorizedSession(credentials)
    try:
        for spreadsheet_id in spreadsheet_ids:
            try:
                response = session.get(
                    f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
                    params={"fields": "spreadsheetId"},
                    timeout=timeout_seconds,
                )
            except Exception as exc:
                raise ExternalProbeError("google") from exc
            if response.status_code != 200:
                raise ExternalProbeError("google", status=response.status_code)
    finally:
        session.close()


def main() -> int:
    interval = float(os.getenv("STATUS_POLLING_INTERVAL_SECONDS", "30") or "30")
    max_age = float(
        os.getenv("STATUS_POLLING_HEARTBEAT_MAX_AGE_SECONDS", str(max(180, interval * 3)))
        or max(180, interval * 3)
    )
    enabled = os.getenv("STATUS_POLLING_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    healthy, message = check_health(
        polling_enabled=enabled,
        heartbeat_path=os.getenv(
            "STATUS_POLLING_HEARTBEAT_PATH",
            "/data/status-polling-heartbeat.json",
        ),
        sqlite_path=os.getenv("SQLITE_PATH", "/data/app.db"),
        max_age_seconds=max_age,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_proxy_url=os.getenv("TELEGRAM_PROXY_URL", "").strip() or None,
        google_credentials_path=os.getenv(
            "GOOGLE_CREDENTIALS_PATH",
            "/run/secrets/google_credentials.json",
        ),
        required_spreadsheet_ids={
            "GOOGLE_FL_SPREADSHEET_ID": os.getenv("GOOGLE_FL_SPREADSHEET_ID", ""),
            "GOOGLE_SME_SPREADSHEET_ID": os.getenv("GOOGLE_SME_SPREADSHEET_ID", ""),
            "GOOGLE_AI_SPREADSHEET_ID": os.getenv("GOOGLE_AI_SPREADSHEET_ID", ""),
            "GOOGLE_VOICE_COLLECTION_SPREADSHEET_ID": os.getenv(
                "GOOGLE_VOICE_COLLECTION_SPREADSHEET_ID",
                "",
            ),
        },
        optional_spreadsheet_ids={
            "GOOGLE_DASHBOARD_SPREADSHEET_ID": os.getenv(
                "GOOGLE_DASHBOARD_SPREADSHEET_ID",
                "",
            ),
        },
        external_cache_path=os.getenv(
            "HEALTH_EXTERNAL_CACHE_PATH",
            "/data/external-health.json",
        ),
        external_check_interval_seconds=float(
            os.getenv("HEALTH_EXTERNAL_CHECK_INTERVAL_SECONDS", "300") or "300"
        ),
        external_failure_threshold=int(
            os.getenv("HEALTH_EXTERNAL_FAILURE_THRESHOLD", "2") or "2"
        ),
        external_timeout_seconds=float(
            os.getenv("HEALTH_EXTERNAL_TIMEOUT_SECONDS", "10") or "10"
        ),
    )
    print(message)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
