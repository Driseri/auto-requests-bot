from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


GOOGLE_SHEETS_EPOCH = datetime(1899, 12, 30)
GOOGLE_SHEETS_DATE_TIME_PATTERN = "dd.MM.yyyy hh:mm"


def google_sheets_date_cell(
    value: datetime | str,
    *,
    timezone_name: str,
    bold: bool = False,
) -> dict[str, Any]:
    """Convert an instant to a typed Google Sheets date/time cell."""
    local_moment = local_datetime(value, timezone_name=timezone_name)
    local_naive = local_moment.replace(tzinfo=None)
    serial = (local_naive - GOOGLE_SHEETS_EPOCH).total_seconds() / 86400
    user_entered_format: dict[str, Any] = {
        "numberFormat": {
            "type": "DATE_TIME",
            "pattern": GOOGLE_SHEETS_DATE_TIME_PATTERN,
        }
    }
    if bold:
        user_entered_format["textFormat"] = {"bold": True}
    return {
        "userEnteredValue": {"numberValue": serial},
        "userEnteredFormat": user_entered_format,
    }


def local_datetime(value: datetime | str, *, timezone_name: str) -> datetime:
    moment = _parse_datetime(value) if isinstance(value, str) else value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo(timezone_name))


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return datetime.fromisoformat(normalized)
