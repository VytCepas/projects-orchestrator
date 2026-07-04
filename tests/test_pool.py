"""Thread-pool fan-out: ordered, bounded, and actually concurrent."""

from __future__ import annotations

import time
from pathlib import Path

from conftest import git_init, make_project

from projects_orchestrator.__main__ import main
from projects_orchestrator.fleet import fleet_rows, fleet_snapshots
from projects_orchestrator.pool import default_jobs, map_ordered
from projects_orchestrator.registry import FleetConfig, discover


def test_default_jobs_is_at_least_one() -> None:
    assert default_jobs() >= 1


def test_map_ordered_preserves_input_order() -> None:
    assert map_ordered(lambda n: n * 2, list(range(20))) == [n * 2 for n in range(20)]


def test_map_ordered_single_item_runs_serially() -> None:
    assert map_ordered(lambda n: n + 1, [41]) == [42]


def test_map_ordered_jobs_one_runs_serially() -> None:
    assert map_ordered(lambda n: n + 1, [1, 2, 3], jobs=1) == [2, 3, 4]


def test_map_ordered_empty_input() -> None:
    assert map_ordered(lambda n: n, []) == []


def test_map_ordered_runs_concurrently() -> None:
    start = time.monotonic()
    map_ordered(lambda _: time.sleep(0.3), [1, 2, 3, 4], jobs=4)
    assert time.monotonic() - start < 1.0


def test_fleet_snapshots_parallel_matches_serial_rows(fleet_dir: Path, tmp_path: Path) -> None:
    for name in ("alpha", "beta", "gamma"):
        git_init(make_project(fleet_dir, name))
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    rows = fleet_rows(fleet_snapshots(fleet, tmp_path / "checks.json"))
    assert [row["Project"] for row in rows] == ["alpha", "beta", "gamma"]


def test_checks_parallel_projects_do_not_serialize(fleet_dir: Path, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for name in ("alpha", "beta", "gamma"):
        make_project(fleet_dir, name, tooling={"lint": "sleep 0.5"})
    start = time.monotonic()
    main(["checks", "--root", str(fleet_dir), "--task", "lint", "--jobs", "4"])
    assert time.monotonic() - start < 1.3


def test_checks_results_keep_fleet_order(fleet_dir: Path, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for name in ("alpha", "beta"):
        make_project(fleet_dir, name, tooling={"lint": "true", "test": "true"})
    main(["checks", "--root", str(fleet_dir)])
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [
        "alpha lint: pass",
        "alpha test: pass",
        "beta lint: pass",
        "beta test: pass",
    ]


def test_checks_jobs_one_still_correct(fleet_dir: Path, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    make_project(fleet_dir, "alpha", tooling={"lint": "false"})
    assert main(["checks", "--root", str(fleet_dir), "--task", "lint", "--jobs", "1"]) == 1
