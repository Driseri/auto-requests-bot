from __future__ import annotations

import shlex
import textwrap

from .config import RemoteConfig


def build_remote_command(
    remote: RemoteConfig,
    *,
    include_logs: bool,
    include_heavy: bool,
) -> str:
    """Build one read-only SSH command that emits one JSON snapshot."""

    env = (
        f"INCLUDE_LOGS={'1' if include_logs else '0'} "
        f"INCLUDE_HEAVY={'1' if include_heavy else '0'} "
        f"COMPOSE_FILE={shlex.quote(remote.compose_file)} "
        f"SERVICE={shlex.quote(remote.service)} "
    )
    script = _REMOTE_SCRIPT_TEMPLATE.replace("__CONTAINER_SCRIPT__", repr(_CONTAINER_SCRIPT))
    return (
        f"cd {shlex.quote(remote.project_dir)} && "
        f"{env}python3 - <<'PY_DASHBOARD_COLLECTOR'\n{script}\nPY_DASHBOARD_COLLECTOR"
    )


_REMOTE_SCRIPT_TEMPLATE = textwrap.dedent(
    r'''
    import json
    import os
    import re
    import subprocess
    from datetime import datetime, timezone

    COMPOSE_FILE = os.environ.get("COMPOSE_FILE", "docker-compose.prod.yml")
    SERVICE = os.environ.get("SERVICE", "bot")
    INCLUDE_LOGS = os.environ.get("INCLUDE_LOGS") == "1"
    INCLUDE_HEAVY = os.environ.get("INCLUDE_HEAVY") == "1"
    CONTAINER_SCRIPT = __CONTAINER_SCRIPT__


    def run(args, timeout=8):
        # Keep all remote commands bounded so the weak VPS is never held hostage.
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
            return {"ok": False, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}", "code": -1}
        return {
            "ok": completed.returncode == 0,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "code": completed.returncode,
        }


    def compose_args(*parts):
        return ["docker", "compose", "-f", COMPOSE_FILE, *parts]


    def parse_json_text(text):
        text = (text or "").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"(\{.*\}|\[.*\])", text, re.S)
            if match:
                return json.loads(match.group(1))
        return {"_raw": text[:2000], "_error": "invalid json"}


    def app_version():
        # Read only APP_VERSION; never expose the rest of .env.
        try:
            with open(".env", encoding="utf-8") as file:
                for line in file:
                    if line.startswith("APP_VERSION="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            return ""
        return ""


    def main():
        errors = []
        container_id = run(compose_args("ps", "-q", SERVICE), timeout=8)
        cid = container_id["stdout"].strip().splitlines()[0] if container_id["ok"] and container_id["stdout"].strip() else ""
        if not cid:
            errors.append("container id not found")

        inspect_raw = run(["docker", "inspect", cid], timeout=8)["stdout"] if cid else "[]"
        inspect = parse_json_text(inspect_raw)
        inspect_item = inspect[0] if isinstance(inspect, list) and inspect else {}
        stats = parse_json_text(run(["docker", "stats", "--no-stream", "--format", "{{json .}}", cid], timeout=8)["stdout"]) if cid else {}
        ps = run(compose_args("ps", SERVICE), timeout=8)
        uptime = run(["uptime"], timeout=5)
        free = run(["free", "-m"], timeout=5)
        df = run(["df", "-P", "-BG", "/"], timeout=5)
        nproc = run(["nproc"], timeout=5)

        container_result = run(
            compose_args("exec", "-T", SERVICE, "python", "-c", CONTAINER_SCRIPT),
            timeout=15,
        )
        container_payload = parse_json_text(container_result["stdout"])
        if not container_result["ok"]:
            errors.append(container_result["stderr"][:500] or "container sqlite collector failed")
        if container_payload.get("_error"):
            errors.append(str(container_payload["_error"]))

        logs = ""
        if INCLUDE_LOGS:
            logs = run(
                compose_args("logs", "--since=15m", "--tail=1000", SERVICE),
                timeout=12,
            )["stdout"]

        heavy = {}
        if INCLUDE_HEAVY:
            heavy = {
                "project_size": run(["du", "-sh", "."], timeout=10)["stdout"].strip(),
                "backup_sizes": run(["sh", "-lc", "du -sh backups 2>/dev/null || true"], timeout=10)["stdout"].strip(),
                "tar_files": run(["sh", "-lc", "find . -maxdepth 1 -name '*.tar' -printf '%f %s\n' 2>/dev/null | head -20"], timeout=10)["stdout"],
                "docker_system_df": run(["docker", "system", "df"], timeout=15)["stdout"],
            }

        print(json.dumps({
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "host": os.uname().nodename if hasattr(os, "uname") else "",
            "app_version": app_version(),
            "errors": errors,
            "container": {
                "id": cid,
                "compose_ps": ps["stdout"],
                "inspect": inspect_item,
                "stats": stats,
            },
            "vps": {
                "uptime": uptime["stdout"],
                "free_m": free["stdout"],
                "df_root": df["stdout"],
                "nproc": nproc["stdout"].strip(),
                "heavy": heavy,
            },
            "container_payload": container_payload,
            "logs": logs,
        }, ensure_ascii=False))


    main()
    '''
).strip()

