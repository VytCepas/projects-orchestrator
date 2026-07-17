"""Agent runs: a record that outlives its process, and never lies about it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from projects_orchestrator.cost import RunCost
from projects_orchestrator.runs import (
    ABANDONED,
    DEFAULT_BUDGET_USD,
    FAILED,
    NEEDS_HUMAN,
    PR_OPENED,
    QUEUED,
    RUNNING,
    TERMINAL,
    AgentRun,
    finish,
    forget,
    latest_open_run,
    list_runs,
    load,
    mark_running,
    new_run,
    record_cost,
    resolve,
    save,
    state_dir,
)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _sleeper() -> subprocess.Popen[bytes]:
    """A real, live child process to probe."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


# --- The record ---------------------------------------------------------------


def test_a_new_run_starts_queued() -> None:
    assert new_run("alpha", "fix the lint").state == QUEUED


def test_run_ids_are_unique_across_rapid_creation() -> None:
    ids = {new_run("alpha", "t").id for _ in range(50)}
    assert len(ids) == 50


def test_the_run_id_carries_the_project_for_human_legibility() -> None:
    assert new_run("alpha", "t").id.startswith("alpha-")


def test_pr_opened_is_terminal() -> None:
    # The run is over even though the WORK is not. Conflating those is exactly
    # how fourteen PRs sit unread behind an empty-looking table.
    assert PR_OPENED in TERMINAL


def test_running_is_not_terminal() -> None:
    assert RUNNING not in TERMINAL


# --- Persistence --------------------------------------------------------------


def test_a_saved_run_round_trips() -> None:
    run = new_run("alpha", "fix the lint")
    save(run)
    loaded = load(run.id)
    assert loaded is not None
    assert loaded.task == "fix the lint"


def test_a_run_survives_the_process_that_made_it() -> None:
    # The whole premise: the record is on disk, not in memory.
    run = finish(new_run("alpha", "t"), PR_OPENED, pr_url="https://example/pr/14")
    assert load(run.id).pr_url == "https://example/pr/14"


def test_loading_an_unknown_run_is_none_not_a_crash() -> None:
    assert load("no-such-run") is None


def test_a_corrupt_record_is_none_not_a_crash() -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / "broken.json").write_text("{not json", encoding="utf-8")
    assert load("broken") is None


def test_a_corrupt_record_does_not_sink_the_whole_listing() -> None:
    # One torn file must not hide every healthy run behind it.
    save(new_run("alpha", "good"))
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / "broken.json").write_text("{not json", encoding="utf-8")
    assert len(list_runs()) == 1


def test_save_is_atomic_leaving_no_partial_file_behind() -> None:
    run = new_run("alpha", "t")
    save(run)
    assert list(state_dir().glob("*.tmp")) == []


# --- Listing ------------------------------------------------------------------


def test_list_runs_filters_by_project() -> None:
    save(new_run("alpha", "t"))
    save(new_run("beta", "t"))
    assert [r.project for r in list_runs("beta")] == ["beta"]


def test_list_runs_is_empty_before_anything_has_run() -> None:
    assert list_runs() == []


def test_list_runs_includes_finished_runs() -> None:
    # A run that ENDED is exactly the one the operator still has to act on.
    finish(new_run("alpha", "t"), PR_OPENED, pr_url="https://example/pr/1")
    assert [r.state for r in list_runs()] == [PR_OPENED]


# --- The pessimistic invariant: silence is not success -------------------------


def test_a_running_run_whose_process_died_resolves_to_failed() -> None:
    # THE load-bearing rule. The process is gone and never recorded an outcome,
    # so we do not know that it worked — and must not imply that it did.
    proc = _sleeper()
    run = mark_running(new_run("alpha", "t"), proc.pid)
    proc.kill()
    proc.wait()
    assert resolve(run).state == FAILED


def test_a_dead_run_says_why_it_is_being_called_failed() -> None:
    proc = _sleeper()
    run = mark_running(new_run("alpha", "t"), proc.pid)
    proc.kill()
    proc.wait()
    assert "without recording an outcome" in resolve(run).detail


def test_a_live_run_still_reads_as_running() -> None:
    proc = _sleeper()
    try:
        run = mark_running(new_run("alpha", "t"), proc.pid)
        assert resolve(run).state == RUNNING
    finally:
        proc.kill()
        proc.wait()


