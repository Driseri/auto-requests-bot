from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .schemas import Summary


STATUS_ORDER = {
    "UNKNOWN": 0,
    "OK": 1,
    "DEGRADED": 2,
    "ACTION_REQUIRED": 3,
    "CRITICAL": 4,
}


def summarize_snapshot(snapshot: dict[str, Any]) -> tuple[Summary, dict[str, Any]]:
    """Calculate overall status and concrete operator recommendations."""

    status = "OK"
    problems: list[str] = []
    recommendations: list[str] = []
    threshold_details: dict[str, Any] = {}

    def raise_to(next_status: str, problem: str, recommendation: str) -> None:
        nonlocal status
        if STATUS_ORDER[next_status] > STATUS_ORDER[status]:
            status = next_status
        problems.append(problem)
        recommendations.append(recommendation)

    if snapshot.get("collection_status") == "failed":
        raise_to("ACTION_REQUIRED", "Dashboard не смог собрать данные с VPS.", "Проверить SSH доступ.")

    container = snapshot.get("container", {})
    state = str(container.get("state", "")).lower()
    health = str(container.get("health", "")).lower()
    if state in {"exited", "restarting"} or health == "unhealthy":
        raise_to("CRITICAL", "Контейнер bot не healthy.", "Смотреть docker ps, healthcheck и логи.")
    elif health == "starting":
        raise_to("DEGRADED", "Контейнер bot еще starting.", "Подождать 2-3 минуты.")

    polling = snapshot.get("polling", {})
    heartbeat_age = _float_or_none(polling.get("heartbeat_age_seconds"))
    max_age = _float_or_none(polling.get("max_age_seconds")) or 180.0
    if heartbeat_age is None:
        raise_to("ACTION_REQUIRED", "Нет polling heartbeat.", "Проверить /data/status-polling-heartbeat.json.")
    else:
        threshold_details["heartbeat_age_seconds"] = heartbeat_age
        if heartbeat_age > max_age:
            raise_to("CRITICAL", "Polling heartbeat stale.", "Проверить Google API и polling loop.")
        elif heartbeat_age > max_age * 0.66:
            raise_to("DEGRADED", "Polling heartbeat стареет.", "Понаблюдать один polling интервал.")

    vps = snapshot.get("vps", {})
    disk_used = _float_or_none(vps.get("disk_used_percent"))
    disk_free_gb = _float_or_none(vps.get("disk_free_gb"))
    if disk_used is not None:
        threshold_details["disk_used_percent"] = disk_used
        if disk_used >= 85 or (disk_free_gb is not None and disk_free_gb < 2):
            raise_to("CRITICAL", "На VPS мало диска.", "Проверить backups, tar images и Docker logs.")
        elif disk_used >= 75 or (disk_free_gb is not None and disk_free_gb < 4):
            raise_to("DEGRADED", "Диск VPS близок к лимиту.", "Запланировать очистку старых артефактов.")

    queues = snapshot.get("queues", {})
    notification = queues.get("notification_outbox", {})
    dashboard = queues.get("dashboard_outbox", {})
    if int(notification.get("failed", 0) or 0) > 0:
        raise_to("ACTION_REQUIRED", "Есть FAILED Telegram уведомления.", "Разобрать notification_outbox.")
    if int(notification.get("old_sending", 0) or 0) > 0:
        raise_to("ACTION_REQUIRED", "Есть зависшие SENDING Telegram уведомления.", "Проверить Telegram delivery.")
    if int(dashboard.get("old_pending", 0) or 0) > 0:
        raise_to("DEGRADED", "Dashboard outbox копится.", "Проверить Google dashboard sync.")

    applications = snapshot.get("applications", {})
    if int(applications.get("not_found_total", 0) or 0) > 0:
        raise_to("DEGRADED", "Есть заявки в NOT_FOUND.", "Проверить polling диапазоны Google Sheets.")

    bulk = snapshot.get("bulk", {})
    if int(bulk.get("stale_creating", 0) or 0) > 0 or int(bulk.get("stale_registering", 0) or 0) > 0:
        raise_to("ACTION_REQUIRED", "Есть stale массовые пачки.", "Проверить bulk workflow.")

    log_events = snapshot.get("log_events", {})
    if log_events.get("traceback"):
        raise_to("ACTION_REQUIRED", "В логах есть traceback.", "Открыть последние проблемы.")
    if log_events.get("oom"):
        raise_to("CRITICAL", "В логах есть OOM.", "Проверить память и рестарты.")

    if not recommendations:
        recommendations.append("Ничего не делать: основные показатели в норме.")

    # Remove duplicates while preserving the most useful order.
    recommendations = list(dict.fromkeys(recommendations))
    problems = list(dict.fromkeys(problems))
    return Summary(status=status, recommendations=recommendations, problems=problems), threshold_details


def heartbeat_age_seconds(updated_at: str | None) -> float | None:
    """Return heartbeat age in seconds from an ISO timestamp."""

    if not updated_at:
        return None
    try:
        parsed = datetime.fromisoformat(updated_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
