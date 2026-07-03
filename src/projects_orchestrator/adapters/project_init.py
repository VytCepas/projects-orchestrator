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

from projects_orchestrator.descriptor import ProjectDescriptor, parse_scaffold_version
from projects_orchestrator.runner import run_command

UPSTREAM_REPO = "VytCepas/project-init"
UPGRADE_WORKFLOW = "project-init-upgrade.yml"

_LATEST_COMMAND = f"gh release view --repo {UPSTREAM_REPO} --json tagName"
_TRIGGER_COMMAND = f"gh workflow run {UPGRADE_WORKFLOW}"

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


def trigger_upgrade(descriptor: ProjectDescriptor, timeout: float = _GH_TIMEOUT) -> str:
    """Dispatch a child's ``project-init-upgrade.yml`` workflow; never raises.

    Runs ``gh workflow run`` in the child's directory so ``gh`` resolves the
    repo from its remote. This only *dispatches* the child's own reviewed-PR
    upgrade channel — it never writes to the child tree.

    Args:
        descriptor: The child project to upgrade.
        timeout: Command timeout in seconds.

    Returns:
        ``dispatched`` on success, else ``failed`` (gh missing/offline/no such
        workflow).
    """
    result = run_command(_TRIGGER_COMMAND, cwd=descriptor.path, timeout=timeout)
    return "dispatched" if result.ok else "failed"
