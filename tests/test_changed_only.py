"""checks --changed-only: trust a cached pass only for an unchanged clean HEAD."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import git_init, make_project

from projects_orchestrator.__main__ import main
from projects_orchestrator.cache import load_results, save_results
from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.status import clean_worktree_head


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _marked_project(fleet_dir: Path, tmp_path: Path, name: str = "alpha") -> Path:
    """A git project whose lint gate appends to a marker file outside the tree."""
    marker = tmp_path / f"{name}-runs"
    project = make_project(fleet_dir, name, tooling={"lint": f"echo run >> {marker}"})
    git_init(project)
    return marker


def _runs(marker: Path) -> int:
    return len(marker.read_text(encoding="utf-8").splitlines()) if marker.exists() else 0


def test_clean_worktree_head_returns_sha(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    git_init(project)
    assert len(clean_worktree_head(load_descriptor(project))) == 40


def test_clean_worktree_head_dirty_is_empty(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    git_init(project)
    (project / "scratch.txt").write_text("x", encoding="utf-8")
    assert clean_worktree_head(load_descriptor(project)) == ""


def test_clean_worktree_head_non_git_is_empty(fleet_dir: Path) -> None:
    assert clean_worktree_head(load_descriptor(make_project(fleet_dir, "alpha"))) == ""


def test_cache_round_trips_head(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    save_results([CheckResult(project="a", task="lint", status="pass", head="abc")], path)
    assert load_results(path)["a"]["lint"].head == "abc"


def test_cache_entry_without_head_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    path.write_text(
        '{"a": {"lint": {"project": "a", "task": "lint", "status": "pass"}}}', encoding="utf-8"
    )
    assert load_results(path)["a"]["lint"].head == ""


def test_changed_only_skips_unchanged_pass(fleet_dir: Path, tmp_path: Path) -> None:
    marker = _marked_project(fleet_dir, tmp_path)
    main(["checks", "--root", str(fleet_dir), "--task", "lint"])
    main(["checks", "--root", str(fleet_dir), "--task", "lint", "--changed-only"])
    assert _runs(marker) == 1


def test_changed_only_marks_cached_in_output(fleet_dir: Path, tmp_path: Path, capsys) -> None:
    _marked_project(fleet_dir, tmp_path)
    main(["checks", "--root", str(fleet_dir), "--task", "lint"])
    capsys.readouterr()
    main(["checks", "--root", str(fleet_dir), "--task", "lint", "--changed-only"])
    assert "alpha lint: pass (cached)" in capsys.readouterr().out


def test_changed_only_reruns_after_new_commit(fleet_dir: Path, tmp_path: Path) -> None:
    marker = _marked_project(fleet_dir, tmp_path)
    main(["checks", "--root", str(fleet_dir), "--task", "lint"])
    project = fleet_dir / "alpha"
    (project / "new.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(project), "commit", "-q", "-m", "change"],
        check=True,
        capture_output=True,
    )
    main(["checks", "--root", str(fleet_dir), "--task", "lint", "--changed-only"])
    assert _runs(marker) == 2


def test_changed_only_reruns_dirty_worktree(fleet_dir: Path, tmp_path: Path) -> None:
    marker = _marked_project(fleet_dir, tmp_path)
    main(["checks", "--root", str(fleet_dir), "--task", "lint"])
    (fleet_dir / "alpha" / "scratch.txt").write_text("x", encoding="utf-8")
    main(["checks", "--root", str(fleet_dir), "--task", "lint", "--changed-only"])
    assert _runs(marker) == 2


def test_changed_only_reruns_cached_fail(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"lint": "false"})
    git_init(project)
    main(["checks", "--root", str(fleet_dir), "--task", "lint"])
    exit_code = main(["checks", "--root", str(fleet_dir), "--task", "lint", "--changed-only"])
    assert exit_code == 1


def test_changed_only_reruns_non_git_project(fleet_dir: Path, tmp_path: Path) -> None:
    marker = tmp_path / "alpha-runs"
    make_project(fleet_dir, "alpha", tooling={"lint": f"echo run >> {marker}"})
    main(["checks", "--root", str(fleet_dir), "--task", "lint"])
    main(["checks", "--root", str(fleet_dir), "--task", "lint", "--changed-only"])
    assert _runs(marker) == 2


def test_changed_only_json_flags_cached(fleet_dir: Path, tmp_path: Path, capsys) -> None:
    import json

    _marked_project(fleet_dir, tmp_path)
    main(["checks", "--root", str(fleet_dir), "--task", "lint"])
    capsys.readouterr()
    main(["checks", "--root", str(fleet_dir), "--task", "lint", "--changed-only", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["cached"] is True
