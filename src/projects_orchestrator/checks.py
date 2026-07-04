"""Run each project's own gates and report pass/fail as data.

A project "passes" when the commands *it* declares in its descriptor
(``tooling.lint_command`` etc.) exit zero. The orchestrator never guesses
commands: no declared command means ``skip``, and every failure mode
(non-zero, missing binary, timeout) is a ``fail`` result — never an
exception.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.runner import DEFAULT_TIMEOUT, run_command

DEFAULT_TASKS: tuple[str, ...] = ("lint", "test")

PASS = "pass"  # noqa: S105 — check-status label, not a credential
FAIL = "fail"
SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one gate on one project.

    Attributes:
        project: Project name.
        task: Gate name (lint, test, format, …).
        status: ``pass`` | ``fail`` | ``skip``.
        detail: Short human-readable explanation (error tail on failure).
        duration: Wall-clock seconds the command took.
        checked_at: UTC ISO timestamp of when the check finished.
        head: HEAD commit SHA the project was at when the check ran, when
            the worktree was clean; empty otherwise. Lets ``--changed-only``
            trust a cached ``pass`` for an unchanged project.
    """

    project: str
    task: str
    status: str
    detail: str = ""
    duration: float = 0.0
    checked_at: str = ""
    head: str = ""


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")


def _failure_detail(stderr: str, stdout: str) -> str:
    """Pick the most useful line to show for a failed command."""
    for stream in (stderr, stdout):
        lines = [line for line in stream.strip().splitlines() if line.strip()]
        if lines:
            return lines[-1][:200]
    return "command failed with no output"


def run_check(
    descriptor: ProjectDescriptor, task: str, timeout: float = DEFAULT_TIMEOUT, head: str = ""
) -> CheckResult:
    """Run one declared gate for one project; never raises.

    Args:
        descriptor: The project to check.
        task: Gate name, resolved via ``descriptor.tooling``.
        timeout: Kill the gate after this many seconds (counts as ``fail``).
        head: Clean-worktree HEAD SHA to stamp the result with (empty when
            unknown or dirty).

    Returns:
        The check result; an undeclared gate yields ``skip``.
    """
    command = descriptor.tooling.get(task, "").strip()
    if not command:
        return CheckResult(
            project=descriptor.name,
            task=task,
            status=SKIP,
            detail="no command declared",
            checked_at=_now(),
            head=head,
        )

    result = run_command(command, cwd=descriptor.path, timeout=timeout)
    if result.ok:
        status, detail = PASS, ""
    elif result.timed_out:
        status, detail = FAIL, f"timed out after {timeout:.0f}s"
    elif result.error:
        status, detail = FAIL, result.error
    else:
        status, detail = FAIL, _failure_detail(result.stderr, result.stdout)

    return CheckResult(
        project=descriptor.name,
        task=task,
        status=status,
        detail=detail,
        duration=result.duration,
        checked_at=_now(),
        head=head,
    )


def collect_checks(
    descriptor: ProjectDescriptor,
    tasks: tuple[str, ...] = DEFAULT_TASKS,
    timeout: float = DEFAULT_TIMEOUT,
    head: str = "",
) -> list[CheckResult]:
    """Run several gates for one project.

    Args:
        descriptor: The project to check.
        tasks: Gate names to run, in order.
        timeout: Per-gate timeout in seconds.
        head: Clean-worktree HEAD SHA to stamp results with.

    Returns:
        One :class:`CheckResult` per task, in the given order.
    """
    return [run_check(descriptor, task, timeout=timeout, head=head) for task in tasks]
