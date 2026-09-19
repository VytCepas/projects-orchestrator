"""Read each project's capability inventory — the "who exposes what" layer.

Every project-init project ships ``.claude/CAPABILITIES.md`` (ADR-017): a
surface-independent, generated inventory of the skills, hooks, and MCP servers
the scaffold gave the agent. Per ADR-025 §3 the root orchestrator aggregates
that inventory across the fleet so "which projects expose which MCP/skill" is
answerable centrally, without opening any project.

This module is a *reader* of that markdown contract. It parses the section
tables project-init emits and never raises: a missing file is an empty,
``present=False`` result with a warning; a malformed table degrades to the
rows it could parse.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.descriptor import ProjectDescriptor

_log = logging.getLogger(__name__)

CAPABILITIES_RELPATH = Path(".claude/CAPABILITIES.md")

_MAX_FILE_BYTES = 262_144

# Kinds the fleet aggregates. The values are the stable identifiers callers pass
# to :func:`aggregate` / the ``--kind`` flag; the tuples map each kind to the
# ``## `` section-title prefixes project-init emits for it (titles carry a count
# suffix, e.g. ``## Skills (14)``, so match by prefix).
SKILL = "skill"
MCP = "mcp"
HOOK = "hook"

_SECTION_PREFIXES: dict[str, tuple[str, ...]] = {
    SKILL: ("Skills",),
    MCP: ("MCP servers",),
    HOOK: ("Hooks",),
}

_SEPARATOR_CELL = re.compile(r":?-+:?")

# Split a table row on pipes that are not escaped (``\|``), so a description
# containing a literal pipe stays in one cell.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


@dataclass(frozen=True)
class Capability:
    """One inventoried capability from one project.

    Attributes:
        kind: ``skill`` | ``mcp`` | ``hook``.
        name: The capability's name (skill name, server name, or hook event).
        detail: The description column when the table has one, else every
            column after the name (an invocation or a script).
        source: The ``Source`` column — ``plugin`` or ``in-tree`` — that
            project-init 1.2.2 added to the skills table (its #962); empty for an
            inventory that predates the column, and for MCP and hook tables.
    """

    kind: str
    name: str
    detail: str = ""
    source: str = ""


@dataclass(frozen=True)
class ProjectCapabilities:
    """One project's parsed capability inventory.

    Attributes:
        project: Owning project name.
        path: The ``CAPABILITIES.md`` that was read (or would have been).
        present: Whether the inventory file existed and was readable.
        skills: Skills the scaffold ships.
        mcp_servers: MCP servers the scaffold wires.
        hooks: Hooks the scaffold wires (event → script).
        warnings: Non-fatal read problems.
    """

    project: str
    path: Path
    present: bool = False
    skills: tuple[Capability, ...] = ()
    mcp_servers: tuple[Capability, ...] = ()
    hooks: tuple[Capability, ...] = ()
    warnings: tuple[str, ...] = ()

    def of_kind(self, kind: str) -> tuple[Capability, ...]:
        """Return this project's capabilities of one ``kind``."""
        return {SKILL: self.skills, MCP: self.mcp_servers, HOOK: self.hooks}.get(kind, ())


def _split_sections(text: str) -> list[tuple[str, list[str]]]:
    """Split markdown into ``(h2 title, body lines)`` pairs (pure).

    Only ``## `` headings start a section; deeper headings (``### GUI surface
    hooks``) stay inside their parent section's body, which is fine — their
    tables are parsed too, and the fleet view only aggregates the h2 kinds.
    """
    sections: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections.append(current)
            current = (line.removeprefix("## ").strip(), [])
        elif current is not None:
            current[1].append(line)
    if current is not None:
        sections.append(current)
    return sections


def _parse_table(lines: list[str]) -> list[tuple[str, str, str]]:
    r"""Parse the first markdown table in ``lines`` into ``(name, detail, source)`` rows.

    Skips the separator, unescapes ``\|`` in cells, and stops at the first
    non-table line after the table began.

    COLUMNS ARE CHOSEN BY HEADER (#259). project-init 1.2.2 inserted a ``Source``
    column into the skills table, and joining every cell after the name turned
    each description into ``plugin | Records…`` — a silent change to what
    ``capabilities --json`` means for every current scaffold. So ``detail`` is
    the ``Description`` column, running to the end of the row as before, and
    ``source`` is the ``Source`` column. A table with neither header (hooks,
    MCP servers, a pre-1.2.2 inventory) keeps the old reading: every cell after
    the name.
    """
    rows: list[tuple[str, str, str]] = []
    started = False
    header: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if started:
                break
            continue
        cells = [
            cell.strip().replace("\\|", "|")
            for cell in _UNESCAPED_PIPE.split(stripped.strip().strip("|"))
        ]
        if cells and all(not cell or _SEPARATOR_CELL.fullmatch(cell) for cell in cells):
            continue
        if not started:  # the header row (e.g. "| Skill | Source | Description |")
            started = True
            header = [cell.lower() for cell in cells]
            continue
        name = cells[0]
        source_at = header.index("source") if "source" in header else None
        if "description" in header:
            detail = " | ".join(cells[header.index("description") :])
        else:
            detail = " | ".join(c for i, c in enumerate(cells) if i and i != source_at)
        source = cells[source_at] if source_at is not None and source_at < len(cells) else ""
        if name:
            rows.append((name, detail, source))
    return rows


def _classify(title: str) -> str | None:
    """Map an h2 section title to a capability kind, or ``None`` if not one."""
    for kind, prefixes in _SECTION_PREFIXES.items():
        if any(title.startswith(prefix) for prefix in prefixes):
            return kind
    return None


def parse_capabilities(text: str, project: str, path: Path) -> ProjectCapabilities:
    """Parse a ``CAPABILITIES.md`` document into a typed inventory (pure).

    Args:
        text: The markdown contents.
        project: Owning project name.
        path: The file the text came from (for the result's ``path``).

    Returns:
        The parsed inventory; unknown sections are ignored, and a document
        with no recognized section yields an empty (but ``present``) result.
    """
    by_kind: dict[str, list[Capability]] = {SKILL: [], MCP: [], HOOK: []}
    for title, body in _split_sections(text):
        kind = _classify(title)
        if kind is None:
            continue
        by_kind[kind].extend(
            Capability(kind=kind, name=name, detail=detail, source=source)
            for name, detail, source in _parse_table(body)
        )
    return ProjectCapabilities(
        project=project,
        path=path,
        present=True,
        skills=tuple(by_kind[SKILL]),
        mcp_servers=tuple(by_kind[MCP]),
        hooks=tuple(by_kind[HOOK]),
    )


def load_capabilities(descriptor: ProjectDescriptor) -> ProjectCapabilities:
    """Read one project's ``CAPABILITIES.md``; never raises.

    Args:
        descriptor: The project whose inventory to load.

    Returns:
        The parsed inventory; a missing or oversized file yields an empty
        ``present=False`` result with a warning.
    """
    # CAPABILITIES.md lives beside the descriptor — under ``.agents/`` on a
    # PI-627 scaffold, ``.claude/`` on a legacy one (descriptor.config_root).
    path = descriptor.path / descriptor.config_root / CAPABILITIES_RELPATH.name
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return ProjectCapabilities(
                project=descriptor.name, path=path, warnings=("CAPABILITIES.md too large to read",)
            )
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _log.debug("cannot read %s: %r", path, exc)
        return ProjectCapabilities(
            project=descriptor.name, path=path, warnings=("no CAPABILITIES.md",)
        )
    return parse_capabilities(text, descriptor.name, path)


def aggregate(inventories: list[ProjectCapabilities], kind: str) -> dict[str, tuple[str, ...]]:
    """Invert the fleet's inventories into ``capability name → projects``.

    Args:
        inventories: Per-project capability inventories.
        kind: Which capability kind to aggregate (``skill`` | ``mcp`` | ``hook``).

    Returns:
        Each capability name of that kind mapped to the sorted, de-duplicated
        names of the projects that expose it — the "which projects expose which
        MCP/skill" view of ADR-025 §3.
    """
    index: dict[str, set[str]] = {}
    for inventory in inventories:
        for capability in inventory.of_kind(kind):
            index.setdefault(capability.name, set()).add(inventory.project)
    return {name: tuple(sorted(projects)) for name, projects in sorted(index.items())}
