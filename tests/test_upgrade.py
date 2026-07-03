"""Fleet upgrade planning: semver classification and composed rows."""

from __future__ import annotations

from pathlib import Path

from conftest import make_project

from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.upgrade import build_row, plan_status, upgrade_plan


def test_plan_status_ok_when_current() -> None:
    assert plan_status((0, 6, 0), (0, 6, 0)) == "ok"


def test_plan_status_outdated_when_behind() -> None:
    assert plan_status((0, 5, 2), (0, 6, 0)) == "outdated"


def test_plan_status_ahead_is_ok() -> None:
    assert plan_status((0, 7, 0), (0, 6, 0)) == "ok"


def test_plan_status_unknown_when_latest_missing() -> None:
    assert plan_status((0, 5, 2), None) == "unknown"


def test_plan_status_unknown_when_current_missing() -> None:
    assert plan_status(None, (0, 6, 0)) == "unknown"


def test_build_row_marks_outdated_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")  # scaffolded at 0.5.2
    row = build_row(load_descriptor(fleet_dir / "alpha"), (0, 6, 0))
    assert row.status == "outdated"


def test_build_row_reads_cached_pr_count(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    cached = {"prs": CheckResult(project="alpha", task="prs", status="ok", detail="2")}
    row = build_row(load_descriptor(fleet_dir / "alpha"), (0, 6, 0), cached)
    assert row.open_prs == "2"


def test_upgrade_plan_all_unknown_when_upstream_unknown(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    rows = upgrade_plan([load_descriptor(fleet_dir / "alpha")], None)
    assert rows[0].status == "unknown"
