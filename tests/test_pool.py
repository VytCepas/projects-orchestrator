"""Thread-pool fan-out: ordered, bounded, and actually concurrent."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
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


# CONCURRENCY IS ASSERTED BY RENDEZVOUS, NOT BY STOPWATCH (#280). The old
# form ran four 0.3s sleeps and required the whole map to finish inside 1.0s.
# That bound measures the machine as much as the pool: under load a correct
# pool can take longer to schedule its threads, and the test went red on a
# busy box while CI stayed green. A barrier asks the actual question: all
# the calls are in flight at the same time. A serial loop can never fill it,
# however fast the machine is, and a parallel pool always does, however slow.
_RENDEZVOUS_TIMEOUT = 10


def test_map_ordered_runs_concurrently() -> None:
    barrier = threading.Barrier(4)
    results = map_ordered(
        lambda n: (barrier.wait(timeout=_RENDEZVOUS_TIMEOUT), n)[1], [1, 2, 3, 4], jobs=4
    )
    assert results == [1, 2, 3, 4]


def test_fleet_snapshots_parallel_matches_serial_rows(fleet_dir: Path, tmp_path: Path) -> None:
    for name in ("alpha", "beta", "gamma"):
        git_init(make_project(fleet_dir, name))
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    rows = fleet_rows(fleet_snapshots(fleet, tmp_path / "checks.json"))
    assert [row["Project"] for row in rows] == ["alpha", "beta", "gamma"]


# The same rendezvous for real gates run by `checks`: each project's gate marks
# its arrival and passes only once every project has arrived. Run serially, the
# first gate waits for peers that have not started, gives up and fails, so the
# command exits 1. The give-up is an iteration count, never a clock reading.
_GATE_RENDEZVOUS = """\
touch "$1/$2"
i=0
while [ $(( $(ls "$1" | wc -l) )) -lt "$3" ] && [ "$i" -lt 200 ]; do
  sleep 0.05
  i=$((i + 1))
done
[ $(( $(ls "$1" | wc -l) )) -ge "$3" ]
"""


def test_checks_parallel_projects_do_not_serialize(fleet_dir: Path, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    script = tmp_path / "rendezvous.sh"
    script.write_text(_GATE_RENDEZVOUS, encoding="utf-8")
    arrived = tmp_path / "arrived"
    arrived.mkdir()
    names = ("alpha", "beta", "gamma")
    for name in names:
        make_project(
            fleet_dir, name, tooling={"lint": f"sh {script} {arrived} {name} {len(names)}"}
        )
    assert main(["checks", "--root", str(fleet_dir), "--task", "lint", "--jobs", "4"]) == 0
    assert sorted(p.name for p in arrived.iterdir()) == sorted(names)


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


# --- A raising callable is an ENGINE BUG, and must look like one (#187) -------
#
# `pool.py` deliberately omits exception translation: engine callables follow
# the never-raise rule, so a raise is a bug that should surface exactly as it
# would have in the serial loop. That contract was documented and untested —
# so a `collect_*` that started raising would have killed the whole fan-out
# with nothing guarding the regression.


def _boom(_item: int) -> int:
    raise RuntimeError("engine callable violated never-raise")


def test_a_raising_callable_propagates_in_the_parallel_path() -> None:
    with pytest.raises(RuntimeError, match="never-raise"):
        map_ordered(_boom, [1, 2, 3, 4], jobs=4)


def test_a_raising_callable_propagates_in_the_serial_path() -> None:
    """`jobs=1` and a single item short-circuit to a plain loop. The two paths
    must fail the same way, or a bug reproduces only at one fan-out width."""
    with pytest.raises(RuntimeError, match="never-raise"):
        map_ordered(_boom, [1, 2, 3, 4], jobs=1)


def test_a_raising_callable_propagates_for_a_single_item() -> None:
    with pytest.raises(RuntimeError, match="never-raise"):
        map_ordered(_boom, [1], jobs=4)


def test_one_raising_item_does_not_silently_drop_the_others() -> None:
    """The failure mode this guards: a partial list returned as if complete."""

    def raise_on_two(item: int) -> int:
        if item == 2:
            raise RuntimeError("never-raise")
        return item

    with pytest.raises(RuntimeError):
        map_ordered(raise_on_two, [1, 2, 3], jobs=3)
