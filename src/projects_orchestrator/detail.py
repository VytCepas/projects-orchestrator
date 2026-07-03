"""Per-project drill-in: one project's depth without leaving the app.

The overview table is fleet-wide and one cell per fact; sometimes you need
everything about *one* project — descriptor, last-known gate results,
recent commits, memory. ``build_detail`` joins that into a pure, renderable
payload shared by the TUI Detail pane and the controller ``detail`` verb,
so both show identical truth. Git access goes through the shared bounded
runner and degrades to an explanatory line, never an exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.memory import load_project_memory
from projects_orchestrator.runner import run_command

_GIT_TIMEOUT = 15.0

DEFAULT_COMMIT_LIMIT = 10


@dataclass(frozen=True)
class ProjectDetail:
    """Everything worth showing about one project, ready to render.

    Attributes:
        project: Project name.
        summary: Descriptor facts, one ``label: value`` line each.
        checks: Last-known gate results, one line per task.
        commits: Recent commit subjects, newest first.
        memory: Memory facts, one ``name — description`` line each.
    """

    project: str
    summary: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    commits: tuple[str, ...] = ()
    memory: tuple[str, ...] = ()


def recent_commits(path: Path, limit: int = DEFAULT_COMMIT_LIMIT) -> tuple[str, ...]:
    """List a repo's most recent commit subjects; never raises.

    Args:
        path: Repository root.
        limit: Maximum commits to return.

    Returns:
        Oneline commit entries, or a single explanatory line for a
        non-git/unreadable directory.
    """
    result = run_command(f"git log -n {int(limit)} --oneline", cwd=path, timeout=_GIT_TIMEOUT)
    if not result.ok:
        return ("no commit history (not a git repository?)",)
    lines = tuple(line for line in result.stdout.splitlines() if line.strip())
    return lines or ("no commits yet",)


def _summary_lines(descriptor: ProjectDescriptor) -> tuple[str, ...]:
    """Render the descriptor's static facts as ``label: value`` lines."""
    contract = f"v{descriptor.contract_version}" if descriptor.contract_version else "none"
    lines = [
        f"path: {descriptor.path}",
        f"language: {descriptor.language}",
        f"delivery: {descriptor.delivery}",
        f"contract: {contract}",
        f"scaffold: {descriptor.project_init_version}",
        f"memory tier: {descriptor.memory_tier}",
        f"tooling: {', '.join(sorted(descriptor.tooling)) or 'none declared'}",
    ]
    if descriptor.deploy is not None:
        lines.append(f"deploy: {descriptor.deploy.target}")
    return tuple(lines)


def _check_lines(cached: dict[str, CheckResult] | None) -> tuple[str, ...]:
    """Render last-known check results, one line per task."""
    if not cached:
        return ("never checked",)
    lines = []
    for task in sorted(cached):
        result = cached[task]
        suffix = f" — {result.detail}" if result.detail else ""
        stamp = f" ({result.checked_at})" if result.checked_at else ""
        lines.append(f"{task}: {result.status}{suffix}{stamp}")
    return tuple(lines)


def build_detail(
    descriptor: ProjectDescriptor, cached: dict[str, CheckResult] | None = None
) -> ProjectDetail:
    """Join one project's descriptor, checks, commits, and memory.

    Args:
        descriptor: The project to detail.
        cached: Last-known check results for the project, if any.

    Returns:
        The renderable detail payload; never raises.
    """
    memory = load_project_memory(descriptor)
    return ProjectDetail(
        project=descriptor.name,
        summary=_summary_lines(descriptor),
        checks=_check_lines(cached),
        commits=recent_commits(descriptor.path),
        memory=tuple(f"{f.name} — {f.description}" for f in memory.files) or ("no memory facts",),
    )


def render_detail(detail: ProjectDetail) -> list[str]:
    """Flatten a detail payload into display lines (pure).

    Args:
        detail: Output of :func:`build_detail`.

    Returns:
        Section-headed lines shared by the TUI pane and the controller.
    """
    sections = (
        ("descriptor", detail.summary),
        ("checks", detail.checks),
        ("recent commits", detail.commits),
        ("memory", detail.memory),
    )
    lines = [f"# {detail.project}"]
    for title, body in sections:
        lines.append(f"## {title}")
        lines.extend(f"  {line}" for line in body)
    return lines
