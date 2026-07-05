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
from dataclasses import dataclass, field
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


def _coerce_int(value: Any, default: int = 0) -> int:
    """Coerce ``value`` to int (project-init emits some numbers as strings)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ScaffoldResult:
    """The machine-readable result of ``project-init scaffold --json`` (#510).

    project-init emits this seam explicitly "for a root orchestrator driving
    project-init": it names the freshly-scaffolded project and its key contract
    facts, so the orchestrator can register the new project from one call
    without a second config read.

    Attributes:
        target: Absolute path to the scaffolded project root.
        preset: Preset the scaffold was rendered from.
        contract_version: Descriptor-contract version stamped into the config.
        config_relpath: Where the descriptor lives under ``target``.
        memory_tier: Memory tier the scaffold selected.
        memory_stack: Memory backend the scaffold selected.
        files_created: Number of files the scaffold wrote.
        conflicts: Paths the scaffold left unwritten because they existed.
    """

    target: Path
    preset: str = ""
    contract_version: int = 0
    config_relpath: str = ".claude/config.yaml"
    memory_tier: int = 0
    memory_stack: str = "unknown"
    files_created: int = 0
    conflicts: tuple[str, ...] = field(default_factory=tuple)


def parse_scaffold_result(text: str) -> ScaffoldResult | None:
    """Parse ``scaffold --json`` stdout into a :class:`ScaffoldResult` (pure).

    Args:
        text: The JSON document project-init wrote to stdout.

    Returns:
        The parsed result, or ``None`` when the document is not an object or
        names no ``target`` (the one field the orchestrator cannot invent).
        Numbers emitted as strings (``"1"``, ``"0"``) are tolerated.
    """
    data = _loads(text)
    if not isinstance(data, dict):
        return None
    target = data.get("target")
    if not isinstance(target, str) or not target.strip():
        return None
    raw_memory = data.get("memory")
    memory: dict[str, Any] = raw_memory if isinstance(raw_memory, dict) else {}
    raw_conflicts = data.get("conflicts")
    conflicts = (
        tuple(str(c) for c in raw_conflicts) if isinstance(raw_conflicts, list) else ()
    )
    return ScaffoldResult(
        target=Path(target.strip()),
        preset=str(data.get("preset") or ""),
        contract_version=_coerce_int(data.get("contract_version")),
        config_relpath=str(data.get("config") or ".claude/config.yaml"),
        memory_tier=_coerce_int(memory.get("tier")),
        memory_stack=str(memory.get("stack") or "unknown"),
        files_created=_coerce_int(data.get("files_created")),
        conflicts=conflicts,
    )


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
