"""Project detail: descriptor + checks + commits + memory in one payload."""

from __future__ import annotations

from pathlib import Path

from conftest import add_memory, git_init, make_project, make_project_v2

from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.detail import build_detail, recent_commits, render_detail


def test_recent_commits_lists_subjects(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    git_init(project)
    assert any("init" in line for line in recent_commits(project))


def test_recent_commits_non_git_degrades(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    assert recent_commits(project) == ("no commit history (not a git repository?)",)


def test_build_detail_summary_includes_language(fleet_dir: Path) -> None:
    detail = build_detail(load_descriptor(make_project(fleet_dir, "alpha")))
    assert "language: python" in detail.summary


def test_build_detail_v2_summary_includes_deploy_target(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    assert "deploy: fly" in build_detail(descriptor).summary


def test_build_detail_without_cache_says_never_checked(fleet_dir: Path) -> None:
    detail = build_detail(load_descriptor(make_project(fleet_dir, "alpha")))
    assert detail.checks == ("never checked",)


def test_build_detail_renders_cached_checks(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    cached = {"lint": CheckResult(project="alpha", task="lint", status="pass")}
    assert build_detail(descriptor, cached).checks == ("lint: pass",)


def test_build_detail_lists_memory_facts(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "fact.md", name="Fact", description="a fact")
    detail = build_detail(load_descriptor(project))
    assert detail.memory == ("Fact — a fact",)


def test_build_detail_no_memory_degrades(fleet_dir: Path) -> None:
    detail = build_detail(load_descriptor(make_project(fleet_dir, "alpha")))
    assert detail.memory == ("no memory facts",)


def test_render_detail_starts_with_project_heading(fleet_dir: Path) -> None:
    lines = render_detail(build_detail(load_descriptor(make_project(fleet_dir, "alpha"))))
    assert lines[0] == "# alpha"


def test_render_detail_has_all_sections(fleet_dir: Path) -> None:
    lines = render_detail(build_detail(load_descriptor(make_project(fleet_dir, "alpha"))))
    headings = [line for line in lines if line.startswith("## ")]
    assert headings == ["## descriptor", "## checks", "## recent commits", "## memory"]
