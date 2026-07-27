from __future__ import annotations

import socket
import time

import paramiko

from .config import SshConfig


class SshCommandError(RuntimeError):
    """Raised when a remote read-only command fails or times out."""


class ParamikoSshClient:
    """Minimal Paramiko wrapper with explicit timeouts for weak VPS safety."""

    def __init__(self, config: SshConfig) -> None:
        self.config = config

    def run(self, command: str) -> str:
        """Execute a single remote command and return stdout."""

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.config.host,
                port=self.config.port,
                username=self.config.username,
                password=self.config.password,
                timeout=self.config.connect_timeout_seconds,
                banner_timeout=self.config.connect_timeout_seconds,
                auth_timeout=self.config.connect_timeout_seconds,
                look_for_keys=False,
                allow_agent=False,
            )
            stdin, stdout, stderr = client.exec_command(
                command,
                timeout=self.config.command_timeout_seconds,
            )
            stdin.close()
            return self._wait_for_result(stdout, stderr)
        except (paramiko.SSHException, socket.timeout, OSError) as exc:
            raise SshCommandError(f"SSH command failed: {exc}") from exc
        finally:
            client.close()

    def _wait_for_result(self, stdout, stderr) -> str:
        """Poll the Paramiko channel so command timeout is enforced locally."""

        channel = stdout.channel
        deadline = time.monotonic() + self.config.command_timeout_seconds
        while not channel.exit_status_ready():
            if time.monotonic() > deadline:
                channel.close()
                raise SshCommandError("SSH command timed out")
            time.sleep(0.05)
        exit_status = channel.recv_exit_status()
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        if exit_status != 0:
            preview = error.strip() or output.strip()
            raise SshCommandError(f"remote command exited {exit_status}: {preview[:1000]}")
        return output
