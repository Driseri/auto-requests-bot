from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_WEDNESDAY_CUTOFF = "14:00"
DEFAULT_THURSDAY_CUTOFF = "14:00"


@dataclass(frozen=True, slots=True)
class RolloutSchedule:
    timezone_name: str
    wednesday_cutoff: time
    thursday_cutoff: time

    @classmethod
    def from_strings(
        cls,
        *,
        timezone_name: str = DEFAULT_TIMEZONE,
        wednesday_cutoff: str = DEFAULT_WEDNESDAY_CUTOFF,
        thursday_cutoff: str = DEFAULT_THURSDAY_CUTOFF,
    ) -> RolloutSchedule:
        """Создать расписание раскатки и сразу проверить timezone и cutoffs."""
        normalized_timezone = timezone_name.strip() or DEFAULT_TIMEZONE
        try:
            ZoneInfo(normalized_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown BOT_TIMEZONE: {normalized_timezone}") from exc

        return cls(
            timezone_name=normalized_timezone,
            wednesday_cutoff=_parse_cutoff(
                "ROLLOUT_WEDNESDAY_CUTOFF",
                wednesday_cutoff,
            ),
            thursday_cutoff=_parse_cutoff(
                "ROLLOUT_THURSDAY_CUTOFF",
                thursday_cutoff,
            ),
        )

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

def rollout_sheet_name(
    submitted_at: datetime,
    schedule: RolloutSchedule,
) -> str:
    """Выбрать лист среды/четверга по фактическому времени отправки заявки."""
    local_moment = _as_aware(submitted_at).astimezone(schedule.timezone)
    monday = local_moment.date() - timedelta(days=local_moment.weekday())
    wednesday_cutoff = datetime.combine(
        monday + timedelta(days=2),
        schedule.wednesday_cutoff,
        tzinfo=schedule.timezone,
    )
    thursday_cutoff = datetime.combine(
        monday + timedelta(days=3),
        schedule.thursday_cutoff,
        tzinfo=schedule.timezone,
    )

    if local_moment < wednesday_cutoff:
        target_monday = monday
        suffix = "ср"
    elif local_moment < thursday_cutoff:
        target_monday = monday
        suffix = "чт"
    else:
        target_monday = monday + timedelta(days=7)
        suffix = "ср"
    return f"{target_monday.strftime('%d.%m')} {suffix}"


def cutoff_to_string(value: time) -> str:
    return value.strftime("%H:%M")


def _parse_cutoff(name: str, value: str) -> time:
    normalized = value.strip()
    if not re.fullmatch(r"\d{2}:\d{2}", normalized):
        raise ValueError(f"{name} must use HH:MM format, got: {value!r}")
    try:
        return datetime.strptime(normalized, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"{name} must use HH:MM format, got: {value!r}") from exc


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


DEFAULT_ROLLOUT_SCHEDULE = RolloutSchedule.from_strings()
