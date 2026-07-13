"""Shared fixtures: build real child projects (and git repos) on disk."""

from __future__ import annotations

import json
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
  memory_path: {memory_path}

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
    layout: str = ".claude",
) -> Path:
    """Create a minimal project-init-shaped project directory.

    Args:
        base: Parent directory to create the project under.
        name: Project (directory) name.
        tooling: Task → shell command map written as ``<task>_command``.
        config_text: Full config.yaml override (ignores ``tooling``).
        layout: Scaffold layout dir the descriptor lives in — ``.agents`` for a
            PI-627 scaffold, ``.claude`` for a legacy one.

    Returns:
        The project root path.
    """
    project = base / name
    config_dir = project / layout
    config_dir.mkdir(parents=True)
    if config_text is None:
        tooling = tooling if tooling is not None else {"lint": "true"}
        lines = "".join(f'  {task}_command: "{cmd}"\n' for task, cmd in tooling.items())
        # Mirror a real scaffold: memory lives beside the descriptor, under the
        # same layout dir (``.agents/memory`` on PI-627, ``.claude/memory`` legacy).
        config_text = CONFIG_TEMPLATE.format(
            name=name, tooling=lines or "  {}\n", memory_path=f"{layout}/memory"
        )
    (config_dir / "config.yaml").write_text(config_text, encoding="utf-8")
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


MEMORY_CONFIG_TEMPLATE = """\
project:
  name: "{name}"
  project_init_version: 0.7.0
  project_init_contract_version: 1

language: python
delivery: library

memory:
  tier: {tier}
  stack: {stack}
  memory_path: .claude/memory
{surfaces}

tooling:
  lint_command: "true"
"""


def make_memory_project(
    base: Path,
    name: str,
    tier: int,
    graph_path: str = "",
    rag_endpoint: str = "",
) -> Path:
    """Create a project declaring a memory tier and its retrieval surfaces.

    Args:
        base: Parent directory to create the project under.
        name: Project (directory) name.
        tier: Memory tier written to ``memory.tier``.
        graph_path: ``memory.graph_path`` value (omitted when empty).
        rag_endpoint: ``memory.rag_endpoint`` value (omitted when empty).

    Returns:
        The project root path.
    """
    lines = []
    if graph_path:
        lines.append(f"  graph_path: {graph_path}")
    if rag_endpoint:
        lines.append(f"  rag_endpoint: {rag_endpoint}")
    stack = "obsidian-graphify-rag" if tier >= 3 else "obsidian-graphify"
    return make_project(
        base,
        name,
        config_text=MEMORY_CONFIG_TEMPLATE.format(
            name=name, tier=tier, stack=stack, surfaces="\n".join(lines)
        ),
    )


def add_graph(project: Path, nodes: list[dict[str, str]], relpath: str = "graphify-out/graph.json") -> Path:
    """Write a graphify-shaped ``graph.json`` with the given nodes.

    Args:
        project: Project root to write under.
        nodes: Node dicts (e.g. ``{"name": ..., "description": ...}``).
        relpath: Where to write the graph, relative to the project root.

    Returns:
        The written graph.json path.
    """
    path = project / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"nodes": nodes}), encoding="utf-8")
    return path


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


CAPABILITIES_TEMPLATE = """\
# Capabilities

## Skills ({skill_count})

| Skill | Description |
|---|---|
{skills}

## Hooks

| Event | Script |
|---|---|
{hooks}

## MCP servers ({mcp_count})

{mcps}
"""


def add_capabilities(
    project: Path,
    skills: list[str] | None = None,
    mcp_servers: list[str] | None = None,
    hooks: list[tuple[str, str]] | None = None,
) -> Path:
    """Write a project-init-shaped ``.claude/CAPABILITIES.md`` inventory.

    Args:
        project: Project root to write under.
        skills: Skill names (each gets a placeholder description).
        mcp_servers: MCP server names (each gets a placeholder invocation).
        hooks: ``(event, script)`` pairs.

    Returns:
        The written CAPABILITIES.md path.
    """
    skills = skills if skills is not None else ["plan", "status"]
    mcp_servers = mcp_servers if mcp_servers is not None else []
    hooks = hooks if hooks is not None else [("PreToolUse", "prod_guard.py")]
    skill_rows = "\n".join(f"| {name} | does {name} |" for name in skills)
    hook_rows = "\n".join(f"| {event} | {script} |" for event, script in hooks)
    if mcp_servers:
        mcp_rows = "| Server | Invocation |\n|---|---|\n" + "\n".join(
            f"| {name} | bunx {name} |" for name in mcp_servers
        )
    else:
        mcp_rows = "_None selected._"
    claude_dir = project / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    path = claude_dir / "CAPABILITIES.md"
    path.write_text(
        CAPABILITIES_TEMPLATE.format(
            skill_count=len(skills),
            skills=skill_rows,
            hooks=hook_rows,
            mcp_count=len(mcp_servers),
            mcps=mcp_rows,
        ),
        encoding="utf-8",
    )
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
