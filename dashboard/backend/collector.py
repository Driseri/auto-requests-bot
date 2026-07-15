from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import re
from statistics import median
from typing import Any, Protocol

from .config import AppConfig
from .log_classifier import classify_logs
from .remote_script import build_remote_command
from .schemas import CollectResponse, Snapshot
from .ssh_client import ParamikoSshClient
from .storage import JsonStorage
from .thresholds import heartbeat_age_seconds, summarize_snapshot


class CommandRunner(Protocol):
    """Protocol used by tests to replace the real Paramiko SSH client."""

    def run(self, command: str) -> str:
        ...


VALID_APPLICATION_STATUSES = {
    "Новая",
    "В работе",
    "Нужны пояснения",
    "Итоговый ответ готов",
    "Принята",
    "Отклонена",
    "Отложена",
    "Удаление",
}
VALID_BULK_APPLICATION_STATUSES = {"Новая", "Нужны пояснения", "Принято"}
VALID_MONITORING_STATUSES = VALID_APPLICATION_STATUSES | VALID_BULK_APPLICATION_STATUSES
FINAL_ANSWER_READY_STATUS = "Итоговый ответ готов"
EDITOR_NOT_SELECTED = "Редактор не выбран"
PILOT_PERIOD_DAYS = (7, 14, 30)
CHIPS_SUCCESS_STATUSES = {"Принята", "Принято"}
CLOSED_WITHOUT_RESULT_STATUSES = {
    "Принята",
    "Принято",
    "Отклонена",
    "Отложена",
    "Удаление",
}


