"""`test-fleet` (#316): every fleet repo's `just test`, one row per repo, a red fleet exits 1.

The fleets here are planted repos whose `justfile` prints the contract's summary
line and exits as told, so the command's reading of real `just` runs is what is
checked — never a stubbed runner.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from projects_orchestrator.__main__ import main
from projects_orchestrator.testfleet import parse_summary
from tests.conftest import make_project

#: The registry id, spelled out: importing the module's own constant would let a
#: changed stamp change the expectation with it.
STAMP = "[check:fleet-test]"

pytestmark = pytest.mark.skipif(
    shutil.which("just") is None and not os.environ.get("CI"),
    reason="needs just; CI installs it, so there it runs",
)


def _repo(fleet: Path, name: str, recipe: str | None) -> Path:
    """A project whose justfile's `test` recipe runs *recipe* (None: no justfile)."""
    project = make_project(fleet, name)
    if recipe is not None:
        (project / "justfile").write_text(f"test:\n    @sh -c '{recipe}'\n", encoding="utf-8")
    return project


def _run(fleet: Path, *extra: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    rc = main(["test-fleet", "--root", str(fleet), *extra])
    return rc, capsys.readouterr().out


def _rows(out: str) -> dict[str, list[str]]:
    rows = {}
    for line in out.splitlines():
        fields = line.split()
        if (
            len(fields) >= 6
            and fields[0] == STAMP
            and fields[1] != "repo"
            and not fields[1].endswith(":")
        ):
            rows[fields[1]] = fields[2:6]
    return rows


def test_one_red_repo_makes_the_fleet_exit_1(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    _repo(fleet_dir, "beta", "echo beta: 1 passed, 2 failed; exit 1")
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 1, out
    rows = _rows(out)
    assert rows["alpha"] == ["3", "0", "0", "pass"], out
    assert rows["beta"] == ["1", "2", "1", "fail"], out


def test_an_all_green_fleet_exits_0(fleet_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    _repo(fleet_dir, "gamma", "echo gamma: 5 passed, 0 failed")
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 0, out
    assert (
        out.strip().splitlines()[-1]
        == f"{STAMP} fleet-test: 2 repos, 2 pass, 0 fail, 0 skip; 8 passed, 0 failed"
    )


def test_every_line_carries_the_check_id(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    _repo(fleet_dir, "beta", "echo beta: 0 passed, 1 failed; exit 1")
    _repo(fleet_dir, "delta", None)
    _, out = _run(fleet_dir, capsys=capsys)
    lines = out.strip().splitlines()
    assert len(lines) == 5, out  # header, three rows, summary
    assert all(line.startswith(STAMP + " ") for line in lines), out


def test_a_repo_without_a_test_recipe_is_skipped_not_failed(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    _repo(fleet_dir, "delta", None)
    (_repo(fleet_dir, "eps", None) / "justfile").write_text("lint:\n    @true\n", encoding="utf-8")
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 0, out
    rows = _rows(out)
    assert rows["delta"] == ["-", "-", "-", "skip"], out
    assert rows["eps"] == ["-", "-", "-", "skip"], out


def test_the_last_summary_line_is_the_count(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "alpha", "echo s1: 1 passed, 9 failed; echo alpha: 4 passed, 0 failed")
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 0, out
    assert _rows(out)["alpha"] == ["4", "0", "0", "pass"], out


def test_exit_0_with_a_counted_failure_is_red(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "liar", "echo liar: 2 passed, 1 failed")
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 1, out
    assert _rows(out)["liar"] == ["2", "1", "0", "fail"], out


def test_no_summary_line_is_unknown_counts_and_the_exit_decides(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "quiet", "echo all good")
    _repo(fleet_dir, "crash", "echo boom >&2; exit 3")
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 1, out
    rows = _rows(out)
    assert rows["quiet"] == ["?", "?", "0", "pass"], out
    assert rows["crash"] == ["?", "?", "3", "fail"], out
    assert "no summary line" in out


def test_a_hung_suite_is_killed_and_red(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "hang", "sleep 30")
    rc, out = _run(fleet_dir, "--timeout", "2", capsys=capsys)
    assert rc == 1, out
    assert _rows(out)["hang"] == ["?", "?", "-", "fail"], out
    assert "timed out" in out


def test_json_carries_the_rows_and_the_exit(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    _repo(fleet_dir, "beta", "echo beta: 1 passed, 2 failed; exit 1")
    rc, out = _run(fleet_dir, "--json", capsys=capsys)
    assert rc == 1
    doc = json.loads(out)
    assert doc["check"] == "fleet-test"
    by_repo = {row["repo"]: row for row in doc["rows"]}
    assert by_repo["beta"]["failed"] == 2
    assert by_repo["beta"]["exit"] == 1
    assert by_repo["alpha"]["status"] == "pass"


def test_an_empty_fleet_is_not_a_green_fleet(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet_dir.mkdir(parents=True)
    rc, out = _run(fleet_dir, capsys=capsys)
    assert rc == 2
    assert out.strip().startswith(STAMP)


def test_discovery_warnings_carry_the_check_id_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(f"roots:\n  - {tmp_path / 'missing'}\n", encoding="utf-8")
    rc = main(["test-fleet", "--fleet", str(fleet_file)])
    captured = capsys.readouterr()
    assert rc == 2
    lines = (captured.out + captured.err).strip().splitlines()
    assert any("cannot scan root" in line for line in lines), lines
    assert all(line.startswith(STAMP + " ") for line in lines), lines


def test_one_project_by_name(fleet_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _repo(fleet_dir, "alpha", "echo alpha: 3 passed, 0 failed")
    _repo(fleet_dir, "beta", "echo beta: 1 passed, 2 failed; exit 1")
    rc, out = _run(fleet_dir, "alpha", capsys=capsys)
    assert rc == 0, out
    assert set(_rows(out)) == {"alpha"}


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("x: 1 passed, 0 failed\n", ("x", 1, 0)),
        ("a: 1 passed, 1 failed\nb: 2 passed, 0 failed\n", ("b", 2, 0)),
        ("browser --self-test: 10 passed, 0 failed\n", None),
        ("x smoke: 1 passed, 0 failed (NOT a full run)\n", None),
        ("1 failed, 2 passed in 0.1s\n", None),
    ],
)
def test_parse_summary_takes_the_last_contract_line(
    text: str, want: tuple[str, int, int] | None
) -> None:
    assert parse_summary(text) == want
