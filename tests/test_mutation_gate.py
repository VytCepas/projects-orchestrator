"""The mutation gate — the check that catches tests which cannot fail.

It had the very defect it exists to detect: `score = (killed/total*100) if total
else 100.0` scored an empty run as 100% and passed. There was no [tool.mutmut]
config, so that is what it did, nightly, in silence. These tests exist so that
cannot happen again unnoticed.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
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


# --- The coupling that only breaks at 3am --------------------------------------


#: mutmut's own additions to ``also_copy`` (``mutmut/configuration.py``), plus
#: the source tree it copies itself. Kept as a literal because the point of the
#: guard is to fail in a PR, where mutmut is not installed.
_MUTMUT_COPIES = frozenset({"src", "tests", "test", "setup.cfg", "pyproject.toml", "uv.lock"})

#: Tracked top-level names that tests quote WITHOUT reading the real one. Each
#: needs a reason; an entry that no longer matches anything fails below.
_NOT_READ_FROM_THE_TREE = {
    ".claude": "tests build synthetic fleet repos with a .claude/ layout; none "
    "reads this repo's own, and its untracked agent worktrees would be copied too",
}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _tracked_top_level() -> set[str]:
    # Ask git for the work tree's top level rather than assuming it is the
    # parent of tests/: this test also runs inside `mutants/`, an untracked copy
    # where `git ls-files` lists nothing.
    root = Path(_git("rev-parse", "--show-toplevel", cwd=Path(__file__).resolve().parent).strip())
    return {line.split("/", 1)[0] for line in _git("ls-files", cwd=root).splitlines() if line}


def _quoted_in_tests(names: set[str]) -> set[str]:
    # This file is skipped: its own exemption table quotes every name it
    # exempts, which would make any exemption look current.
    here = Path(__file__).resolve()
    sources = [
        path.read_text(encoding="utf-8") for path in here.parent.rglob("*.py") if path != here
    ]
    return {name for name in names if any(f'"{name}"' in text for text in sources)}


def test_mutmuts_test_tree_carries_every_repo_path_a_test_names() -> None:
    """mutmut runs pytest from a COPY of the tree (`mutants/`), and copies only
    the source, `tests/` and what `[tool.mutmut] also_copy` names. A test that
    opens a repo file mutmut did not copy fails there with FileNotFoundError,
    and pytest's `-x` stops the whole nightly run at the first one.

    It happened twice. The gate script under `.agents/scripts/` first; then the
    README read by test_docs.py, which kept the nightly red for eleven days
    while every PR stayed green (#253). The first fix pinned one path by hand,
    so the second went unseen. This derives the set instead: every tracked
    top-level name a test quotes must be copied, or exempted with a reason.
    Over-copying a name a test only uses for a synthetic repo costs nothing.

    Directory entries, not nested files: mutmut's `copy_also_copy_files` does
    `shutil.copy2` for a file and never creates the parent dirs.
    """
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    also_copy = set(
        tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["mutmut"]["also_copy"]
    )
    assert not {entry for entry in also_copy if "/" in entry and not entry.endswith("/")}, (
        "also_copy entries must be top-level names; a nested file is never copied"
    )
    copied = {entry.rstrip("/") for entry in also_copy} | _MUTMUT_COPIES
    tracked = _tracked_top_level()
    needed = _quoted_in_tests(tracked) - set(_NOT_READ_FROM_THE_TREE)
    assert sorted(needed - copied) == [], "add these to [tool.mutmut] also_copy"
    stale = {name for name in _NOT_READ_FROM_THE_TREE if name not in _quoted_in_tests(tracked)}
    assert sorted(stale) == [], "exemption no longer matches a quoted tracked name"
    assert sorted(also_copy - tracked) == [], "also_copy names a path the repo does not track"
