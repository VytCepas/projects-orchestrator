"""Per-project GitHub state via ``gh`` — latest CI conclusion and open-PR count.

The fleet table is otherwise entirely local: a project can be clean on disk
but red in CI, or have PRs waiting. This adapter fills that gap by shelling
out to ``gh`` through the shared timeout-bounded runner, and degrades exactly
like :mod:`status` — missing/unauthenticated/offline ``gh`` yields ``unknown``
(CI) or ``None`` (PR count), never an exception.

Results are mapped to :class:`~projects_orchestrator.checks.CheckResult` so
they persist in the existing checks cache with freshness: the ``status`` table
then shows the last-known CI state offline, without ever blocking on the
network itself (only the explicit ``ci`` command makes the calls).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.runner import run_command

CI_UNKNOWN = "unknown"
CI_SUCCESS = "pass"
CI_FAIL = "fail"
CI_RUNNING = "running"

# gh reads the repo from the working directory's remote; both degrade offline.
_CI_COMMAND = "gh run list --limit 1 --json status,conclusion"
# --limit overrides gh's default page size of 30, which would silently cap the
# open-PR count exactly where a large backlog is the signal that matters.
_PR_COMMAND = "gh pr list --state open --json number --limit 1000"

_GH_TIMEOUT = 20.0

_FAIL_CONCLUSIONS = {"failure", "cancelled", "timed_out", "startup_failure", "action_required"}


@dataclass(frozen=True)
class GithubStatus:
    """One project's GitHub state.

    Attributes:
        project: Project name.
        ci: Latest run conclusion — ``pass`` | ``fail`` | ``running`` | ``unknown``.
        open_prs: Count of open PRs, or ``None`` when unknown (gh unavailable).
    """

    project: str
    ci: str = CI_UNKNOWN
    open_prs: int | None = None


def _loads(stdout: str) -> Any:
    """Parse JSON stdout, returning ``None`` on any problem."""
    try:
        return json.loads(stdout)
    except (ValueError, TypeError):
        return None


def parse_ci_conclusion(stdout: str) -> str:
    """Map ``gh run list --json status,conclusion`` output to a CI state (pure).

    Args:
        stdout: JSON array from ``gh run list``.

    Returns:
        ``pass`` | ``fail`` | ``running`` | ``unknown``; anything unparseable
        or empty is ``unknown``.
    """
    runs = _loads(stdout)
    if not isinstance(runs, list) or not runs or not isinstance(runs[0], dict):
        return CI_UNKNOWN
    run = runs[0]
    if run.get("status") != "completed":
        return CI_RUNNING
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return CI_SUCCESS
    if conclusion in _FAIL_CONCLUSIONS:
        return CI_FAIL
    return CI_UNKNOWN


def parse_pr_count(stdout: str) -> int | None:
    """Count open PRs from ``gh pr list --json number`` output (pure).

    Args:
        stdout: JSON array from ``gh pr list``.

    Returns:
        The number of PRs, or ``None`` when the output is unparseable.
    """
    prs = _loads(stdout)
    if not isinstance(prs, list):
        return None
    return len(prs)


def collect_github(descriptor: ProjectDescriptor, timeout: float = _GH_TIMEOUT) -> GithubStatus:
    """Probe one project's CI conclusion and open-PR count; never raises.

    Args:
        descriptor: The project to probe (``gh`` runs in its directory).
        timeout: Per-command timeout in seconds.

    Returns:
        A :class:`GithubStatus`; ``gh`` failure degrades to ``unknown``/``None``.
    """
    ci_result = run_command(_CI_COMMAND, cwd=descriptor.path, timeout=timeout)
    ci = parse_ci_conclusion(ci_result.stdout) if ci_result.ok else CI_UNKNOWN
    pr_result = run_command(_PR_COMMAND, cwd=descriptor.path, timeout=timeout)
    open_prs = parse_pr_count(pr_result.stdout) if pr_result.ok else None
    return GithubStatus(project=descriptor.name, ci=ci, open_prs=open_prs)


def as_check_results(status: GithubStatus, checked_at: str) -> list[CheckResult]:
    """Adapt a :class:`GithubStatus` into cacheable check results.

    Args:
        status: The probed GitHub state.
        checked_at: ISO-8601 timestamp to stamp the results with.

    Returns:
        A ``ci`` result (status = the CI state) and a ``prs`` result (the open
        count in ``detail``; ``unknown`` when the count is unavailable).
    """
    prs_known = status.open_prs is not None
    return [
        CheckResult(project=status.project, task="ci", status=status.ci, checked_at=checked_at),
        CheckResult(
            project=status.project,
            task="prs",
            status="ok" if prs_known else CI_UNKNOWN,
            detail=str(status.open_prs) if prs_known else "",
            checked_at=checked_at,
        ),
    ]