class DashboardCollector:
    """Coordinates rate-limited read-only collection from the VPS."""

    def __init__(
        self,
        *,
        config: AppConfig,
        storage: JsonStorage,
        runner: CommandRunner | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.runner = runner or ParamikoSshClient(config.ssh)
        self._lock = asyncio.Lock()
        self._background_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def collect(self, *, force: bool = False) -> CollectResponse:
        """Run one collection cycle unless another cycle is still running."""

        if self._lock.locked():
            return CollectResponse(skipped=True, reason="collection already running")
        async with self._lock:
            state = self.storage.load_state()
            now = datetime.now(timezone.utc)
            include_logs = force or _due(state.last_logs_at, self.config.collector.logs_interval_seconds, now)
            include_heavy = force or _due(
                state.last_heavy_at,
                self.config.collector.heavy_interval_seconds,
                now,
            )
            command = build_remote_command(
                self.config.remote,
                include_logs=include_logs,
                include_heavy=include_heavy,
            )
            try:
                output = await asyncio.to_thread(self.runner.run, command)
                raw = json.loads(output)
                snapshot = self._normalize_raw(
                    raw,
                    include_logs=include_logs,
                    include_heavy=include_heavy,
                    collection_errors=[],
                    collection_status="ok",
                )
                self.storage.save_snapshot(snapshot)
                state.last_fast_at = now.isoformat()
                if include_logs:
                    state.last_logs_at = now.isoformat()
                if include_heavy:
                    state.last_heavy_at = now.isoformat()
                state.last_success_at = now.isoformat()
                state.last_error = None
                self.storage.save_state(state)
                return CollectResponse(snapshot=snapshot)
            except Exception as exc:
                snapshot = self._failed_snapshot(exc)
                self.storage.save_snapshot(snapshot, update_latest=False)
                state.last_error_at = now.isoformat()
                state.last_error = f"{type(exc).__name__}: {exc}"
                self.storage.save_state(state)
                return CollectResponse(snapshot=snapshot)

    async def start_background(self) -> None:
        """Start conservative auto collection for the local FastAPI process."""

        if self._background_task is not None or not self.config.collector.auto_collect:
            return
        self._background_task = asyncio.create_task(self._run_forever())

    async def stop_background(self) -> None:
        """Stop background collection during application shutdown."""

        self._stop_event.set()
        if self._background_task is not None:
            self._background_task.cancel()
            await asyncio.gather(self._background_task, return_exceptions=True)

    async def _run_forever(self) -> None:
        """Poll on the fast interval; due checks decide logs/heavy sections."""

        while not self._stop_event.is_set():
            state = self.storage.load_state()
            if _due(
                state.last_fast_at,
                self.config.collector.fast_interval_seconds,
                datetime.now(timezone.utc),
            ):
                await self.collect()
            await asyncio.sleep(min(5, self.config.collector.fast_interval_seconds))

    def _normalize_raw(
        self,
        raw: dict[str, Any],
        *,
        include_logs: bool,
        include_heavy: bool,
        collection_errors: list[str],
        collection_status: str,
    ) -> Snapshot:
        """Convert remote JSON into the stable API snapshot shape."""

        container_payload = raw.get("container_payload", {}) if isinstance(raw, dict) else {}
        sqlite = container_payload.get("sqlite", {}) if isinstance(container_payload, dict) else {}
        metrics = sqlite.get("metrics", {}) if isinstance(sqlite, dict) else {}
        heartbeat = container_payload.get("heartbeat", {}) if isinstance(container_payload, dict) else {}
        external_health = (
            container_payload.get("external_health", {}) if isinstance(container_payload, dict) else {}
        )
        logs = raw.get("logs", "") if include_logs else ""
        queues = self._normalize_queues(metrics)
        applications = self._normalize_applications(metrics)
        bulk = self._normalize_bulk(metrics)
        urgent = self._normalize_urgent(metrics)
        pilot = self._normalize_pilot(metrics)

        normalized = {
            "collected_at": raw.get("collected_at") or datetime.now(timezone.utc).isoformat(),
            "source_host": self.config.ssh.host,
            "collection_status": collection_status,
            "collection_errors": [*collection_errors, *raw.get("errors", [])],
            "sections_collected": {"fast": True, "logs": include_logs, "heavy": include_heavy},
            "container": self._normalize_container(raw),
            "vps": self._normalize_vps(raw),
            "polling": self._normalize_polling(heartbeat),
            "external_health": external_health if isinstance(external_health, dict) else {},
            "sqlite_metrics": {
                "ok": bool(sqlite.get("ok")),
                "errors": sqlite.get("errors", []),
                "quick_check": sqlite.get("quick_check"),
            },
            "queues": queues,
            "applications": applications,
            "bulk": bulk,
            "urgent": urgent,
            "business": self._normalize_business(
                applications=applications,
                bulk=bulk,
                urgent=urgent,
                queues=queues,
            ),
            "pilot": pilot,
            "drafts": {
                "summary": metrics.get("drafts_summary", []),
                "pending_workflows": metrics.get("user_workflow_pending", []),
            },
            "log_events": classify_logs(logs),
            "raw": {"app_version": raw.get("app_version", ""), "remote_collected_at": raw.get("collected_at")},
        }
        summary, thresholds = summarize_snapshot(normalized)
        normalized["summary"] = summary
        normalized["thresholds"] = thresholds
        return Snapshot.model_validate(normalized)

    def _failed_snapshot(self, exc: Exception) -> Snapshot:
        """Store failed collection attempts in history without replacing latest."""

        normalized = {
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source_host": self.config.ssh.host,
            "collection_status": "failed",
            "collection_errors": [f"{type(exc).__name__}: {exc}"],
            "sections_collected": {"fast": False, "logs": False, "heavy": False},
        }
        summary, thresholds = summarize_snapshot(normalized)
        normalized["summary"] = summary
        normalized["thresholds"] = thresholds
        return Snapshot.model_validate(normalized)

    @staticmethod
    def _normalize_container(raw: dict[str, Any]) -> dict[str, Any]:
        """Extract safe Docker container status fields."""

        container = raw.get("container", {})
        inspect = container.get("inspect", {}) if isinstance(container, dict) else {}
        state = inspect.get("State", {}) if isinstance(inspect, dict) else {}
        health = state.get("Health", {}) if isinstance(state, dict) else {}
        stats = container.get("stats", {}) if isinstance(container, dict) else {}
        return {
            "id": container.get("id", ""),
            "app_version": raw.get("app_version", ""),
            "state": state.get("Status", ""),
            "health": health.get("Status", ""),
            "started_at": state.get("StartedAt", ""),
            "finished_at": state.get("FinishedAt", ""),
            "restart_count": inspect.get("RestartCount", 0),
            "oom_killed": state.get("OOMKilled", False),
            "stats": stats if isinstance(stats, dict) else {},
            "compose_ps": container.get("compose_ps", ""),
        }

    @staticmethod
    def _normalize_vps(raw: dict[str, Any]) -> dict[str, Any]:
        """Parse lightweight VPS metrics from command outputs."""

        vps = raw.get("vps", {})
        df = _parse_df_root(vps.get("df_root", ""))
        return {
            "uptime": vps.get("uptime", "").strip(),
            "load": _parse_load(vps.get("uptime", "")),
            "cpu_count": _int_or_none(vps.get("nproc")),
            "memory": _parse_free(vps.get("free_m", "")),
            "disk_used_percent": df.get("used_percent"),
            "disk_free_gb": df.get("free_gb"),
            "disk": df,
            "heavy": vps.get("heavy", {}),
        }

    @staticmethod
    def _normalize_polling(heartbeat: dict[str, Any]) -> dict[str, Any]:
        """Normalize heartbeat details and calculate age locally."""

        updated_at = heartbeat.get("updated_at") if isinstance(heartbeat, dict) else None
        age = heartbeat_age_seconds(updated_at)
        return {
            "heartbeat": heartbeat if isinstance(heartbeat, dict) else {},
            "heartbeat_updated_at": updated_at,
            "heartbeat_age_seconds": age,
            "iteration": heartbeat.get("iteration") if isinstance(heartbeat, dict) else None,
            "max_age_seconds": 180,
        }

    @staticmethod
    def _normalize_queues(metrics: dict[str, Any]) -> dict[str, Any]:
        """Prepare Telegram and dashboard outbox metrics for widgets."""

        notification_counts = _counts(metrics.get("notification_outbox_by_state", []))
        dashboard_counts = _counts(metrics.get("dashboard_outbox_by_state", []))
        notification_open = metrics.get("notification_outbox_open", [])
        return {
            "notification_outbox": {
                **notification_counts,
                "failed": notification_counts.get("FAILED", 0),
                "pending": notification_counts.get("PENDING", 0),
                "sending": notification_counts.get("SENDING", 0),
                "sent": notification_counts.get("SENT", 0),
                "old_sending": _old_state_count(notification_open, "SENDING"),
                "open": notification_open,
                "last_errors": metrics.get("notification_outbox_last_errors", []),
            },
            "dashboard_outbox": {
                **dashboard_counts,
                "pending": dashboard_counts.get("PENDING", 0),
                "sending": dashboard_counts.get("SENDING", 0),
                "old_pending": _old_state_count(
                    metrics.get("dashboard_outbox_by_entity_state", []),
                    "PENDING",
                ),
                "by_entity_state": metrics.get("dashboard_outbox_by_entity_state", []),
                "last_errors": metrics.get("dashboard_outbox_last_errors", []),
            },
        }

    @staticmethod
    def _normalize_applications(metrics: dict[str, Any]) -> dict[str, Any]:
        """Prepare business application aggregates without full request text."""

        summary = metrics.get("applications_summary", {}) or {}
        return {
            **summary,
            "by_status": _normalize_status_counts(metrics.get("applications_by_status", [])),
            "by_status_raw": metrics.get("applications_by_status", []),
            "by_direction": metrics.get("applications_by_direction", []),
            "problem_rows": metrics.get("applications_problem_rows", []),
            "not_found_total": summary.get("not_found_total", 0) or 0,
        }

    @staticmethod
    def _normalize_pilot(metrics: dict[str, Any]) -> dict[str, Any]:
        """Build pilot product metrics locally from read-only SQLite metadata."""

        raw = metrics.get("pilot_metrics_raw", {}) or {}
        applications = raw.get("applications", []) or []
        events = raw.get("events", []) or []
        notification_errors = raw.get("notification_errors", []) or []
        event_count_by_type: dict[str, int] = {}
        for event in events:
            event_type = str(event.get("event_type") or "")
            if event_type:
                event_count_by_type[event_type] = event_count_by_type.get(event_type, 0) + 1
        notes: list[str] = []
        if not events:
            notes.append("Нет событий application_events за период, временные метрики недоступны.")
        elif event_count_by_type.get("draft_started", 0) == 0:
            notes.append("Нет событий draft_started, метрика времени создания заявки пока недоступна.")

        periods = {
            f"{days}d": _pilot_period(
                applications=applications,
                events=events,
                notification_errors=notification_errors,
                days=days,
            )
            for days in PILOT_PERIOD_DAYS
        }
        has_events = bool(events)
        has_partial_events = has_events and any(
            period["kpi"]["total_applications"] > 0
            and (
                period["kpi"]["creation_time_seconds"]["sample_size"] == 0
                or period["kpi"]["first_editor_action_seconds"]["sample_size"] == 0
                or period["kpi"]["full_cycle_seconds"]["sample_size"] == 0
            )
            for period in periods.values()
        )
        if has_partial_events:
            notes.append("Временные KPI считаются только по точным application_events; fallback по текущему состоянию строк не используется.")
        return {
            "default_period": "7d",
            "stickiness": _pilot_stickiness(applications),
            "data_quality": {
                "status": "missing_events" if not has_events else ("partial_events" if has_partial_events else "ok"),
                "events_available": has_events,
                "events_count": len(events),
                "event_count_by_type": event_count_by_type,
                "notes": notes,
            },
            "periods": periods,
        }

    @staticmethod
    def _normalize_bulk(metrics: dict[str, Any]) -> dict[str, Any]:
        """Prepare new bulk-reservation workflow aggregates only."""

        summary = metrics.get("bulk_summary", {}) or {}
        return {
            **summary,
            "by_state": metrics.get("bulk_reservations_by_state", []),
            "active_by_user": metrics.get("bulk_active_by_user", []),
            "recent": metrics.get("bulk_recent", []),
            "problem_rows": metrics.get("bulk_problem_rows", []),
        }

    @staticmethod
    def _normalize_urgent(metrics: dict[str, Any]) -> dict[str, Any]:
        """Prepare only open urgent requests using type-specific completion rules."""

        rows = [row for row in metrics.get("urgent_applications", []) if _is_open_application(row)]
        oldest_created_at, oldest_age_seconds = _oldest_age(rows, "created_at")
        return {
            "open": len(rows),
            "no_editor": sum(1 for row in rows if not _has_editor(row)),
            "no_final_answer": len(rows),
            "oldest_created_at": oldest_created_at,
            "oldest_age_seconds": oldest_age_seconds,
            "rows": rows,
        }

    @staticmethod
    def _normalize_business(
        *,
        applications: dict[str, Any],
        bulk: dict[str, Any],
        urgent: dict[str, Any],
        queues: dict[str, Any],
    ) -> dict[str, Any]:
        """Build manager-facing business widgets from already collected metrics."""

        by_direction = applications.get("by_direction", []) or []
        total_by_direction = sum(int(row.get("count", 0) or 0) for row in by_direction)
        top_directions = sorted(
            (
                {
                    "direction": row.get("direction") or "Не указано",
                    "count": int(row.get("count", 0) or 0),
                    "share_percent": _percent(row.get("count", 0), total_by_direction),
                }
                for row in by_direction
            ),
            key=lambda item: item["count"],
            reverse=True,
        )[:5]

        notification = queues.get("notification_outbox", {})
        dashboard = queues.get("dashboard_outbox", {})
        risks: list[str] = []
        if int(urgent.get("open", 0) or 0) > 0:
            risks.append("Есть открытые срочные заявки")
        if int(applications.get("without_editor", 0) or 0) > 0:
            risks.append("Есть заявки без редактора")
        if int(applications.get("not_found_total", 0) or 0) > 0:
            risks.append("Есть заявки, которые polling не находит")
        if int(notification.get("failed", 0) or 0) > 0:
            risks.append("Есть недоставленные Telegram-уведомления")
        if int(dashboard.get("pending", 0) or 0) > 0:
            risks.append("Общий Google dashboard может отставать")

        focus = "Ничего не делать"
        if risks:
            focus = risks[0]
        if int(urgent.get("open", 0) or 0) > 0:
            focus = "Разобрать срочные заявки"
        elif int(applications.get("without_editor", 0) or 0) > 0:
            focus = "Назначить редакторов"
        elif int(notification.get("failed", 0) or 0) > 0:
            focus = "Проверить доставку уведомлений"

        return {
            "today": {
                "created": int(applications.get("created_today", 0) or 0),
                "urgent_created": int(applications.get("urgent_today", 0) or 0),
                "final_answers": int(applications.get("with_final_answer_today", 0) or 0),
                "bulk_registered": int(bulk.get("registered_today", 0) or 0),
            },
            "team_load": {
                "without_editor": int(applications.get("without_editor", 0) or 0),
                "urgent_without_editor": int(urgent.get("no_editor", 0) or 0),
                "urgent_without_final_answer": int(urgent.get("no_final_answer", 0) or 0),
                "active_bulk_reservations": int(bulk.get("active", 0) or 0),
            },
            "directions": {
                "total": total_by_direction,
                "top": top_directions,
            },
            "manager_focus": {
                "action": focus,
                "risks": risks,
                "is_clear": not risks,
            },
        }


def _normalize_status_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only real workflow statuses and aggregate migrated intent-like values."""

    grouped: dict[str, int] = {}
    invalid_count = 0
    for row in rows or []:
        status = str(row.get("status") or "").strip()
        count = int(row.get("count", 0) or 0)
        if status in VALID_MONITORING_STATUSES:
            grouped[status] = grouped.get(status, 0) + count
        elif count:
            invalid_count += count
    normalized = [{"status": status, "count": count} for status, count in grouped.items()]
    if invalid_count:
        normalized.append({"status": "Некорректные статусы/интенты", "count": invalid_count})
    return sorted(normalized, key=lambda item: item["count"], reverse=True)


def _pilot_period(
    *,
    applications: list[dict[str, Any]],
    events: list[dict[str, Any]],
    notification_errors: list[dict[str, Any]],
    days: int,
) -> dict[str, Any]:
    """Calculate one pilot period from already fetched metadata."""

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    period_apps = [row for row in applications if _row_time(row, "submitted_at", "created_at") >= since]
    period_events = [row for row in events if _row_time(row, "event_at") >= since]
    period_errors = [row for row in notification_errors if _row_time(row, "updated_at", "created_at") >= since]
    events_by_app: dict[str, list[dict[str, Any]]] = {}
    all_events_by_app: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        app_id = str(event.get("application_id") or "")
        if app_id:
            all_events_by_app.setdefault(app_id, []).append(event)
    for event in period_events:
        app_id = str(event.get("application_id") or "")
        if app_id:
            events_by_app.setdefault(app_id, []).append(event)

    creation_durations = _durations_between(events_by_app, {"draft_started"}, {"application_submitted"})
    first_editor_durations = _first_editor_durations(period_apps, events_by_app)
    completed_ids = _application_ids_with_event(
        period_events,
        {"final_answer_added", "status_final_answer_ready"},
    )
    completed_apps = [
        app for app in applications if str(app.get("application_id") or "") in completed_ids
    ]
    full_cycle_durations = _full_cycle_durations(
        completed_apps,
        all_events_by_app,
        final_since=since,
    )
    urgent_apps = [app for app in period_apps if _is_urgent(app)]
    regular_apps = [app for app in period_apps if not _is_urgent(app)]
    completed_urgent_apps = [app for app in completed_apps if _is_urgent(app)]
    completed_regular_apps = [app for app in completed_apps if not _is_urgent(app)]
    clarification_count = sum(1 for app in period_apps if _has_clarification(app, events_by_app.get(str(app.get("application_id") or ""), [])))
    not_found_count = sum(1 for app in period_apps if int(app.get("not_found_count", 0) or 0) > 0 or app.get("polling_state") != "ACTIVE")

    return {
        "days": days,
        "kpi": {
            "total_applications": len(period_apps),
            "active_users": len({app.get("telegram_user_id") for app in period_apps if app.get("telegram_user_id") is not None}),
            "creation_time_seconds": _duration_stats(creation_durations),
            "first_editor_action_seconds": _duration_stats(first_editor_durations),
            "full_cycle_seconds": _duration_stats(full_cycle_durations),
            "first_editor_action_seconds_by_urgency": {
                "urgent": _duration_stats(_first_editor_durations(urgent_apps, events_by_app)),
                "regular": _duration_stats(_first_editor_durations(regular_apps, events_by_app)),
            },
            "full_cycle_seconds_by_urgency": {
                "urgent": _duration_stats(
                    _full_cycle_durations(
                        completed_urgent_apps,
                        all_events_by_app,
                        final_since=since,
                    )
                ),
                "regular": _duration_stats(
                    _full_cycle_durations(
                        completed_regular_apps,
                        all_events_by_app,
                        final_since=since,
                    )
                ),
            },
            "clarification_share_percent": _percent(clarification_count, len(period_apps)),
            "clarification_count": clarification_count,
            "not_found_or_tracking_errors": not_found_count,
            "notification_errors": len(period_errors),
        },
        "daily": _pilot_daily(
            applications=period_apps,
            all_applications=applications,
            events_by_app=events_by_app,
            all_events_by_app=all_events_by_app,
            notification_errors=period_errors,
            since=since,
            days=days,
        ),
        "funnel": _pilot_funnel(period_apps, period_events),
        "problem_rows": _pilot_problem_rows(period_apps, events_by_app, period_errors),
    }


def _pilot_daily(
    *,
    applications: list[dict[str, Any]],
    all_applications: list[dict[str, Any]],
    events_by_app: dict[str, list[dict[str, Any]]],
    all_events_by_app: dict[str, list[dict[str, Any]]],
    notification_errors: list[dict[str, Any]],
    since: datetime,
    days: int,
) -> list[dict[str, Any]]:
    """Build compact daily series for the pilot page."""

    rows: list[dict[str, Any]] = []
    for offset in range(days):
        day = (since + timedelta(days=offset + 1)).date()
        day_apps = [app for app in applications if _row_time(app, "submitted_at", "created_at").date() == day]
        day_error_count = sum(1 for err in notification_errors if _row_time(err, "updated_at", "created_at").date() == day)
        app_ids = {str(app.get("application_id") or "") for app in day_apps}
        day_events_by_app = {app_id: events_by_app.get(app_id, []) for app_id in app_ids}
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        completed_app_ids = {
            app_id
            for app_id, app_events in all_events_by_app.items()
            if _first_event_at(app_events, {"final_answer_added", "status_final_answer_ready"}, since=day_start, before=day_end)
        }
        completed_apps = [
            app for app in all_applications if str(app.get("application_id") or "") in completed_app_ids
        ]
        rows.append(
            {
                "date": day.isoformat(),
                "applications": len(day_apps),
                "active_users": len({app.get("telegram_user_id") for app in day_apps if app.get("telegram_user_id") is not None}),
                "creation_time_median_seconds": _duration_stats(
                    _durations_between(day_events_by_app, {"draft_started"}, {"application_submitted"})
                )["median_seconds"],
                "first_editor_action_median_seconds": _duration_stats(_first_editor_durations(day_apps, day_events_by_app))["median_seconds"],
                "full_cycle_median_seconds": _duration_stats(
                    _full_cycle_durations(
                        completed_apps,
                        all_events_by_app,
                        final_since=day_start,
                        final_before=day_end,
                    )
                )["median_seconds"],
                "clarification_share_percent": _percent(
                    sum(1 for app in day_apps if _has_clarification(app, day_events_by_app.get(str(app.get("application_id") or ""), []))),
                    len(day_apps),
                ),
                "not_found_or_errors": day_error_count
                + sum(1 for app in day_apps if int(app.get("not_found_count", 0) or 0) > 0 or app.get("polling_state") != "ACTIVE"),
            }
        )
    return rows


def _pilot_stickiness(applications: list[dict[str, Any]]) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    msk = timezone(timedelta(hours=3))
    today_msk = now.astimezone(msk).date()
    dau_users: set[Any] = set()
    wau_users: set[Any] = set()
    mau_users: set[Any] = set()
    for app in applications:
        user_id = app.get("telegram_user_id")
        if user_id is None:
            continue
        submitted_at = _row_time(app, "submitted_at", "created_at")
        if submitted_at.astimezone(msk).date() == today_msk:
            dau_users.add(user_id)
        if submitted_at >= now - timedelta(days=7):
            wau_users.add(user_id)
        if submitted_at >= now - timedelta(days=30):
            mau_users.add(user_id)
    dau = len(dau_users)
    wau = len(wau_users)
    mau = len(mau_users)
    return {
        "dau": dau,
        "wau": wau,
        "mau": mau,
        "dau_wau_percent": _percent(dau, wau),
        "dau_mau_percent": _percent(dau, mau),
        "wau_mau_percent": _percent(wau, mau),
    }


def _pilot_funnel(applications: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return exact event-based funnel stages and transitions.

    The funnel deliberately contains only business events that the bot writes to
    ``application_events``. Current row fields and indexing metadata are not
    used as substitutes because they do not contain the time of the transition.
    """

    event_types = {str(event.get("event_type") or "") for event in events}
    events_by_app: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        app_id = str(event.get("application_id") or "")
        if app_id:
            events_by_app.setdefault(app_id, []).append(event)
    editor_assigned_ids = _application_ids_with_event(events, {"editor_changed"})
    status_changed_ids = _application_ids_with_event(events, {"status_changed"})
    editor_comment_ids = _application_ids_with_event(events, {"editor_comment_added", "clarification_requested"})
    # The bot does not emit user_comment_added. scriptwriter_response_added is
    # the separate, observable event for a response prepared by a scriptwriter.
    scriptwriter_response_ids = _application_ids_with_event(events, {"scriptwriter_response_added"})
    final_answer_ids = _application_ids_with_event(events, {"final_answer_added", "status_final_answer_ready"})
    deletion_ids = _application_ids_with_event(events, {"application_deleted"})
    stages = [
        ("draft_started", "Черновик начат", len(_application_ids_with_event(events, {"draft_started"})), {"draft_started"}),
        ("submitted", "Заявка отправлена", len(_application_ids_with_event(events, {"application_submitted"})), {"application_submitted"}),
        ("editor_assigned", "Редактор назначен", len(editor_assigned_ids), {"editor_changed"}),
        ("status_changed", "Статус изменен", len(status_changed_ids), {"status_changed"}),
        ("editor_comment", "Комментарий редактора", len(editor_comment_ids), {"editor_comment_added", "clarification_requested"}),
        ("scriptwriter_response", "Ответ сценариста", len(scriptwriter_response_ids), {"scriptwriter_response_added"}),
        ("final_answer", "Итоговый ответ", len(final_answer_ids), {"final_answer_added", "status_final_answer_ready"}),
        ("deletion", "Удаление", len(deletion_ids), {"application_deleted"}),
    ]
    result = []
    previous: int | None = None
    previous_event_types: set[str] | None = None
    for key, label, count, current_event_types in stages:
        conversion = None if previous in (None, 0) else round(count / previous * 100)
        source = "events" if current_event_types & event_types else "missing_events"
        average_transition, transition_sample = _average_transition(
            events_by_app,
            previous_event_types,
            current_event_types,
        )
        result.append(
            {
                "key": key,
                "label": label,
                "count": count,
                "conversion_percent": conversion,
                "source": source,
                "average_transition_seconds": average_transition,
                "transition_sample_size": transition_sample,
            }
        )
        previous = count
        previous_event_types = current_event_types
    return result


def _application_ids_with_event(events: list[dict[str, Any]], event_types: set[str]) -> set[str]:
    return {
        str(event.get("application_id"))
        for event in events
        if event.get("application_id") and event.get("event_type") in event_types
    }


def _application_ids_where(applications: list[dict[str, Any]], predicate: Any) -> set[str]:
    return {
        str(app.get("application_id"))
        for app in applications
        if app.get("application_id") and predicate(app)
    }


def _pilot_problem_rows(
    applications: list[dict[str, Any]],
    events_by_app: dict[str, list[dict[str, Any]]],
    notification_errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Select manager-facing problematic applications without sensitive text."""

    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for app in applications:
        app_id = str(app.get("application_id") or "")
        submitted_at = _row_time(app, "submitted_at", "created_at")
        app_events = events_by_app.get(app_id, [])
        reasons: list[str] = []
        if not _has_first_editor_action(app, app_events) and now - submitted_at > timedelta(hours=24):
            reasons.append("нет первого действия редактора >24ч")
        if _is_open_application(app) and now - submitted_at > timedelta(hours=72):
            reasons.append("нет итогового ответа >72ч")
        if int(app.get("not_found_count", 0) or 0) > 0 or app.get("polling_state") != "ACTIVE":
            reasons.append("tracking/not_found")
        if _clarification_repeats(app_events) > 1:
            reasons.append("повторные пояснения")
        if reasons:
            rows.append(_pilot_problem_row(app, reasons, submitted_at))
    for error in notification_errors[:20]:
        rows.append(
            {
                "application_id": "-",
                "telegram_user_id": error.get("telegram_user_id"),
                "problem": "ошибка уведомления",
                "status": error.get("state") or "-",
                "direction": "-",
                "sheet_name": "-",
                "row_number": None,
                "age_seconds": _age_seconds(_row_time(error, "updated_at", "created_at")),
                "updated_at": error.get("updated_at") or error.get("created_at"),
            }
        )
    event_rows = [event for app_events in events_by_app.values() for event in app_events]
    for event in event_rows:
        if event.get("event_type") != "application_deletion_error":
            continue
        rows.append(
            {
                "application_id": event.get("application_id") or "-",
                "telegram_user_id": event.get("telegram_user_id"),
                "problem": "ошибка удаления",
                "status": "-",
                "direction": "-",
                "sheet_name": "-",
                "row_number": None,
                "age_seconds": _age_seconds(_row_time(event, "event_at")),
                "updated_at": event.get("event_at"),
            }
        )
    return sorted(rows, key=lambda item: int(item.get("age_seconds") or 0), reverse=True)[:100]


def _pilot_problem_row(app: dict[str, Any], reasons: list[str], submitted_at: datetime) -> dict[str, Any]:
    return {
        "application_id": app.get("application_id"),
        "telegram_user_id": app.get("telegram_user_id"),
        "problem": "; ".join(reasons),
        "status": app.get("last_known_status") or "-",
        "direction": app.get("direction") or "-",
        "sheet_name": app.get("sheet_name") or "-",
        "row_number": app.get("last_seen_row_number"),
        "age_seconds": _age_seconds(submitted_at),
        "updated_at": app.get("updated_at"),
    }


def _first_editor_durations(applications: list[dict[str, Any]], events_by_app: dict[str, list[dict[str, Any]]]) -> list[int]:
    durations: list[int] = []
    for app in applications:
        events = events_by_app.get(str(app.get("application_id") or ""), [])
        submitted_at = _first_event_at(events, {"application_submitted"})
        editor_at = _first_event_at(events, {"editor_changed", "status_changed", "editor_comment_added", "final_answer_added", "status_final_answer_ready"})
        if submitted_at and editor_at and editor_at >= submitted_at:
            durations.append(int((editor_at - submitted_at).total_seconds()))
    return durations


def _full_cycle_durations(
    applications: list[dict[str, Any]],
    events_by_app: dict[str, list[dict[str, Any]]],
    *,
    final_since: datetime | None = None,
    final_before: datetime | None = None,
) -> list[int]:
    """Measure cycles completed in a period from exact submission and final events."""

    durations: list[int] = []
    for app in applications:
        events = events_by_app.get(str(app.get("application_id") or ""), [])
        submitted_at = _first_event_at(events, {"application_submitted"})
        final_at = _first_event_at(
            events,
            {"final_answer_added", "status_final_answer_ready"},
            since=final_since,
            before=final_before,
        )
        if submitted_at and final_at and final_at >= submitted_at:
            durations.append(int((final_at - submitted_at).total_seconds()))
    return durations


def _durations_between(events_by_app: dict[str, list[dict[str, Any]]], start_types: set[str], end_types: set[str]) -> list[int]:
    durations: list[int] = []
    for events in events_by_app.values():
        start = _first_event_at(events, start_types)
        end = _first_event_at(events, end_types)
        if start and end and end >= start:
            durations.append(int((end - start).total_seconds()))
    return durations


def _average_transition(
    events_by_app: dict[str, list[dict[str, Any]]],
    previous_types: set[str] | None,
    current_types: set[str],
) -> tuple[int | None, int]:
    if not previous_types:
        return None, 0
    durations = _durations_between(events_by_app, previous_types, current_types)
    if not durations:
        return None, 0
    return round(sum(durations) / len(durations)), len(durations)


def _duration_stats(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"average_seconds": None, "median_seconds": None, "sample_size": 0}
    return {
        "average_seconds": round(sum(values) / len(values)),
        "median_seconds": round(median(values)),
        "sample_size": len(values),
    }


def _first_event_at(
    events: list[dict[str, Any]],
    event_types: set[str],
    *,
    since: datetime | None = None,
    before: datetime | None = None,
) -> datetime | None:
    """Return the earliest exact event timestamp inside an optional half-open window."""

    moments = [_parse_datetime(event.get("event_at")) for event in events if event.get("event_type") in event_types]
    moments = [moment for moment in moments if moment is not None]
    if since is not None:
        moments = [moment for moment in moments if moment >= since]
    if before is not None:
        moments = [moment for moment in moments if moment < before]
    return min(moments) if moments else None


def _has_final_answer(app: dict[str, Any]) -> bool:
    """Return whether an application is complete by its own business model."""

    if _is_chips(app):
        return str(app.get("last_known_status") or "").strip() in CHIPS_SUCCESS_STATUSES
    return bool(
        app.get("has_final_answer")
        or app.get("last_known_status") == FINAL_ANSWER_READY_STATUS
    )


def _is_chips(app: dict[str, Any]) -> bool:
    """Normalize the historical CHIP spelling without enabling legacy bulk metrics."""

    return str(app.get("change_type") or "").strip().upper() in {"CHIPS", "CHIP"}


def _is_open_application(app: dict[str, Any]) -> bool:
    """Apply one type-specific open/closed rule to manager-facing calculations."""

    status = str(app.get("last_known_status") or "").strip()
    if status in CLOSED_WITHOUT_RESULT_STATUSES:
        return False
    return not _has_final_answer(app)


def _has_editor(app: dict[str, Any]) -> bool:
    editor = str(app.get("last_seen_editor") or "").strip()
    return bool(editor and editor != EDITOR_NOT_SELECTED)


def _is_urgent(app: dict[str, Any]) -> bool:
    return str(app.get("is_urgent") or "").lower() in {"1", "true", "yes"}


def _has_first_editor_action(app: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    return bool(
        _has_editor(app)
        or app.get("has_editor_comment")
        or _has_final_answer(app)
        or str(app.get("last_known_status") or "") not in {"", "Новая"}
        or _first_event_at(events, {"editor_changed", "status_changed", "editor_comment_added", "final_answer_added"})
    )


def _has_clarification(app: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    return bool(
        app.get("has_editor_comment")
        or app.get("last_known_status") == "Нужны пояснения"
        or _first_event_at(events, {"editor_comment_added", "clarification_requested"})
    )


def _clarification_repeats(events: list[dict[str, Any]]) -> int:
    return sum(1 for event in events if event.get("event_type") in {"editor_comment_added", "clarification_requested"})


def _row_time(row: dict[str, Any], *fields: str) -> datetime:
    for field in fields:
        parsed = _parse_datetime(row.get(field))
        if parsed is not None:
            return parsed
    return datetime.fromtimestamp(0, timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(moment: datetime) -> int:
    return max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))


def _due(value: str | None, interval_seconds: int, now: datetime) -> bool:
    """Return True when a section should be collected again."""

    if not value:
        return True
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed.astimezone(timezone.utc)).total_seconds() >= interval_seconds


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Convert GROUP BY rows into a compact count mapping."""

    result: dict[str, int] = {}
    for row in rows:
        key = str(row.get("state") or row.get("registration_state") or "")
        if key:
            result[key] = int(row.get("count", 0) or 0)
    return result


def _old_state_count(rows: list[dict[str, Any]], state: str) -> int:
    """Approximate stale count from grouped rows until UI gets detailed aging."""

    for row in rows:
        if row.get("state") == state:
            return int(row.get("count", 0) or 0)
    return 0


def _parse_load(uptime: str) -> dict[str, float]:
    match = re.search(r"load average[s]?:\s*([0-9.,]+),\s*([0-9.,]+),\s*([0-9.,]+)", uptime)
    if not match:
        return {}
    return {
        "1m": float(match.group(1).replace(",", ".")),
        "5m": float(match.group(2).replace(",", ".")),
        "15m": float(match.group(3).replace(",", ".")),
    }


def _parse_free(text: str) -> dict[str, int]:
    rows = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].rstrip(":") in {"Mem", "Swap"}:
            rows[parts[0].rstrip(":").lower()] = {
                "total_mb": _int_or_none(parts[1]),
                "used_mb": _int_or_none(parts[2]),
                "free_mb": _int_or_none(parts[3]),
            }
    return rows


def _parse_df_root(text: str) -> dict[str, Any]:
    lines = [line.split() for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or len(lines[1]) < 5:
        return {}
    row = lines[1]
    return {
        "filesystem": row[0],
        "size": row[1],
        "used": row[2],
        "available": row[3],
        "used_percent": _int_or_none(row[4].rstrip("%")),
        "free_gb": _int_or_none(row[3].rstrip("G")),
        "mount": row[5] if len(row) > 5 else "/",
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _oldest_age(rows: list[dict[str, Any]], field: str) -> tuple[str | None, int | None]:
    """Return oldest timestamp and its age in seconds for manager SLA widgets."""

    oldest: datetime | None = None
    oldest_raw: str | None = None
    for row in rows:
        raw = row.get(field)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        if oldest is None or parsed < oldest:
            oldest = parsed
            oldest_raw = str(raw)
    if oldest is None:
        return None, None
    age_seconds = int((datetime.now(timezone.utc) - oldest).total_seconds())
    return oldest_raw, max(0, age_seconds)


def _percent(value: Any, total: int) -> int:
    try:
        numeric = int(value or 0)
    except (TypeError, ValueError):
        numeric = 0
    if total <= 0:
        return 0
    return round(numeric / total * 100)
