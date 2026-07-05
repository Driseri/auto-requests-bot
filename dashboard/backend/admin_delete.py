from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import re
import shlex
import textwrap
from typing import Literal, Protocol

from .config import AppConfig, RemoteConfig
from .schemas import AdminDeleteResponse
from .ssh_client import ParamikoSshClient
from .storage import JsonStorage


class AdminCommandRunner(Protocol):
    """Protocol used by tests to replace the real Paramiko SSH client."""

    def run(self, command: str) -> str:
        ...


class AdminDeleteService:
    """Coordinates rare destructive SQLite cleanup through one SSH command."""

    def __init__(
        self,
        *,
        config: AppConfig,
        storage: JsonStorage,
        runner: AdminCommandRunner | None = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.runner = runner or ParamikoSshClient(config.ssh)
        self._execute_lock = asyncio.Lock()

    async def preview(self, application_ids: list[str]) -> AdminDeleteResponse:
        """Run a read-only preview for the requested application IDs."""

        normalized_ids = normalize_application_ids(
            application_ids,
            max_ids=self.config.admin.max_delete_ids,
        )
        command = build_admin_delete_command(
            self.config.remote,
            mode="preview",
            application_ids=normalized_ids,
        )
        try:
            output = await asyncio.to_thread(self.runner.run, command)
            result = json.loads(output)
        except Exception as exc:
            result = {
                "status": "failed",
                "mode": "preview",
                "application_ids": normalized_ids,
                "errors": [f"{type(exc).__name__}: {exc}"],
            }
        return AdminDeleteResponse(result=result)

    async def execute(
        self,
        *,
        application_ids: list[str],
        confirmation: str,
    ) -> AdminDeleteResponse:
        """Execute confirmed deletion and append a local audit row."""

        if not self.config.admin.destructive_actions_enabled:
            return AdminDeleteResponse(skipped=True, reason="destructive actions are disabled")
        if self._execute_lock.locked():
            return AdminDeleteResponse(skipped=True, reason="delete operation already running")

        normalized_ids = normalize_application_ids(
            application_ids,
            max_ids=self.config.admin.max_delete_ids,
        )
        expected_confirmation = confirmation_phrase(normalized_ids)
        if confirmation.strip() != expected_confirmation:
            return AdminDeleteResponse(
                skipped=True,
                reason=f"confirmation must equal: {expected_confirmation}",
            )

        async with self._execute_lock:
            audit_started_at = datetime.now(timezone.utc).isoformat()
            command = build_admin_delete_command(
                self.config.remote,
                mode="execute",
                application_ids=normalized_ids,
                confirmation=confirmation,
            )
            try:
                output = await asyncio.to_thread(self.runner.run, command)
                result = json.loads(output)
                audit_id = self.storage.append_admin_delete_audit(
                    {
                        "action": "admin_delete_execute",
                        "started_at": audit_started_at,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "source_host": self.config.ssh.host,
                        "target_application_ids": normalized_ids,
                        "result": result,
                        "errors": result.get("errors", []),
                    }
                )
                return AdminDeleteResponse(audit_id=audit_id, result=result)
            except Exception as exc:
                audit_id = self.storage.append_admin_delete_audit(
                    {
                        "action": "admin_delete_execute",
                        "started_at": audit_started_at,
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "source_host": self.config.ssh.host,
                        "target_application_ids": normalized_ids,
                        "result": {},
                        "errors": [f"{type(exc).__name__}: {exc}"],
                    }
                )
                return AdminDeleteResponse(audit_id=audit_id, result={"errors": [f"{type(exc).__name__}: {exc}"]})


def normalize_application_ids(application_ids: list[str], *, max_ids: int) -> list[str]:
    """Normalize and validate admin-provided application IDs."""

    seen: set[str] = set()
    normalized: list[str] = []
    for raw in application_ids:
        item = str(raw or "").strip().upper()
        if not item or item in seen:
            continue
        if not re.fullmatch(r"[A-Z0-9_-]{3,64}", item):
            raise ValueError(f"invalid application id: {raw}")
        seen.add(item)
        normalized.append(item)
    if not normalized:
        raise ValueError("at least one application id is required")
    if len(normalized) > max_ids:
        raise ValueError(f"at most {max_ids} application ids can be deleted at once")
    return normalized


def confirmation_phrase(application_ids: list[str]) -> str:
    """Build the exact phrase required before destructive deletion."""

    return "DELETE " + " ".join(application_ids)


def build_admin_delete_command(
    remote: RemoteConfig,
    *,
    mode: Literal["preview", "execute"],
    application_ids: list[str],
    confirmation: str = "",
) -> str:
    """Build one bounded SSH command for preview or confirmed deletion."""

    env = (
        f"COMPOSE_FILE={shlex.quote(remote.compose_file)} "
        f"SERVICE={shlex.quote(remote.service)} "
    )
    script = _REMOTE_SCRIPT_TEMPLATE.replace("__CONTAINER_SCRIPT__", repr(_CONTAINER_SCRIPT))
    script = script.replace("__MODE__", repr(mode))
    script = script.replace("__APPLICATION_IDS__", repr(application_ids))
    script = script.replace("__CONFIRMATION__", repr(confirmation))
    return (
        f"cd {shlex.quote(remote.project_dir)} && "
        f"{env}python3 - <<'PY_DASHBOARD_ADMIN_DELETE'\n"
        f"{script}\n"
        "PY_DASHBOARD_ADMIN_DELETE"
    )


_REMOTE_SCRIPT_TEMPLATE = textwrap.dedent(
    r'''
    import json
    import os
    import subprocess
    from datetime import datetime, timezone

    COMPOSE_FILE = os.environ.get("COMPOSE_FILE", "docker-compose.prod.yml")
    SERVICE = os.environ.get("SERVICE", "bot")
    MODE = __MODE__
    APPLICATION_IDS = __APPLICATION_IDS__
    CONFIRMATION = __CONFIRMATION__
    CONTAINER_SCRIPT = __CONTAINER_SCRIPT__


    def compose_args(*parts):
        return ["docker", "compose", "-f", COMPOSE_FILE, *parts]


    env_args = [
        "DASHBOARD_DELETE_MODE=" + MODE,
        "DASHBOARD_APPLICATION_IDS=" + json.dumps(APPLICATION_IDS),
        "DASHBOARD_CONFIRMATION=" + CONFIRMATION,
    ]
    completed = subprocess.run(
        compose_args("exec", "-T", SERVICE, "env", *env_args, "python", "-c", CONTAINER_SCRIPT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        print(json.dumps({
            "status": "failed",
            "mode": MODE,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "errors": [completed.stderr[:1000] or completed.stdout[:1000] or "admin delete command failed"],
        }, ensure_ascii=False))
    else:
        print(completed.stdout)
    '''
).strip()


_CONTAINER_SCRIPT = textwrap.dedent(
    r'''
    import json
    import os
    import sqlite3
    from datetime import datetime, timezone


    SAFE_APPLICATION_COLUMNS = """
        application_id, telegram_user_id, spreadsheet_id, sheet_id, sheet_name,
        last_seen_row_number, last_known_status, direction, answer_type,
        application_type, change_type, is_urgent, batch_id, last_seen_editor,
        submitted_at, polling_state, not_found_count, last_not_found_at,
        next_status_check_at, created_at, updated_at
    """


    def placeholders(values):
        return ",".join("?" for _ in values)


    def fetch_all(connection, sql, params=()):
        cursor = connection.execute(sql, params)
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


    def delete_in(connection, sql_template, values):
        if not values:
            return 0
        return connection.execute(sql_template.format(placeholders(values)), values).rowcount


    def preview(connection, application_ids):
        apps = fetch_all(
            connection,
            f"SELECT {SAFE_APPLICATION_COLUMNS} FROM submitted_applications WHERE application_id IN ({placeholders(application_ids)})",
            application_ids,
        )
        batch_ids = sorted({row.get("batch_id") for row in apps if row.get("batch_id")})
        notification_rows = []
        for app_id in application_ids:
            notification_rows.extend(fetch_all(
                connection,
                """
                SELECT event_id, dedupe_key, telegram_user_id, event_type, state, attempts,
                       created_at, updated_at, substr(COALESCE(last_error, ''), 1, 300) AS last_error
                FROM notification_outbox
                WHERE snapshot_json LIKE ? OR html LIKE ? OR dedupe_key LIKE ?
                LIMIT 50
                """,
                (f"%{app_id}%", f"%{app_id}%", f"%{app_id}%"),
            ))
        dashboard_rows = fetch_all(
            connection,
            f"""
            SELECT entity_type, entity_id, state, attempts, created_at, updated_at,
                   substr(COALESCE(last_error, ''), 1, 300) AS last_error
            FROM dashboard_outbox
            WHERE entity_type = 'APPLICATION' AND entity_id IN ({placeholders(application_ids)})
            """,
            application_ids,
        )
        bulk_rows = []
        if batch_ids:
            bulk_rows = fetch_all(
                connection,
                f"""
                SELECT batch_id, telegram_user_id, direction, sheet_name, registration_state,
                       registered_count, data_end_row, location_state, location_miss_count,
                       created_at, updated_at
                FROM bulk_batches
                WHERE batch_id IN ({placeholders(batch_ids)})
                """,
                batch_ids,
            )
        return {
            "application_ids": application_ids,
            "confirmation_phrase": "DELETE " + " ".join(application_ids),
            "counts": {
                "submitted_applications": len(apps),
                "dashboard_outbox": len(dashboard_rows),
                "notification_outbox": len(notification_rows),
                "affected_bulk_batches": len(bulk_rows),
            },
            "export": {
                "submitted_applications": apps,
                "dashboard_outbox": dashboard_rows,
                "notification_outbox": notification_rows,
                "affected_bulk_batches": bulk_rows,
            },
            "batch_ids": batch_ids,
        }


    def execute(connection, application_ids, confirmation):
        expected = "DELETE " + " ".join(application_ids)
        if confirmation != expected:
            raise RuntimeError("confirmation phrase mismatch")

        before = preview(connection, application_ids)
        batch_ids = before["batch_ids"]
        connection.execute("BEGIN IMMEDIATE")
        try:
            deleted = {}
            notification_count = 0
            for app_id in application_ids:
                notification_count += connection.execute(
                    """
                    DELETE FROM notification_outbox
                    WHERE snapshot_json LIKE ? OR html LIKE ? OR dedupe_key LIKE ?
                    """,
                    (f"%{app_id}%", f"%{app_id}%", f"%{app_id}%"),
                ).rowcount
            deleted["notification_outbox"] = notification_count
            deleted["dashboard_outbox"] = delete_in(
                connection,
                "DELETE FROM dashboard_outbox WHERE entity_type = 'APPLICATION' AND entity_id IN ({})",
                application_ids,
            )
            deleted["submitted_applications"] = delete_in(
                connection,
                "DELETE FROM submitted_applications WHERE application_id IN ({})",
                application_ids,
            )
            for batch_id in batch_ids:
                connection.execute(
                    """
                    UPDATE bulk_batches
                    SET registered_count = (
                            SELECT COUNT(*)
                            FROM submitted_applications
                            WHERE submitted_applications.batch_id = bulk_batches.batch_id
                        ),
                        data_end_row = COALESCE(
                            (
                                SELECT MAX(last_seen_row_number)
                                FROM submitted_applications
                                WHERE submitted_applications.batch_id = bulk_batches.batch_id
                            ),
                            data_end_row
                        ),
                        updated_at = ?
                    WHERE batch_id = ?
                    """,
                    (datetime.now(timezone.utc).isoformat(), batch_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        remaining = preview(connection, application_ids)
        return {"before": before, "deleted": deleted, "remaining": remaining}


    def main():
        mode = os.environ["DASHBOARD_DELETE_MODE"]
        application_ids = json.loads(os.environ["DASHBOARD_APPLICATION_IDS"])
        confirmation = os.environ.get("DASHBOARD_CONFIRMATION", "")
        db_path = os.environ.get("DASHBOARD_SQLITE_PATH", "/data/app.db")
        errors = []
        result = {
            "status": "ok",
            "mode": mode,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "application_ids": application_ids,
            "errors": errors,
        }
        try:
            uri = f"file:{db_path}?mode=ro" if mode == "preview" else db_path
            connection = sqlite3.connect(uri, uri=(mode == "preview"), timeout=2)
            connection.execute("PRAGMA busy_timeout=2000")
            if mode == "preview":
                connection.execute("PRAGMA query_only=ON")
                result["preview"] = preview(connection, application_ids)
            elif mode == "execute":
                result["execution"] = execute(connection, application_ids, confirmation)
            else:
                raise RuntimeError(f"unsupported mode: {mode}")
            connection.close()
        except Exception as exc:
            result["status"] = "failed"
            errors.append(f"{type(exc).__name__}: {exc}")
        print(json.dumps(result, ensure_ascii=False))


    main()
    '''
).strip()
