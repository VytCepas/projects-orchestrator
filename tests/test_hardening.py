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


def test_missing_hooks_action_targets_agents_layout(fleet_dir: Path) -> None:
    # The hooks item's whole job is to name the script to run. It named
    # `.claude/scripts/install_hooks.sh` on every project regardless of layout —
    # a path that does not exist on a PI-627 scaffold, where the lifecycle
    # scripts live under `.agents/`. An action line that 404s is worse than none.
    project = make_project(fleet_dir, "alpha", layout=".agents")
    _with_uninstalled_hook(project)
    report = checklist([_descriptor(project)], {})
    action = next(item.action for item in report[0].items if item.category == "hooks")
    assert action.endswith(".agents/scripts/install_hooks.sh")


def test_missing_hooks_action_targets_legacy_layout(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", layout=".claude")
    _with_uninstalled_hook(project)
    report = checklist([_descriptor(project)], {})
    action = next(item.action for item in report[0].items if item.category == "hooks")
    assert action.endswith(".claude/scripts/install_hooks.sh")


def test_checklist_flags_missing_memory(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    report = checklist([_descriptor(project)], {})
    assert any(item.category == "memory" for item in report[0].items)


_MEMORY_NONE_CONFIG = """\
project:
  name: "coreproj"
  language: "python"
memory:
  stack: "none"
  tier: 0
"""

_MEMORY_OMITTED_CONFIG = """\
project:
  name: "coreproj"
  language: "python"
"""


def test_a_project_that_declared_no_memory_backend_gets_no_memory_item(fleet_dir: Path) -> None:
    """#208: `core` is a shipped preset whose memory mode is `none`. The only
    way to clear this item was to create the directory the project explicitly
    declined, which makes its own scaffold record a lie — an item that can never
    go green on a correctly configured project."""
    project = make_project(fleet_dir, "coreproj", config_text=_MEMORY_NONE_CONFIG)
    report = checklist([_descriptor(project)], {})
    assert not any(item.category == "memory" for item in report[0].items)


def test_a_project_that_omitted_the_memory_key_still_warns(fleet_dir: Path) -> None:
    """The guard against over-fixing: `unknown` is an ABSENCE, `none` is a
    DECLARATION. Silencing both would hide the case the item exists for."""
    project = make_project(fleet_dir, "coreproj", config_text=_MEMORY_OMITTED_CONFIG)
    report = checklist([_descriptor(project)], {})
    assert any(item.category == "memory" for item in report[0].items)


_CORE_SCAFFOLD_CONFIG = """\
project:
  name: "coreproj"
  project_init_contract_version: 2
language: python
"""


def test_a_core_scaffold_that_rendered_no_memory_block_gets_no_memory_item(
    fleet_dir: Path,
) -> None:
    """#257 (project-init #964 rung 2): a real `core` scaffold does not write
    `stack: none` into a block — it renders NO block, which the contract reads as
    declined. The test above used a block no scaffold emits, so the #208 guard
    passed its own test and never fired on a real `core` project."""
    project = make_project(fleet_dir, "coreproj", config_text=_CORE_SCAFFOLD_CONFIG)
    report = checklist([_descriptor(project)], {})
    assert not any(item.category == "memory" for item in report[0].items)


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
