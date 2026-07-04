"""Start, stop, and watch long-running project processes.

The fleet engine could *check* projects but not *run* them: the ``Runnable``
column knew a ``run_command`` existed, yet nothing could launch a service
and keep an eye on it. This module is that missing half. ``start`` launches
the descriptor's declared ``run_command`` in its own session (detached
process group) with output captured to a log file; per-project state (pid,
start time, command, log path) persists under ``$XDG_STATE_HOME`` so any
later invocation — CLI, controller, or the fleet table's ``Running``
column — can answer "is it up, since when, and what did it print?".

Like the rest of the engine, nothing here raises: a missing run command,
an already-dead pid, or an unreadable state file degrades to a message or
``None``, and stale state is cleaned up on sight (a recycled pid cannot be
distinguished cheaply, so liveness is a probe of the recorded pid — the
usual supervisor trade-off).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.descriptor import ProjectDescriptor

_STATE_DIRNAME = "projects-orchestrator"
_RUN_SUBDIR = "run"

_STOP_GRACE_SECONDS = 5.0

DEFAULT_LOG_LINES = 40


@dataclass(frozen=True)
class RunState:
    """One supervised process, as recorded at start time.

    Attributes:
        project: Project name.
        pid: Process id of the launched session leader.
        started_at: UTC ISO timestamp of the launch.
        command: The ``run_command`` that was launched.
        log_path: File capturing the process's stdout+stderr.
    """

    project: str
    pid: int
    started_at: str
    command: str
    log_path: Path
    start_ticks: int | None = None


def state_dir() -> Path:
    """Return the supervisor state directory, honoring ``$XDG_STATE_HOME``."""
    base = os.environ.get("XDG_STATE_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / _STATE_DIRNAME / _RUN_SUBDIR


def _state_file(project: str) -> Path:
    """Return the state-file path for one project."""
    return state_dir() / f"{project}.json"


def _proc_start_ticks(pid: int) -> int | None:
    """Read a pid's start time (clock ticks since boot) from ``/proc``.

    Returns ``None`` when ``/proc`` is unavailable (non-Linux) or the pid is
    gone. Two processes that reuse the same pid across a reboot or pid wrap
    have different start times, so comparing this against the value recorded
    at launch distinguishes our process from a recycled impostor.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        # Field 22 (1-indexed) is starttime. comm (field 2) is parenthesized and
        # may contain spaces/parens, so parse the fields after the final ')'.
        after_comm = stat.rsplit(")", 1)[1].split()
        return int(after_comm[19])
    except (IndexError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """Return whether a pid is a live process (not exited, not a zombie).

    When the pid is our own child (the launching orchestrator process is
    still alive — controller REPL, TUI, tests), an exited child lingers as
    a zombie that a plain signal-0 probe would report as running; reap it
    with a non-blocking ``waitpid`` first. Non-children (the normal CLI
    case, where the child was reparented at our exit) fall through to the
    signal-0 probe.
    """
    with contextlib.suppress(ChildProcessError, OSError):
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _load_state(project: str) -> RunState | None:
    """Read one project's recorded state; ``None`` on any problem."""
    try:
        raw = json.loads(_state_file(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    start_ticks_raw = raw.get("start_ticks")
    start_ticks = int(start_ticks_raw) if isinstance(start_ticks_raw, int) else None
    try:
        return RunState(
            project=project,
            pid=int(raw["pid"]),
            started_at=str(raw.get("started_at", "")),
            command=str(raw.get("command", "")),
            log_path=Path(str(raw.get("log_path", ""))),
            start_ticks=start_ticks,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _clear_state(project: str) -> None:
    """Remove one project's state file, ignoring failures."""
    with contextlib.suppress(OSError):
        _state_file(project).unlink(missing_ok=True)


def running_state(descriptor: ProjectDescriptor) -> RunState | None:
    """Return the live run state for a project, cleaning up stale records.

    Args:
        descriptor: The project to look up.

    Returns:
        The recorded state when its pid is still alive; ``None`` otherwise
        (a dead pid's state file is deleted on the way out).
    """
    state = _load_state(descriptor.name)
    if state is None:
        return None
    if not _pid_alive(state.pid):
        _clear_state(descriptor.name)
        return None
    # Guard against pid reuse: if the pid is live but its start time no longer
    # matches what we recorded at launch, it is a different process — treat the
    # supervised one as gone rather than reporting it up (or signaling it).
    if state.start_ticks is not None and _proc_start_ticks(state.pid) != state.start_ticks:
        _clear_state(descriptor.name)
        return None
    return state


def start(descriptor: ProjectDescriptor) -> str:
    """Launch a project's declared ``run_command`` detached; never raises.

    Args:
        descriptor: The project to start.

    Returns:
        A human-readable outcome line (started / already running / no
        command declared / launch failure).
    """
    command = descriptor.tooling.get("run", "").strip()
    if not command:
        return f"{descriptor.name}: no run_command declared — nothing to start"
    existing = running_state(descriptor)
    if existing is not None:
        return f"{descriptor.name}: already running (pid {existing.pid})"

    directory = state_dir()
    log_path = directory / f"{descriptor.name}.log"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log_file:
            process = subprocess.Popen(  # noqa: S602 — descriptor-declared run command, same trust as runner.py
                command,
                shell=True,  # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true — trusted descriptor command (ADR-003)
                cwd=descriptor.path,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        state = RunState(
            project=descriptor.name,
            pid=process.pid,
            started_at=_dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds"),
            command=command,
            log_path=log_path,
            start_ticks=_proc_start_ticks(process.pid),
        )
        _state_file(descriptor.name).write_text(
            json.dumps(
                {
                    "pid": state.pid,
                    "started_at": state.started_at,
                    "command": state.command,
                    "log_path": str(state.log_path),
                    "start_ticks": state.start_ticks,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        return f"{descriptor.name}: failed to start — {exc}"
    return f"{descriptor.name}: started (pid {state.pid}, log {log_path})"


def _terminate_group(pid: int, grace: float) -> None:
    """SIGTERM a process group, escalating to SIGKILL after ``grace``.

    Supervised processes are launched with ``start_new_session=True``, so a
    legitimate target is always its own group leader (``pgid == pid``). A
    recycled pid that joined some other group is provably not our process —
    fall back to signalling only the pid rather than killing an unrelated
    group.
    """
    try:
        group = os.getpgid(pid)
    except OSError:
        group = None
    if group is not None and group != pid:
        group = None
    try:
        if group is not None:
            os.killpg(group, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    with contextlib.suppress(OSError):
        if group is not None:
            os.killpg(group, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)


def stop(descriptor: ProjectDescriptor, grace: float = _STOP_GRACE_SECONDS) -> str:
    """Terminate a project's supervised process; never raises.

    Args:
        descriptor: The project to stop.
        grace: Seconds to wait after SIGTERM before escalating to SIGKILL.

    Returns:
        A human-readable outcome line.
    """
    state = running_state(descriptor)
    if state is None:
        return f"{descriptor.name}: not running"
    _terminate_group(state.pid, grace)
    _clear_state(descriptor.name)
    return f"{descriptor.name}: stopped (pid {state.pid})"


def logs(descriptor: ProjectDescriptor, lines: int = DEFAULT_LOG_LINES) -> list[str]:
    """Return the tail of a project's captured run output; never raises.

    Args:
        descriptor: The project whose log to read.
        lines: Maximum trailing lines to return.

    Returns:
        The last ``lines`` lines, or one explanatory line when there is no
        readable log yet.
    """
    state = _load_state(descriptor.name)
    log_path = state.log_path if state is not None else state_dir() / f"{descriptor.name}.log"
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [f"{descriptor.name}: no run log (never started?)"]
    tail = text.splitlines()[-max(1, lines) :]
    return tail or [f"{descriptor.name}: log is empty"]
