"""`work`: launch a tracked, detached agent run — and list, tail, stop it.

Every external effect is injected — spawn (the detached process), agent (the
claude CLI), land (the write boundary) — so nothing here starts a process or a
real agent. The composition of the five layers is what is under test, not their
individual internals (those have their own suites).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git_init, make_project

from projects_orchestrator import runs, work
from projects_orchestrator.descriptor import load_descriptor


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _repo(fleet_dir: Path, name: str = "alpha") -> object:
    project = make_project(fleet_dir, name, tooling={"lint": "true"})
    git_init(project)
    return load_descriptor(project)


def _recording_spawn(calls: list[list[str]]) -> work.Spawn:
    # Return THIS process's pid: it is genuinely alive, so runs' reconciliation
    # (a dead pid → failed, #113) leaves the launched run RUNNING. A fake dead pid
    # would correctly reconcile to failed — a different test, not this one.
    def spawn(argv: list[str], _log_path: Path) -> int:
        calls.append(argv)
        return os.getpid()

    return spawn


# --- Launch ------------------------------------------------------------------


def test_launch_records_a_running_agent(fleet_dir: Path) -> None:
    run = work.launch(_repo(fleet_dir), "add a health endpoint", spawn=_recording_spawn([]))
    assert run.state == runs.RUNNING
    assert run.task == "add a health endpoint"
    assert run.pid == os.getpid()


def test_launch_is_visible_in_the_listing(fleet_dir: Path) -> None:
    # The whole point of the run record: it outlives the launching call.
    work.launch(_repo(fleet_dir), "t", spawn=_recording_spawn([]))
    assert [r.state for r in work.list_runs()] == [runs.RUNNING]


def test_launch_cuts_a_worktree_not_the_operators_clone(fleet_dir: Path) -> None:
    descriptor = _repo(fleet_dir)
    run = work.launch(descriptor, "t", spawn=_recording_spawn([]))
    assert run.worktree
    assert Path(run.worktree) != descriptor.path
    assert Path(run.worktree).is_dir()


def test_launch_spawns_the_detached_runner_for_this_run(fleet_dir: Path) -> None:
    calls: list[list[str]] = []
    run = work.launch(_repo(fleet_dir), "t", spawn=_recording_spawn(calls))
    assert calls == [[sys.argv[0], work.RUNNER_SUBCOMMAND, run.id]]


def test_launch_stages_the_briefing_for_the_runner(fleet_dir: Path) -> None:
    # The detached runner reads the prompt from a file (it is large and carries
    # untrusted output); launch must write it before spawning.
    run = work.launch(_repo(fleet_dir), "fix the frobnicator", spawn=_recording_spawn([]))
    prompt = work._prompt_path(run.id).read_text(encoding="utf-8")
    assert "fix the frobnicator" in prompt
    assert "do not commit" in prompt.lower()  # the write-boundary contract


def test_launch_on_a_non_repo_fails_synchronously(fleet_dir: Path) -> None:
    # A repo that cannot yield a worktree fails HERE, in the operator's shell —
    # not silently inside a detached process discovered later via --list.
    plain = make_project(fleet_dir, "alpha", tooling={"lint": "true"})  # no git_init
    run = work.launch(load_descriptor(plain), "t", spawn=_recording_spawn([]))
    assert run.state == runs.FAILED
    assert "worktree" in run.detail


def test_a_failed_launch_never_spawns_anything(fleet_dir: Path) -> None:
    plain = make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    calls: list[list[str]] = []
    work.launch(load_descriptor(plain), "t", spawn=_recording_spawn(calls))
    assert calls == []


def test_a_spawn_failure_degrades_to_failed(fleet_dir: Path) -> None:
    def boom(_argv: list[str], _log: Path) -> int:
        raise OSError("no fork for you")

    run = work.launch(_repo(fleet_dir), "t", spawn=boom)
    assert run.state == runs.FAILED
    assert "launch" in run.detail


def test_the_branch_is_unique_per_run(fleet_dir: Path) -> None:
    descriptor = _repo(fleet_dir)
    a = work.launch(descriptor, "t", spawn=_recording_spawn([]))
    b = work.launch(descriptor, "t", spawn=_recording_spawn([]))
    assert a.branch != b.branch  # else the second run deadlocks on the first's kept worktree


# --- The detached runner body ------------------------------------------------


def _launched(fleet_dir: Path, task: str = "t") -> runs.AgentRun:
    return work.launch(_repo(fleet_dir), task, spawn=_recording_spawn([]))


def test_run_agent_lands_a_pr_on_success(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    landed = runs.AgentRun(**{**vars(run), "state": runs.PR_OPENED, "pr_url": "https://x/pr/1"})
    result = work.run_agent(
        run.id,
        agent=lambda *_: True,
        land=lambda _r: runs.finish(_r, runs.PR_OPENED, pr_url="https://x/pr/1"),
    )
    assert result.state == runs.PR_OPENED
    assert runs.load(run.id).pr_url == "https://x/pr/1"
    assert landed  # (constructed only to document the expected shape)


def test_run_agent_fails_and_keeps_the_worktree_when_the_agent_dies(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    landed_called: list[int] = []
    result = work.run_agent(
        run.id,
        agent=lambda *_: False,
        land=lambda _r: landed_called.append(1) or _r,  # must NOT be reached
    )
    assert result.state == runs.FAILED
    assert landed_called == []  # a dead agent is never landed
    assert Path(run.worktree).is_dir()  # evidence kept


def test_run_agent_never_lands_without_a_successful_agent(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    result = work.run_agent(run.id, agent=lambda *_: False, land=lambda _r: _r)
    assert result.state == runs.FAILED


def test_run_agent_on_an_unknown_run_is_failed_not_a_crash() -> None:
    result = work.run_agent("no-such-run", agent=lambda *_: True, land=lambda r: r)
    assert result.state == runs.FAILED


def test_run_agent_leaves_an_already_terminal_run_untouched(fleet_dir: Path) -> None:
    # A stop that raced ahead of the wrapper already settled the run; the wrapper
    # must not resurrect it, overwrite it, OR waste a real agent run on it.
    run = _launched(fleet_dir)
    runs.finish(run, runs.ABANDONED, detail="stopped first")
    agent_calls: list[int] = []
    result = work.run_agent(
        run.id, agent=lambda *_: agent_calls.append(1) or True, land=lambda r: r
    )
    assert result.state == runs.ABANDONED
    assert agent_calls == []  # the abandoned run must not still burn an agent


def test_run_agent_fails_when_the_briefing_is_missing(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    work._prompt_path(run.id).unlink()
    result = work.run_agent(run.id, agent=lambda *_: True, land=lambda r: r)
    assert result.state == runs.FAILED


# --- Logs --------------------------------------------------------------------


def test_logs_tails_the_run_log(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    Path(run.log_path).write_text("line one\nline two\nline three\n", encoding="utf-8")
    assert work.logs(run.id, lines=2) == ["line two", "line three"]


def test_logs_on_an_unknown_run_is_empty() -> None:
    assert work.logs("no-such-run") == []


def test_logs_before_anything_is_written_is_empty(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    Path(run.log_path).unlink(missing_ok=True)
    assert work.logs(run.id) == []


# --- Stop --------------------------------------------------------------------


def test_stop_terminates_and_marks_abandoned(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[int] = []
    monkeypatch.setattr(work, "terminate_group", lambda pid, _grace: killed.append(pid))
    run = _launched(fleet_dir)
    stopped = work.stop(run.id)
    assert stopped is not None
    assert stopped.state == runs.ABANDONED
    assert killed == [run.pid]


def test_stop_on_an_unknown_run_is_none() -> None:
    assert work.stop("no-such-run") is None


def test_stop_does_not_bury_a_run_that_already_opened_its_pr(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stop racing a natural completion must not overwrite pr-opened with
    # abandoned (finish's first-writer-wins, #131).
    killed: list[int] = []
    monkeypatch.setattr(work, "terminate_group", lambda pid, _grace: killed.append(pid))
    run = _launched(fleet_dir)
    runs.finish(run, runs.PR_OPENED, pr_url="https://x/pr/9")
    stopped = work.stop(run.id)
    assert stopped.state == runs.PR_OPENED
    assert killed == []  # nothing to kill; the run already finished


# --- The default agent launch is sandboxed -----------------------------------


def test_the_default_agent_runs_with_a_scrubbed_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # work's own agent launch must scrub the data plane, exactly as heal's does.
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
    monkeypatch.setenv("HOME", "/home/me")
    seen: dict[str, object] = {}

    def spy(*_args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        raise OSError("stop before a real claude runs")

    monkeypatch.setattr(work.subprocess, "run", spy)
    work._default_agent(
        tmp_path, "do the thing", tmp_path / "log", budget_usd=work.DEFAULT_BUDGET_USD
    )

    env = seen["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["HOME"] != "/home/me"


def test_no_cloud_credential_reaches_the_agent_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #127, criterion 1: an agent cannot gcloud/gsutil/flyctl its way to
    # production. Blocking the `deploy` verb is not enough — the SHELL under it
    # must carry no cloud credential of any provider. Set one token per platform
    # in the operator env and prove NONE survive into the real agent launch env,
    # and that HOME is redirected so file-backed creds (~/.config/gcloud, ~/.aws)
    # are unreachable too.
    families = {
        "GOOGLE_APPLICATION_CREDENTIALS": "/keys/gcp.json",
        "CLOUDSDK_CORE_PROJECT": "prod",
        "GOOGLE_CLOUD_PROJECT": "prod",
        "AWS_SECRET_ACCESS_KEY": "leak",
        "AWS_SESSION_TOKEN": "leak",
        "FLY_API_TOKEN": "leak",
        "GH_TOKEN": "leak",
        "GITHUB_TOKEN": "leak",
    }
    for key, value in families.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HOME", "/home/operator")
    seen: dict[str, object] = {}

    def spy(*_args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        raise OSError("stop before a real claude runs")

    monkeypatch.setattr(work.subprocess, "run", spy)
    work._default_agent(
        tmp_path, "deploy me to prod", tmp_path / "log", budget_usd=work.DEFAULT_BUDGET_USD
    )

    env = seen["env"]
    for key in families:
        assert key not in env, f"{key} leaked into the agent environment"
    assert env["HOME"] != "/home/operator"  # file-backed creds unreachable too


def test_the_default_spawn_starts_a_detached_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    class _Proc:
        pid = 999

    def spy(*_args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return _Proc()

    monkeypatch.setattr(work.subprocess, "Popen", spy)
    pid = work._default_spawn(["prog", "_run-agent", "x"], tmp_path / "log")
    assert pid == 999
    assert seen["start_new_session"] is True  # so stop() can kill the whole tree
    assert seen["stdin"] == subprocess.DEVNULL  # detached: no inherited stdin


# --- The CLI surface ---------------------------------------------------------


def test_cli_work_launch_and_list(fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from projects_orchestrator.__main__ import main

    _repo(fleet_dir)
    # Stub the spawn so the CLI path launches nothing real, with a live pid.
    monkeypatch.setattr(work, "_default_spawn", lambda _argv, _log: os.getpid())
    code = main(["work", "alpha", "make the tests pass", "--root", str(fleet_dir)])
    assert code == 0
    assert "running" in capsys.readouterr().out

    assert main(["work", "--list", "--root", str(fleet_dir)]) == 0
    assert "make the tests pass" in capsys.readouterr().out


def test_cli_work_launch_on_a_non_repo_exits_nonzero(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from projects_orchestrator.__main__ import main

    make_project(fleet_dir, "alpha", tooling={"lint": "true"})  # no git_init
    monkeypatch.setattr(work, "_default_spawn", lambda _argv, _log: os.getpid())
    assert main(["work", "alpha", "t", "--root", str(fleet_dir)]) == 1


def test_cli_work_stop_unknown_run_exits_two() -> None:
    from projects_orchestrator.__main__ import main

    assert main(["work", "--stop", "no-such-run"]) == 2


def test_cli_work_list_empty_is_friendly(capsys) -> None:
    from projects_orchestrator.__main__ import main

    assert main(["work", "--list"]) == 0
    assert "no agent runs" in capsys.readouterr().out


# --- The real landing path (commit → push → PR → cleanup) --------------------
# The composition tests above inject `land`, so they never exercised the REAL
# _default_land — which is exactly how the "push without committing" bug shipped.
# These drive it end to end against a real bare remote.


def _repo_with_remote(fleet_dir: Path, tmp_path: Path) -> object:
    descriptor = _repo(fleet_dir)
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(
        ["git", "-C", str(descriptor.path), "remote", "add", "origin", str(remote)], check=True
    )
    return descriptor


def test_default_land_commits_the_agents_edits_before_pushing(
    fleet_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # THE P1. The agent is told not to commit, so its edits are uncommitted in the
    # worktree; landing must commit them or the branch is empty and the PR is a
    # no-diff. Drive the real _default_land, stubbing only the final `gh pr create`.
    descriptor = _repo_with_remote(fleet_dir, tmp_path)
    run = work.launch(descriptor, "add a file", spawn=_recording_spawn([]))

    # Simulate the agent: write a file in the worktree WITHOUT committing.
    (Path(run.worktree) / "created_by_agent.txt").write_text("hi", encoding="utf-8")

    # Let commit and push run for real against the bare remote; stub only the PR.
    opened: list[str] = []

    def patched_open(_worktree: Path, branch: str, _title: str, _body: str) -> object:
        opened.append(branch)
        return work.landing.Landing(work.landing.LANDED, pr_url="https://example/pr/1")

    monkeypatch.setattr(work.landing, "open_draft_pr", patched_open)
    result = work._default_land(run)

    assert result.state == runs.PR_OPENED
    # The agent's file reached the pushed branch as a real commit.
    show = subprocess.run(
        ["git", "-C", str(descriptor.path), "show", "--stat", f"origin/{run.branch}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "created_by_agent.txt" in show
    assert opened == [run.branch]  # the PR was opened after the commit landed


def test_default_land_fails_when_the_agent_changed_nothing(fleet_dir: Path, tmp_path: Path) -> None:
    # An agent that touched nothing has not produced a PR-worthy result. Landing
    # must fail loudly, not open an empty PR.
    descriptor = _repo_with_remote(fleet_dir, tmp_path)
    run = work.launch(descriptor, "do nothing", spawn=_recording_spawn([]))
    result = work._default_land(run)  # no edits made in the worktree
    assert result.state == runs.FAILED
    assert "nothing" in result.detail


def test_default_land_removes_the_worktree_on_success(
    fleet_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The P2: a landed run's checkout is redundant (the work is in the PR) and must
    # not accrete in the state dir. Only FAILED runs keep their worktree.
    descriptor = _repo_with_remote(fleet_dir, tmp_path)
    run = work.launch(descriptor, "add a file", spawn=_recording_spawn([]))
    (Path(run.worktree) / "f.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        work.landing,
        "open_draft_pr",
        lambda *_: work.landing.Landing(work.landing.LANDED, pr_url="https://x/1"),
    )
    work._default_land(run)
    assert not Path(run.worktree).exists()


# --- #123: clearing a settled run so it leaves the Work column -----------------


def test_clear_forgets_a_pr_opened_run() -> None:
    # The merge case: once a run's PR is dealt with, clearing forgets the record
    # so the Work column stops showing it.
    runs.save(runs.AgentRun(id="r1", project="alpha", task="t", state=runs.PR_OPENED))
    assert work.clear("r1") == work.CLEARED
    assert runs.load("r1") is None  # gone


def test_clear_refuses_a_running_run(fleet_dir: Path) -> None:
    # THE guard: forgetting a live run would strand the agent and hide open work.
    # A running run must be stopped, not cleared.
    run = work.launch(_repo(fleet_dir), "t", spawn=_recording_spawn([]))
    assert run.state == runs.RUNNING
    assert work.clear(run.id) == work.CLEAR_ACTIVE  # refused
    assert runs.load(run.id) is not None  # still tracked


def test_clear_an_unknown_run_reports_unknown() -> None:
    assert work.clear("nope") == work.CLEAR_UNKNOWN


def test_clear_reports_failure_when_the_record_cannot_be_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # THE guard for the silent-clear bug: if forget() cannot unlink the file, the
    # record survives and the run reappears next read — so clear() must NOT report
    # success. It returns CLEAR_FAILED, and the record is still there.
    runs.save(runs.AgentRun(id="r1", project="alpha", task="t", state=runs.PR_OPENED))
    monkeypatch.setattr(work.runs, "forget", lambda _run_id: False)
    assert work.clear("r1") == work.CLEAR_FAILED
    assert runs.load("r1") is not None  # NOT gone — the failure was honest


def test_cli_work_clear(capsys) -> None:
    from projects_orchestrator.__main__ import main

    runs.save(runs.AgentRun(id="r1", project="alpha", task="t", state=runs.PR_OPENED))
    assert main(["work", "--clear", "r1"]) == 0
    assert "cleared r1" in capsys.readouterr().out
    assert runs.load("r1") is None


def test_cli_work_clear_refuses_active_run(fleet_dir: Path, capsys) -> None:
    from projects_orchestrator.__main__ import main

    run = work.launch(_repo(fleet_dir), "t", spawn=_recording_spawn([]))
    assert main(["work", "--clear", run.id]) == 2
    assert "still active" in capsys.readouterr().err


def test_cli_work_clear_reports_a_failed_removal(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from projects_orchestrator.__main__ import main

    runs.save(runs.AgentRun(id="r1", project="alpha", task="t", state=runs.PR_OPENED))
    monkeypatch.setattr(work.runs, "forget", lambda _run_id: False)
    assert main(["work", "--clear", "r1"]) == 2
    assert "could not be removed" in capsys.readouterr().err


# --- #119: needs-human handoff and work --attach -------------------------------


def _agent_writes_marker(reason: str) -> work.Agent:
    """An agent seam that drops a NEEDS_HUMAN marker in the worktree, then 'exits'."""

    def agent(worktree: Path, _prompt: str, _log: Path) -> bool:
        (worktree / work.briefing.NEEDS_HUMAN_MARKER).write_text(reason, encoding="utf-8")
        return True

    return agent


def test_run_agent_stops_at_needs_human_when_the_marker_is_written(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    landed: list[int] = []
    result = work.run_agent(
        run.id,
        agent=_agent_writes_marker("should I use Postgres or MySQL?"),
        land=lambda _r: landed.append(1) or _r,  # must NOT land a blocked run
    )
    assert result.state == runs.NEEDS_HUMAN
    assert "Postgres or MySQL" in result.detail  # the recorded reason
    assert landed == []
    assert Path(run.worktree).is_dir()  # worktree kept for --attach


def test_needs_human_takes_precedence_over_a_failed_agent(fleet_dir: Path) -> None:
    # An agent that wrote the marker AND exited non-zero explained itself; it must
    # not be buried as a generic failure.
    run = _launched(fleet_dir)

    def agent(worktree: Path, _p: str, _l: Path) -> bool:
        (worktree / work.briefing.NEEDS_HUMAN_MARKER).write_text("blocked", encoding="utf-8")
        return False

    result = work.run_agent(run.id, agent=agent, land=lambda r: r)
    assert result.state == runs.NEEDS_HUMAN


def test_an_empty_marker_still_hands_off(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    result = work.run_agent(run.id, agent=_agent_writes_marker(""), land=lambda r: r)
    assert result.state == runs.NEEDS_HUMAN
    assert result.detail  # a generic reason, not blank


def test_no_marker_lands_as_before(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    result = work.run_agent(
        run.id,
        agent=lambda *_: True,
        land=lambda r: runs.finish(r, runs.PR_OPENED, pr_url="https://x/1"),
    )
    assert result.state == runs.PR_OPENED  # the marker path did not steal the land


def test_needs_human_run_finds_the_blocked_run(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    runs.finish(run, runs.NEEDS_HUMAN, detail="pick one")
    found = work.needs_human_run("alpha")
    assert found is not None
    assert found.id == run.id


def test_needs_human_run_is_none_without_one(fleet_dir: Path) -> None:
    _launched(fleet_dir)  # a running run, not needs-human
    assert work.needs_human_run("alpha") is None


def test_attach_opens_a_session_in_the_run_worktree(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    runs.finish(run, runs.NEEDS_HUMAN, detail="which database?")
    seen: list[tuple[Path, str]] = []

    def session(worktree: Path, prompt: str, reason: str) -> None:
        seen.append((worktree, reason))
        assert prompt  # the original briefing is loaded, not blank

    attached = work.attach("alpha", session=session)
    assert attached is not None and attached.id == run.id
    assert seen == [(Path(run.worktree), "which database?")]


def test_attach_without_a_needs_human_run_returns_none() -> None:
    launched: list[int] = []
    result = work.attach("alpha", session=lambda *_: launched.append(1))
    assert result is None
    assert launched == []  # no session opened for a phantom


def test_cli_work_attach(fleet_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from projects_orchestrator.__main__ import main

    run = _launched(fleet_dir)
    runs.finish(run, runs.NEEDS_HUMAN, detail="which db?")
    opened: list[str] = []
    monkeypatch.setattr(work, "_default_session", lambda *_a: opened.append("session"))
    assert main(["work", "alpha", "--attach", "--root", str(fleet_dir)]) == 0
    assert opened == ["session"]


def test_cli_work_attach_without_a_needs_human_run_exits_two(fleet_dir: Path, capsys) -> None:
    from projects_orchestrator.__main__ import main

    _repo(fleet_dir)  # a project, but no needs-human run
    assert main(["work", "alpha", "--attach", "--root", str(fleet_dir)]) == 2
    assert "no needs-human run" in capsys.readouterr().err


def test_a_pre_existing_marker_left_untouched_is_not_a_handoff(fleet_dir: Path) -> None:
    # A repo that already ships a NEEDS_HUMAN.md (a tracked doc, or one left from a
    # prior handoff) must NOT wedge every successful run into needs-human.
    run = _launched(fleet_dir)
    (Path(run.worktree) / work.briefing.NEEDS_HUMAN_MARKER).write_text("old doc", encoding="utf-8")
    result = work.run_agent(
        run.id,
        agent=lambda *_: True,  # a clean run that never touches the marker
        land=lambda r: runs.finish(r, runs.PR_OPENED, pr_url="https://x/1"),
    )
    assert result.state == runs.PR_OPENED  # landed, not falsely handed off


def test_a_pre_existing_marker_the_agent_changes_is_a_handoff(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    marker = Path(run.worktree) / work.briefing.NEEDS_HUMAN_MARKER
    marker.write_text("old doc", encoding="utf-8")

    def agent(worktree: Path, _p: str, _l: Path) -> bool:
        (worktree / work.briefing.NEEDS_HUMAN_MARKER).write_text(
            "now I am blocked", encoding="utf-8"
        )
        return True

    result = work.run_agent(run.id, agent=agent, land=lambda r: r)
    assert result.state == runs.NEEDS_HUMAN
    assert "now I am blocked" in result.detail


def test_attach_returns_none_when_the_session_cannot_start(fleet_dir: Path) -> None:
    # `claude` missing or the worktree pruned raises OSError; attach must degrade
    # to None (ADR-003), not crash.
    run = _launched(fleet_dir)
    runs.finish(run, runs.NEEDS_HUMAN, detail="which db?")

    def broken_session(*_a: object) -> None:
        raise OSError("claude: command not found")

    assert work.attach("alpha", session=broken_session) is None


def test_cli_work_attach_reports_a_session_failure(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from projects_orchestrator.__main__ import main

    run = _launched(fleet_dir)
    runs.finish(run, runs.NEEDS_HUMAN, detail="which db?")

    def broken(*_a: object) -> None:
        raise OSError("no claude")

    monkeypatch.setattr(work, "_default_session", broken)
    assert main(["work", "alpha", "--attach", "--root", str(fleet_dir)]) == 2
    assert "could not open a session" in capsys.readouterr().err


# --- Cost accounting (#146) ---------------------------------------------------

_COST_RESULT = {
    "type": "result",
    "num_turns": 4,
    "total_cost_usd": 0.42,
    "usage": {"input_tokens": 100, "output_tokens": 50},
}


def _agent_writing_cost(payload: dict[str, object]) -> work.Agent:
    """An agent that ends the way the real CLI does: a result object on its log."""

    def agent(_tree: Path, _prompt: str, log_path: Path) -> bool:
        log_path.write_text(json.dumps(payload), encoding="utf-8")
        return True

    return agent


def _silent_agent(_tree: Path, _prompt: str, _log: Path) -> bool:
    """An agent killed before it could report anything — the unmetered case."""
    return False


def test_run_agent_records_the_cost_from_the_log(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    result = work.run_agent(run.id, agent=_agent_writing_cost(_COST_RESULT), land=lambda r: r)
    assert result.cost.usd == pytest.approx(0.42)


def test_run_agent_records_the_token_counts_from_the_log(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    result = work.run_agent(run.id, agent=_agent_writing_cost(_COST_RESULT), land=lambda r: r)
    assert result.cost.output_tokens == 50


def test_a_recorded_cost_survives_into_the_terminal_record(fleet_dir: Path) -> None:
    # The ordering guard: `finish` rebases onto the on-disk record, so a cost
    # banked before it must be carried through — not dropped by first-writer-wins.
    run = _launched(fleet_dir)
    work.run_agent(
        run.id,
        agent=_agent_writing_cost(_COST_RESULT),
        land=lambda r: runs.finish(r, runs.PR_OPENED, pr_url="https://x/pr/1"),
    )
    assert runs.load(run.id).cost.usd == pytest.approx(0.42)


def test_a_failed_run_still_records_what_it_cost(fleet_dir: Path) -> None:
    # A run that burned money and then failed is exactly the one you want priced.
    run = _launched(fleet_dir)
    failing = _agent_writing_cost({**_COST_RESULT, "is_error": True})

    def agent(tree: Path, prompt: str, log_path: Path) -> bool:
        failing(tree, prompt, log_path)
        return False

    result = work.run_agent(run.id, agent=agent, land=lambda r: r)
    assert result.cost.usd == pytest.approx(0.42)


def test_a_needs_human_run_still_records_what_it_cost(fleet_dir: Path) -> None:
    run = _launched(fleet_dir)
    marker = _agent_writes_marker("which db?")

    def agent(tree: Path, prompt: str, log_path: Path) -> bool:
        log_path.write_text(json.dumps(_COST_RESULT), encoding="utf-8")
        return marker(tree, prompt, log_path)

    result = work.run_agent(run.id, agent=agent, land=lambda r: r)
    assert result.cost.usd == pytest.approx(0.42)


def test_a_killed_run_is_unmetered_not_free(fleet_dir: Path) -> None:
    # The whole point: no result object on the log means UNKNOWN cost, not $0.00.
    run = _launched(fleet_dir)
    result = work.run_agent(run.id, agent=_silent_agent, land=lambda r: r)
    assert result.cost is None


# --- Budget threading (#150) --------------------------------------------------


def test_launch_records_the_default_budget_when_none_is_given(fleet_dir: Path) -> None:
    run = work.launch(_repo(fleet_dir), "t", spawn=_recording_spawn([]))
    assert run.budget_usd == work.DEFAULT_BUDGET_USD


def test_launch_records_an_explicit_budget(fleet_dir: Path) -> None:
    run = work.launch(_repo(fleet_dir), "t", 2.5, spawn=_recording_spawn([]))
    assert run.budget_usd == 2.5


def test_the_detached_runner_caps_at_the_budget_the_launcher_chose(fleet_dir: Path) -> None:
    # The whole point of persisting it: run_agent is a fresh process that reloads
    # the run from disk, so the budget the real _default_agent enforces is the
    # LAUNCHER's, read back off the record — not this file's default.
    run = work.launch(_repo(fleet_dir), "t", 3.0, spawn=_recording_spawn([]))
    seen: list[float] = []

    def agent(_tree: Path, _prompt: str, _log: Path) -> bool:
        # run_agent binds budget via functools.partial, so a plain 3-arg substitute
        # never sees it. Read it off the reloaded record, which is what the real
        # agent's partial closes over.
        seen.append(runs.load(run.id).budget_usd)
        return False

    work.run_agent(run.id, agent=agent, land=lambda r: r)
    assert seen == [3.0]


def test_the_default_agent_caps_spend_at_the_given_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[list[str]] = []

    def fake_run(command, **_kwargs):
        captured.append(command)
        raise OSError("stop here — we only want the argv")

    monkeypatch.setattr(work.subprocess, "run", fake_run)
    work._default_agent(tmp_path, "t", tmp_path / "log", budget_usd=1.25)
    argv = captured[0]
    assert argv[argv.index("--max-budget-usd") + 1] == "1.25"


def test_cli_work_budget_flag_sets_the_runs_cap(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from projects_orchestrator.__main__ import main

    _repo(fleet_dir)
    monkeypatch.setattr(work, "_default_spawn", lambda _argv, _log: os.getpid())
    launched: list[float] = []
    real_launch = work.launch

    def spy(descriptor, task, budget_usd=work.DEFAULT_BUDGET_USD, **kw):
        launched.append(budget_usd)
        return real_launch(descriptor, task, budget_usd, **kw)

    monkeypatch.setattr(work, "launch", spy)
    assert main(["work", "alpha", "t", "--budget", "2.50", "--root", str(fleet_dir)]) == 0
    assert launched == [2.5]


def test_cli_work_without_budget_uses_the_default(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from projects_orchestrator.__main__ import main

    _repo(fleet_dir)
    monkeypatch.setattr(work, "_default_spawn", lambda _argv, _log: os.getpid())
    seen: list[float] = []
    monkeypatch.setattr(
        work,
        "launch",
        lambda d, t, b=work.DEFAULT_BUDGET_USD, **_k: seen.append(b) or runs.new_run(d.name, t),
    )
    main(["work", "alpha", "t", "--root", str(fleet_dir)])
    assert seen == [work.DEFAULT_BUDGET_USD]


def test_cli_work_rejects_a_non_positive_budget(fleet_dir: Path, capsys) -> None:
    from projects_orchestrator.__main__ import main

    _repo(fleet_dir)
    assert main(["work", "alpha", "t", "--budget", "0", "--root", str(fleet_dir)]) == 2
    assert "finite positive number" in capsys.readouterr().err


def test_cli_work_rejects_a_non_finite_budget(fleet_dir: Path, capsys) -> None:
    from projects_orchestrator.__main__ import main

    _repo(fleet_dir)
    assert main(["work", "alpha", "t", "--budget", "inf", "--root", str(fleet_dir)]) == 2
    assert "finite" in capsys.readouterr().err
