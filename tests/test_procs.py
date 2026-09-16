"""Process identity and liveness, tested directly (#187).

`procs.py` decides whether a recorded pid is still *our* process — the guard
that stops the supervisor reporting on, or signalling, a process that merely
inherited a recycled pid. It was exercised only indirectly through
`test_supervisor.py`, so the pid-guard and the start-ticks parser had no tests
of their own and their edge cases were never stated anywhere.

Signalling the wrong process is the failure that matters here, so the
impostor cases are the point of the file.
"""

from __future__ import annotations

import os

from projects_orchestrator.procs import is_our_process, pid_alive, proc_start_ticks


def test_our_own_pid_is_alive() -> None:
    assert pid_alive(os.getpid()) is True


def test_pid_zero_is_not_alive() -> None:
    """Pid 0 is the kernel scheduler on Linux and invalid to signal. It must
    never read as a live project process, because `terminate_group` would aim
    at it."""
    assert pid_alive(0) is False


def test_a_negative_pid_is_not_alive() -> None:
    """A negative pid addresses a process GROUP in kill(2). A recorded -1 that
    read as alive would broadcast a signal, so this is a safety property, not
    an input-validation nicety."""
    assert pid_alive(-1) is False


def test_an_almost_certainly_absent_pid_is_not_alive() -> None:
    assert pid_alive(4_194_303) is False


def test_start_ticks_for_an_absent_pid_is_none() -> None:
    assert proc_start_ticks(4_194_303) is None


def test_start_ticks_for_pid_zero_is_none() -> None:
    assert proc_start_ticks(0) is None


def test_a_process_with_no_recorded_start_is_accepted() -> None:
    """`None` start-ticks means the platform could not supply them — every
    non-Linux box. Refusing there would make the supervisor unusable on macOS,
    so liveness alone has to carry it."""
    assert is_our_process(os.getpid(), None) is True


def test_a_pid_whose_start_time_disagrees_is_an_impostor() -> None:
    """THE RECYCLED-PID CASE, and the reason this module exists. A recorded
    start time that no longer matches means some OTHER process holds the pid
    now — it is not ours to report on and emphatically not ours to signal."""
    ours = os.getpid()
    actual = proc_start_ticks(ours)
    if actual is None:
        # No /proc: the guard degrades to liveness, asserted above.
        return
    assert is_our_process(ours, actual + 1) is False


def test_a_pid_whose_start_time_matches_is_ours() -> None:
    ours = os.getpid()
    actual = proc_start_ticks(ours)
    if actual is None:
        return
    assert is_our_process(ours, actual) is True


def test_a_dead_pid_is_never_ours() -> None:
    assert is_our_process(4_194_303, 12345) is False
