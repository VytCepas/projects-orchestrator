"""Admission control for orchestrator-started processes.

The supervisor can launch long-lived project processes (dev servers, compose
stacks). Left unbounded these exhaust memory — on WSL2 the OOM killer takes
``dbus.service`` and wedges the systemd user session until ``wsl --shutdown``
(ADR-004). Every launch is therefore gated on two limits: a concurrent-worker
cap and a ``MemAvailable`` floor, failing fast with a clear reason *before*
anything is spawned.

Where ``MemAvailable`` cannot be read (non-Linux platforms), the memory floor
is skipped and only the worker cap applies, matching the runtime module's
"degrade gracefully off Linux" contract.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_MEMINFO = Path("/proc/meminfo")

# Conservative defaults: allow a handful of concurrent servers, and refuse to
# start another once free memory drops below ~1 GiB so a launch can never be
# the spawn that triggers the OOM killer.
DEFAULT_MAX_WORKERS = 4
DEFAULT_MIN_FREE_BYTES = 1024 * 1024 * 1024


class LaunchRefusedError(RuntimeError):
    """Raised when admission control declines to start a process.

    Attributes:
        reason: Human-readable explanation to surface to the operator/agent.
    """

    def __init__(self, reason: str) -> None:
        """Store the refusal reason and use it as the exception message."""
        super().__init__(reason)
        self.reason = reason


def mem_available_bytes(meminfo: Path = _MEMINFO) -> int | None:
    """Return ``MemAvailable`` in bytes from ``/proc/meminfo``.

    Args:
        meminfo: Path to a ``meminfo``-formatted file (injectable for tests).

    Returns:
        Available memory in bytes, or ``None`` when the file or field is absent
        (e.g. off Linux), in which case the memory floor is not enforced.
    """
    try:
        text = meminfo.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            fields = line.split()
            if len(fields) >= 2 and fields[1].isdigit():
                return int(fields[1]) * 1024  # meminfo reports kibibytes
    return None


@dataclass(frozen=True)
class Admission:
    """The verdict of an admission check.

    Attributes:
        ok: Whether a new process may be started.
        reason: Why it was refused (empty when ``ok`` is True).
    """

    ok: bool
    reason: str = ""


def admit(
    active: int,
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    available: Callable[[], int | None] = mem_available_bytes,
) -> Admission:
    """Decide whether another supervised process may start.

    Args:
        active: How many supervised processes are already running.
        max_workers: Maximum concurrent supervised processes allowed.
        min_free_bytes: Refuse to launch when free memory is below this.
        available: Callable returning free bytes, or ``None`` if unknown.

    Returns:
        An :class:`Admission`; ``ok`` is False with a ``reason`` when a limit
        would be exceeded. An unknown memory reading never blocks a launch.
    """
    if active >= max_workers:
        return Admission(False, f"worker cap reached ({active}/{max_workers} running)")
    free = available()
    if free is not None and free < min_free_bytes:
        return Admission(
            False,
            f"only {free // (1024 * 1024)} MiB free; need {min_free_bytes // (1024 * 1024)} MiB to launch safely",
        )
    return Admission(True)
