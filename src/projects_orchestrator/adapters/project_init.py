"""Upstream project-init state via ``gh`` — latest release, upgrade dispatch.

Scaffold drift *within* a project is detected offline (``drift.py``); drift
*behind upstream* is not — each child upgrades via its own
``project-init-upgrade.yml`` workflow. This adapter reads the latest upstream
release and can dispatch a child's upgrade workflow, both through the shared
timeout-bounded runner and degrading to ``None``/failure offline.

The orchestrator never mutates a child tree: ``trigger_upgrade`` only *dispatches*
the child's own reviewed-PR upgrade workflow, which stays the sole write path
(ADR-003 / ADR-012).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from projects_orchestrator.adapters.gitlab import provider_is_gitlab
from projects_orchestrator.descriptor import ProjectDescriptor, parse_scaffold_version
from projects_orchestrator.runner import run_command

UPSTREAM_REPO = "VytCepas/project-init"
UPGRADE_WORKFLOW = "project-init-upgrade.yml"

# Where each forge's upgrade workflow lives in a child tree. project-init ships
# the GitHub Actions workflow today; the GitLab CI path is the convention its
# forge-agnostic upgrade path will adopt (VytCepas/project-init side). Gating
# dispatch on the file's presence keeps the mutation forge-aware and makes a
# child without an upgrade path report a clear reason instead of a silent fail.
GITHUB_UPGRADE_WORKFLOW = Path(".github/workflows") / UPGRADE_WORKFLOW
GITLAB_UPGRADE_WORKFLOW = Path(".gitlab") / UPGRADE_WORKFLOW

# Dispatch outcomes (returned by trigger_upgrade).
DISPATCHED = "dispatched"
FAILED = "failed"
NO_WORKFLOW = "no upgrade workflow"

_LATEST_COMMAND = f"gh release view --repo {UPSTREAM_REPO} --json tagName"
_GITHUB_TRIGGER_COMMAND = f"gh workflow run {UPGRADE_WORKFLOW}"
# GitLab has no per-file workflow_dispatch; the mirror is triggering the child's
# pipeline (the reviewed upgrade job runs inside it). Only reached when the child
# ships GITLAB_UPGRADE_WORKFLOW, so this never fires an unrelated pipeline.
_GITLAB_TRIGGER_COMMAND = "glab ci run"

_GH_TIMEOUT = 20.0


def _loads(stdout: str) -> Any:
    """Parse JSON stdout, returning ``None`` on any problem."""
    try:
        return json.loads(stdout)
    except (ValueError, TypeError):
        return None


def parse_release_tag(stdout: str) -> tuple[int, int, int] | None:
    """Parse ``gh release view --json tagName`` output to a version tuple (pure).

    Args:
        stdout: JSON object from ``gh release view``.

    Returns:
        The release version (``v`` prefix tolerated), or ``None`` when the
        output is unparseable or the tag is not ``MAJOR.MINOR.PATCH``.
    """
    data = _loads(stdout)
    if not isinstance(data, dict):
        return None
    tag = data.get("tagName")
    if not isinstance(tag, str):
        return None
    return parse_scaffold_version(tag.removeprefix("v"))


def latest_upstream_version(cwd: Path, timeout: float = _GH_TIMEOUT) -> tuple[int, int, int] | None:
    """Fetch the newest upstream project-init release; never raises.

    Args:
        cwd: Directory to run ``gh`` in (the command is repo-explicit via
            ``--repo``, so any valid directory works).
        timeout: Command timeout in seconds.

    Returns:
        The latest release version, or ``None`` when ``gh`` is unavailable,
        unauthenticated, offline, or returns an unparseable tag.
    """
    result = run_command(_LATEST_COMMAND, cwd=cwd, timeout=timeout)
    if not result.ok:
        return None
    return parse_release_tag(result.stdout)


def upgrade_workflow_relpath(descriptor: ProjectDescriptor) -> Path:
    """Return where this child's upgrade workflow lives, by forge (pure)."""
    return GITLAB_UPGRADE_WORKFLOW if provider_is_gitlab(descriptor) else GITHUB_UPGRADE_WORKFLOW


def has_upgrade_workflow(descriptor: ProjectDescriptor) -> bool:
    """Whether the child ships a reachable upgrade workflow for its forge.

    The one sanctioned mutation path (``upgrade-plan --apply``) can only
    dispatch when this is true; a GitLab-hosted or ``--lifecycle none`` child
    that lacks the workflow is diagnosable via ``doctor`` rather than a silent
    dispatch failure.
    """
    return (descriptor.path / upgrade_workflow_relpath(descriptor)).is_file()


def trigger_upgrade(descriptor: ProjectDescriptor, timeout: float = _GH_TIMEOUT) -> str:
    """Dispatch a child's forge-appropriate upgrade workflow; never raises.

    Chooses the forge from the descriptor host: a GitLab child dispatches via
    ``glab`` (its pipeline), every other child via ``gh workflow run``. Runs in
    the child's directory so the CLI resolves the repo from its remote. This
    only *dispatches* the child's own reviewed-PR upgrade channel — it never
    writes to the child tree.

    Args:
        descriptor: The child project to upgrade.
        timeout: Command timeout in seconds.

    Returns:
        :data:`DISPATCHED` on success; :data:`NO_WORKFLOW` when the child ships
        no upgrade workflow for its forge (a clear, non-silent reason
        ``--apply`` cannot proceed); else :data:`FAILED` (CLI missing/offline).
    """
    if not has_upgrade_workflow(descriptor):
        return NO_WORKFLOW
    command = _GITLAB_TRIGGER_COMMAND if provider_is_gitlab(descriptor) else _GITHUB_TRIGGER_COMMAND
    result = run_command(command, cwd=descriptor.path, timeout=timeout)
    return DISPATCHED if result.ok else FAILED
