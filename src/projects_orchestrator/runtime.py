"""Detect projects that are already running outside the cockpit.

Linux/WSL-specific: listening sockets come from ``ss`` mapped to a PID's
working directory via ``/proc``; containers come from ``docker ps`` compose
labels. On platforms without these, detection returns empty and the cockpit
falls back to supervised-only state (ADR-003).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_PID_RE = re.compile(r"pid=(\d+)")
_PORT_RE = re.compile(r":(\d+)\s")


@dataclass(frozen=True)
class RuntimeState:
    """What is observed running for a project right now."""

    ports: list[int] = field(default_factory=list)
    containers: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        """True when any port or container was detected."""
        return bool(self.ports or self.containers)


def _run(*args: str) -> str | None:
    """Run a command and return stdout, or None if it is unavailable."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _pid_cwd(pid: str) -> Path | None:
    """Resolve a PID's working directory via /proc."""
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None


def _listening() -> list[tuple[int, int]]:
    """Return (pid, port) pairs for TCP listening sockets."""
    out = _run("ss", "-ltnpH")
    if out is None:
        return []
    pairs: list[tuple[int, int]] = []
    for line in out.splitlines():
        pid = _PID_RE.search(line)
        port = _PORT_RE.search(line)
        if pid and port:
            pairs.append((int(pid.group(1)), int(port.group(1))))
    return pairs


def _within(cwd: Path | None, root: Path) -> bool:
    """True when ``cwd`` is ``root`` or nested under it."""
    if cwd is None:
        return False
    try:
        return cwd == root or root in cwd.parents
    except (OSError, ValueError):
        return False


def _docker_containers(root: Path) -> list[str]:
    """Return names of running compose containers rooted at ``root``."""
    fmt = '{{.Names}}\t{{.Label "com.docker.compose.project.working_dir"}}'
    out = _run("docker", "ps", "--format", fmt)
    if out is None:
        return []
    names = []
    for line in out.splitlines():
        name, _, workdir = line.partition("\t")
        if workdir and _within(Path(workdir), root):
            names.append(name)
    return names


def observe(project_path: Path) -> RuntimeState:
    """Observe a project's current runtime state.

    Args:
        project_path: The project root to inspect.

    Returns:
        A :class:`RuntimeState` with detected listening ports and containers.
    """
    root = project_path.resolve()
    ports = sorted({port for pid, port in _listening() if _within(_pid_cwd(str(pid)), root)})
    return RuntimeState(ports=ports, containers=_docker_containers(root))
