"""Capability inventory: CAPABILITIES.md in, fleet 'who exposes what' out."""

from __future__ import annotations

from pathlib import Path

from conftest import add_capabilities, make_project

from projects_orchestrator.capabilities import (
    HOOK,
    MCP,
    SKILL,
    aggregate,
    load_capabilities,
    parse_capabilities,
)
from projects_orchestrator.descriptor import load_descriptor

_REAL_FIXTURE = (
    Path(__file__).parent / "fixtures" / "project_init" / "capabilities.v1.md"
)


def _load(project: Path):
    return load_capabilities(load_descriptor(project))


def test_parse_reads_skill_names() -> None:
    inventory = parse_capabilities(
        "## Skills (1)\n\n| Skill | Description |\n|---|---|\n| plan | plan it |\n",
        "alpha",
        Path("x"),
    )
    assert inventory.skills[0].name == "plan"


def test_parse_reads_skill_detail() -> None:
    inventory = parse_capabilities(
        "## Skills (1)\n\n| Skill | Description |\n|---|---|\n| plan | plan it |\n",
        "alpha",
        Path("x"),
    )
    assert inventory.skills[0].detail == "plan it"


def test_parse_reads_mcp_servers() -> None:
    text = "## MCP servers (1)\n\n| Server | Invocation |\n|---|---|\n| context7 | bunx c7 |\n"
    inventory = parse_capabilities(text, "alpha", Path("x"))
    assert inventory.mcp_servers[0].name == "context7"


def test_parse_reads_hooks() -> None:
    text = "## Hooks\n\n| Event | Script |\n|---|---|\n| PreToolUse | prod_guard.py |\n"
    inventory = parse_capabilities(text, "alpha", Path("x"))
    assert inventory.hooks[0].name == "PreToolUse"


def test_parse_unescapes_pipes_in_detail() -> None:
    text = "## Skills (1)\n\n| Skill | Description |\n|---|---|\n| s | a \\| b |\n"
    inventory = parse_capabilities(text, "alpha", Path("x"))
    assert inventory.skills[0].detail == "a | b"


def test_parse_ignores_none_selected_placeholder() -> None:
    inventory = parse_capabilities("## MCP servers (0)\n\n_None selected._\n", "alpha", Path("x"))
    assert inventory.mcp_servers == ()


def test_parse_ignores_unknown_section() -> None:
    text = "## Chosen options\n\n| Option | Value |\n|---|---|\n| Profile | individual |\n"
    inventory = parse_capabilities(text, "alpha", Path("x"))
    assert inventory.skills == ()


def test_load_missing_file_is_not_present(fleet_dir: Path) -> None:
    assert _load(make_project(fleet_dir, "alpha")).present is False


def test_load_missing_file_warns(fleet_dir: Path) -> None:
    assert _load(make_project(fleet_dir, "alpha")).warnings == ("no CAPABILITIES.md",)


def test_load_present_file_is_present(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_capabilities(project, skills=["plan"])
    assert _load(project).present is True


def test_load_reads_written_skills(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_capabilities(project, skills=["plan", "status", "review"])
    assert [c.name for c in _load(project).skills] == ["plan", "status", "review"]


def test_aggregate_inverts_skill_to_projects() -> None:
    a = parse_capabilities(
        "## Skills (1)\n\n| Skill | Description |\n|---|---|\n| plan | x |\n", "alpha", Path("a")
    )
    b = parse_capabilities(
        "## Skills (1)\n\n| Skill | Description |\n|---|---|\n| plan | x |\n", "beta", Path("b")
    )
    assert aggregate([a, b], SKILL)["plan"] == ("alpha", "beta")


def test_aggregate_dedupes_and_sorts_projects() -> None:
    text = "## MCP servers (1)\n\n| Server | Invocation |\n|---|---|\n| c7 | x |\n"
    a = parse_capabilities(text, "zeta", Path("z"))
    b = parse_capabilities(text, "alpha", Path("a"))
    assert aggregate([a, b], MCP)["c7"] == ("alpha", "zeta")


def test_aggregate_empty_when_no_capabilities_of_kind() -> None:
    a = parse_capabilities(
        "## Skills (1)\n\n| Skill | Description |\n|---|---|\n| plan | x |\n", "alpha", Path("a")
    )
    assert aggregate([a], HOOK) == {}


def test_of_kind_selects_the_right_tuple() -> None:
    inventory = parse_capabilities(
        "## Hooks\n\n| Event | Script |\n|---|---|\n| PreToolUse | g.py |\n", "alpha", Path("x")
    )
    assert inventory.of_kind(HOOK) == inventory.hooks


def test_real_fixture_parses_the_full_skill_set() -> None:
    # The golden project-init render ships 14 skills for a github-lifecycle scaffold.
    inventory = parse_capabilities(
        _REAL_FIXTURE.read_text(encoding="utf-8"), "demo-service", _REAL_FIXTURE
    )
    assert len(inventory.skills) == 14


def test_real_fixture_parses_the_wired_hooks() -> None:
    inventory = parse_capabilities(
        _REAL_FIXTURE.read_text(encoding="utf-8"), "demo-service", _REAL_FIXTURE
    )
    assert ("PreToolUse", "prod_guard.py") in [(h.name, h.detail) for h in inventory.hooks]


def test_real_fixture_has_no_mcp_servers() -> None:
    # demo-service scaffolds with installed_mcps: none.
    inventory = parse_capabilities(
        _REAL_FIXTURE.read_text(encoding="utf-8"), "demo-service", _REAL_FIXTURE
    )
    assert inventory.mcp_servers == ()
