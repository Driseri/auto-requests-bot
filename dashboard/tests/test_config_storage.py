from __future__ import annotations

from pathlib import Path

from backend.config import load_config, validate_config
from backend.schemas import Snapshot
from backend.storage import JsonStorage


def test_toml_config_masks_password_in_safe_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.toml"
    config_path.write_text(
        """
[ssh]
host = "example.org"
username = "admin"
password = "secret"

[storage]
data_dir = "data"
""",
        encoding="utf-8",
    )

    config = load_config(config_path)
    assert validate_config(config) == []
    safe = config.safe_dict()

    assert safe["ssh"]["password_configured"] is True
    assert "secret" not in str(safe)
    assert config.storage.data_dir == tmp_path / "data"


def test_storage_writes_latest_and_history(tmp_path: Path) -> None:
    storage = JsonStorage(tmp_path / "data")
    snapshot = Snapshot(
        collected_at="2026-06-18T10:00:00+00:00",
        source_host="vps",
        collection_status="ok",
    )

    storage.save_snapshot(snapshot)

    assert storage.load_latest() is not None
    assert storage.load_history(limit=10)[0]["source_host"] == "vps"
    assert storage.is_writable() is True
