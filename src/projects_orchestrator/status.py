"""Collect per-project health/status for the orchestrator's monitor view.

Status is derived cheaply from git (is this a repo, which branch, is the tree
dirty, how far ahead/behind upstream) plus the descriptor. It never raises on a
non-repo or a git hiccup — a project the orchestrator cannot read git for is
reported as ``no-git`` rather than crashing the whole sweep.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.descriptor import ProjectDescriptor


@dataclass(frozen=True)
class GitStatus:
    """Git working-tree state for a project.

    Attributes:
        is_repo: Whether the project root is inside a git work tree.
        branch: Current branch name, or ``None`` when unknown/detached.
        dirty: Whether the working tree has uncommitted changes.
        ahead: Commits ahead of the upstream branch (0 when no upstream).
        behind: Commits behind the upstream branch (0 when no upstream).
    """

    is_repo: bool
    branch: str | None
    dirty: bool
    ahead: int
    behind: int


@dataclass(frozen=True)
class ProjectStatus:
    """A project's descriptor paired with its live status.

    Attributes:
        descriptor: The project's parsed descriptor.
        git: The project's git status.
    """

    descriptor: ProjectDescriptor
    git: GitStatus

    @property
    def health(self) -> str:
        """Return a one-word health verdict.

        Returns:
            ``no-git`` when not a repo, ``dirty`` when there are uncommitted
            changes, otherwise ``clean``.
        """
        if not self.git.is_repo:
            return "no-git"
        return "dirty" if self.git.dirty else "clean"


def collect_status(descriptor: ProjectDescriptor) -> ProjectStatus:
    """Collect the current status of a single project.

    Args:
        descriptor: The project to inspect.

    Returns:
        The project's :class:`ProjectStatus`.
    """
    return ProjectStatus(descriptor=descriptor, git=_git_status(descriptor.root))


def _git(root: Path, *args: str) -> str | None:
    """Run a git command in ``root``, returning stripped stdout or ``None``.

    Args:
        root: Directory to run git in.
        *args: Git arguments.

    Returns:
        Stripped stdout on success; ``None`` if git fails or is unavailable.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _git_status(root: Path) -> GitStatus:
    """Derive :class:`GitStatus` for a project root.

    Args:
        root: Project root path.

    Returns:
        The resolved git status; ``is_repo=False`` when ``root`` is not a repo.
    """
    if _git(root, "rev-parse", "--is-inside-work-tree") != "true":
        return GitStatus(is_repo=False, branch=None, dirty=False, ahead=0, behind=0)
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    dirty = bool(_git(root, "status", "--porcelain"))
    ahead, behind = _ahead_behind(root)
    return GitStatus(is_repo=True, branch=branch, dirty=dirty, ahead=ahead, behind=behind)


def _ahead_behind(root: Path) -> tuple[int, int]:
    """Return ``(ahead, behind)`` counts versus the upstream branch.

    Args:
        root: Project root path.

    Returns:
        Ahead/behind commit counts, or ``(0, 0)`` when there is no upstream.
    """
    counts = _git(root, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if not counts:
        return (0, 0)
    parts = counts.split()
    if len(parts) != 2:
        return (0, 0)
    behind, ahead = parts
    return (int(ahead), int(behind))
