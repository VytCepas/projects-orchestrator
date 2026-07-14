"""Is that pid still ours? — process liveness, shared by every run tracker.

Two things track long-lived child processes now: :mod:`supervisor` (a project's
``run_command``) and :mod:`runs` (an agent run). Both must answer the same
deceptively hard question — *is the process I recorded still alive?* — and both
must resist the same trap: **pid reuse**.

A bare ``kill(pid, 0)`` says only that *some* process holds that pid, not that
it is the one we launched. After a reboot or a pid wraparound it may be a
stranger, and reporting a stranger as "your agent is still running" is worse
than reporting nothing. So a recorded pid is paired with its *start time* (clock
ticks since boot), which differs between the original and any impostor.

Duplicating this in two modules would have been the obvious move and the wrong
one: the zombie case below is exactly the sort of subtlety that gets fixed in one
copy and quietly rots in the other.

Everything here degrades rather than raises (ADR-003): off Linux there is no
``/proc``, so start-ticks are ``None`` and callers fall back to a plain liveness
probe.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def proc_start_ticks(pid: int) -> int | None:
    """Read a pid's start time (clock ticks since boot) from ``/proc``.

    Returns ``None`` when ``/proc`` is unavailable (non-Linux) or the pid is
    gone. Two processes that reuse a pid across a reboot or a wraparound have
    different start times, so comparing this against the value recorded at launch
    distinguishes our process from a recycled impostor.
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


def pid_alive(pid: int) -> bool:
    """Return whether a pid is a live process (not exited, not a zombie).

    When the pid is our own child (the launching process is still alive —
    controller REPL, TUI, tests), an exited child lingers as a zombie that a
    plain signal-0 probe would report as running; reap it with a non-blocking
    ``waitpid`` first. Non-children (the normal CLI case, where the child was
    reparented at our exit) fall through to the signal-0 probe.
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


def is_our_process(pid: int, start_ticks: int | None) -> bool:
    """Return whether ``pid`` is still the process we recorded, not an impostor.

    A recorded ``start_ticks`` that no longer matches the pid's current start
    time means the pid was recycled: some *other* process holds it now, and it
    is emphatically not ours to report on or signal.

    When ``start_ticks`` is ``None`` (recorded off-Linux, or before this was
    tracked) there is nothing to compare against, so this degrades to a plain
    liveness probe — the same trade-off the rest of the engine makes when a
    signal is unavailable.
    """
    if not pid_alive(pid):
        return False
    if start_ticks is None:
        return True
    current = proc_start_ticks(pid)
    if current is None:
        # /proc unreadable (non-Linux): we cannot disprove ownership, and the
        # process IS alive, so treat it as ours rather than silently orphaning a
        # live run.
        return True
    return current == start_ticks
