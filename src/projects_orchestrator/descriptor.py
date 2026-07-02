"""Read a child project's machine-readable self-description.

Every project scaffolded by project-init ships ``.claude/config.yaml``
(descriptor contract v1): name, language, tooling commands, memory tier and
path. The orchestrator is a *reader* of that contract — it never invents a
parallel one. Parsing never raises; malformed input degrades to defaults and
is surfaced through :attr:`ProjectDescriptor.warnings`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_RELPATH = Path(".claude/config.yaml")

_TOOLING_SUFFIX = "_command"


@dataclass(frozen=True)
class ProjectDescriptor:
    """Everything the orchestrator knows about a project without running it.

    Attributes:
        name: Project name (directory name when the config omits it).
        path: Absolute path to the project root.
        language: Primary language declared at scaffold time.
        delivery: How the project ships (library | service | prototype).
        contract_version: Descriptor-contract schema version (0 when absent).
        project_init_version: Scaffold version the project was rendered with.
        memory_tier: Memory tier (0 auto … 3 obsidian-graphify-rag).
        memory_path: Absolute path to the project's memory directory.
        tooling: Task name → shell command (lint, format, test, run, …).
        warnings: Human-readable parse problems, empty when the config is clean.
    """

    name: str
    path: Path
    language: str = "unknown"
    delivery: str = "unknown"
    contract_version: int = 0
    project_init_version: str = "unknown"
    memory_tier: int = 0
    memory_path: Path | None = None
    tooling: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def has_task(self, task: str) -> bool:
        """Return whether the project declares a runnable command for ``task``."""
        return bool(self.tooling.get(task, "").strip())


def _as_mapping(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a mapping, else an empty dict."""
    return value if isinstance(value, dict) else {}


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce ``value`` to int, falling back to ``default``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_tooling(raw: dict[str, Any]) -> dict[str, str]:
    """Map ``<task>_command`` keys in the ``tooling`` block to task names."""
    tooling: dict[str, str] = {}
    for raw_key, value in _as_mapping(raw.get("tooling")).items():
        key = str(raw_key)
        if key.endswith(_TOOLING_SUFFIX) and isinstance(value, str) and value.strip():
            tooling[key.removesuffix(_TOOLING_SUFFIX)] = value.strip()
    return tooling


def parse_config(text: str, project_dir: Path) -> ProjectDescriptor:
    """Build a descriptor from raw config text (pure; never raises).

    Args:
        text: Contents of ``.claude/config.yaml``.
        project_dir: Project root the config belongs to.

    Returns:
        A descriptor; parse failures degrade to defaults with a warning.
    """
    project_dir = project_dir.resolve()
    warnings: list[str] = []
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raw = None
        warnings.append(f"config.yaml is not valid YAML: {exc}".splitlines()[0])
    raw = _as_mapping(raw)
    if not raw and not warnings:
        warnings.append("config.yaml is empty")

    project = _as_mapping(raw.get("project"))
    memory = _as_mapping(raw.get("memory"))
    memory_rel = str(memory.get("memory_path") or ".claude/memory")

    return ProjectDescriptor(
        name=str(project.get("name") or project_dir.name),
        path=project_dir,
        language=str(raw.get("language") or "unknown"),
        delivery=str(raw.get("delivery") or "unknown"),
        contract_version=_as_int(project.get("project_init_contract_version")),
        project_init_version=str(project.get("project_init_version") or "unknown"),
        memory_tier=_as_int(memory.get("tier")),
        memory_path=(project_dir / memory_rel),
        tooling=_extract_tooling(raw),
        warnings=tuple(warnings),
    )


def load_descriptor(project_dir: Path) -> ProjectDescriptor | None:
    """Load the descriptor for one project directory.

    Args:
        project_dir: Candidate project root.

    Returns:
        The parsed descriptor, or ``None`` when the directory is not a
        project-init project (no readable ``.claude/config.yaml``).
    """
    config_path = project_dir / CONFIG_RELPATH
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_config(text, project_dir)
