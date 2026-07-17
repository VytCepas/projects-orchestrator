"""Bounded subprocess execution shared by every engine module.

One rule everywhere: external commands are timeout-bounded and never raise.
Failures come back as data (:class:`RunResult`), so callers render them
instead of crashing the controller.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 300.0

_OUTPUT_CAP = 20_000

#: How long to wait for a killed command's pipes to close before abandoning its
#: output (:func:`_drain`). Deliberately short: the process group has already been
#: SIGKILLed, so anything still holding a pipe has escaped that group and is not
#: about to let go — waiting longer only delays a result we already have.
_DRAIN_TIMEOUT = 2.0


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
        # start_new_session puts the shell (and everything it spawns) in its
        # own process group, so a timeout can kill the whole tree instead of
        # only /bin/sh — otherwise a gate like `uv run pytest` leaks the real
        # runner, holding ports and locks after the "kill".
        proc = subprocess.Popen(  # noqa: S602 — tooling commands are trusted shell strings from project descriptors (see docstring)
            command,
            shell=True,  # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true — see above; trusted descriptor commands, not user input
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return RunResult(
            command=command,
            returncode=None,
            duration=time.monotonic() - start,
            error=str(exc),
        )

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        stdout, stderr = _drain(proc)
        return RunResult(
            command=command,
            returncode=None,
            stdout=_cap(stdout),
            stderr=_cap(stderr),
            duration=time.monotonic() - start,
            timed_out=True,
        )
    return RunResult(
        command=command,
        returncode=proc.returncode,
        stdout=_cap(stdout),
        stderr=_cap(stderr),
        duration=time.monotonic() - start,
    )


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the timed-out command's whole process group, then the process."""
    with contextlib.suppress(OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def _drain(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Collect a killed command's output without inheriting its hang.

    SIGKILLing the process group does **not** guarantee the pipes close. A
    grandchild that called ``setsid()`` for itself is no longer in the group
    ``killpg`` targets: it survives the kill and keeps the inherited write end
    open. An unbounded ``communicate()`` then waits for *that* process to exit,
    so a 1-second gate whose child daemonised takes as long as the daemon lives
    — forever, for a real daemon. The timeout stops bounding anything, and the
    hang moves out of the child and into the orchestrator, which is precisely
    what the timeout existed to prevent.

    So the drain is bounded too. Losing a timed-out command's tail is strictly
    better than never returning from it.
    """
    try:
        stdout, stderr = proc.communicate(timeout=_DRAIN_TIMEOUT)
    except subprocess.TimeoutExpired:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
        return "", "(output lost: a child survived the kill and held the pipe open)"
    return _decode(stdout), _decode(stderr)


def _decode(data: str | bytes | None) -> str:
    """Normalize TimeoutExpired output (bytes or str or None) to str."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data
