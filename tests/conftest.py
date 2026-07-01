"""Shared fixtures: synthesize project-init project trees on disk."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Callable

import pytest

ProjectFactory = Callable[..., Path]


def _config_yaml(
    *,
    name: str,
    description: str,
    language: str,
    delivery: str,
    memory_tier: int,
    memory_stack: str,
    contract_version: int | None,
    project_init_version: str,
    mcps: list[str],
) -> str:
    contract_line = (
        f"  project_init_contract_version: {contract_version}\n"
        if contract_version is not None
        else ""
    )
    mcp_block = (
        "\n".join(f"    - {m}" for m in mcps) if mcps else "  installed: []"
    )
    installed = f"  installed:\n{mcp_block}" if mcps else mcp_block
    return textwrap.dedent(
        f"""\
        project:
          name: "{name}"
          description: "{description}"
          created: 2026-06-28
          project_init_version: {project_init_version}
        {contract_line}language: {language}

        delivery: {delivery}

        memory:
          tier: {memory_tier}
          stack: {memory_stack}
          memory_path: .claude/memory

        mcps:
        {installed}
        """
    )


@pytest.fixture
def make_project(tmp_path: Path) -> ProjectFactory:
    """Return a factory that writes a project-init project tree under a temp root.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        A callable that materializes ``<root>/<name>/.claude/config.yaml`` (plus a
        memory index) and returns the project root path.
    """

    def _factory(
        name: str = "sample",
        *,
        description: str = "A sample project",
        language: str = "python",
        delivery: str = "library",
        memory_tier: int = 0,
        memory_stack: str = "auto",
        contract_version: int | None = 1,
        project_init_version: str = "0.5.2",
        mcps: list[str] | None = None,
        with_memory_index: bool = True,
        under: Path | None = None,
    ) -> Path:
        base = under if under is not None else tmp_path
        root = base / name
        claude = root / ".claude"
        claude.mkdir(parents=True, exist_ok=True)
        (claude / "config.yaml").write_text(
            _config_yaml(
                name=name,
                description=description,
                language=language,
                delivery=delivery,
                memory_tier=memory_tier,
                memory_stack=memory_stack,
                contract_version=contract_version,
                project_init_version=project_init_version,
                mcps=mcps or [],
            ),
            encoding="utf-8",
        )
        memory = claude / "memory"
        memory.mkdir(exist_ok=True)
        if with_memory_index:
            (memory / "MEMORY.md").write_text("- index\n", encoding="utf-8")
        return root

    return _factory