def test_a_dead_process_does_not_downgrade_a_run_that_already_succeeded() -> None:
    # The process is always gone by the time a PR exists. If a dead process alone
    # meant "failed", every successful run would flip to failed the moment it
    # finished — the exact inverse of the bug above, and just as wrong.
    run = finish(new_run("alpha", "t"), PR_OPENED, pr_url="https://example/pr/1")
    assert resolve(run).state == PR_OPENED


def test_a_queued_run_is_not_failed_for_having_no_process() -> None:
    # It has not been launched yet. "No pid" is not "died".
    assert resolve(new_run("alpha", "t")).state == QUEUED


def test_the_listing_reconciles_a_crashed_run_without_being_asked() -> None:
    # Truth is derived on read, not maintained by a cleanup pass that may never
    # run — there is nothing to forget to call.
    proc = _sleeper()
    run = mark_running(new_run("alpha", "t"), proc.pid)
    proc.kill()
    proc.wait()
    assert list_runs()[0].state == FAILED
    assert load(run.id).state == FAILED


def test_a_recycled_pid_is_not_mistaken_for_our_run() -> None:
    # Some other process now holds that pid. Reporting a stranger as "your agent
    # is still running" is worse than reporting nothing.
    proc = _sleeper()
    try:
        run = mark_running(new_run("alpha", "t"), proc.pid)
        impostor = AgentRun(**{**vars(run), "start_ticks": (run.start_ticks or 0) + 1})
        assert resolve(impostor).state == FAILED
    finally:
        proc.kill()
        proc.wait()


# --- Finishing ----------------------------------------------------------------


def test_finish_records_needs_human_with_its_reason() -> None:
    run = finish(new_run("alpha", "t"), NEEDS_HUMAN, detail="which database is canonical?")
    assert run.state == NEEDS_HUMAN
    assert "canonical" in load(run.id).detail


def test_finish_records_abandoned() -> None:
    assert finish(new_run("alpha", "t"), ABANDONED, detail="stopped").state == ABANDONED


def test_finish_stamps_an_end_time() -> None:
    assert finish(new_run("alpha", "t"), FAILED, detail="boom").ended_at != ""


def test_finishing_into_a_non_terminal_state_is_coerced_to_failed() -> None:
    # An unknown outcome is not a good one. Recording `running` as an outcome
    # would strand the run in exactly the limbo this module exists to prevent.
    assert finish(new_run("alpha", "t"), RUNNING).state == FAILED


def test_a_finished_run_is_terminal() -> None:
    assert finish(new_run("alpha", "t"), PR_OPENED).is_terminal


# --- Name safety --------------------------------------------------------------


def test_a_traversing_project_name_cannot_escape_the_state_dir() -> None:
    # The name comes from a CHILD repo's own config.yaml — it is not ours.
    run = new_run("../../etc/passwd", "t")
    save(run)
    written = list(state_dir().glob("*.json"))
    assert len(written) == 1
    assert written[0].parent == state_dir()


# (safe_component's own behaviour is covered in test_worktree/naming; here we
# only pin that runs.py actually USES it — the traversal test above.)


# --- Forgetting ---------------------------------------------------------------


def test_forget_removes_a_run() -> None:
    run = new_run("alpha", "t")
    save(run)
    assert forget(run.id) is True
    assert load(run.id) is None


def test_forgetting_an_unknown_run_is_true_not_a_crash() -> None:
    assert forget("no-such-run") is True


def test_the_state_file_is_json_a_human_can_read() -> None:
    run = new_run("alpha", "fix the lint")
    save(run)
    raw = json.loads((state_dir() / f"{run.id}.json").read_text(encoding="utf-8"))
    assert raw["task"] == "fix the lint"


def test_state_dir_honors_xdg_state_home(tmp_path: Path) -> None:
    assert str(tmp_path / "state") in str(state_dir())
    assert os.environ["XDG_STATE_HOME"] in str(state_dir())


# --- A corrupt pid must not describe itself as alive --------------------------


def test_a_negative_pid_is_not_alive() -> None:
    # On POSIX these are BROADCAST selectors, not process ids: kill(-1, 0)
    # addresses every process the caller may signal and returns cleanly, so a
    # corrupt record claiming pid -1 would read as a live, healthy run forever.
    from projects_orchestrator.procs import pid_alive

    assert pid_alive(-1) is False


def test_pid_zero_is_not_alive() -> None:
    # 0 means "my whole process group" — no better than -1.
    from projects_orchestrator.procs import pid_alive

    assert pid_alive(0) is False


def test_a_corrupt_running_record_with_a_negative_pid_resolves_to_failed() -> None:
    run = new_run("alpha", "t")
    save(run)
    path = state_dir() / f"{run.id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update({"state": RUNNING, "pid": -1})
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert load(run.id).state == FAILED