_CONTAINER_SCRIPT = textwrap.dedent(
    r'''
    import json
    import os
    import sqlite3
    from pathlib import Path


    def read_json(path):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            return {"_error": f"{type(exc).__name__}: {exc}"}


    def fetch_all(connection, sql, params=()):
        cursor = connection.execute(sql, params)
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


    def fetch_one(connection, sql, params=()):
        rows = fetch_all(connection, sql, params)
        return rows[0] if rows else {}


    def state_counts(connection, table):
        return fetch_all(connection, f"SELECT state, COUNT(*) AS count FROM {table} GROUP BY state")


    def main():
        # This code runs inside the bot container and only opens SQLite read-only.
        result = {
            "heartbeat": read_json("/data/status-polling-heartbeat.json"),
            "external_health": read_json(os.environ.get("HEALTH_EXTERNAL_CACHE_PATH", "/data/external-health.json")),
            "sqlite": {"ok": False, "metrics": {}, "errors": []},
        }
        try:
            db = sqlite3.connect("file:/data/app.db?mode=ro", uri=True, timeout=5)
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=5000")
            metrics = {}
            metrics["notification_outbox_by_state"] = state_counts(db, "notification_outbox")
            metrics["notification_outbox_open"] = fetch_all(db, """
                SELECT state, COUNT(*) AS count, MIN(created_at) AS oldest_created_at,
                       MIN(sending_started_at) AS oldest_sending_started_at,
                       MAX(attempts) AS max_attempts
                FROM notification_outbox
                WHERE state IN ('PENDING', 'SENDING', 'FAILED')
                GROUP BY state
            """)
            metrics["notification_outbox_last_errors"] = fetch_all(db, """
                SELECT state, attempts, updated_at,
                       substr(COALESCE(last_error, ''), 1, 300) AS last_error
                FROM notification_outbox
                WHERE COALESCE(last_error, '') != ''
                ORDER BY updated_at DESC
                LIMIT 20
            """)
            metrics["dashboard_outbox_by_state"] = state_counts(db, "dashboard_outbox")
            metrics["dashboard_outbox_by_entity_state"] = fetch_all(db, """
                SELECT entity_type, state, COUNT(*) AS count, MAX(attempts) AS max_attempts,
                       MIN(updated_at) AS oldest_updated_at
                FROM dashboard_outbox
                GROUP BY entity_type, state
            """)
            metrics["dashboard_outbox_last_errors"] = fetch_all(db, """
                SELECT entity_type, entity_id, state, attempts, next_attempt_at,
                       substr(COALESCE(last_error, ''), 1, 300) AS last_error, updated_at
                FROM dashboard_outbox
                WHERE COALESCE(last_error, '') != ''
                ORDER BY updated_at DESC
                LIMIT 20
            """)
            metrics["applications_by_status"] = fetch_all(db,
                "SELECT last_known_status AS status, COUNT(*) AS count FROM submitted_applications GROUP BY last_known_status")
            metrics["applications_by_direction"] = fetch_all(db,
                "SELECT COALESCE(direction, '') AS direction, COUNT(*) AS count FROM submitted_applications GROUP BY direction")
            metrics["applications_summary"] = fetch_one(db, """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN date(COALESCE(submitted_at, created_at)) = date('now') THEN 1 ELSE 0 END) AS created_today,
                       SUM(CASE WHEN is_urgent = 1 THEN 1 ELSE 0 END) AS urgent_total,
                       SUM(CASE WHEN is_urgent = 1 AND date(COALESCE(submitted_at, created_at)) = date('now') THEN 1 ELSE 0 END) AS urgent_today,
                       SUM(CASE WHEN polling_state = 'NOT_FOUND' THEN 1 ELSE 0 END) AS not_found_total,
                       SUM(CASE WHEN not_found_count > 0 THEN 1 ELSE 0 END) AS not_found_count_positive,
                       SUM(CASE WHEN COALESCE(last_seen_editor, '') = '' THEN 1 ELSE 0 END) AS without_editor,
                       SUM(CASE WHEN COALESCE(last_seen_final_answer, '') != '' THEN 1 ELSE 0 END) AS with_final_answer
                FROM submitted_applications
            """)
            metrics["applications_problem_rows"] = fetch_all(db, """
                SELECT application_id, telegram_user_id, direction, sheet_name,
                       last_seen_row_number, last_known_status, polling_state,
                       not_found_count, next_status_check_at, updated_at
                FROM submitted_applications
                WHERE not_found_count > 0 OR polling_state != 'ACTIVE'
                ORDER BY updated_at DESC
                LIMIT 50
            """)
            metrics["urgent_applications"] = fetch_all(db, """
                SELECT application_id, telegram_user_id, direction, sheet_name,
                       last_seen_row_number, last_known_status, last_seen_editor,
                       CASE WHEN COALESCE(last_seen_final_answer, '') != '' THEN 1 ELSE 0 END AS has_final_answer,
                       COALESCE(submitted_at, created_at) AS created_at, updated_at
                FROM submitted_applications
                WHERE is_urgent = 1 AND COALESCE(last_seen_final_answer, '') = ''
                ORDER BY COALESCE(submitted_at, created_at) ASC
                LIMIT 50
            """)
            metrics["bulk_by_registration_state"] = fetch_all(db,
                "SELECT registration_state, COUNT(*) AS count FROM bulk_batches GROUP BY registration_state")
            metrics["bulk_summary"] = fetch_one(db, """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN registration_state != 'REGISTERED' THEN 1 ELSE 0 END) AS unfinished,
                       SUM(CASE WHEN registration_state = 'REGISTERING' THEN 1 ELSE 0 END) AS registering,
                       SUM(CASE WHEN registration_state = 'REGISTERED' THEN 1 ELSE 0 END) AS registered
                FROM bulk_batches
            """)
            metrics["bulk_unfinished_by_user"] = fetch_all(db, """
                SELECT telegram_user_id, COUNT(*) AS unfinished_batches
                FROM bulk_batches
                WHERE registration_state != 'REGISTERED'
                GROUP BY telegram_user_id
                ORDER BY unfinished_batches DESC
                LIMIT 20
            """)
            metrics["bulk_recent"] = fetch_all(db, """
                SELECT batch_id, telegram_user_id, direction, sheet_name, start_row,
                       data_start_row, reserved_rows, data_end_row, registration_state,
                       registered_count, last_known_batch_status, registration_started_at, updated_at
                FROM bulk_batches
                ORDER BY updated_at DESC
                LIMIT 50
            """)
            metrics["bulk_creation_by_state"] = state_counts(db, "bulk_creation_requests")
            metrics["bulk_creation_problem_rows"] = fetch_all(db, """
                SELECT idempotency_key, telegram_user_id, direction, state, batch_id,
                       substr(COALESCE(last_error, ''), 1, 300) AS last_error, started_at, updated_at
                FROM bulk_creation_requests
                WHERE state IN ('BULK_CREATING', 'FAILED')
                ORDER BY updated_at DESC
                LIMIT 50
            """)
            metrics["drafts_summary"] = fetch_all(db, """
                SELECT current_step, submission_state, COUNT(*) AS count
                FROM drafts
                GROUP BY current_step, submission_state
            """)
            metrics["user_workflow_pending"] = fetch_all(db, """
                SELECT telegram_user_id, pending_action, active_chat_id,
                       active_message_id, updated_at
                FROM user_settings
                WHERE pending_action IS NOT NULL OR active_message_id IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT 50
            """)
            result["sqlite"] = {"ok": True, "metrics": metrics, "errors": []}
            if os.environ.get("INCLUDE_HEAVY") == "1":
                quick = db.execute("PRAGMA quick_check").fetchone()
                result["sqlite"]["quick_check"] = quick[0] if quick else "missing"
            db.close()
        except Exception as exc:
            result["sqlite"]["errors"].append(f"{type(exc).__name__}: {exc}")
        print(json.dumps(result, ensure_ascii=False))


    main()
    '''
).strip()
