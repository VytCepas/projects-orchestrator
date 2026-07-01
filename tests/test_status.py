"""Tests for collecting per-project health/status."""

from __future__ import annotations

from pathlib import Path

from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.status import ProjectStatus, collect_status


def _status(root: Path) -> ProjectStatus:
    return collect_status(load_descriptor(root / ".claude" / "config.yaml"))


def test_non_git_project_reports_not_a_repo(make_project):
    assert _status(make_project("alpha")).git.is_repo is False


def test_non_git_project_health_is_no_git(make_project):
    assert _status(make_project("alpha")).health == "no-git"


def test_clean_repo_reports_is_repo(make_project, git_init):
    root = make_project("alpha")
    git_init(root)
    assert _status(root).git.is_repo is True


def test_clean_repo_reports_branch(make_project, git_init):
    root = make_project("alpha")
    git_init(root)
    assert _status(root).git.branch == "main"


def test_clean_repo_health_is_clean(make_project, git_init):
    root = make_project("alpha")
    git_init(root)
    assert _status(root).health == "clean"


def test_dirty_repo_reports_dirty(make_project, git_init):
    root = make_project("alpha")
    git_init(root)
    (root / "new.txt").write_text("x", encoding="utf-8")
    assert _status(root).git.dirty is True


def test_dirty_repo_health_is_dirty(make_project, git_init):
    root = make_project("alpha")
    git_init(root)
    (root / "new.txt").write_text("x", encoding="utf-8")
    assert _status(root).health == "dirty"


def test_clean_repo_has_zero_ahead(make_project, git_init):
    root = make_project("alpha")
    git_init(root)
    assert _status(root).git.ahead == 0


def test_status_carries_descriptor_name(make_project):
    assert _status(make_project("alpha")).descriptor.name == "alpha"
