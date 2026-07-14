"""Worktree isolation: cut, remove, retain, expire — and never touch the clone."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from conftest import git_init, make_project

from projects_orchestrator.worktree import (
    create,
    prune_expired,
    remove,
    run_slug,
    worktree_root,
)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _branch_of(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(fleet_dir: Path, name: str = "alpha") -> Path:
    project = make_project(fleet_dir, name)
    git_init(project)
    return project


# --- Cutting a worktree ------------------------------------------------------


def test_create_returns_a_checkout_on_the_requested_branch(fleet_dir: Path) -> None:
    tree = create(_repo(fleet_dir), "alpha", "heal/lint-alpha", run_slug())
    assert tree is not None
    assert _branch_of(tree.path) == "heal/lint-alpha"


def test_create_never_switches_the_operators_clone(fleet_dir: Path) -> None:
    # The entire reason this module exists.
    repo = _repo(fleet_dir)
    create(repo, "alpha", "heal/lint-alpha", run_slug())
    assert _branch_of(repo) == "main"


def test_the_worktree_lives_outside_the_project(fleet_dir: Path) -> None:
    # Inside the project it would land in a .gitignore we do not control.
    repo = _repo(fleet_dir)
    tree = create(repo, "alpha", "heal/lint-alpha", run_slug())
    assert tree is not None
    assert repo not in tree.path.parents


def test_create_honors_xdg_state_home(fleet_dir: Path) -> None:
    tree = create(_repo(fleet_dir), "alpha", "b", run_slug())
    assert tree is not None
    assert worktree_root() in tree.path.parents


def test_create_on_a_non_repo_is_none_not_a_crash(fleet_dir: Path) -> None:
    plain = make_project(fleet_dir, "alpha")  # no git_init
    assert create(plain, "alpha", "b", run_slug()) is None


def test_create_refuses_to_reuse_an_existing_slug(fleet_dir: Path) -> None:
    # Reusing a slug would silently hand an agent a stale checkout.
    repo = _repo(fleet_dir)
    slug = run_slug()
    assert create(repo, "alpha", "heal/one", slug) is not None
    assert create(repo, "alpha", "heal/two", slug) is None


def test_two_runs_on_one_repo_get_distinct_worktrees(fleet_dir: Path) -> None:
    # Impossible under the old checkout-based design: they would fight over HEAD.
    repo = _repo(fleet_dir)
    first = create(repo, "alpha", "heal/one", run_slug())
    second = create(repo, "alpha", "heal/two", run_slug())
    assert first is not None and second is not None
    assert first.path != second.path


def test_run_slug_does_not_collide_within_one_second() -> None:
    # A timestamp+pid slug collides when one process starts two runs in the same
    # second — and create() then (rightly) refuses the second for no good reason.
    assert run_slug() != run_slug()


# --- Removal and retention ---------------------------------------------------


def test_remove_deletes_the_worktree(fleet_dir: Path) -> None:
    repo = _repo(fleet_dir)
    tree = create(repo, "alpha", "heal/lint-alpha", run_slug())
    assert tree is not None
    assert remove(tree) is True
    assert not tree.path.exists()


def test_remove_deregisters_the_worktree_from_git(fleet_dir: Path) -> None:
    # A leaked administrative entry would make git refuse the branch next time.
    repo = _repo(fleet_dir)
    tree = create(repo, "alpha", "heal/lint-alpha", run_slug())
    assert tree is not None
    remove(tree)
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(tree.path) not in listed


def test_remove_survives_a_worktree_already_gone(fleet_dir: Path) -> None:
    repo = _repo(fleet_dir)
    tree = create(repo, "alpha", "heal/lint-alpha", run_slug())
    assert tree is not None
    remove(tree)
    assert remove(tree) is True  # idempotent, never raises


def test_removing_a_worktree_leaves_the_clone_on_its_branch(fleet_dir: Path) -> None:
    repo = _repo(fleet_dir)
    tree = create(repo, "alpha", "heal/lint-alpha", run_slug())
    assert tree is not None
    remove(tree)
    assert _branch_of(repo) == "main"


# --- Expiry ------------------------------------------------------------------


def test_prune_expired_leaves_a_fresh_worktree_alone(fleet_dir: Path) -> None:
    # Retention is the point: a failed run's evidence must survive to be read.
    repo = _repo(fleet_dir)
    tree = create(repo, "alpha", "heal/lint-alpha", run_slug())
    assert tree is not None
    assert prune_expired(repo, "alpha") == 0
    assert tree.path.exists()


def test_prune_expired_deletes_a_stale_worktree(fleet_dir: Path) -> None:
    repo = _repo(fleet_dir)
    tree = create(repo, "alpha", "heal/lint-alpha", run_slug())
    assert tree is not None
    stale = time.time() - 30 * 86400
    os.utime(tree.path, (stale, stale))
    assert prune_expired(repo, "alpha", expiry_days=7) == 1
    assert not tree.path.exists()


def test_prune_expired_respects_the_expiry_window(fleet_dir: Path) -> None:
    # 3 days old, 7-day window — still evidence, not litter.
    repo = _repo(fleet_dir)
    tree = create(repo, "alpha", "heal/lint-alpha", run_slug())
    assert tree is not None
    recent = time.time() - 3 * 86400
    os.utime(tree.path, (recent, recent))
    assert prune_expired(repo, "alpha", expiry_days=7) == 0


def test_prune_expired_on_a_missing_project_dir_is_zero_not_a_crash(fleet_dir: Path) -> None:
    assert prune_expired(_repo(fleet_dir), "never-healed") == 0
