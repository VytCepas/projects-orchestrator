"""Fleet snapshot rows and table rendering (pure functions)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from conftest import add_memory, git_init, make_project

from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.fleet import (
    collect_snapshot,
    fleet_rows,
    humanize_age,
    render_table,
    snapshot_row,
)


def _snapshot(fleet_dir: Path, cached: dict | None = None, name: str = "alpha"):
    return collect_snapshot(load_descriptor(fleet_dir / name), cached)


def test_snapshot_row_has_all_columns(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    row = snapshot_row(_snapshot(fleet_dir))
    assert set(row) == {
        "Project", "Health", "Branch", "Sync", "Scaffold", "Drift", "Hooks",
        "Lint", "Tests", "Runnable", "Memory", "Checked",
    }


def test_snapshot_row_scaffold_version(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert snapshot_row(_snapshot(fleet_dir))["Scaffold"] == "0.5.2"


def test_snapshot_row_drift_without_manifest_is_dash(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert snapshot_row(_snapshot(fleet_dir))["Drift"] == "-"


def test_snapshot_row_hooks_without_hooks_dir_is_dash(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert snapshot_row(_snapshot(fleet_dir))["Hooks"] == "-"


def test_snapshot_row_unchecked_lint_is_question_mark(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert snapshot_row(_snapshot(fleet_dir))["Lint"] == "?"


def test_snapshot_row_uses_cached_check_status(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    cached = {"lint": CheckResult(project="alpha", task="lint", status="pass")}
    assert snapshot_row(_snapshot(fleet_dir, cached))["Lint"] == "pass"


def test_snapshot_row_runnable_reflects_run_command(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"run": "echo hi"})
    assert snapshot_row(_snapshot(fleet_dir))["Runnable"] == "yes"


def test_snapshot_row_counts_memory_facts(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "project_context.md")
    assert snapshot_row(_snapshot(fleet_dir))["Memory"] == "1 fact"


def test_snapshot_row_health_for_clean_repo(fleet_dir: Path) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    assert snapshot_row(_snapshot(fleet_dir))["Health"] == "clean"


def test_render_table_contains_project_name(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert "alpha" in render_table(fleet_rows([_snapshot(fleet_dir)]))


def test_render_table_empty_fleet_is_friendly() -> None:
    assert render_table([]) == "no projects discovered"


def test_humanize_age_minutes() -> None:
    now = dt.datetime(2026, 7, 2, 12, 10, tzinfo=dt.UTC)
    assert humanize_age("2026-07-02T12:05:00+00:00", now=now) == "5m"


def test_humanize_age_days() -> None:
    now = dt.datetime(2026, 7, 4, tzinfo=dt.UTC)
    assert humanize_age("2026-07-02T00:00:00+00:00", now=now) == "2d"


def test_humanize_age_empty_is_never() -> None:
    assert humanize_age("") == "never"


def test_humanize_age_garbage_is_never() -> None:
    assert humanize_age("not-a-date") == "never"
