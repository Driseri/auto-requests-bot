from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from typing import Any

from .schemas import ApplicationReport, CollectionState, Snapshot


class JsonStorage:
    """Small local JSON store for latest snapshot, history and collector state."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.history_dir = data_dir / "history"
        self.application_reports_dir = data_dir / "application-reports"
        self.application_report_history_dir = self.application_reports_dir / "history"
        self.application_report_latest_path = self.application_reports_dir / "latest.json"
        self.latest_path = data_dir / "latest.json"
        self.state_path = data_dir / "collector-state.json"

    def ensure_ready(self) -> None:
        """Create local storage directories if they do not exist."""

        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.application_report_history_dir.mkdir(parents=True, exist_ok=True)

    def is_writable(self) -> bool:
        """Check that the local dashboard can write its JSON state."""

        try:
            self.ensure_ready()
            probe = self.data_dir / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    def load_latest(self) -> Snapshot | None:
        """Load the latest full snapshot, if it exists."""

        if not self.latest_path.exists():
            return None
        return Snapshot.model_validate_json(self.latest_path.read_text(encoding="utf-8"))

    def save_snapshot(self, snapshot: Snapshot, *, update_latest: bool = True) -> None:
        """Persist a snapshot in history and optionally replace latest atomically."""

        self.ensure_ready()
        payload = snapshot.model_dump(mode="json")
        self._append_history(payload, history_dir=self.history_dir)
        if update_latest:
            self._atomic_write_json(self.latest_path, payload)

    def load_latest_application_report(self) -> ApplicationReport | None:
        """Load the latest application report without touching the VPS."""

        if not self.application_report_latest_path.exists():
            return None
        return ApplicationReport.model_validate_json(
            self.application_report_latest_path.read_text(encoding="utf-8")
        )

    def save_application_report(
        self,
        report: ApplicationReport,
        *,
        update_latest: bool = True,
    ) -> None:
        """Persist an application report and optionally replace latest atomically."""

        self.ensure_ready()
        payload = report.model_dump(mode="json")
        self._append_history(payload, history_dir=self.application_report_history_dir)
        if update_latest:
            self._atomic_write_json(self.application_report_latest_path, payload)

    def load_state(self) -> CollectionState:
        """Load collector state; missing state means this is the first run."""

        if not self.state_path.exists():
            return CollectionState()
        return CollectionState.model_validate_json(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: CollectionState) -> None:
        """Persist collector timestamps atomically."""

        self.ensure_ready()
        self._atomic_write_json(self.state_path, state.model_dump(mode="json"))

    def load_history(self, limit: int = 100) -> list[dict[str, Any]]:
        """Read recent snapshots from JSONL history files, newest first."""

        self.ensure_ready()
        rows: list[dict[str, Any]] = []
        for path in sorted(self.history_dir.glob("*.jsonl"), reverse=True):
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                if not line.strip():
                    continue
                rows.append(json.loads(line))
                if len(rows) >= limit:
                    return rows
        return rows

    def _append_history(self, payload: dict[str, Any], *, history_dir: Path) -> None:
        day = datetime.now(timezone.utc).date().isoformat()
        path = history_dir / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            file.write("\n")

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        """Write JSON through a temp file to avoid partial latest/state files."""

        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            suffix=".tmp",
        ) as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            temp_name = file.name
        Path(temp_name).replace(path)
