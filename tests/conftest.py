"""Shared fixtures: build real child projects (and git repos) on disk."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

CONFIG_TEMPLATE = """\
project:
  name: "{name}"
  description: "test project"
  project_init_version: 0.5.2
  project_init_contract_version: 1

language: python
delivery: library

memory:
  tier: 0
  memory_path: .claude/memory

tooling:
{tooling}
"""

CONFIG_TEMPLATE_V2 = """\
project:
  name: "{name}"
  description: "test project"
  project_init_version: 0.7.0
  project_init_contract_version: 2

language: python
delivery: service

memory:
  tier: 0
  memory_path: .claude/memory

tooling:
  lint_command: "true"
  run_command: "true"

deploy:
  target: {deploy_target}
  app: {name}-svc
  region: fra
  health_url: "{health_url}"

observability:
  path: {observability_path}

hooks:
  expected: [pre-commit, commit-msg]
"""

MEMORY_TEMPLATE = """\
---
name: {name}
description: {description}
type: {type}
---

{body}
"""


def make_project(
    base: Path,
    name: str,
    tooling: dict[str, str] | None = None,
    config_text: str | None = None,
) -> Path:
    """Create a minimal project-init-shaped project directory.

    Args:
        base: Parent directory to create the project under.
        name: Project (directory) name.
        tooling: Task → shell command map written as ``<task>_command``.
        config_text: Full config.yaml override (ignores ``tooling``).

    Returns:
        The project root path.
    """
    project = base / name
    claude_dir = project / ".claude"
    claude_dir.mkdir(parents=True)
    if config_text is None:
        tooling = tooling if tooling is not None else {"lint": "true"}
        lines = "".join(f'  {task}_command: "{cmd}"\n' for task, cmd in tooling.items())
        config_text = CONFIG_TEMPLATE.format(name=name, tooling=lines or "  {}\n")
    (claude_dir / "config.yaml").write_text(config_text, encoding="utf-8")
    return project


def make_project_v2(
    base: Path,
    name: str,
    deploy_target: str = "none",
    health_url: str = "",
    observability_path: str = ".claude/observability",
) -> Path:
    """Create a contract-v2 project directory (deploy/observability/hooks).

    Args:
        base: Parent directory to create the project under.
        name: Project (directory) name.
        deploy_target: Value for the ``deploy.target`` field.
        health_url: Value for the ``deploy.health_url`` field.
        observability_path: Value for the ``observability.path`` field.

    Returns:
        The project root path.
    """
    return make_project(
        base,
        name,
        config_text=CONFIG_TEMPLATE_V2.format(
            name=name,
            deploy_target=deploy_target,
            health_url=health_url,
            observability_path=observability_path,
        ),
    )


def add_memory(project: Path, filename: str, **fields: str) -> Path:
    """Write one schema-conformant memory file (plus MEMORY.md index).

    Args:
        project: Project root to write under.
        filename: Memory file name.
        **fields: Frontmatter/body overrides: name, description, type_, body.

    Returns:
        The written memory file path.
    """
    meta = {"name": "Fact", "description": "a fact", "type_": "project"}
    meta |= {k: v for k, v in fields.items() if k in meta}
    body = fields.get("body", "**Why:** because.")
    memory_dir = project / ".claude" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = memory_dir / filename
    path.write_text(
        MEMORY_TEMPLATE.format(
            name=meta["name"], description=meta["description"], type=meta["type_"], body=body
        ),
        encoding="utf-8",
    )
    index = memory_dir / "MEMORY.md"
    index.write_text(f"- [{meta['name']}]({filename}) — {meta['description']}\n", encoding="utf-8")
    return path


def git_init(project: Path, commit: bool = True) -> None:
    """Turn a project directory into a real git repo with one commit."""
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", str(project), *args], check=True, capture_output=True
    )
    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    if commit:
        run("add", "-A")
        run("commit", "-q", "-m", "init", "--allow-empty")


@pytest.fixture()
def fleet_dir(tmp_path: Path) -> Path:
    """A directory to build fleets under."""
    return tmp_path / "fleet"
