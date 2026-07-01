"""Parse a project's descriptor contract from its ``.claude/config.yaml``.

project-init records each project's stable descriptor in ``.claude/config.yaml``
(``project_init_contract_version`` marks the schema a root orchestrator reads).
This module turns that file into a typed :class:`ProjectDescriptor` so the
orchestrator can introspect a fleet of projects without re-parsing raw YAML.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MEMORY_INDEX = "MEMORY.md"


class DescriptorError(Exception):
    """Raised when a descriptor file is missing, malformed, or incomplete."""


@dataclass(frozen=True)
class Tooling:
    """Declared task commands for a project (from the ``tooling`` config block).

    Attributes:
        lint: Lint command, or ``None`` if unset.
        test: Test command, or ``None`` if unset.
        format: Format command, or ``None`` if unset.
    """

    lint: str | None
    test: str | None
    format: str | None

    def commands(self) -> dict[str, str]:
        """Return the set task commands keyed by task name.

        Returns:
            A mapping of task name (``lint`` | ``test`` | ``format``) to command,
            omitting any task that is unset.
        """
        named = {"lint": self.lint, "test": self.test, "format": self.format}
        return {task: cmd for task, cmd in named.items() if cmd}


@dataclass(frozen=True)
class MemoryDescriptor:
    """Resolved memory surface for a project.

    Attributes:
        tier: Memory tier (0 auto ... 3 rag) declared in ``config.yaml``.
        stack: Memory stack name (e.g. ``auto``, ``obsidian``).
        path: Absolute path to the project's memory directory.
        has_index: Whether a ``MEMORY.md`` index exists in that directory.
    """

    tier: int
    stack: str
    path: Path
    has_index: bool


@dataclass(frozen=True)
class ProjectDescriptor:
    """Typed view of a single project-init project.

    Attributes:
        name: Project name.
        description: One-line project description.
        language: Primary language (``python`` | ``node`` | ``go`` | ``none``).
        delivery: How the project ships (``library`` | ``service`` | ``prototype``).
        root: Absolute path to the project root (parent of ``.claude``).
        memory: Resolved memory descriptor.
        contract_version: Descriptor-contract schema version (0 when absent).
        project_init_version: project-init version that scaffolded the project.
        mcps: Installed MCP server names.
        tooling: Declared lint/test/format task commands.
        raw: The full parsed ``config.yaml`` mapping for extension fields.
    """

    name: str
    description: str
    language: str
    delivery: str
    root: Path
    memory: MemoryDescriptor
    contract_version: int
    project_init_version: str
    mcps: tuple[str, ...]
    tooling: Tooling
    raw: dict[str, Any] = field(repr=False)

    def summary(self) -> str:
        """Return a compact one-line summary of the project.

        Returns:
            A human-readable line, e.g. ``alpha (python/library, memory t0, contract v1)``.
        """
        return (
            f"{self.name} ({self.language}/{self.delivery}, "
            f"memory t{self.memory.tier}, contract v{self.contract_version})"
        )


def load_descriptor(config_path: Path) -> ProjectDescriptor:
    """Load and validate a project descriptor from a ``config.yaml`` path.

    Args:
        config_path: Path to a project's ``.claude/config.yaml``.

    Returns:
        The parsed :class:`ProjectDescriptor`.

    Raises:
        DescriptorError: If the file is missing, unreadable, malformed, or is
            missing a required field (currently ``project.name``).
    """
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DescriptorError(f"cannot read descriptor: {config_path}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DescriptorError(f"malformed descriptor: {config_path}") from exc

    if not isinstance(data, dict):
        raise DescriptorError(f"descriptor is not a mapping: {config_path}")

    project = data.get("project") or {}
    name = project.get("name")
    if not name:
        raise DescriptorError(f"descriptor missing project.name: {config_path}")

    root = config_path.parent.parent.resolve()
    memory = _memory_descriptor(root, data.get("memory") or {})

    return ProjectDescriptor(
        name=str(name),
        description=str(project.get("description", "")),
        language=str(data.get("language", "none")),
        delivery=str(data.get("delivery", "none")),
        root=root,
        memory=memory,
        contract_version=int(project.get("project_init_contract_version", 0) or 0),
        project_init_version=str(project.get("project_init_version", "")),
        mcps=tuple((data.get("mcps") or {}).get("installed") or ()),
        tooling=_tooling(data.get("tooling") or {}),
        raw=data,
    )


def _tooling(tooling: dict[str, Any]) -> Tooling:
    """Build a :class:`Tooling` from the ``tooling`` config block.

    Args:
        tooling: The ``tooling`` mapping from ``config.yaml`` (possibly empty).

    Returns:
        A :class:`Tooling` with unset commands left as ``None``.
    """

    def _cmd(key: str) -> str | None:
        value = tooling.get(key)
        return str(value) if value else None

    return Tooling(
        lint=_cmd("lint_command"),
        test=_cmd("test_command"),
        format=_cmd("format_command"),
    )


def _memory_descriptor(root: Path, memory: dict[str, Any]) -> MemoryDescriptor:
    """Resolve the memory block of a descriptor against the project root.

    Args:
        root: Absolute project root path.
        memory: The ``memory`` mapping from ``config.yaml`` (possibly empty).

    Returns:
        A resolved :class:`MemoryDescriptor`.
    """
    rel_path = str(memory.get("memory_path", ".claude/memory"))
    path = (root / rel_path).resolve()
    return MemoryDescriptor(
        tier=int(memory.get("tier", 0) or 0),
        stack=str(memory.get("stack", "auto")),
        path=path,
        has_index=(path / MEMORY_INDEX).is_file(),
    )
