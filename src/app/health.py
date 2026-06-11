from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone


def write_heartbeat(path: str, *, iteration: int) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "iteration": iteration,
            }
        ),
        encoding="utf-8",
    )
    temporary.replace(target)


def check_health(
    *,
    polling_enabled: bool,
    heartbeat_path: str,
    sqlite_path: str,
    max_age_seconds: float,
) -> tuple[bool, str]:
    if polling_enabled:
        try:
            payload = json.loads(Path(heartbeat_path).read_text(encoding="utf-8"))
            updated_at = datetime.fromisoformat(payload["updated_at"])
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - updated_at).total_seconds()
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return False, f"heartbeat unavailable: {exc}"
        if age > max_age_seconds:
            return False, f"heartbeat is stale: {age:.1f}s"

    try:
        with sqlite3.connect(sqlite_path, timeout=10) as connection:
            connection.execute("PRAGMA busy_timeout=10000")
            result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        return False, f"sqlite unavailable: {exc}"
    if not result or result[0] != "ok":
        return False, f"sqlite quick_check failed: {result}"
    return True, "ok"


def main() -> int:
    interval = float(os.getenv("STATUS_POLLING_INTERVAL_SECONDS", "30") or "30")
    max_age = float(
        os.getenv("STATUS_POLLING_HEARTBEAT_MAX_AGE_SECONDS", str(max(180, interval * 3)))
        or max(180, interval * 3)
    )
    enabled = os.getenv("STATUS_POLLING_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    healthy, message = check_health(
        polling_enabled=enabled,
        heartbeat_path=os.getenv(
            "STATUS_POLLING_HEARTBEAT_PATH",
            "/data/status-polling-heartbeat.json",
        ),
        sqlite_path=os.getenv("SQLITE_PATH", "/data/app.db"),
        max_age_seconds=max_age,
    )
    print(message)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
