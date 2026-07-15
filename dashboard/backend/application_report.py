from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import shlex
import textwrap
from typing import Any, Protocol

from .config import AppConfig, RemoteConfig
from .schemas import ApplicationReport, ApplicationReportResponse
from .ssh_client import ParamikoSshClient
from .storage import JsonStorage


FINAL_ANSWER_READY_STATUS = "\u0418\u0442\u043e\u0433\u043e\u0432\u044b\u0439 \u043e\u0442\u0432\u0435\u0442 \u0433\u043e\u0442\u043e\u0432"
CHIPS_SUCCESS_STATUSES = {"\u041f\u0440\u0438\u043d\u044f\u0442\u0430", "\u041f\u0440\u0438\u043d\u044f\u0442\u043e"}


class ReportCommandRunner(Protocol):
    """Protocol used by tests to replace the real Paramiko SSH client."""

    def run(self, command: str) -> str:
        ...


class ApplicationReportCollector:
    """Runs a manual read-only report for problematic business applications."""

    def __init__(
        self,
        *,
        config: AppConfig,
        storage: JsonStorage,
        runner: ReportCommandRunner | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.runner = runner or ParamikoSshClient(config.ssh)
        self._lock = asyncio.Lock()

    async def collect(self) -> ApplicationReportResponse:
        """Collect one report unless another manual report is still running."""

        if self._lock.locked():
            return ApplicationReportResponse(
                skipped=True,
                reason="application report collection already running",
            )
        async with self._lock:
            try:
                command = build_application_report_command(self.config.remote)
                output = await asyncio.to_thread(self.runner.run, command)
                raw = json.loads(output)
                report = normalize_application_report(raw, source_host=self.config.ssh.host)
                self.storage.save_application_report(report)
                return ApplicationReportResponse(report=report)
            except Exception as exc:
                report = failed_application_report(exc, source_host=self.config.ssh.host)
                self.storage.save_application_report(report, update_latest=False)
                return ApplicationReportResponse(report=report)


def build_application_report_command(remote: RemoteConfig) -> str:
    """Build one bounded SSH command that reads SQLite in query-only mode."""

    env = (
        f"COMPOSE_FILE={shlex.quote(remote.compose_file)} "
        f"SERVICE={shlex.quote(remote.service)} "
    )
    script = _REMOTE_SCRIPT_TEMPLATE.replace("__CONTAINER_SCRIPT__", repr(_CONTAINER_SCRIPT))
    return (
        f"cd {shlex.quote(remote.project_dir)} && "
        f"{env}python3 - <<'PY_DASHBOARD_APPLICATION_REPORT'\n"
        f"{script}\n"
        "PY_DASHBOARD_APPLICATION_REPORT"
    )


def normalize_application_report(raw: dict[str, Any], *, source_host: str) -> ApplicationReport:
    """Normalize remote rows and add UI-ready labels without exposing request text."""

    payload = raw.get("container_payload", {}) if isinstance(raw, dict) else {}
    report = payload.get("report", {}) if isinstance(payload, dict) else {}
    errors = [*raw.get("errors", []), *payload.get("errors", [])]
    lost = [_normalize_application_row(row) for row in report.get("lost", [])]
    urgent = [_normalize_application_row(row) for row in report.get("urgent_without_final_answer", [])]
    without_owner = [_normalize_application_row(row) for row in report.get("without_owner", [])]
    clarification = [_normalize_application_row(row) for row in report.get("needs_clarification", [])]
    stale = [_normalize_application_row(row) for row in report.get("stale_without_movement", [])]

    normalized = {
        "collected_at": raw.get("collected_at") or datetime.now(timezone.utc).isoformat(),
        "source_host": source_host,
        "collection_status": "failed" if errors else "ok",
        "collection_errors": errors,
        "summary": {
            "lost": len(lost),
            "urgent_without_final_answer": len(urgent),
            "without_owner": len(without_owner),
            "needs_clarification": len(clarification),
            "stale_without_movement": len(stale),
            "problematic_bulk_reservations": len(report.get("problematic_bulk_reservations", [])),
            "unfinished_workflows": len(report.get("unfinished_workflows", [])),
        },
        "lost": lost,
        "urgent_without_final_answer": urgent,
        "without_owner": without_owner,
        "needs_clarification": clarification,
        "stale_without_movement": stale,
        "problematic_bulk_reservations": [
            _normalize_bulk_row(row) for row in report.get("problematic_bulk_reservations", [])
        ],
        "unfinished_workflows": report.get("unfinished_workflows", []),
    }
    return ApplicationReport.model_validate(normalized)


def failed_application_report(exc: Exception, *, source_host: str) -> ApplicationReport:
    """Create a failed report for history while preserving previous latest."""

    return ApplicationReport(
        collected_at=datetime.now(timezone.utc).isoformat(),
        source_host=source_host,
        collection_status="failed",
        collection_errors=[f"{type(exc).__name__}: {exc}"],
    )


def _normalize_application_row(row: dict[str, Any]) -> dict[str, Any]:
    """Attach manager-friendly problem/type labels and a safe Google row link."""

    normalized = dict(row)
    normalized["problem"] = _application_problem(row)
    normalized["type_label"] = _application_type(row)
    normalized["has_final_answer"] = _has_result(row)
    normalized["row_link"] = _row_link(row)
    normalized["problem_age_seconds"] = _age_from(row.get("last_not_found_at") or row.get("updated_at"))
    return normalized


def _normalize_bulk_row(row: dict[str, Any]) -> dict[str, Any]:
    """Attach a safe row link for problematic bulk reservations."""

    normalized = dict(row)
    normalized["row_link"] = _row_link(row, row_number_key="start_row")
    normalized["problem_age_seconds"] = _age_from(row.get("last_location_search_at") or row.get("updated_at"))
    return normalized


def _application_problem(row: dict[str, Any]) -> str:
    polling_state = str(row.get("polling_state") or "")
    not_found_count = int(row.get("not_found_count") or 0)
    if polling_state != "ACTIVE":
        return "не найдена polling"
    if not_found_count > 1:
        return "повторные not_found"
    if not_found_count > 0:
        return "ожидает перепроверку"
    return "требует внимания"


def _application_type(row: dict[str, Any]) -> str:
    urgency = "срочная" if row.get("is_urgent") else "обычная"
    application_type = row.get("application_type") or "тип не указан"
    change_type = row.get("change_type") or ""
    parts = [urgency, str(application_type)]
    if change_type:
        parts.append(str(change_type))
    return " / ".join(parts)


def _has_result(row: dict[str, Any]) -> bool:
    """Use the same type-specific completion rule as the monitoring snapshot."""

    is_chips = str(row.get("change_type") or "").strip().upper() in {"CHIPS", "CHIP"}
    if is_chips:
        return str(row.get("last_known_status") or "").strip() in CHIPS_SUCCESS_STATUSES
    return bool(
        row.get("has_final_answer")
        or row.get("last_known_status") == FINAL_ANSWER_READY_STATUS
    )


def _row_link(row: dict[str, Any], *, row_number_key: str = "last_seen_row_number") -> str | None:
    spreadsheet_id = row.get("spreadsheet_id")
    sheet_id = row.get("sheet_id")
    row_number = row.get(row_number_key)
    if not spreadsheet_id or sheet_id is None or not row_number:
        return None
    return (
        "https://docs.google.com/spreadsheets/d/"
        f"{spreadsheet_id}/edit#gid={sheet_id}&range=A{row_number}"
    )


def _age_from(value: Any) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
    return max(0, seconds)


_REMOTE_SCRIPT_TEMPLATE = textwrap.dedent(
    r'''
    import json
    import os
    import subprocess
    from datetime import datetime, timezone

    COMPOSE_FILE = os.environ.get("COMPOSE_FILE", "docker-compose.prod.yml")
    SERVICE = os.environ.get("SERVICE", "bot")
    CONTAINER_SCRIPT = __CONTAINER_SCRIPT__


    def run(args, timeout=15):
        # Keep the report command bounded; it is started only by a manual button.
        try:
            completed = subprocess.run(
                args,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except Exception as exc:
            return {"ok": False, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}
        return {"ok": completed.returncode == 0, "stdout": completed.stdout, "stderr": completed.stderr}


    def compose_args(*parts):
        return ["docker", "compose", "-f", COMPOSE_FILE, *parts]


    result = run(compose_args("exec", "-T", SERVICE, "python", "-c", CONTAINER_SCRIPT), timeout=20)
    errors = []
    payload = {}
    if result["ok"]:
        try:
            payload = json.loads(result["stdout"])
        except json.JSONDecodeError as exc:
            errors.append(f"invalid report json: {exc}")
    else:
        errors.append(result["stderr"][:500] or "container application report failed")

    print(json.dumps({
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "errors": errors,
        "container_payload": payload,
    }, ensure_ascii=False))
    '''
).strip()


_CONTAINER_SCRIPT = textwrap.dedent(
    r'''
    import json
    import sqlite3


    LIMIT = 100


    def fetch_all(connection, sql, params=()):
        cursor = connection.execute(sql, params)
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


    # Completion differs by type: CHIPS finishes with an accepted status,
    # whereas ADD/EDIT finishes with an editor final answer.
    CHIPS_SQL = "UPPER(TRIM(COALESCE(change_type, ''))) IN ('CHIPS', 'CHIP')"
    CLOSED_STATUS_SQL = "COALESCE(last_known_status, '') IN ('Итоговый ответ готов', 'Принята', 'Принято', 'Отклонена', 'Отложена', 'Удаление')"
    CHIPS_SUCCESS_SQL = "COALESCE(last_known_status, '') IN ('Принята', 'Принято')"
    OPEN_APPLICATION_SQL = f"""
        CASE
          WHEN {CHIPS_SQL} THEN NOT (
            {CHIPS_SUCCESS_SQL}
            OR COALESCE(last_known_status, '') IN ('Отклонена', 'Отложена', 'Удаление')
          )
          ELSE NOT (
            COALESCE(last_seen_final_answer, '') != ''
            OR {CLOSED_STATUS_SQL}
          )
        END
    """


    APPLICATION_COLUMNS = """
        application_id, telegram_user_id, spreadsheet_id, sheet_id, sheet_name,
        last_seen_row_number, last_known_status, direction, answer_type,
        application_type, change_type, is_urgent, batch_id, last_seen_editor,
        CASE
          WHEN COALESCE(last_seen_final_answer, '') != ''
               OR last_known_status = 'Итоговый ответ готов'
          THEN 1 ELSE 0
        END AS has_final_answer,
        submitted_at, polling_state, not_found_count, last_not_found_at,
        next_status_check_at, created_at, updated_at
    """


    def main():
        # This code runs inside the bot container and opens production SQLite read-only.
        result = {"report": {}, "errors": []}
        try:
            db = sqlite3.connect("file:/data/app.db?mode=ro", uri=True, timeout=5)
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=5000")

            report = {}
            report["lost"] = fetch_all(db, f"""
                SELECT {APPLICATION_COLUMNS}
                FROM submitted_applications
                WHERE polling_state != 'ACTIVE' OR not_found_count > 0
                ORDER BY not_found_count DESC, updated_at DESC
                LIMIT ?
            """, (LIMIT,))
            report["urgent_without_final_answer"] = fetch_all(db, f"""
                SELECT {APPLICATION_COLUMNS}
                FROM submitted_applications
                WHERE is_urgent = 1
                  AND ({OPEN_APPLICATION_SQL})
                ORDER BY COALESCE(submitted_at, created_at) ASC
                LIMIT ?
            """, (LIMIT,))
            report["without_owner"] = fetch_all(db, f"""
                SELECT {APPLICATION_COLUMNS}
                FROM submitted_applications
                WHERE COALESCE(last_seen_editor, '') IN ('', 'Редактор не выбран')
                  AND ({OPEN_APPLICATION_SQL})
                ORDER BY updated_at ASC
                LIMIT ?
            """, (LIMIT,))
            report["needs_clarification"] = fetch_all(db, f"""
                SELECT {APPLICATION_COLUMNS}
                FROM submitted_applications
                WHERE last_known_status = 'Нужны пояснения'
                ORDER BY updated_at DESC
                LIMIT ?
            """, (LIMIT,))
            report["stale_without_movement"] = fetch_all(db, f"""
                SELECT {APPLICATION_COLUMNS}
                FROM submitted_applications
                WHERE ({OPEN_APPLICATION_SQL})
                  AND datetime(updated_at) <= datetime('now', '-24 hours')
                ORDER BY updated_at ASC
                LIMIT ?
            """, (LIMIT,))
            report["problematic_bulk_reservations"] = fetch_all(db, """
                SELECT reservation_id, telegram_user_id, spreadsheet_id, sheet_id, sheet_name,
                       start_row, end_row, direction, target_kind, change_type, requested_count,
                       state, registered_count, substr(COALESCE(last_error, ''), 1, 300) AS last_error,
                       created_at, updated_at, registered_at
                FROM bulk_reservations
                WHERE state = 'FAILED'
                   OR (state NOT IN ('REGISTERED', 'CANCELLED', 'FAILED')
                       AND datetime(updated_at) <= datetime('now', '-24 hours'))
                ORDER BY updated_at DESC
                LIMIT ?
            """, (LIMIT,))
            report["unfinished_workflows"] = fetch_all(db, """
                SELECT d.telegram_user_id, d.current_step, d.submission_state,
                       d.application_id, d.direction, d.answer_type, d.is_urgent,
                       d.application_type, d.change_type, d.submission_started_at,
                       d.created_at, d.updated_at,
                       u.pending_action, u.active_chat_id, u.active_message_id
                FROM drafts d
                LEFT JOIN user_settings u ON u.telegram_user_id = d.telegram_user_id
                WHERE d.current_step != 'completed'
                   OR d.submission_state IN ('DRAFT', 'PENDING', 'FAILED')
                   OR u.pending_action IS NOT NULL
                   OR u.active_message_id IS NOT NULL
                ORDER BY d.updated_at DESC
                LIMIT ?
            """, (LIMIT,))

            result["report"] = report
            db.close()
        except Exception as exc:
            result["errors"].append(f"{type(exc).__name__}: {exc}")
        print(json.dumps(result, ensure_ascii=False))


    main()
    '''
).strip()
