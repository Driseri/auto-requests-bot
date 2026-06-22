from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class LogRule:
    """Pattern and operator guidance for one log problem category."""

    key: str
    pattern: re.Pattern[str]
    recommendation: str


RULES = [
    LogRule("traceback", re.compile(r"traceback", re.I), "Разобрать traceback в логах."),
    LogRule(
        "polling_failed",
        re.compile(r"Status polling iteration failed", re.I),
        "Проверить polling, Google API и heartbeat.",
    ),
    LogRule(
        "google_429",
        re.compile(r"429|quota exceeded|rate limit", re.I),
        "Подождать retry; если повторяется, снизить частоту операций Google.",
    ),
    LogRule(
        "google_400_403",
        re.compile(r"HttpError 40[03]|HTTP 40[03]|unable to parse range|permission", re.I),
        "Проверить права service account, диапазоны и схему Google Sheets.",
    ),
    LogRule(
        "google_transient",
        re.compile(r"Temporary Google API failure|BrokenPipe|timeout|HTTP 5\d\d", re.I),
        "Подождать retry, если после ошибки есть восстановление.",
    ),
    LogRule(
        "telegram_network",
        re.compile(r"TelegramNetworkError|Connection reset|Request timeout", re.I),
        "Считать временным, если уведомления продолжают уходить.",
    ),
    LogRule(
        "dashboard_outbox_failed",
        re.compile(r"Dashboard outbox delivery failed|dashboard outbox", re.I),
        "Проверить dashboard spreadsheet, права и схему.",
    ),
    LogRule(
        "gigachat_failed",
        re.compile(r"GigaChat.*(validation|invalid|fallback|error)|invalid JSON|schema_validation", re.I),
        "Проверить долю fallback и последние ошибки GigaChat.",
    ),
    LogRule("oom", re.compile(r"\bOOM\b|out of memory", re.I), "Проверить память и restart count."),
    LogRule("unhealthy", re.compile(r"unhealthy", re.I), "Проверить healthcheck и heartbeat."),
]


def classify_logs(log_text: str) -> dict[str, dict[str, object]]:
    """Group fetched Docker logs by operationally useful categories."""

    groups: dict[str, dict[str, object]] = {}
    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for rule in RULES:
            if not rule.pattern.search(line):
                continue
            group = groups.setdefault(
                rule.key,
                {
                    "count": 0,
                    "last_message": "",
                    "recommendation": rule.recommendation,
                },
            )
            group["count"] = int(group["count"]) + 1
            group["last_message"] = _safe_preview(line)
    return groups


def _safe_preview(value: str, limit: int = 500) -> str:
    """Keep log previews short and avoid storing large raw payloads."""

    value = re.sub(r"(bot)[0-9]{8,}:[A-Za-z0-9_-]+", r"\1<redacted>", value)
    return value[:limit]
