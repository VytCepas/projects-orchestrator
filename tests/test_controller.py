"""Deterministic controller: parsing table and dispatch flows."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import add_memory, make_project

from projects_orchestrator.controller import ControllerContext, dispatch, parse_command
from projects_orchestrator.registry import FleetConfig


@pytest.mark.parametrize(
    ("text", "verb"),
    [
        ("help", "help"),
        ("", "help"),
        ("status", "status"),
        ("lint", "check"),
        ("test alpha", "check"),
        ("checks all", "check"),
        ("run build alpha", "run"),
        ("memory postgres", "memory"),
        ("drift", "drift"),
        ("doctor", "doctor"),
        ("doctor alpha", "doctor"),
        ("audit", "audit"),
        ("audit alpha", "audit"),
        ("ci", "ci"),
        ("ci alpha", "ci"),
        ("projects", "projects"),
        ("refresh", "refresh"),
        ("quit", "quit"),
        ("exit", "quit"),
        ("/ask what is broken", "ask"),
        ("frobnicate", "unknown"),
    ],
)
def test_parse_command_maps_verb(text: str, verb: str) -> None:
    assert parse_command(text).verb == verb


def test_parse_command_checks_expands_to_both_tasks() -> None:
    assert parse_command("checks").args == ("lint", "test")


def test_parse_command_defaults_target_to_all() -> None:
    assert parse_command("lint").target == "all"


def test_parse_command_run_extracts_task() -> None:
    assert parse_command("run build alpha").args == ("build",)


def test_parse_command_memory_joins_query_words() -> None:
    assert parse_command("memory deploy target").args == ("deploy target",)


def _ctx(fleet_dir: Path, cache_file: Path | None = None) -> ControllerContext:
    return ControllerContext(
        config=FleetConfig(roots=(fleet_dir,)),
        cache_file=cache_file or fleet_dir / "checks.json",
    )


def test_dispatch_lint_reports_pass(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    lines = list(dispatch(parse_command("lint alpha"), _ctx(fleet_dir)))
    assert lines == ["alpha lint: PASS"]


def test_dispatch_lint_reports_fail_with_detail(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "echo boom >&2; false"})
    lines = list(dispatch(parse_command("lint alpha"), _ctx(fleet_dir)))
    assert "FAIL" in lines[0]


def test_dispatch_check_unknown_project_lists_known(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("lint nope"), _ctx(fleet_dir)))
    assert "unknown project: nope" in lines[0]


def test_dispatch_check_all_covers_every_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    make_project(fleet_dir, "beta", tooling={"lint": "true"})
    lines = list(dispatch(parse_command("lint all"), _ctx(fleet_dir)))
    assert len(lines) == 2


def test_dispatch_check_persists_results_to_cache(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    cache_file = fleet_dir / "checks.json"
    list(dispatch(parse_command("lint alpha"), _ctx(fleet_dir, cache_file)))
    assert cache_file.is_file()


def test_dispatch_status_table_has_header(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("status"), _ctx(fleet_dir)))
    assert lines[0].startswith("Project")


def test_dispatch_status_single_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("status alpha"), _ctx(fleet_dir)))
    assert lines[0].startswith("alpha:")


def test_dispatch_memory_finds_fact(fleet_dir: Path) -> None:
    add_memory(make_project(fleet_dir, "alpha"), "project_context.md", body="deploys to fly.io")
    lines = list(dispatch(parse_command("memory fly.io"), _ctx(fleet_dir)))
    assert "fly.io" in lines[0]


def test_dispatch_memory_no_match_is_friendly(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("memory unicorns"), _ctx(fleet_dir)))
    assert lines == ["no memory matches for: unicorns"]


def test_dispatch_projects_lists_names(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert list(dispatch(parse_command("projects"), _ctx(fleet_dir))) == ["alpha"]


def test_dispatch_refresh_picks_up_new_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    ctx = _ctx(fleet_dir)
    make_project(fleet_dir, "beta")
    lines = list(dispatch(parse_command("refresh"), ctx))
    assert "2 project(s)" in lines[0]


def test_dispatch_drift_reports_per_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("drift"), _ctx(fleet_dir)))
    assert lines == ["alpha: -"]


def test_dispatch_doctor_reports_project_status(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("doctor"), _ctx(fleet_dir)))
    assert lines[0] == "alpha: warn"


def test_dispatch_audit_reports_project_status(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("audit"), _ctx(fleet_dir)))
    assert lines[0] == "alpha: warn"


def test_dispatch_ci_reports_per_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("ci"), _ctx(fleet_dir)))
    assert lines[0].startswith("alpha: CI ")


def test_dispatch_ask_is_disabled(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("/ask anything"), _ctx(fleet_dir)))
    assert "not enabled" in lines[0]


def test_dispatch_unknown_points_to_help(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("frobnicate"), _ctx(fleet_dir)))
    assert "try: help" in lines[0]
