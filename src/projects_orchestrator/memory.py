"""Read and search the fleet's memory — the "all-knowing" layer.

Every project-init project keeps small structured facts under its memory
directory (``.claude/memory/*.md`` with ``name``/``description``/``type``
frontmatter, indexed by ``MEMORY.md``). The orchestrator reads that contract
across the whole fleet, so one query answers "what do my projects know
about X?" without opening any of them. Reading never raises; malformed
files degrade to untyped entries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from projects_orchestrator.descriptor import TIER_GRAPH, TIER_RAG, ProjectDescriptor

_INDEX_FILES = {"MEMORY.md", "SCHEMA.md", "README.md"}

_MAX_FILE_BYTES = 262_144

_MAX_GRAPH_BYTES = 4_194_304

# Retrieval surfaces, in the degrade-by-tier order of ADR-025 §4: a reader picks
# the richest surface its tier provides and degrades to the grep baseline, whose
# anchors never move — so a tier-0 read stays correct against a tier-3 child.
MODE_RAG = "rag"
MODE_GRAPH = "graph"
MODE_GREP = "grep"

# Node keys a graphify export may use for a fact's title / prose. The graph is
# produced by an external tool whose schema is not part of the frozen contract,
# so the reader is deliberately tolerant: it takes the first key it finds and
# degrades to the grep baseline on anything it cannot read.
_GRAPH_NAME_KEYS = ("name", "label", "title", "id")
_GRAPH_TEXT_KEYS = ("description", "summary", "text", "body", "content")


@dataclass(frozen=True)
class MemoryFile:
    """One memory fact file from one project.

    Attributes:
        project: Owning project name.
        path: Absolute path to the file.
        name: Frontmatter title (file stem when missing).
        description: Frontmatter one-line summary.
        type: ``user`` | ``feedback`` | ``project`` | ``reference`` | ``unknown``.
        body: Markdown body without the frontmatter block.
    """

    project: str
    path: Path
    name: str
    description: str = ""
    type: str = "unknown"
    body: str = ""


@dataclass(frozen=True)
class ProjectMemory:
    """Everything one project remembers.

    Attributes:
        project: Project name.
        memory_path: The memory directory that was read.
        files: Parsed fact files (index/schema files excluded).
        index_present: Whether ``MEMORY.md`` exists.
        warnings: Non-fatal read problems.
    """

    project: str
    memory_path: Path | None
    files: tuple[MemoryFile, ...] = ()
    index_present: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryHit:
    """One search match.

    Attributes:
        file: The memory file that matched.
        line_number: 1-based line of the match within the body (0 = metadata).
        line: The matching line (or the description for metadata hits).
        score: Rank weight — higher sorts first.
    """

    file: MemoryFile
    line_number: int
    line: str
    score: int = 0


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``---`` YAML frontmatter from the markdown body."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}, parts[2]
    if not isinstance(meta, dict):
        return {}, parts[2]
    return {str(k): str(v) for k, v in meta.items()}, parts[2]


def _read_memory_file(path: Path, project: str) -> MemoryFile | None:
    """Parse one memory markdown file; ``None`` when unreadable."""
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, body = _split_frontmatter(text)
    return MemoryFile(
        project=project,
        path=path,
        name=meta.get("name", path.stem),
        description=meta.get("description", ""),
        type=meta.get("type", "unknown"),
        body=body.strip(),
    )


def load_project_memory(descriptor: ProjectDescriptor) -> ProjectMemory:
    """Read one project's memory directory; never raises.

    Args:
        descriptor: The project whose memory to load.

    Returns:
        The project's memory; a missing directory yields an empty result
        with a warning rather than an error.
    """
    memory_path = descriptor.memory_path
    if memory_path is None or not memory_path.is_dir():
        return ProjectMemory(
            project=descriptor.name,
            memory_path=memory_path,
            warnings=("no memory directory",),
        )

    files: list[MemoryFile] = []
    warnings: list[str] = []
    for path in sorted(memory_path.glob("*.md")):
        if path.name in _INDEX_FILES:
            continue
        parsed = _read_memory_file(path, descriptor.name)
        if parsed is None:
            warnings.append(f"unreadable memory file: {path.name}")
        else:
            files.append(parsed)

    return ProjectMemory(
        project=descriptor.name,
        memory_path=memory_path,
        files=tuple(files),
        index_present=(memory_path / "MEMORY.md").is_file(),
        warnings=tuple(warnings),
    )


def retrieval_mode(descriptor: ProjectDescriptor) -> str:
    """Pick a project's memory retrieval surface from its tier (pure).

    The ADR-025 §4 reader rule: ``tier ≥ 3`` with an endpoint queries RAG;
    ``tier ≥ 2`` with a graph reads the graph; everything else greps the
    ``memory_path`` baseline. A higher tier only *offers* a richer surface —
    when the surface is undeclared (a tier-3 child that has not run its RAG
    setup, say) the mode degrades to the next one down.

    Args:
        descriptor: The project to choose a retrieval surface for.

    Returns:
        One of :data:`MODE_RAG`, :data:`MODE_GRAPH`, :data:`MODE_GREP`.
    """
    if descriptor.memory_tier >= TIER_RAG and descriptor.rag_endpoint:
        return MODE_RAG
    if descriptor.memory_tier >= TIER_GRAPH and descriptor.graph_path is not None:
        return MODE_GRAPH
    return MODE_GREP


def _first_str(node: dict[str, object], keys: tuple[str, ...]) -> str:
    """Return the first non-empty string value among ``keys`` (pure)."""
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def load_graph_facts(descriptor: ProjectDescriptor) -> tuple[MemoryFile, ...]:
    """Read a graphify graph's nodes as memory facts; never raises.

    Best-effort against the external graph schema: accepts a top-level list of
    nodes or a ``{"nodes": [...]}`` object, reads a name and prose from the
    common node keys, and returns nothing for an unreadable, oversized, or
    unrecognized graph (the caller keeps the grep baseline).
    """
    path = descriptor.graph_path
    if path is None:
        return ()
    try:
        if path.stat().st_size > _MAX_GRAPH_BYTES:
            return ()
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return ()
    nodes = data.get("nodes") if isinstance(data, dict) else data
    if not isinstance(nodes, list):
        return ()
    facts: list[MemoryFile] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = _first_str(node, _GRAPH_NAME_KEYS)
        if not name:
            continue
        description = _first_str(node, _GRAPH_TEXT_KEYS)
        facts.append(
            MemoryFile(
                project=descriptor.name,
                path=path,
                name=name,
                description=description,
                type="graph",
                body=description,
            )
        )
    return tuple(facts)


def load_memory(descriptor: ProjectDescriptor) -> ProjectMemory:
    """Load one project's memory via its tier's retrieval surface; never raises.

    The grep baseline (``memory_path`` files) is always read — it is the
    anchor that never moves. At ``tier ≥ 2`` the graph's facts are *added* to
    it, so a query still finds everything grep would plus what only the graph
    knows. (RAG, :data:`MODE_RAG`, is a live query handled by the caller; this
    loader returns the local surfaces it can read without a network call.)
    """
    base = load_project_memory(descriptor)
    if descriptor.memory_tier >= TIER_GRAPH and descriptor.graph_path is not None:
        return replace(base, files=base.files + load_graph_facts(descriptor))
    return base


def _score_metadata(memory_file: MemoryFile, needle: str) -> MemoryHit | None:
    """Match against name/description — the highest-signal surfaces."""
    if needle in memory_file.name.lower():
        return MemoryHit(file=memory_file, line_number=0, line=memory_file.description, score=3)
    if needle in memory_file.description.lower():
        return MemoryHit(file=memory_file, line_number=0, line=memory_file.description, score=2)
    return None


def search_memory(memories: list[ProjectMemory], query: str) -> list[MemoryHit]:
    """Search all loaded memories for a case-insensitive substring.

    Args:
        memories: Per-project memories (see :func:`load_project_memory`).
        query: Text to look for in names, descriptions, and bodies.

    Returns:
        Hits sorted by score (metadata first), then project and file.
    """
    needle = query.strip().lower()
    if not needle:
        return []

    hits: list[MemoryHit] = []
    for memory in memories:
        for memory_file in memory.files:
            metadata_hit = _score_metadata(memory_file, needle)
            if metadata_hit is not None:
                hits.append(metadata_hit)
            for number, line in enumerate(memory_file.body.splitlines(), start=1):
                if needle in line.lower():
                    hits.append(
                        MemoryHit(file=memory_file, line_number=number, line=line.strip(), score=1)
                    )

    hits.sort(key=lambda h: (-h.score, h.file.project.lower(), h.file.path.name, h.line_number))
    return hits
