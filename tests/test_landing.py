"""The write boundary: a draft PR on its own branch, or nothing.

Every test here runs against a repo with **no hooks installed**. That is
deliberate and it is the whole point (ADR-007 §3): a project-init'd repo has a
`pre-push` hook that blocks pushes to main, and the first campaign this system
exists to run targets *precisely the repos that do not have it yet*. Testing the
boundary on a scaffolded repo would prove the child's guard works, not ours.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import git_init, make_project

from projects_orchestrator.landing import (
    LANDED,
    PUSH_FAILED,
    REFUSED,
    default_branch,
    is_protected,
    open_draft_pr,
    push_branch,
)


def _unscaffolded_repo(fleet_dir: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A git repo with a remote and NO hooks whatsoever."""
    project = make_project(fleet_dir, "alpha")
    git_init(project)
    assert not (project / ".git" / "hooks" / "pre-push").exists(), "the repo must be unguarded"
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "-C", str(project), "remote", "add", "origin", str(remote)], check=True)
    return project, remote


def _remote_branches(remote: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(remote), "branch", "--format=%(refname:short)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return out.split()


class _Ok:
    """A successful RunResult stand-in."""

    ok = True
    stdout = "https://example/pr/1"
    stderr = ""


def _spy(launched: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """Record every argv the boundary launches, and run none of them."""

    def record(args: list[str], cwd: Path, timeout: float = 30.0) -> object:  # noqa: ARG001
        launched.append(args)
        return _Ok()

    monkeypatch.setattr("projects_orchestrator.landing._run_argv", record)


# --- What may never be pushed --------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    ["main", "master", "trunk", "develop", "HEAD", "@", "", "   "],
)
def test_a_protected_ref_is_refused(forbidden: str) -> None:
    assert is_protected(forbidden) is True


def test_the_repos_own_default_branch_is_refused_whatever_it_is_called() -> None:
    # A child is free to call its trunk anything. "main/master" is a guess, not
    # an answer, so the real default is resolved and refused by name.
    assert is_protected("production", repo_default="production") is True


def test_a_flag_wearing_a_branchs_clothes_is_refused() -> None:
    assert is_protected("--force") is True


def test_a_raw_ref_path_is_refused() -> None:
    # Not a branch we created, and the only safe answer to an unrecognised ref
    # is no.
    assert is_protected("refs/heads/main") is True
    assert is_protected("origin/main") is True


@pytest.mark.parametrize("refspec", ["heal/x:main", "heal/x:refs/heads/main", ":main", "heal/x:"])
def test_a_refspec_is_refused_before_it_reaches_git(refspec: str) -> None:
    # P1. `heal/x:main` is a `src:dst` refspec; `git push origin -- heal/x:main`
    # updates remote `main`, because `--` ends OPTION parsing, not REFSPEC parsing.
    # The colon must be rejected here, before the value ever reaches git.
    assert is_protected(refspec) is True


@pytest.mark.parametrize("hostile", ["../evil", "a..b", "x.lock", "heal/x main"])
def test_git_ref_metacharacters_are_refused(hostile: str) -> None:
    assert is_protected(hostile) is True


def test_a_slash_qualified_default_branch_is_recognised_in_full() -> None:
    # P2. `release/2026` must not be truncated to `2026` — otherwise is_protected
    # does not recognise the real default and a push to it is allowed.
    assert is_protected("release/2026", repo_default="release/2026") is True


def test_strip_ref_prefix_keeps_a_slashed_branch_whole() -> None:
    from projects_orchestrator.landing import _strip_ref_prefix

    assert _strip_ref_prefix("refs/remotes/origin/release/2026") == "release/2026"
    assert _strip_ref_prefix("refs/heads/release/2026") == "release/2026"


def test_an_ordinary_agent_branch_is_allowed() -> None:
    # The guard must not refuse everything — one that does gets removed.
    assert is_protected("heal/lint-alpha-abc123", repo_default="main") is False


# --- The boundary holds on a repo with NO hooks --------------------------------


def test_pushing_main_is_refused_by_the_harness_not_by_a_hook(
    fleet_dir: Path, tmp_path: Path
) -> None:
    # THE load-bearing test. No pre-push hook exists here — nothing but this
    # module stands between an agent run and the default branch.
    project, remote = _unscaffolded_repo(fleet_dir, tmp_path)
    result = push_branch(project, "main")
    assert result.status == REFUSED
    assert "main" not in _remote_branches(remote)


def test_a_refusal_says_why(fleet_dir: Path, tmp_path: Path) -> None:
    # A refusal with no reason is indistinguishable from a bug.
    project, _ = _unscaffolded_repo(fleet_dir, tmp_path)
    assert "refusing to push" in push_branch(project, "main").detail


def test_pushing_the_repos_actual_default_is_refused(fleet_dir: Path, tmp_path: Path) -> None:
    project, remote = _unscaffolded_repo(fleet_dir, tmp_path)
    result = push_branch(project, "production", repo_default="production")
    assert result.status == REFUSED
    assert "production" not in _remote_branches(remote)


def test_a_force_flag_smuggled_through_the_branch_name_is_refused(
    fleet_dir: Path, tmp_path: Path
) -> None:
    # Refused because it contains whitespace — a branch we created never does.
    project, _ = _unscaffolded_repo(fleet_dir, tmp_path)
    assert push_branch(project, "heal/x --force").status == REFUSED


@pytest.mark.parametrize(
    "legitimate",
    [
        "heal/lint-alpha-f1a2b3c4",  # suffix starts with `f`
        "heal/lint-alpha-fffffff0",
        "work/fix-the-frobnicator",
    ],
)
def test_a_legitimate_branch_is_not_refused_for_merely_containing_a_flag_substring(
    legitimate: str,
) -> None:
    # The guard's first version scanned for "-f" as a SUBSTRING, which matches any
    # branch whose random hex suffix begins with `f` — so it refused roughly one in
    # sixteen of its own legitimate branches, at random. The suite caught it as a
    # flake, which is the only reason it was not shipped.
    #
    # A guard that randomly blocks real work is deleted by the third person who
    # hits it, and then there is no guard at all. Refuse the STRUCTURE (a leading
    # dash, whitespace, a raw ref), not a substring.
    assert is_protected(legitimate, repo_default="main") is False


def test_an_ordinary_agent_branch_actually_pushes(fleet_dir: Path, tmp_path: Path) -> None:
    # The boundary must let the sanctioned write through, or it is just a wall.
    project, remote = _unscaffolded_repo(fleet_dir, tmp_path)
    subprocess.run(["git", "-C", str(project), "checkout", "-q", "-b", "heal/lint-x"], check=True)
    result = push_branch(project, "heal/lint-x", repo_default="main")
    assert result.status == LANDED
    assert "heal/lint-x" in _remote_branches(remote)


def test_a_push_that_cannot_reach_the_remote_degrades_with_a_reason(fleet_dir: Path) -> None:
    # No remote at all: fail with a reason, never raise (ADR-003).
    project = make_project(fleet_dir, "alpha")
    git_init(project)
    result = push_branch(project, "heal/lint-x", repo_default="main")
    assert result.status == PUSH_FAILED
    assert result.detail


# --- The PR is a DRAFT ---------------------------------------------------------


def test_the_pr_is_opened_as_a_draft(fleet_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Not a nicety: a ready-for-review PR is one click — or one auto-merge-on-green
    # rule — from landing an agent's work with no human in the loop, which is the
    # one thing this system promises not to do.
    project = make_project(fleet_dir, "alpha")
    seen: list[list[str]] = []
    _spy(seen, monkeypatch)
    open_draft_pr(project, "heal/lint-x", "t", "b")
    assert "--draft" in seen[0]


def test_the_pr_is_opened_from_the_agent_branch(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(fleet_dir, "alpha")
    seen: list[list[str]] = []
    _spy(seen, monkeypatch)
    open_draft_pr(project, "heal/lint-x", "t", "b")
    assert "--head" in seen[0] and "heal/lint-x" in seen[0]


# --- What the boundary never does ----------------------------------------------


def test_the_boundary_never_merges(fleet_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Asserted on BEHAVIOUR: no `gh pr merge` may ever be launched from here,
    # whatever the caller asks for. There is no merge verb, and there must not be.
    project = make_project(fleet_dir, "alpha")
    launched: list[list[str]] = []
    _spy(launched, monkeypatch)
    push_branch(project, "heal/x", repo_default="main")
    open_draft_pr(project, "heal/x", "t", "b")
    for args in launched:
        assert "merge" not in args
        assert not any(flag.startswith("--force") for flag in args)


def test_the_push_refspec_is_fully_qualified_on_both_sides(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Defence in depth: even if the guard were bypassed, `refs/heads/x:refs/heads/x`
    # can only create/update branch `x` — a bare `heal/x:main` updates main.
    project = make_project(fleet_dir, "alpha")
    launched: list[list[str]] = []
    _spy(launched, monkeypatch)
    push_branch(project, "heal/lint-x", repo_default="main")
    push_argv = next(a for a in launched if a[:2] == ["git", "push"])
    assert "refs/heads/heal/lint-x:refs/heads/heal/lint-x" in push_argv


def test_the_boundary_never_force_pushes(fleet_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An agent run creates history; it does not rewrite it.
    project = make_project(fleet_dir, "alpha")
    launched: list[list[str]] = []
    _spy(launched, monkeypatch)
    push_branch(project, "heal/x", repo_default="main")
    assert launched
    assert not any("-f" in args or "--force" in args for args in launched)


# --- Default-branch resolution -------------------------------------------------


def test_default_branch_is_empty_when_it_cannot_be_resolved(fleet_dir: Path) -> None:
    # No remote at all: we do not know, and we say so.
    project = make_project(fleet_dir, "alpha")
    git_init(project)
    assert default_branch(project) == ""


def test_an_unknown_default_still_refuses_the_protected_names() -> None:
    # Not knowing the default must never mean "allow the push".
    assert is_protected("main", repo_default="") is True
    assert is_protected("master", repo_default="") is True


def test_default_branch_is_read_from_the_remote_not_guessed(
    fleet_dir: Path, tmp_path: Path
) -> None:
    # It must NOT fall back to the global `init.defaultBranch`. That setting says
    # what NEW repos get, not what THIS repo uses — so a machine whose global
    # default is `main`, pointed at a child whose trunk is `production`, would be
    # told "main" and would then wave a push to `production` straight through.
    project = make_project(fleet_dir, "alpha")
    git_init(project)
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "production", str(remote)], check=True)
    subprocess.run(["git", "-C", str(project), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(
        ["git", "-C", str(project), "push", "-q", "origin", "main:production"], check=True
    )

    assert default_branch(project) == "production"


def test_a_child_whose_trunk_is_not_main_is_still_protected(
    fleet_dir: Path, tmp_path: Path
) -> None:
    # The end-to-end form of the bug above: pushing the child's real trunk must be
    # refused even though it is called nothing like "main".
    project = make_project(fleet_dir, "alpha")
    git_init(project)
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "production", str(remote)], check=True)
    subprocess.run(["git", "-C", str(project), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(
        ["git", "-C", str(project), "push", "-q", "origin", "main:production"], check=True
    )

    result = push_branch(project, "production", repo_default=default_branch(project))
    assert result.status == REFUSED
