"""Launch, tail, and stop the processes the cockpit itself starts.

State lives in memory for the life of one ``serve``/``tui`` session. Each
managed process runs in its own session (process group) so stopping it also
stops the children it spawned (e.g. ``just dev`` -> ``uv run ...``).
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path

from projects_orchestrator.guard import (
    DEFAULT_MAX_WORKERS,
    DEFAULT_MIN_FREE_BYTES,
    LaunchRefusedError,
    admit,
    mem_available_bytes,
)

_LOG_LINES = 500


class _Managed:
    """A single supervised process and its captured output."""

    def __init__(self, command: str, popen: subprocess.Popen[str]) -> None:
        """Store the process handle and start draining its output."""
        self.command = command
        self.popen = popen
        self.logs: deque[str] = deque(maxlen=_LOG_LINES)
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        """Copy the process's merged output into the ring buffer."""
        stream = self.popen.stdout
        if stream is None:
            return
        for line in stream:
            self.logs.append(line.rstrip("\n"))

    @property
    def running(self) -> bool:
        """True while the process has not exited."""
        return self.popen.poll() is None

    @property
    def exit_code(self) -> int | None:
        """The exit code once finished, else ``None``."""
        return self.popen.poll()


class Supervisor:
    """Track the processes started for each project by name."""

    def __init__(
        self,
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        available: Callable[[], int | None] = mem_available_bytes,
    ) -> None:
        """Create an empty supervisor with launch admission limits.

        Args:
            max_workers: Maximum concurrent supervised processes.
            min_free_bytes: Refuse to launch below this much free memory.
            available: Callable returning free bytes (injectable for tests).
        """
        self._procs: dict[str, _Managed] = {}
        self._lock = threading.Lock()
        self._max_workers = max_workers
        self._min_free_bytes = min_free_bytes
        self._available = available

    def start(self, name: str, command: str, cwd: Path) -> bool:
        """Start ``command`` for project ``name``; no-op if already running.

        Args:
            name: Project key the process is filed under.
            command: Shell command to run.
            cwd: Working directory to launch it in.

        Returns:
            True if a new process was started, False if one was already live.

        Raises:
            LaunchRefusedError: When the worker cap or memory floor would be
                exceeded; no process is spawned.
        """
        with self._lock:
            existing = self._procs.get(name)
            if existing and existing.running:
                return False
            active = sum(1 for managed in self._procs.values() if managed.running)
            verdict = admit(
                active,
                max_workers=self._max_workers,
                min_free_bytes=self._min_free_bytes,
                available=self._available,
            )
            if not verdict.ok:
                raise LaunchRefusedError(verdict.reason)
            popen = subprocess.Popen(
                shlex.split(command),
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            self._procs[name] = _Managed(command, popen)
            return True

    def stop(self, name: str) -> bool:
        """Terminate the process group for ``name``.

        Args:
            name: Project key to stop.

        Returns:
            True if a live process was signalled, False if none was running.
        """
        with self._lock:
            managed = self._procs.get(name)
            if managed is None or not managed.running:
                return False
            try:
                os.killpg(managed.popen.pid, signal.SIGTERM)
                managed.popen.wait(timeout=5)
            except (ProcessLookupError, OSError):
                pass
            except subprocess.TimeoutExpired:
                os.killpg(managed.popen.pid, signal.SIGKILL)
            return True

    def status(self, name: str) -> str:
        """Return ``running``, ``exited``, or ``stopped`` for ``name``."""
        managed = self._procs.get(name)
        if managed is None:
            return "stopped"
        return "running" if managed.running else "exited"

    def logs(self, name: str) -> list[str]:
        """Return the captured log lines for ``name`` (newest last)."""
        managed = self._procs.get(name)
        return list(managed.logs) if managed else []

    def stop_all(self) -> None:
        """Stop every live process (called on cockpit shutdown)."""
        for name in list(self._procs):
            self.stop(name)
