from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import re
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
            "by_status": metrics.get("applications_by_status", []),
            "by_direction": metrics.get("applications_by_direction", []),
            "problem_rows": metrics.get("applications_problem_rows", []),
            "not_found_total": summary.get("not_found_total", 0) or 0,
        }

    @staticmethod
    def _normalize_bulk(metrics: dict[str, Any]) -> dict[str, Any]:
        """Prepare bulk workflow aggregates and stale hints."""

        summary = metrics.get("bulk_summary", {}) or {}
        creation_counts = _counts(metrics.get("bulk_creation_by_state", []))
        return {
            **summary,
            "by_registration_state": metrics.get("bulk_by_registration_state", []),
            "creation_by_state": metrics.get("bulk_creation_by_state", []),
            "unfinished_by_user": metrics.get("bulk_unfinished_by_user", []),
            "recent": metrics.get("bulk_recent", []),
            "creation_problem_rows": metrics.get("bulk_creation_problem_rows", []),
            "stale_creating": creation_counts.get("BULK_CREATING", 0),
            "stale_registering": summary.get("registering", 0) or 0,
        }

    @staticmethod
    def _normalize_urgent(metrics: dict[str, Any]) -> dict[str, Any]:
        """Prepare urgent request rows and high-level counts."""

        rows = metrics.get("urgent_applications", [])
        oldest_created_at, oldest_age_seconds = _oldest_age(rows, "created_at")
        return {
            "open": len(rows),
            "no_editor": sum(1 for row in rows if not row.get("last_seen_editor")),
            "no_final_answer": sum(1 for row in rows if not row.get("has_final_answer")),
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
                "final_answers": int(applications.get("with_final_answer", 0) or 0),
                "bulk_registered": int(bulk.get("registered", 0) or 0),
            },
            "team_load": {
                "without_editor": int(applications.get("without_editor", 0) or 0),
                "urgent_without_editor": int(urgent.get("no_editor", 0) or 0),
                "urgent_without_final_answer": int(urgent.get("no_final_answer", 0) or 0),
                "active_bulk_batches": int(bulk.get("unfinished", 0) or 0),
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
