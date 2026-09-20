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
import tomllib
from pathlib import Path

import pytest
from conftest import _REPO, _tracked_top_level, touched_by, uncopied

# The gate lives under .agents/scripts/ (it is CI tooling, not library code), so
# it is loaded by path. It must be registered in sys.modules BEFORE exec_module:
# it uses `from __future__ import annotations`, and @dataclass resolves those
# string annotations by looking its own module up in sys.modules.
_GATE = Path(__file__).parent.parent / ".agents" / "scripts" / "mutation_gate.py"
_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
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


def test_also_copy_names_only_tracked_top_level_paths() -> None:
    """The runtime guard in conftest.py checks what each test reads against
    `[tool.mutmut] also_copy`; this checks the list itself.

    Top-level names only: mutmut's `copy_also_copy_files` does `shutil.copy2`
    for a file and never creates the parent dirs, so a nested file entry is
    never copied. And every entry must be tracked, or it copies nothing.
    """
    config = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    also_copy = {entry.rstrip("/") for entry in config["tool"]["mutmut"]["also_copy"]}
    assert sorted(entry for entry in also_copy if "/" in entry) == [], "nested entry"
    assert sorted(also_copy - _tracked_top_level()) == [], "also_copy names an untracked path"


def test_a_read_is_placed_by_the_path_it_resolves_to_not_by_how_it_was_spelled() -> None:
    """The first version of this guard matched quoted names in test sources, so
    a path built at runtime was missed and a name in a comment looked live."""
    computed = _REPO.joinpath("".join(["LI", "CENSE"]))
    assert touched_by("open", (str(computed), "r", 0)) == {"LICENSE"}
    assert touched_by("os.scandir", (str(_REPO / "docs" / "reference"),)) == {"docs"}
    assert touched_by(
        "subprocess.Popen", ("/bin/sh", ["/bin/sh", str(_REPO / "contrib" / "x.sh")], None, None)
    ) == {"contrib"}
    assert touched_by(
        "subprocess.Popen",
        ("/usr/bin/git", ["/usr/bin/git", "status"], str(_REPO / ".github"), None),
    ) == {".github"}


def test_what_cannot_be_placed_under_the_repo_is_not_counted() -> None:
    assert touched_by("open", ("/etc/hosts", "r", 0)) == set()
    # `os.open` with no mode may be relative to a directory fd, as in shutil.rmtree.
    assert touched_by("open", (".claude", None, 0)) == set()
    assert touched_by("os.scandir", (3,)) == set()
    assert touched_by("exec", (str(_REPO / "LICENSE"),)) == set()


def test_only_tracked_names_mutmut_will_not_copy_are_reported() -> None:
    # README.md is in also_copy, src/ is copied by mutmut itself, .venv is not
    # tracked, and LICENSE is tracked and not copied.
    assert uncopied({"LICENSE", "README.md", "src", ".venv"}) == ["LICENSE"]


# --- mutmut's leftover copy does not break the ordinary run (#297) -------------


def _tree_with_a_mutants_copy(root: Path) -> None:
    """A miniature repo whose `mutants/` holds a second copy of the suite.

    That is what `just test-mutation` leaves behind. Without the ignore, pytest
    collects both copies, finds two modules named `test_dup`, and reports an
    import file mismatch for every one of them.
    """
    addopts = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]["addopts"]
    (root / "pyproject.toml").write_text(
        f'[tool.pytest.ini_options]\naddopts = "{addopts}"\n', encoding="utf-8"
    )
    for where in ("tests", "mutants/tests"):
        directory = root / where
        directory.mkdir(parents=True)
        (directory / "test_dup.py").write_text(
            "def test_one():\n    assert True\n", encoding="utf-8"
        )


def test_a_leftover_mutants_copy_does_not_break_collection(tmp_path: Path) -> None:
    import subprocess

    _tree_with_a_mutants_copy(tmp_path)
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout[-2000:]
    assert "1 passed" in run.stdout, run.stdout[-2000:]


def test_the_ignore_does_not_hide_the_suite_from_mutmut_itself(tmp_path: Path) -> None:
    # mutmut runs pytest FROM `mutants/`, where the same setting names
    # `mutants/mutants` — a path that does not exist. The copy's own tests must
    # still be collected, or the ignore would kill the nightly it protects.
    import shutil
    import subprocess

    _tree_with_a_mutants_copy(tmp_path)
    shutil.copy(tmp_path / "pyproject.toml", tmp_path / "mutants" / "pyproject.toml")
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path / "mutants",
        capture_output=True,
        text=True,
        check=False,
    )
    assert "1 passed" in run.stdout, run.stdout[-2000:]
