"""Fleet hardening checklist: setup gaps become actionable next steps."""

from __future__ import annotations

from pathlib import Path

from conftest import add_memory, make_project

from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.hardening import checklist, render_text


def _descriptor(project: Path):
    return load_descriptor(project)


def _with_uninstalled_hook(project: Path) -> None:
    source = project / ".github" / "hooks"
    source.mkdir(parents=True)
    (source / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")


def test_checklist_flags_missing_hooks(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    _with_uninstalled_hook(project)
    report = checklist([_descriptor(project)], {})
    assert any(item.category == "hooks" for item in report[0].items)


def test_checklist_flags_missing_memory(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    report = checklist([_descriptor(project)], {})
    assert any(item.category == "memory" for item in report[0].items)


def test_missing_memory_action_targets_agents_layout(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", layout=".agents")
    report = checklist([_descriptor(project)], {})
    action = next(item.action for item in report[0].items if item.category == "memory")
    assert action.endswith(".agents/memory with MEMORY.md")


def test_missing_memory_action_targets_legacy_layout(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", layout=".claude")
    report = checklist([_descriptor(project)], {})
    action = next(item.action for item in report[0].items if item.category == "memory")
    assert action.endswith(".claude/memory with MEMORY.md")


def test_checklist_flags_empty_check_cache(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    report = checklist([_descriptor(project)], {})
    assert any(item.category == "checks" for item in report[0].items)


def test_checklist_flags_cache_with_no_gate_results(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    cached = {"alpha": {"cloud": CheckResult(project="alpha", task="cloud", status="none")}}
    report = checklist([_descriptor(project)], cached)
    assert any(item.category == "checks" for item in report[0].items)


def test_checklist_clean_when_hooks_memory_and_checks_exist(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "project_context.md", body="- **Fact:** ready.")
    cached = {"alpha": {"lint": CheckResult(project="alpha", task="lint", status="pass")}}
    report = checklist([_descriptor(project)], cached)
    assert report[0].items == ()


def test_render_text_groups_gaps_by_project(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    text = render_text(checklist([_descriptor(project)], {}))
    assert "alpha:" in text
    assert "checks:" in text