# --- Terminal states never leave, even against a stale handle ------------------


def test_finish_does_not_bury_an_already_recorded_success() -> None:
    # The caller holds an AgentRun captured at launch. Something else — a cleanup
    # pass, a second CLI invocation, a --stop racing a natural finish — settled
    # the same id since. Writing the stale copy would hide a real PR from every
    # listing. First writer wins.
    run = new_run("alpha", "t")
    save(run)
    finish(run, PR_OPENED, pr_url="https://example/pr/14")

    settled = finish(run, ABANDONED, detail="stopped by the operator")
    assert settled.state == PR_OPENED
    assert settled.pr_url == "https://example/pr/14"
    assert load(run.id).state == PR_OPENED


def test_finish_still_records_a_success_after_the_agent_process_has_exited() -> None:
    # The trap in guarding the above: `resolve` turns a running record whose
    # process is gone into `failed`, and the agent's process is ALWAYS gone by
    # the time we record pr-opened. A finish() that consulted the RESOLVED record
    # would see `failed`, call it terminal, and refuse every real success.
    proc = _sleeper()
    run = mark_running(new_run("alpha", "t"), proc.pid)
    proc.kill()
    proc.wait()

    settled = finish(run, PR_OPENED, pr_url="https://example/pr/9")
    assert settled.state == PR_OPENED
    assert load(run.id).pr_url == "https://example/pr/9"


def test_finish_on_a_never_saved_run_still_records() -> None:
    assert finish(new_run("alpha", "t"), FAILED, detail="boom").state == FAILED


def test_latest_open_run_returns_the_newest_open_run() -> None:
    newest_open = AgentRun(id="a", project="p", task="t", state=PR_OPENED)
    older_open = AgentRun(id="b", project="p", task="t", state=RUNNING)
    assert latest_open_run([newest_open, older_open]) is newest_open


def test_latest_open_run_skips_settled_runs_to_find_an_open_one() -> None:
    settled = AgentRun(id="a", project="p", task="t", state=FAILED)
    open_run = AgentRun(id="b", project="p", task="t", state=NEEDS_HUMAN)
    assert latest_open_run([settled, open_run]) is open_run


def test_latest_open_run_is_none_when_all_settled() -> None:
    a = AgentRun(id="a", project="p", task="t", state=FAILED)
    b = AgentRun(id="b", project="p", task="t", state=ABANDONED)
    assert latest_open_run([a, b]) is None


# --- Cost on the record (#146) ------------------------------------------------


def test_record_cost_persists_the_cost() -> None:
    run = new_run("alpha", "t")
    save(run)
    record_cost(run, RunCost(usd=0.75, output_tokens=10))
    assert load(run.id).cost.usd == 0.75


def test_record_cost_of_an_unknown_cost_leaves_the_run_unmetered() -> None:
    run = new_run("alpha", "t")
    save(run)
    record_cost(run, None)
    assert load(run.id).cost is None


def test_record_cost_does_not_write_an_unknown_cost_as_zero() -> None:
    run = new_run("alpha", "t")
    save(run)
    assert record_cost(run, None).cost is None


def test_a_run_recorded_before_cost_existed_reloads_as_unmetered() -> None:
    # Back-compat: an old record simply has no `cost` key. It is unmetered, and
    # must not be resurrected as a $0.00 run.
    run = new_run("alpha", "t")
    save(run)
    path = state_dir() / f"{run.id}.json"
    raw = json.loads(path.read_text())
    del raw["cost"]
    path.write_text(json.dumps(raw))
    assert load(run.id).cost is None


def test_a_cost_survives_a_finish() -> None:
    run = new_run("alpha", "t")
    save(run)
    priced = record_cost(run, RunCost(usd=0.5))
    finish(priced, PR_OPENED, pr_url="https://x/pr/1")
    assert load(run.id).cost.usd == 0.5


def test_mark_running_does_not_rewind_a_run_that_already_finished() -> None:
    # `launch` spawns the detached agent and only THEN calls mark_running — there
    # is no pid to record until it has. An agent that fails instantly, or a racing
    # `work --stop`, settles the record inside that window; writing the pre-spawn
    # copy would resurrect a finished run as live work.
    run = new_run("alpha", "t")
    save(run)
    finish(run, PR_OPENED, pr_url="https://x/pr/1")  # the agent beat us to it
    mark_running(run, 1234)
    assert load(run.id).state == PR_OPENED


