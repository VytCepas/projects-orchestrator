"""The mutation gate — the check that catches tests which cannot fail.

It had the very defect it exists to detect: `score = (killed/total*100) if total
else 100.0` scored an empty run as 100% and passed. There was no [tool.mutmut]
config, so that is what it did, nightly, in silence. These tests exist so that
cannot happen again unnoticed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# The gate lives under .agents/scripts/ (it is CI tooling, not library code), so
# it is loaded by path. It must be registered in sys.modules BEFORE exec_module:
# it uses `from __future__ import annotations`, and @dataclass resolves those
# string annotations by looking its own module up in sys.modules.
_GATE = Path(__file__).parent.parent / ".agents" / "scripts" / "mutation_gate.py"
_spec = importlib.util.spec_from_file_location("mutation_gate", _GATE)
assert _spec and _spec.loader
mutation_gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mutation_gate
_spec.loader.exec_module(mutation_gate)

evaluate = mutation_gate.evaluate
main = mutation_gate.main


# --- The bug this file exists for ---------------------------------------------


def test_zero_mutants_is_a_failure_not_a_perfect_score() -> None:
    # THE bug. An empty run tested NOTHING; calling it 100% is the single most
    # misleading thing this script could say.
    assert evaluate({"total": 0, "killed": 0}).ok is False


def test_zero_mutants_says_nothing_was_tested() -> None:
    # And it must say WHY, or the next person "fixes" it by lowering the floor.
    assert "nothing was tested" in evaluate({"total": 0, "killed": 0}).message


def test_zero_mutants_points_at_the_config_that_is_missing() -> None:
    assert "tool.mutmut" in evaluate({"total": 0, "killed": 0}).message


# --- Ordinary scoring ----------------------------------------------------------


def test_a_score_above_the_floor_passes() -> None:
    assert evaluate({"total": 100, "killed": 80}, floor=60).ok is True


def test_a_score_below_the_floor_fails() -> None:
    assert evaluate({"total": 100, "killed": 10}, floor=60).ok is False


def test_a_score_exactly_at_the_floor_passes() -> None:
    # The floor is a floor, not a cliff edge just above it.
    assert evaluate({"total": 100, "killed": 60}, floor=60).ok is True


def test_the_score_is_reported_so_the_ratchet_can_be_raised() -> None:
    assert "80.0%" in evaluate({"total": 100, "killed": 80}, floor=60).message


def test_killing_everything_passes() -> None:
    assert evaluate({"total": 100, "killed": 100}, floor=60).ok is True


# --- A broken run must never read as a good one --------------------------------


@pytest.mark.parametrize(
    "broken",
    [
        {},  # no keys at all
        {"total": 100},  # no killed
        {"killed": 100},  # no total
        {"total": "many", "killed": 1},  # not numbers
        [],  # not even an object
        None,
        "totally fine, honest",
    ],
)
def test_a_malformed_stats_blob_fails_rather_than_passing(broken: object) -> None:
    # Every one of these is "the run is broken". None of them is "the run is fine".
    assert evaluate(broken).ok is False


def test_a_negative_total_fails() -> None:
    assert evaluate({"total": -1, "killed": 0}).ok is False


# --- End to end, through the CLI the workflow actually calls --------------------


def test_main_exits_nonzero_when_the_stats_file_is_missing(tmp_path: Path) -> None:
    # A missing file means the run did not happen — not that it went well.
    assert main(["--stats", str(tmp_path / "nope.json")]) == 1


def test_main_exits_nonzero_on_an_unparseable_stats_file(tmp_path: Path) -> None:
    stats = tmp_path / "s.json"
    stats.write_text("{not json", encoding="utf-8")
    assert main(["--stats", str(stats)]) == 1


def test_main_exits_nonzero_on_an_empty_run(tmp_path: Path) -> None:
    stats = tmp_path / "s.json"
    stats.write_text(json.dumps({"total": 0, "killed": 0}), encoding="utf-8")
    assert main(["--stats", str(stats)]) == 1


def test_main_exits_zero_on_a_real_passing_run(tmp_path: Path) -> None:
    stats = tmp_path / "s.json"
    stats.write_text(json.dumps({"total": 873, "killed": 552}), encoding="utf-8")
    assert main(["--stats", str(stats), "--floor", "60"]) == 0


def test_main_exits_nonzero_on_a_real_failing_run(tmp_path: Path) -> None:
    stats = tmp_path / "s.json"
    stats.write_text(json.dumps({"total": 873, "killed": 100}), encoding="utf-8")
    assert main(["--stats", str(stats), "--floor", "60"]) == 1
