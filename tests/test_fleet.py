"""Fleet snapshot rows and table rendering (pure functions)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from conftest import add_memory, git_init, make_project

from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.fleet import (
    cell_status,
    collect_snapshot,
    fleet_rows,
    fleet_snapshots,
    humanize_age,
    newest_scaffold_version,
    render_table,
    snapshot_row,
)
from projects_orchestrator.registry import FleetConfig, discover


def _snapshot(fleet_dir: Path, cached: dict | None = None, name: str = "alpha"):
    return collect_snapshot(load_descriptor(fleet_dir / name), cached)


def test_cell_status_classifies_good_values() -> None:
    assert cell_status("pass") == "good"


def test_cell_status_classifies_bad_values() -> None:
    assert cell_status("fail") == "bad"


def test_cell_status_classifies_warn_values() -> None:
    assert cell_status("behind") == "warn"


def test_cell_status_uptime_is_good() -> None:
    assert cell_status("up 3d") == "good"


def test_cell_status_unknown_is_neutral() -> None:
    assert cell_status("?") == ""


def test_snapshot_row_has_all_columns(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    row = snapshot_row(_snapshot(fleet_dir))
    assert set(row) == {
        "Project",
        "Health",
        "Branch",
        "Sync",
        "Scaffold",
        "Latest",
        "Contract",
        "Drift",
        "Hooks",
        "Lint",
        "Tests",
        "CI",
        "PRs",
        "Cloud",
        "Runnable",
        "Running",
        "Memory",
        "Checked",
        "Trend",
    }


def test_snapshot_row_scaffold_version(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert snapshot_row(_snapshot(fleet_dir))["Scaffold"] == "0.5.2"


def test_snapshot_row_contract_version(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert snapshot_row(_snapshot(fleet_dir))["Contract"] == "v1"


def test_snapshot_row_contract_none_when_absent(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="project:\n  name: alpha\n")
    assert snapshot_row(_snapshot(fleet_dir))["Contract"] == "none"


def test_snapshot_row_latest_dash_without_fleet_reference(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert snapshot_row(_snapshot(fleet_dir))["Latest"] == "-"


def test_newest_scaffold_version_picks_maximum(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta", config_text="project:\n  project_init_version: 0.6.0\n")
    snapshots = [_snapshot(fleet_dir, name="alpha"), _snapshot(fleet_dir, name="beta")]
    assert newest_scaffold_version(snapshots) == (0, 6, 0)


def test_fleet_rows_flags_project_behind_newest(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta", config_text="project:\n  project_init_version: 0.6.0\n")
    rows = fleet_rows([_snapshot(fleet_dir, name="alpha"), _snapshot(fleet_dir, name="beta")])
    by_name = {row["Project"]: row for row in rows}
    assert by_name["alpha"]["Latest"] == "behind"


def test_fleet_rows_measures_newest_against_the_reference_not_the_subset(
    fleet_dir: Path,
) -> None:
    # THE guard for the filtered-status bug: alpha (older) is the only row shown,
    # but the reference is the whole fleet where beta is newer. Passing only the
    # subset would compute "newest" as alpha's own version and render "=" — the
    # false all-clear that hides a selected project being behind the real fleet.
    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta", config_text="project:\n  project_init_version: 0.6.0\n")
    alpha = _snapshot(fleet_dir, name="alpha")
    beta = _snapshot(fleet_dir, name="beta")

    rows_subset_only = fleet_rows([alpha])
    assert {r["Project"]: r for r in rows_subset_only}["alpha"]["Latest"] == "="  # the trap

    rows_with_reference = fleet_rows([alpha], [alpha, beta])
    assert {r["Project"]: r for r in rows_with_reference}["alpha"]["Latest"] == "behind"


def test_fleet_rows_marks_newest_project_current(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta", config_text="project:\n  project_init_version: 0.6.0\n")
    rows = fleet_rows([_snapshot(fleet_dir, name="alpha"), _snapshot(fleet_dir, name="beta")])
    by_name = {row["Project"]: row for row in rows}
    assert by_name["beta"]["Latest"] == "="


def test_snapshot_row_drift_without_manifest_is_dash(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert snapshot_row(_snapshot(fleet_dir))["Drift"] == "-"


def test_snapshot_row_hooks_without_hooks_dir_is_dash(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert snapshot_row(_snapshot(fleet_dir))["Hooks"] == "-"


def test_snapshot_row_unchecked_lint_is_question_mark(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert snapshot_row(_snapshot(fleet_dir))["Lint"] == "?"


def test_snapshot_row_ci_unprobed_is_question_mark(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert snapshot_row(_snapshot(fleet_dir))["CI"] == "?"


def test_snapshot_row_ci_uses_cached_conclusion(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    cached = {"ci": CheckResult(project="alpha", task="ci", status="pass")}
    assert snapshot_row(_snapshot(fleet_dir, cached))["CI"] == "pass"


def test_snapshot_row_prs_uses_cached_count(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    cached = {"prs": CheckResult(project="alpha", task="prs", status="ok", detail="4")}
    assert snapshot_row(_snapshot(fleet_dir, cached))["PRs"] == "4"


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


def test_humanize_age_naive_timestamp_does_not_crash() -> None:
    now = dt.datetime(2026, 7, 2, 12, 5, tzinfo=dt.UTC)
    assert humanize_age("2026-07-02T12:00:00", now=now) == "5m"


def test_cloud_column_defaults_to_question_mark(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    rows = fleet_rows(fleet_snapshots(fleet, fleet_dir / "no-cache.json"))
    assert rows[0]["Cloud"] == "?"


def test_snapshot_row_trend_from_snapshot(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    snapshot = collect_snapshot(load_descriptor(fleet_dir / "alpha"), None, trend="++x")
    assert snapshot_row(snapshot)["Trend"] == "++x"


def test_snapshot_row_trend_dash_when_empty(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert snapshot_row(_snapshot(fleet_dir))["Trend"] == "-"


def test_fleet_snapshots_reads_history_once(fleet_dir: Path, monkeypatch) -> None:
    # The history log is read once per fleet render, not once per project (no N+1).
    import projects_orchestrator.fleet as fleet_mod

    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta")
    make_project(fleet_dir, "gamma")
    calls = {"n": 0}

    def counting_load_history():
        calls["n"] += 1
        return []

    monkeypatch.setattr(fleet_mod, "load_history", counting_load_history)
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    fleet_snapshots(fleet, fleet_dir / "no-cache.json")
    assert calls["n"] == 1
