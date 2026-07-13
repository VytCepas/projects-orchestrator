"""Git health collection against real repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import git_init, make_project

from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.status import collect_status


def _descriptor(fleet_dir: Path, name: str = "alpha"):
    return load_descriptor(fleet_dir / name)


def test_collect_status_reads_branch(fleet_dir: Path) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    assert collect_status(_descriptor(fleet_dir)).branch == "main"


def test_collect_status_clean_repo_health(fleet_dir: Path) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    assert collect_status(_descriptor(fleet_dir)).health == "clean"


def test_collect_status_detects_dirty_worktree(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    git_init(project)
    (project / "scratch.txt").write_text("wip", encoding="utf-8")
    assert collect_status(_descriptor(fleet_dir)).health == "dirty"


def test_collect_status_non_git_is_unknown(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert collect_status(_descriptor(fleet_dir)).health == "unknown"


def test_status_health_unknown_when_dirty_unreadable() -> None:
    from projects_orchestrator.status import ProjectStatus

    assert ProjectStatus(project="x", branch="main", dirty=None).health == "unknown"


def test_collect_status_non_git_has_detail(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert collect_status(_descriptor(fleet_dir)).detail == "not a git repository"


def test_collect_status_reports_last_commit(fleet_dir: Path) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    assert collect_status(_descriptor(fleet_dir)).last_commit is not None


def test_collect_status_no_upstream_means_unknown_ahead(fleet_dir: Path) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    assert collect_status(_descriptor(fleet_dir)).ahead is None


def test_collect_status_ahead_of_upstream(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    git_init(project)
    clone = fleet_dir / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(project), str(clone)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.email=t@e.c",
            "-c",
            "user.name=T",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "ahead",
        ],
        check=True,
        capture_output=True,
    )
    descriptor = load_descriptor(project).__class__(name="clone", path=clone)
    assert collect_status(descriptor).ahead == 1
