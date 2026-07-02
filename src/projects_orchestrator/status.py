"""Per-project git health, degraded gracefully to ``unknown``.

Answers "is this project in a sane state?" without running its gates:
current branch, dirty worktree, ahead/behind upstream, last commit time.
Every git call is timeout-bounded and failure becomes ``unknown`` — the
fleet view must render even for a corrupted or non-git directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.runner import run_command

GIT_TIMEOUT = 15.0


@dataclass(frozen=True)
class ProjectStatus:
    """Git health of one project.

    Attributes:
        project: Project name.
        branch: Current branch, or ``None`` when unknown.
        dirty: Whether the worktree has uncommitted changes (``None`` unknown).
        ahead: Commits ahead of upstream (``None`` when no upstream/unknown).
        behind: Commits behind upstream (``None`` when no upstream/unknown).
        last_commit: ISO timestamp of the last commit, or ``None``.
        detail: Human-readable explanation when health is degraded.
    """

    project: str
    branch: str | None = None
    dirty: bool | None = None
    ahead: int | None = None
    behind: int | None = None
    last_commit: str | None = None
    detail: str = ""

    @property
    def health(self) -> str:
        """One-word health summary: clean | dirty | diverged | behind | ahead | unknown."""
        if self.branch is None:
            return "unknown"
        if self.dirty:
            return "dirty"
        if self.ahead and self.behind:
            return "diverged"
        if self.behind:
            return "behind"
        if self.ahead:
            return "ahead"
        return "clean"


def _git(path: Path, *args: str) -> str | None:
    """Run one git subcommand in ``path``; ``None`` on any failure."""
    result = run_command("git " + " ".join(args), cwd=path, timeout=GIT_TIMEOUT)
    return result.stdout.strip() if result.ok else None


def _ahead_behind(path: Path) -> tuple[int | None, int | None]:
    """Return (ahead, behind) relative to upstream, or (None, None)."""
    counts = _git(path, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if counts is None:
        return None, None
    try:
        behind_str, ahead_str = counts.split()
        return int(ahead_str), int(behind_str)
    except ValueError:
        return None, None


def collect_status(descriptor: ProjectDescriptor) -> ProjectStatus:
    """Collect git health for one project; never raises.

    Args:
        descriptor: The project to inspect.

    Returns:
        The project's status; a non-git or unreadable directory yields
        ``health == "unknown"`` with an explanatory detail.
    """
    path = descriptor.path
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return ProjectStatus(project=descriptor.name, detail="not a git repository")

    porcelain = _git(path, "status", "--porcelain")
    ahead, behind = _ahead_behind(path)
    return ProjectStatus(
        project=descriptor.name,
        branch=branch,
        dirty=None if porcelain is None else bool(porcelain),
        ahead=ahead,
        behind=behind,
        last_commit=_git(path, "log", "-1", "--format=%cI"),
    )
