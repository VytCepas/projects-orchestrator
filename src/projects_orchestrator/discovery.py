"""Discover sibling projects scaffolded with project-init.

A project-init project is identified by a ``.claude/project-init.md`` marker.
This module reads that marker plus local git state so the orchestrator can
present a cross-project overview without touching the network.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

MARKER = Path(".claude") / "project-init.md"
NA = "—"

_TITLE_RE = re.compile(r"^#\s*Project:\s*(.+?)\s*$", re.MULTILINE)
_QUOTE_RE = re.compile(r"^>\s*(.+?)\s*$", re.MULTILINE)
_ROW_RE = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Project:
    """A single project-init project and its current local state.

    Attributes:
        name: Human-readable project name from the marker (or directory name).
        path: Absolute path to the project root.
        description: One-line description from the marker blockquote.
        language: Primary language recorded in the marker metadata table.
        memory_stack: Memory backend recorded in the marker metadata table.
        mcps: MCP servers recorded in the marker metadata table.
        branch: Current git branch, or ``"—"`` when unavailable.
        dirty: Whether the working tree has uncommitted changes.
        last_commit: Subject line of the most recent commit.
    """

    name: str
    path: Path
    description: str
    language: str
    memory_stack: str
    mcps: str
    branch: str
    dirty: bool
    last_commit: str


def _git(path: Path, *args: str) -> str | None:
    """Run a git command in ``path`` and return trimmed stdout, or None."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _parse_marker(text: str) -> dict[str, str]:
    """Extract name, description, and metadata rows from a marker file."""
    fields: dict[str, str] = {}

    title = _TITLE_RE.search(text)
    if title:
        fields["name"] = title.group(1)

    quote = _QUOTE_RE.search(text)
    if quote:
        fields["description"] = quote.group(1)

    for key, value in _ROW_RE.findall(text):
        normalized = key.strip().lower()
        if normalized in {"language", "memory stack", "mcps"}:
            fields[normalized] = value.strip()
    return fields


def _read_project(root: Path) -> Project:
    """Build a :class:`Project` from a marker directory."""
    fields = _parse_marker((root / MARKER).read_text(encoding="utf-8"))
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    porcelain = _git(root, "status", "--porcelain")
    last_commit = _git(root, "log", "-1", "--format=%s")
    return Project(
        name=fields.get("name", root.name),
        path=root,
        description=fields.get("description", ""),
        language=fields.get("language", "—"),
        memory_stack=fields.get("memory stack", "—"),
        mcps=fields.get("mcps", "—"),
        branch=branch or NA,
        dirty=bool(porcelain),
        last_commit=last_commit or NA,
    )


def status_of(project: Project) -> str:
    """Return the working-tree status label for a project.

    Args:
        project: The project to classify.

    Returns:
        ``"unversioned"`` when the directory is not a git repository,
        otherwise ``"dirty"`` or ``"clean"``.
    """
    if project.branch == NA:
        return "unversioned"
    return "dirty" if project.dirty else "clean"


def discover(root: Path, *, max_depth: int = 2) -> list[Project]:
    """Find project-init projects under ``root``.

    Args:
        root: Directory to scan for ``.claude/project-init.md`` markers.
        max_depth: How many directory levels below ``root`` to search.

    Returns:
        Projects sorted by name. Directories without a marker are ignored.
    """
    root = root.expanduser().resolve()
    projects: list[Project] = []
    seen: set[Path] = set()
    for depth in range(max_depth + 1):
        for marker in root.glob("/".join(["*"] * depth + [str(MARKER)]) if depth else str(MARKER)):
            project_root = marker.parent.parent
            if project_root in seen:
                continue
            seen.add(project_root)
            projects.append(_read_project(project_root))
    return sorted(projects, key=lambda p: p.name.lower())
