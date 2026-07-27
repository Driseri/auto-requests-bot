from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


CLEANUP_SCHEMA_VERSION = 1
DELIVERABLE_NOTIFICATION_STATES = {"PENDING", "SENDING", "FAILED"}


class LegacyCleanupError(RuntimeError):
    pass


def build_cleanup_plan(sqlite_path: str) -> dict[str, Any]:
    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    try:
        return _build_cleanup_plan(connection)
    finally:
        connection.close()


def execute_cleanup(sqlite_path: str, confirmation_token: str) -> dict[str, Any]:
    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = _build_cleanup_plan(connection)
        if plan["blockers"]:
            raise LegacyCleanupError(
                "cleanup is blocked: " + ", ".join(plan["blockers"])
            )
        if confirmation_token != plan["confirmation_token"]:
            raise LegacyCleanupError(
                "confirmation token does not match the current database state"
            )

        application_ids = plan["application_ids"]
        batch_ids = plan["batch_ids"]
        request_keys = plan["bulk_creation_request_keys"]
        notification_ids = plan["deliverable_legacy_notification_ids"]

        deleted = {
            "submitted_applications": _delete_by_values(
                connection, "submitted_applications", "application_id", application_ids
            ),
            "bulk_batches": _delete_by_values(
                connection, "bulk_batches", "batch_id", batch_ids
            ),
            "bulk_creation_requests": _delete_by_values(
                connection,
                "bulk_creation_requests",
                "idempotency_key",
                request_keys,
            ),
            "notification_outbox": _delete_by_values(
                connection, "notification_outbox", "event_id", notification_ids
            ),
            "dashboard_outbox_applications": _delete_dashboard_applications(
                connection, application_ids
            ),
            "dashboard_outbox_batches": connection.execute(
                "DELETE FROM dashboard_outbox WHERE entity_type = 'BULK_BATCH'"
            ).rowcount,
            "retired_pending_actions": connection.execute(
                """
                UPDATE user_settings
                SET pending_action = NULL
                WHERE pending_action LIKE 'create_bulk_direction:%'
                """
            ).rowcount,
        }

        remaining = {
            "legacy_submitted_applications": connection.execute(
                """
                SELECT COUNT(*) FROM submitted_applications
                WHERE batch_id IS NOT NULL AND TRIM(batch_id) <> ''
                """
            ).fetchone()[0],
            "bulk_batches": connection.execute(
                "SELECT COUNT(*) FROM bulk_batches"
            ).fetchone()[0],
            "bulk_creation_requests": connection.execute(
                "SELECT COUNT(*) FROM bulk_creation_requests"
            ).fetchone()[0],
        }
        if any(remaining.values()):
            raise LegacyCleanupError(f"cleanup postcondition failed: {remaining}")

        connection.commit()
        return {
            **plan,
            "mode": "execute",
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "deleted": deleted,
            "remaining": remaining,
            "google_sheets_changed": False,
            "application_events_preserved": True,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _build_cleanup_plan(connection: sqlite3.Connection) -> dict[str, Any]:
    application_ids = [
        row[0]
        for row in connection.execute(
            """
            SELECT application_id FROM submitted_applications
            WHERE batch_id IS NOT NULL AND TRIM(batch_id) <> ''
            ORDER BY application_id
            """
        )
    ]
    legacy_ids = set(application_ids)
    batch_ids = [
        row[0]
        for row in connection.execute(
            "SELECT batch_id FROM bulk_batches ORDER BY batch_id"
        )
    ]
    request_keys = [
        row[0]
        for row in connection.execute(
            "SELECT idempotency_key FROM bulk_creation_requests ORDER BY idempotency_key"
        )
    ]

    deliverable_legacy: list[str] = []
    mixed: list[str] = []
    for row in connection.execute(
        """
        SELECT event_id, snapshot_json FROM notification_outbox
        WHERE state IN ('PENDING', 'SENDING', 'FAILED')
        ORDER BY event_id
        """
    ):
        referenced = _application_ids_from_snapshot(row["snapshot_json"])
        if referenced and referenced <= legacy_ids:
            deliverable_legacy.append(row["event_id"])
        elif referenced & legacy_ids:
            mixed.append(row["event_id"])

    payload = {
        "application_ids": application_ids,
        "batch_ids": batch_ids,
        "bulk_creation_request_keys": request_keys,
        "deliverable_legacy_notification_ids": deliverable_legacy,
    }
    token = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    event_count = (
        _count_by_values(
            connection, "application_events", "application_id", application_ids
        )
        if _table_exists(connection, "application_events")
        else 0
    )
    return {
        "schema_version": CLEANUP_SCHEMA_VERSION,
        "mode": "dry-run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **payload,
        "counts": {
            "submitted_applications": len(application_ids),
            "bulk_batches": len(batch_ids),
            "bulk_creation_requests": len(request_keys),
            "deliverable_legacy_notifications": len(deliverable_legacy),
            "application_events_preserved": event_count,
        },
        "blockers": (
            [f"mixed_notification_outbox:{event_id}" for event_id in mixed]
        ),
        "confirmation_token": token,
        "google_sheets_changed": False,
        "application_events_preserved": True,
    }


def _application_ids_from_snapshot(raw: str) -> set[str]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return set()

    found: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            application_id = item.get("application_id")
            if isinstance(application_id, str) and application_id:
                found.add(application_id)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def _delete_by_values(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    values: list[str],
) -> int:
    if not values:
        return 0
    placeholders = ",".join("?" for _ in values)
    return connection.execute(
        f"DELETE FROM {table} WHERE {column} IN ({placeholders})", values
    ).rowcount


def _delete_dashboard_applications(
    connection: sqlite3.Connection, application_ids: list[str]
) -> int:
    if not application_ids:
        return 0
    placeholders = ",".join("?" for _ in application_ids)
    return connection.execute(
        f"""
        DELETE FROM dashboard_outbox
        WHERE entity_type = 'APPLICATION'
          AND entity_id IN ({placeholders})
        """,
        application_ids,
    ).rowcount


def _count_by_values(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    values: list[str],
) -> int:
    if not values:
        return 0
    placeholders = ",".join("?" for _ in values)
    return connection.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})", values
    ).fetchone()[0]


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _write_report(report: dict[str, Any], output: str | None) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Safely remove retired legacy bulk tracking from SQLite."
    )
    parser.add_argument("--sqlite-path", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-token")
    parser.add_argument("--output")
    args = parser.parse_args()

    if args.execute:
        if not args.confirm_token:
            parser.error("--execute requires --confirm-token from a fresh dry-run")
        report = execute_cleanup(args.sqlite_path, args.confirm_token)
    else:
        report = build_cleanup_plan(args.sqlite_path)
    _write_report(report, args.output)


if __name__ == "__main__":
    main()
