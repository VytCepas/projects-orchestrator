"""Bounded subprocess execution shared by every engine module.

One rule everywhere: external commands are timeout-bounded and never raise.
Failures come back as data (:class:`RunResult`), so callers render them
instead of crashing the controller.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 300.0

_OUTPUT_CAP = 20_000


@dataclass(frozen=True)
class RunResult:
    """Outcome of one shell command.

    Attributes:
        command: The shell command that was run.
        returncode: Process exit code; ``None`` when the process never ran
            or was killed on timeout.
        stdout: Captured standard output (tail-capped).
        stderr: Captured standard error (tail-capped).
        duration: Wall-clock seconds spent.
        timed_out: Whether the command hit the timeout.
        error: OS-level failure description (missing shell, bad cwd), if any.
    """

    command: str
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Return whether the command completed with exit code 0."""
        return self.returncode == 0


def _cap(text: str) -> str:
    """Keep the tail of ``text`` — the end is where errors summarize."""
    return text if len(text) <= _OUTPUT_CAP else text[-_OUTPUT_CAP:]


def run_command(command: str, cwd: Path, timeout: float = DEFAULT_TIMEOUT) -> RunResult:
    """Run a shell command with a hard timeout; never raises.

    Args:
        command: Shell command line (tooling commands are shell strings).
        cwd: Working directory to run in.
        timeout: Kill the process after this many seconds.

    Returns:
        A :class:`RunResult` describing what happened.
    """
    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S602 — tooling commands are trusted shell strings from project descriptors (see docstring)
            command,
            shell=True,  # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true — see above; trusted descriptor commands, not user input
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return RunResult(
            command=command,
            returncode=None,
            stdout=_cap(_decode(exc.stdout)),
            stderr=_cap(_decode(exc.stderr)),
            duration=time.monotonic() - start,
            timed_out=True,
        )
    except OSError as exc:
        return RunResult(
            command=command,
            returncode=None,
            duration=time.monotonic() - start,
            error=str(exc),
        )
    return RunResult(
        command=command,
        returncode=proc.returncode,
        stdout=_cap(proc.stdout),
        stderr=_cap(proc.stderr),
        duration=time.monotonic() - start,
    )


def _decode(data: str | bytes | None) -> str:
    """Normalize TimeoutExpired output (bytes or str or None) to str."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data
