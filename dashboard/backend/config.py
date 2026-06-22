from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.local.toml"


@dataclass(frozen=True, slots=True)
class SshConfig:
    """SSH connection settings kept only in the local dashboard config."""

    host: str
    username: str
    password: str
    port: int = 22
    connect_timeout_seconds: float = 5.0
    command_timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class RemoteConfig:
    """Remote project coordinates used by the read-only collector."""

    project_dir: str = "/opt/alfa-auto-requests"
    compose_file: str = "docker-compose.prod.yml"
    service: str = "bot"


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """Collection cadence tuned for a small 1 CPU / 2 GB RAM VPS."""

    fast_interval_seconds: int = 60
    logs_interval_seconds: int = 120
    heavy_interval_seconds: int = 300
    auto_collect: bool = True


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Local JSON storage settings."""

    data_dir: Path


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Complete local dashboard configuration."""

    ssh: SshConfig
    remote: RemoteConfig
    collector: CollectorConfig
    storage: StorageConfig

    def safe_dict(self) -> dict[str, Any]:
        """Return config values that are safe to expose through the API."""

        return {
            "ssh": {
                "host": self.ssh.host,
                "port": self.ssh.port,
                "username": self.ssh.username,
                "password_configured": bool(self.ssh.password),
                "connect_timeout_seconds": self.ssh.connect_timeout_seconds,
                "command_timeout_seconds": self.ssh.command_timeout_seconds,
            },
            "remote": {
                "project_dir": self.remote.project_dir,
                "compose_file": self.remote.compose_file,
                "service": self.remote.service,
            },
            "collector": {
                "fast_interval_seconds": self.collector.fast_interval_seconds,
                "logs_interval_seconds": self.collector.logs_interval_seconds,
                "heavy_interval_seconds": self.collector.heavy_interval_seconds,
                "auto_collect": self.collector.auto_collect,
            },
            "storage": {"data_dir": str(self.storage.data_dir)},
        }


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Load local TOML config and apply conservative defaults."""

    config_path = Path(path)
    raw = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    base_dir = config_path.parent

    ssh = raw.get("ssh", {})
    remote = raw.get("remote", {})
    collector = raw.get("collector", {})
    storage = raw.get("storage", {})
    data_dir = Path(storage.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = base_dir / data_dir

    return AppConfig(
        ssh=SshConfig(
            host=str(ssh.get("host", "")).strip(),
            port=int(ssh.get("port", 22)),
            username=str(ssh.get("username", "")).strip(),
            password=str(ssh.get("password", "")),
            connect_timeout_seconds=float(ssh.get("connect_timeout_seconds", 5)),
            command_timeout_seconds=float(ssh.get("command_timeout_seconds", 30)),
        ),
        remote=RemoteConfig(
            project_dir=str(remote.get("project_dir", "/opt/alfa-auto-requests")),
            compose_file=str(remote.get("compose_file", "docker-compose.prod.yml")),
            service=str(remote.get("service", "bot")),
        ),
        collector=CollectorConfig(
            fast_interval_seconds=int(collector.get("fast_interval_seconds", 60)),
            logs_interval_seconds=int(collector.get("logs_interval_seconds", 120)),
            heavy_interval_seconds=int(collector.get("heavy_interval_seconds", 300)),
            auto_collect=bool(collector.get("auto_collect", True)),
        ),
        storage=StorageConfig(data_dir=data_dir),
    )


def validate_config(config: AppConfig) -> list[str]:
    """Return configuration errors without raising so /api/health stays useful."""

    errors: list[str] = []
    if not config.ssh.host:
        errors.append("ssh.host is required")
    if not config.ssh.username:
        errors.append("ssh.username is required")
    if not config.ssh.password:
        errors.append("ssh.password is required")
    if config.ssh.port <= 0:
        errors.append("ssh.port must be positive")
    if config.collector.fast_interval_seconds <= 0:
        errors.append("collector.fast_interval_seconds must be positive")
    if config.collector.logs_interval_seconds <= 0:
        errors.append("collector.logs_interval_seconds must be positive")
    if config.collector.heavy_interval_seconds <= 0:
        errors.append("collector.heavy_interval_seconds must be positive")
    return errors