def test_mark_running_does_not_bury_the_pr_url_of_a_run_that_already_landed() -> None:
    # The PR is the whole product of the run; rewinding to `running` makes it
    # invisible to every listing.
    run = new_run("alpha", "t")
    save(run)
    finish(run, PR_OPENED, pr_url="https://x/pr/1")
    mark_running(run, 1234)
    assert load(run.id).pr_url == "https://x/pr/1"


def test_mark_running_returns_the_settled_record_not_a_running_one() -> None:
    run = new_run("alpha", "t")
    save(run)
    finish(run, ABANDONED, detail="operator stopped it")
    assert mark_running(run, 1234).state == ABANDONED


def test_record_cost_does_not_rewind_a_run_another_process_already_stopped() -> None:
    # THE race: the wrapper holds a stale `running` copy; `work --stop` settled the
    # same id to `abandoned` meanwhile. Saving our stale copy "just to add a cost"
    # would bury the operator's stop. First writer wins applies to cost too.
    run = new_run("alpha", "t")
    save(run)
    stale = mark_running(run, 1234)
    finish(stale, ABANDONED, detail="operator stopped it")  # the racing stop
    record_cost(stale, RunCost(usd=0.9))
    assert load(run.id).state == ABANDONED


def test_record_cost_still_prices_a_run_another_process_stopped() -> None:
    # ...and the stopped run's cost is still recorded. It burned money before it
    # was killed; preserving the state must not mean discarding the price.
    run = new_run("alpha", "t")
    save(run)
    stale = mark_running(run, 1234)
    finish(stale, ABANDONED, detail="operator stopped it")
    record_cost(stale, RunCost(usd=0.9))
    assert load(run.id).cost.usd == 0.9


def test_record_cost_preserves_the_stop_reason() -> None:
    run = new_run("alpha", "t")
    save(run)
    stale = mark_running(run, 1234)
    finish(stale, ABANDONED, detail="operator stopped it")
    record_cost(stale, RunCost(usd=0.9))
    assert "operator stopped it" in load(run.id).detail


def test_a_run_keeps_its_first_recorded_price() -> None:
    run = new_run("alpha", "t")
    save(run)
    record_cost(run, RunCost(usd=0.5))
    record_cost(run, RunCost(usd=9.99))
    assert load(run.id).cost.usd == 0.5


# --- Budget on the record (#150) ----------------------------------------------


def test_a_new_run_has_the_default_budget() -> None:
    assert new_run("alpha", "t").budget_usd == DEFAULT_BUDGET_USD


def test_a_budget_round_trips_through_persistence() -> None:
    run = replace(new_run("alpha", "t"), budget_usd=12.5)
    save(run)
    assert load(run.id).budget_usd == 12.5


def test_a_run_recorded_before_budgets_reloads_at_the_default() -> None:
    # Back-compat: an old record has no `budget_usd` key. It must not reload as 0.
    run = new_run("alpha", "t")
    save(run)
    path = state_dir() / f"{run.id}.json"
    raw = json.loads(path.read_text())
    del raw["budget_usd"]
    path.write_text(json.dumps(raw))
    assert load(run.id).budget_usd == DEFAULT_BUDGET_USD


def test_a_corrupt_non_positive_budget_falls_back_to_the_default() -> None:
    # A run must never launch under a $0 cap it did not ask for.
    run = new_run("alpha", "t")
    save(run)
    path = state_dir() / f"{run.id}.json"
    raw = json.loads(path.read_text())
    raw["budget_usd"] = 0
    path.write_text(json.dumps(raw))
    assert load(run.id).budget_usd == DEFAULT_BUDGET_USD


def test_a_corrupt_non_numeric_budget_falls_back_to_the_default() -> None:
    run = new_run("alpha", "t")
    save(run)
    path = state_dir() / f"{run.id}.json"
    raw = json.loads(path.read_text())
    raw["budget_usd"] = "lots"
    path.write_text(json.dumps(raw))
    assert load(run.id).budget_usd == DEFAULT_BUDGET_USD


def test_a_non_finite_persisted_budget_falls_back_to_the_default() -> None:
    # nan/inf survive float() and pass every `> 0` test; an inf cap is no cap.
    for bad in ("Infinity", "NaN"):
        run = new_run("alpha", "t")
        save(run)
        path = state_dir() / f"{run.id}.json"
        raw = json.loads(path.read_text())
        raw["budget_usd"] = float(bad)
        # json.dumps emits bare Infinity/NaN (non-standard but Python round-trips it).
        path.write_text(json.dumps(raw))
        assert load(run.id).budget_usd == DEFAULT_BUDGET_USD
