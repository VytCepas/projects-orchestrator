"""Heal: fix-scoping prompt, verified-fix gate, and PR-landing flow."""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import git_init, make_project

from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.heal import (
    AGENT_FAILED,
    BRANCH_FAILED,
    FIXED,
    NO_FAILURES,
    PUSH_FAILED,
    VERIFY_FAILED,
    WORKTREE_DIRTY,
    AgentOutcome,
    HealResult,
    PrOutcome,
    build_heal_prompt,
    heal_project,
    pending_failures,
    render_heal_result,
)


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _fail(task: str, detail: str = "boom") -> dict[str, CheckResult]:
    return {task: CheckResult(project="alpha", task=task, status="fail", detail=detail)}


def test_pending_failures_keeps_only_healable_failing_tasks() -> None:
    cached = {
        "lint": CheckResult(project="alpha", task="lint", status="fail"),
        "test": CheckResult(project="alpha", task="test", status="pass"),
        "ci": CheckResult(project="alpha", task="ci", status="fail"),
    }
    failing = pending_failures(cached)
    assert [r.task for r in failing] == ["lint"]


def test_pending_failures_empty_when_nothing_failing() -> None:
    assert pending_failures({"lint": CheckResult(project="alpha", task="lint", status="pass")}) == ()


def test_build_heal_prompt_includes_command_and_detail(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"lint": "ruff check ."})
    descriptor = load_descriptor(project)
    failing = (CheckResult(project="alpha", task="lint", status="fail", detail="E501 too long"),)
    prompt = build_heal_prompt(descriptor, failing)
    assert "ruff check ." in prompt
    assert "E501 too long" in prompt
    assert "do not create a git commit" in prompt.lower()


def test_render_heal_result_fixed_shows_pr_url() -> None:
    result = HealResult("alpha", FIXED, branch="heal/lint-alpha", pr_url="https://example/pr/1")
    line = render_heal_result(result)
    assert "https://example/pr/1" in line
    assert "heal/lint-alpha" in line


def test_render_heal_result_no_failures_is_friendly() -> None:
    assert render_heal_result(HealResult("alpha", NO_FAILURES)) == "alpha: no_action"


def test_heal_project_no_action_when_nothing_cached_failing(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    git_init(project)
    descriptor = load_descriptor(project)
    result = heal_project(descriptor, {})
    assert result.status == NO_FAILURES


def test_heal_project_refuses_on_dirty_worktree(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    git_init(project)
    (project / "scratch.txt").write_text("uncommitted", encoding="utf-8")
    descriptor = load_descriptor(project)
    result = heal_project(descriptor, _fail("lint"))
    assert result.status == WORKTREE_DIRTY


def test_heal_project_reports_agent_failure_and_restores_branch(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    git_init(project)
    descriptor = load_descriptor(project)

    def failing_agent(_descriptor: object, _prompt: str) -> AgentOutcome:
        return AgentOutcome(ok=False, summary="could not figure it out")

    result = heal_project(descriptor, _fail("lint"), agent_run=failing_agent)
    assert result.status == AGENT_FAILED
    assert result.detail == "could not figure it out"
    branch = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert branch == "main"


def test_heal_project_verify_failed_when_gate_still_fails(fleet_dir: Path) -> None:
    # tooling.lint always fails ("false"), so even a "successful" agent run
    # cannot pass re-verification.
    project = make_project(fleet_dir, "alpha", tooling={"lint": "false"})
    git_init(project)
    descriptor = load_descriptor(project)

    def noop_agent(_descriptor: object, _prompt: str) -> AgentOutcome:
        return AgentOutcome(ok=True, summary="looked but changed nothing")

    result = heal_project(descriptor, _fail("lint"), agent_run=noop_agent)
    assert result.status == VERIFY_FAILED
    assert "lint" in result.detail


def test_heal_project_push_failed_without_remote(fleet_dir: Path) -> None:
    # A fix that actually works (agent creates the file the gate checks for)
    # but there is no "origin" remote configured, so landing fails at push.
    project = make_project(fleet_dir, "alpha", tooling={"lint": "test -f fixed.txt"})
    git_init(project)
    descriptor = load_descriptor(project)

    def fixing_agent(descriptor_: object, _prompt: str) -> AgentOutcome:
        (descriptor_.path / "fixed.txt").write_text("ok", encoding="utf-8")
        return AgentOutcome(ok=True, summary="created fixed.txt")

    result = heal_project(descriptor, _fail("lint"), agent_run=fixing_agent)
    assert result.status == PUSH_FAILED
    assert result.branch.startswith("heal/lint-")


def test_heal_project_fixes_and_opens_pr_end_to_end(fleet_dir: Path, tmp_path: Path) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"lint": "test -f fixed.txt"})
    git_init(project)
    remote = tmp_path / "origin.git"
    _run("init", "--bare", "-q", str(remote), cwd=fleet_dir)
    _run("remote", "add", "origin", str(remote), cwd=project)
    descriptor = load_descriptor(project)

    def fixing_agent(descriptor_: object, _prompt: str) -> AgentOutcome:
        (descriptor_.path / "fixed.txt").write_text("ok", encoding="utf-8")
        return AgentOutcome(ok=True, summary="created fixed.txt")

    def fake_open_pr(_descriptor: object, branch: str, tasks: tuple[str, ...]) -> PrOutcome:
        assert tasks == ("lint",)
        return PrOutcome(ok=True, url=f"https://example/pr/{branch}")

    result = heal_project(descriptor, _fail("lint"), agent_run=fixing_agent, open_pr=fake_open_pr)

    assert result.status == FIXED
    assert result.pr_url == f"https://example/pr/{result.branch}"
    assert result.branch == "heal/lint-alpha"

    current_branch = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert current_branch == "main"

    remote_branches = subprocess.run(
        ["git", "-C", str(remote), "branch", "--list", result.branch],
        check=True, capture_output=True, text=True,
    ).stdout
    assert result.branch in remote_branches


def test_heal_project_malicious_project_name_cannot_inject_shell_commands(fleet_dir: Path) -> None:
    # A crafted project name (attacker-controlled: it comes from the child's
    # own .claude/config.yaml, not something this module vets as a trusted
    # shell string) must never be interpreted by a shell — regression test
    # for the git/gh argv-only fix.
    project = make_project(fleet_dir, "a;touch pwn", tooling={"lint": "test -f fixed.txt"})
    git_init(project)
    descriptor = load_descriptor(project)
    assert descriptor.name == "a;touch pwn"

    def fixing_agent(descriptor_: object, _prompt: str) -> AgentOutcome:
        (descriptor_.path / "fixed.txt").write_text("ok", encoding="utf-8")
        return AgentOutcome(ok=True, summary="created fixed.txt")

    heal_project(descriptor, _fail("lint"), agent_run=fixing_agent)

    assert not (project / "pwn").exists()
    assert not (fleet_dir / "pwn").exists()
    assert not (Path.cwd() / "pwn").exists()


def test_heal_project_branch_checkout_failure_reports_branch_failed(fleet_dir: Path) -> None:
    # A locked index (a stale .git/index.lock) makes any git command in the
    # worktree fail, including the initial checkout -B.
    project = make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    git_init(project)
    (project / ".git" / "index.lock").write_text("", encoding="utf-8")
    descriptor = load_descriptor(project)
    result = heal_project(descriptor, _fail("lint"))
    assert result.status == BRANCH_FAILED
