"""The installed CLI, run as a subprocess, against a fleet of real git repos.

The unit suite calls ``main([...])`` in-process. That cannot see the console
script being wired to the wrong function, an import that only fails in a fresh
interpreter, or output that reaches stdout mixed with something else, and it
never runs ``git`` against a real worktree layout. These tests do: each one
starts the ``projects-orchestrator`` entry point the wheel installs, in its own
process, over repositories built with real ``git``.

They run in CI's "Integration tests" job, which printed "No integration tests
directory found" and passed for as long as this directory did not exist (#254).
`pyproject.toml` keeps them out of the unit run (``--ignore``), so they run once.

A missing entry point FAILS rather than skips: a skipped integration test is the
green-having-done-nothing that #254 was filed about.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from conftest import git_init, make_project
from jsonschema import Draft202012Validator

import projects_orchestrator

_SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def _entry_point() -> Path:
    """The console script installed beside this interpreter, not a PATH lookup:
    PATH could resolve a different install, such as a `uv tool` copy."""
    script = Path(sys.executable).parent / "projects-orchestrator"
    assert script.is_file(), f"no installed entry point at {script}"
    return script


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_entry_point()), *args], capture_output=True, text=True, timeout=120, check=False
    )


def _json(*args: str) -> Any:
    result = _cli(*args)
    assert result.returncode in (0, 1), result.stderr
    return json.loads(result.stdout)


def _valid(schema_file: str, payload: Any) -> None:
    schema = json.loads((_SCHEMAS / schema_file).read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(payload)


def _fleet(root: Path) -> Path:
    """Two real repos, one with a failing gate and uncommitted work, and a linked
    worktree of the other kept beside it the way task worktrees are."""
    root.mkdir()
    alpha = make_project(root, "alpha", tooling={"lint": "true"})
    git_init(alpha)
    beta = make_project(root, "beta", tooling={"lint": "false"})
    git_init(beta)
    (beta / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(alpha), "worktree", "add", "-q", "-b", "task", str(root / "alpha-task")],
        check=True,
        capture_output=True,
    )
    return root


def test_the_entry_point_reports_the_package_version() -> None:
    result = _cli("--version")
    assert result.returncode == 0, result.stderr
    assert projects_orchestrator.__version__ in result.stdout


def test_snapshot_reads_real_repos_and_validates(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path / "fleet")
    payload = _json("snapshot", "--root", str(fleet), "--json")
    _valid("snapshot.v1.schema.json", payload)
    status = {p["descriptor"]["name"]: p["status"] for p in payload}
    # The worktree is the same repo as alpha, not a third project (#260).
    assert sorted(status) == ["alpha", "beta"]
    assert (status["alpha"]["branch"], status["alpha"]["dirty"]) == ("main", False)
    assert status["beta"]["dirty"] is True


def test_checks_run_each_declared_gate_in_its_repo(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path / "fleet")
    verdicts = {
        (r["project"], r["task"]): r["status"]
        for r in _json("checks", "--root", str(fleet), "--task", "lint", "--json")
    }
    assert verdicts == {("alpha", "lint"): "pass", ("beta", "lint"): "fail"}


def test_checks_exit_nonzero_when_a_gate_fails(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path / "fleet")
    result = _cli("checks", "--root", str(fleet), "--task", "lint")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "beta lint: fail" in result.stdout


def test_checks_exit_zero_when_every_gate_passes(tmp_path: Path) -> None:
    root = tmp_path / "fleet"
    root.mkdir()
    git_init(make_project(root, "alpha", tooling={"lint": "true"}))
    result = _cli("checks", "--root", str(root), "--task", "lint")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "alpha lint: pass" in result.stdout


def test_events_and_audit_digest_validate(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path / "fleet")
    _valid("events.v1.schema.json", _json("events", "--root", str(fleet), "--json"))
    digest = _json("audit", "--root", str(fleet), "--json", "--digest")
    _valid("audit-digest.v1.schema.json", digest)
    assert digest["schema_version"] == 1
