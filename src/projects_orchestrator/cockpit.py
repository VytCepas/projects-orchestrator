"""Assemble the fleet view-model shared by every cockpit surface.

`snapshot` is the single source of truth the web dashboard, the Textual TUI,
and the JSON/CLI renderers all consume, so control logic is written once
(ADR-003).
"""

from __future__ import annotations

from pathlib import Path

from projects_orchestrator import runtime
from projects_orchestrator.discovery import Project, discover, status_of
from projects_orchestrator.runcommands import plan_for
from projects_orchestrator.supervisor import Supervisor


def project_view(project: Project, supervisor: Supervisor | None = None) -> dict[str, object]:
    """Build the view-model for a single project.

    Args:
        project: The discovered project.
        supervisor: Supervisor tracking cockpit-started processes, or ``None``
            for read-only surfaces that start nothing (the web dashboard).

    Returns:
        A JSON-serialisable dict of the project's status and controls.
    """
    plan = plan_for(project.path)
    observed = runtime.observe(project.path)
    supervised = supervisor.status(project.name) if supervisor else "stopped"
    return {
        "name": project.name,
        "path": str(project.path),
        "description": project.description,
        "language": project.language,
        "branch": project.branch,
        "vcs_state": status_of(project),
        "run": plan.run,
        "test": plan.test,
        "run_source": plan.source,
        "supervised": supervised,
        "ports": observed.ports,
        "containers": observed.containers,
        "running": supervised == "running" or observed.running,
    }


def snapshot(root: Path, supervisor: Supervisor | None = None) -> list[dict[str, object]]:
    """Build the view-model for every project under ``root``.

    Args:
        root: Directory to scan for project-init projects.
        supervisor: Supervisor tracking cockpit-started processes, or ``None``
            for read-only surfaces (the web dashboard).

    Returns:
        A list of per-project view-models, ordered by name.
    """
    return [project_view(p, supervisor) for p in discover(root)]
