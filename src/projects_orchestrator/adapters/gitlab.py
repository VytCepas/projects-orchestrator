"""Per-project GitLab state via ``glab`` — latest pipeline status and open MRs.

The GitLab analog of :mod:`~projects_orchestrator.adapters.github`: the same
gap (a project clean on disk can be red in CI or have merge requests waiting)
filled by shelling out to ``glab`` through the shared timeout-bounded runner.
It degrades identically — missing/unauthenticated/offline ``glab`` yields
``unknown`` (CI) or ``None`` (MR count), never an exception — and maps to the
same ``ci`` / ``prs`` :class:`~projects_orchestrator.checks.CheckResult` tasks
so a GitLab-hosted project renders in the existing fleet-table columns.
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

# glab reads the project from the working directory's remote; both degrade offline.
_CI_COMMAND = "glab ci list -F json -P 1"
# -P 100 is GitLab's maximum page size (the REST API clamps per_page to 100), so
# unlike github.py's `--limit 1000` this cannot simply be raised: a project with
# more than 100 open MRs reports exactly 100. Lifting it needs real pagination
# (repeated --page requests), not a bigger number. See MR_COUNT_CAP.
_MR_COMMAND = "glab mr list --output json -P 100"

#: The most open MRs :func:`parse_mr_count` can observe in one ``glab`` call.
#: Exposed so callers can tell "100 open MRs" from "at least 100 open MRs".
MR_COUNT_CAP = 100

_GLAB_TIMEOUT = 20.0

# GitLab pipeline states → our CI vocabulary.
_SUCCESS_STATES = {"success"}
_FAIL_STATES = {"failed", "canceled", "cancelled"}
_RUNNING_STATES = {
    "created",
    "waiting_for_resource",
    "preparing",
    "pending",
    "running",
    "scheduled",
}


def _detect_gitlab(host: str) -> bool:
    """Whether a descriptor host string names a GitLab forge."""
    return "gitlab" in host.lower()


def provider_is_gitlab(descriptor: ProjectDescriptor) -> bool:
    """Whether this project's CI should be probed via ``glab`` rather than ``gh``."""
    return _detect_gitlab(descriptor.host)


@dataclass(frozen=True)
class GitlabStatus:
    """One project's GitLab state.

    Attributes:
        project: Project name.
        ci: Latest pipeline status — ``pass`` | ``fail`` | ``running`` | ``unknown``.
        open_mrs: Count of open merge requests, or ``None`` when unknown.
    """

    project: str
    ci: str = CI_UNKNOWN
    open_mrs: int | None = None


def _loads(stdout: str) -> Any:
    """Parse JSON stdout, returning ``None`` on any problem."""
    try:
        return json.loads(stdout)
    except (ValueError, TypeError):
        return None


def parse_pipeline_status(stdout: str) -> str:
    """Map ``glab ci list -F json`` output to a CI state (pure).

    Args:
        stdout: JSON array of pipelines (newest first) from ``glab ci list``.

    Returns:
        ``pass`` | ``fail`` | ``running`` | ``unknown``; anything unparseable
        or empty is ``unknown``.
    """
    pipelines = _loads(stdout)
    if not isinstance(pipelines, list) or not pipelines or not isinstance(pipelines[0], dict):
        return CI_UNKNOWN
    state = pipelines[0].get("status")
    if state in _SUCCESS_STATES:
        return CI_SUCCESS
    if state in _FAIL_STATES:
        return CI_FAIL
    if state in _RUNNING_STATES:
        return CI_RUNNING
    return CI_UNKNOWN


def parse_mr_count(stdout: str) -> int | None:
    """Count open merge requests from ``glab mr list --output json`` (pure).

    Args:
        stdout: JSON array from ``glab mr list``.

    Returns:
        The number of MRs, or ``None`` when the output is unparseable.
    """
    mrs = _loads(stdout)
    if not isinstance(mrs, list):
        return None
    return len(mrs)


def collect_gitlab(descriptor: ProjectDescriptor, timeout: float = _GLAB_TIMEOUT) -> GitlabStatus:
    """Probe one project's pipeline status and open-MR count; never raises.

    Args:
        descriptor: The project to probe (``glab`` runs in its directory).
        timeout: Per-command timeout in seconds.

    Returns:
        A :class:`GitlabStatus`; ``glab`` failure degrades to ``unknown``/``None``.
    """
    ci_result = run_command(_CI_COMMAND, cwd=descriptor.path, timeout=timeout)
    ci = parse_pipeline_status(ci_result.stdout) if ci_result.ok else CI_UNKNOWN
    mr_result = run_command(_MR_COMMAND, cwd=descriptor.path, timeout=timeout)
    open_mrs = parse_mr_count(mr_result.stdout) if mr_result.ok else None
    return GitlabStatus(project=descriptor.name, ci=ci, open_mrs=open_mrs)


def as_check_results(status: GitlabStatus, checked_at: str) -> list[CheckResult]:
    """Adapt a :class:`GitlabStatus` into cacheable check results.

    Mapped to the same ``ci`` / ``prs`` tasks as the GitHub adapter so a
    GitLab-hosted project fills the existing ``CI`` and ``PRs`` columns (the
    open-MR count lands in the ``prs`` result's detail).

    Args:
        status: The probed GitLab state.
        checked_at: ISO-8601 timestamp to stamp the results with.

    Returns:
        A ``ci`` result and a ``prs`` result (the open-MR count in ``detail``).
    """
    mrs_known = status.open_mrs is not None
    return [
        CheckResult(project=status.project, task="ci", status=status.ci, checked_at=checked_at),
        CheckResult(
            project=status.project,
            task="prs",
            status="ok" if mrs_known else CI_UNKNOWN,
            detail=str(status.open_mrs) if mrs_known else "",
            checked_at=checked_at,
        ),
    ]
