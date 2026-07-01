"""Shared fixtures: synthesize project-init project trees on disk."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

ProjectFactory = Callable[..., Path]
GitRunner = Callable[..., subprocess.CompletedProcess[str]]


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
    tooling: dict[str, str] | None,
) -> str:
    project: dict[str, Any] = {
        "name": name,
        "description": description,
        "created": "2026-06-28",
        "project_init_version": project_init_version,
    }
    if contract_version is not None:
        project["project_init_contract_version"] = contract_version
    config: dict[str, Any] = {
        "project": project,
        "language": language,
        "delivery": delivery,
        "memory": {
            "tier": memory_tier,
            "stack": memory_stack,
            "memory_path": ".claude/memory",
        },
        "mcps": {"installed": list(mcps)},
    }
    if tooling is not None:
        config["tooling"] = tooling
    return yaml.safe_dump(config, sort_keys=False)


@pytest.fixture
def git() -> GitRunner:
    """Return a helper that runs a git command in a given directory.

    Returns:
        A callable ``git(cwd, *args)`` returning the completed process.
    """

    def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )

    return _git


@pytest.fixture
def git_init(git: GitRunner) -> Callable[[Path], None]:
    """Return a helper that initializes a committed git repo at a path.

    Args:
        git: The ``git`` runner fixture.

    Returns:
        A callable ``git_init(root)`` that makes ``root`` a repo with one commit.
    """

    def _init(root: Path) -> None:
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "test@example.com")
        git(root, "config", "user.name", "Test")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "initial")

    return _init


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
        tooling: dict[str, str] | None = None,
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
                tooling=tooling,
            ),
            encoding="utf-8",
        )
        memory = claude / "memory"
        memory.mkdir(exist_ok=True)
        if with_memory_index:
            (memory / "MEMORY.md").write_text("- index\n", encoding="utf-8")
        return root

    return _factory
