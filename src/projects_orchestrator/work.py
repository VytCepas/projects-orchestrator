"""``work`` — put an agent to work on a project, as a tracked, detached run.

This is the verb the whole floor was built for. It composes the five pieces that
each landed on their own, and every one of them is load-bearing here:

- :mod:`runs` — the record that outlives the process, so a launched agent can be
  listed, tailed, and killed after the launching command has returned.
- :mod:`worktree` — a throwaway checkout, so the agent never touches the
  operator's clone (ADR-007 §3).
- :mod:`briefing` — the injected "why you are here", so the agent starts informed.
- :mod:`landing` — the write boundary, so the only thing a run can produce is a
  draft PR on its own branch (ADR-007 §3).
- :mod:`sandbox` — the scrubbed environment, so the agent cannot reach the data
  plane whatever it is asked to do (ADR-007 §4).

**Detached, not synchronous.** ``heal`` runs its agent inline and blocks. ``work``
must not: an operator launches a run and gets their shell back, then lists / tails
/ stops it later. So :func:`launch` spawns a *wrapper* process — this program
re-invoking itself — which runs the agent and lands the result on its own time.
The wrapper is a session leader (``start_new_session``), so :func:`stop` can kill
the whole agent process tree, and its liveness is what :mod:`runs` reconciles a
crashed run against.

Everything external is injectable (``spawn``, ``agent``, ``land``) and nothing
raises (ADR-003): a repo that will not yield a worktree, a wrapper that will not
start, or an agent that dies all degrade to a terminal :class:`~runs.AgentRun`
the caller renders.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from projects_orchestrator import briefing, landing, runs, sandbox
from projects_orchestrator import worktree as wt
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.naming import safe_component
from projects_orchestrator.procs import terminate_group

AGENT_TIMEOUT = 1800.0  # a real task may take many tool calls
_MAX_BUDGET_USD = "5.00"
_STOP_GRACE_SECONDS = 5.0
DEFAULT_LOG_LINES = 40

# A work agent may run the project's own tooling (build, test, format) — that is
# most of what a coding task needs. General Bash is acceptable HERE, unlike heal,
# precisely because the environment is scrubbed of every credential (sandbox) and
# the checkout is a throwaway worktree, not the operator's clone: the containment
# is the environment and the output boundary, not a narrow toolset.
_AGENT_TOOLS = "Edit,Write,Read,Grep,Glob,Bash"

#: argv[1] of the wrapper process. Hidden (leading underscore): it is this program
#: re-invoking itself to BE the detached run, not a verb an operator ever types.
RUNNER_SUBCOMMAND = "_run-agent"

# Callable seams, so tests never spawn a process or a real agent.
Spawn = Callable[[list[str], Path], int]
Agent = Callable[[Path, str, Path], bool]
Land = Callable[[runs.AgentRun], runs.AgentRun]


def _prompt_path(run_id: str) -> Path:
    """Where a run's briefing is stashed for the detached wrapper to read.

    The prompt is written to a file rather than passed on the wrapper's command
    line: it is large, and it contains untrusted child output (:mod:`briefing`),
    neither of which belongs in an argv another process can read from ``ps``.
    """
    return runs.state_dir() / f"{safe_component(run_id)}.prompt"


def _log_path(run_id: str) -> Path:
    """Where a run's agent output is captured."""
    return runs.state_dir() / f"{safe_component(run_id)}.log"


def _default_spawn(argv: list[str], log_path: Path) -> int:
    """Launch the wrapper detached, output to ``log_path``; return its pid.

    Mirrors :mod:`supervisor`: ``start_new_session`` makes the child its own
    process-group leader, so the agent tree can be signalled as a group and a
    recycled pid can be told apart from ours.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(  # noqa: S603 — fixed argv (this program), no shell
            argv,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return process.pid


def _default_agent(worktree: Path, prompt: str, log_path: Path) -> bool:
    """Run the ``claude`` CLI in ``worktree`` with the data plane scrubbed out.

    Returns whether the process exited 0. Output streams to ``log_path`` so a
    detached run can be tailed. The environment carries no operator credential
    and a fresh HOME (:mod:`sandbox`), so even the general Bash tool cannot reach
    production.
    """
    command = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        _AGENT_TOOLS,
        "--max-budget-usd",
        _MAX_BUDGET_USD,
    ]
    try:
        with (
            tempfile.TemporaryDirectory(prefix="po-agent-home-") as home,
            log_path.open("a") as log,
        ):
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell; cwd is the throwaway worktree
                command,
                cwd=worktree,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=AGENT_TIMEOUT,
                check=False,
                env=sandbox.agent_env(home=home),
            )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _default_land(run: runs.AgentRun) -> runs.AgentRun:
    """Commit the agent's edits, push the branch, open a draft PR; then clean up.

    The commit is the step it is easy to forget and fatal to skip: the agent is
    told NOT to commit (the briefing's contract), so its edits sit uncommitted in
    the worktree. Pushing without committing sends only the branch ref cut from
    HEAD — an empty PR, or a ``gh pr create`` that fails for having no commits.
    """
    tree = Path(run.worktree)

    committed = landing.commit_all(tree, f"agent: {run.task[:72]}")
    if committed.status == landing.NOTHING_TO_COMMIT:
        return runs.finish(run, runs.FAILED, detail="the agent changed nothing — nothing to land")
    if not committed.ok:
        return runs.finish(run, runs.FAILED, detail=committed.detail)

    pushed = landing.push_branch(tree, run.branch, repo_default=landing.default_branch(tree))
    if not pushed.ok:
        return runs.finish(run, runs.FAILED, detail=pushed.detail)

    body = (
        f"Opened by projects-orchestrator `work` for run `{run.id}`.\n\n"
        f"Task: {run.task}\n\nReview before merging — this is unreviewed agent output."
    )
    opened = landing.open_draft_pr(tree, run.branch, f"agent: {run.task[:60]}", body)
    if not opened.ok:
        return runs.finish(run, runs.FAILED, detail=opened.detail)

    # The work is in the PR now, so the checkout is redundant. Only FAILED runs
    # keep their worktree as evidence (ADR-007); a landed one is removed here, or
    # the state dir accretes a dead worktree per successful run forever.
    wt.remove_path(tree)
    return runs.finish(run, runs.PR_OPENED, pr_url=opened.pr_url)


def launch(
    descriptor: ProjectDescriptor, task: str, *, spawn: Spawn | None = None
) -> runs.AgentRun:
    """Start a tracked, detached agent run against ``descriptor``; never raises.

    The worktree is cut and the briefing built **synchronously**, so a repo that
    cannot yield a checkout fails right here in the operator's shell rather than
    silently inside a detached process they would only discover via ``--list``.
    Only the agent itself is detached.

    ``spawn`` defaults to :func:`_default_spawn`, resolved at CALL time (not bound
    as a default argument) so a test — or the CLI — can substitute it. A default
    argument would capture the original function and defeat monkeypatching, which
    is exactly the flake that taught this lesson.
    """
    spawn = spawn or _default_spawn
    run = runs.new_run(descriptor.name, task)

    wt.prune_expired(descriptor.path, descriptor.name)
    slug = wt.run_slug()
    branch = f"work/{safe_component(descriptor.name)}-{slug.rsplit('-', 1)[-1]}"
    tree = wt.create(repo=descriptor.path, project=descriptor.name, branch=branch, slug=slug)
    if tree is None:
        return runs.finish(
            run,
            runs.FAILED,
            detail="could not cut an isolated worktree (branch may be held by a kept failed run)",
        )

    prompt = briefing.build_briefing(replace(descriptor, path=tree.path), task)
    log_path = _log_path(run.id)
    run = replace(run, worktree=str(tree.path), branch=branch, log_path=str(log_path))
    try:
        prompt_path = _prompt_path(run.id)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
    except OSError as exc:
        return runs.finish(run, runs.FAILED, detail=f"could not stage the briefing: {exc}")
    runs.save(run)

    try:
        pid = spawn([sys.argv[0], RUNNER_SUBCOMMAND, run.id], log_path)
    except OSError as exc:
        return runs.finish(run, runs.FAILED, detail=f"could not launch the agent: {exc}")
    return runs.mark_running(run, pid)


def run_agent(
    run_id: str, *, agent: Agent | None = None, land: Land | None = None
) -> runs.AgentRun:
    """The detached wrapper body: run the agent, then land or fail; never raises.

    This executes in the spawned process, not the operator's shell. It reads the
    run that :func:`launch` recorded, runs the agent in that run's worktree, and
    moves the run to a terminal state — ``pr-opened`` on a landed draft PR,
    ``failed`` otherwise (the worktree is kept as evidence, ADR-007). ``agent`` and
    ``land`` resolve at call time for the same monkeypatch reason as ``launch``.
    """
    agent = agent or _default_agent
    land = land or _default_land
    run = runs.load(run_id)
    if run is None:
        # Nothing to reconcile against — the record is the source of truth and it
        # is gone. Returning a synthetic failed run at least gives the caller data.
        return runs.AgentRun(
            id=run_id, project="", task="", state=runs.FAILED, detail="run not found"
        )
    if run.is_terminal:
        return run  # already settled (e.g. stopped before the wrapper got here)

    prompt = _read_prompt(run_id)
    if prompt is None:
        return runs.finish(run, runs.FAILED, detail="the staged briefing was missing")

    ok = agent(Path(run.worktree), prompt, Path(run.log_path))
    if not ok:
        return runs.finish(run, runs.FAILED, detail="the agent did not complete (see the run log)")
    return land(run)


def _read_prompt(run_id: str) -> str | None:
    try:
        return _prompt_path(run_id).read_text(encoding="utf-8")
    except OSError:
        return None


def list_runs(project: str = "") -> list[runs.AgentRun]:
    """Every run (optionally for one project), newest first, reconciled.

    Reconciliation is the point: a run whose wrapper crashed shows as ``failed``
    here without any cleanup pass having to have run (:mod:`runs`).
    """
    return runs.list_runs(project)


def logs(run_id: str, lines: int = DEFAULT_LOG_LINES) -> list[str]:
    """The tail of a run's captured agent output; ``[]`` when there is none yet."""
    run = runs.load(run_id)
    if run is None or not run.log_path:
        return []
    try:
        text = Path(run.log_path).read_text(encoding="utf-8")
    except OSError:
        return []
    return text.splitlines()[-lines:]


def stop(run_id: str, grace: float = _STOP_GRACE_SECONDS) -> runs.AgentRun | None:
    """Kill a running run's agent tree and record it ``abandoned``; ``None`` if unknown.

    A run that has already reached a terminal state is returned untouched — you
    cannot abandon a run that already opened its PR (``finish`` enforces this, so
    a stop racing a natural completion cannot bury the PR).
    """
    run = runs.load(run_id)
    if run is None:
        return None
    if run.is_terminal:
        return run
    if run.pid:
        terminate_group(run.pid, grace)
    return runs.finish(run, runs.ABANDONED, detail="stopped by the operator")


CLEARED = "cleared"
CLEAR_UNKNOWN = "unknown"
CLEAR_ACTIVE = "active"
CLEAR_FAILED = "failed"


def clear(run_id: str) -> str:
    """Forget a SETTLED run's record so it leaves the fleet's Work column.

    This is how a merged (or closed) PR clears its run: once the operator has
    dealt with the outcome, the record has served its purpose. Only a terminal run
    is cleared — a queued or running one is still open work, and forgetting it
    would strand a live agent and hide exactly what the Work column exists to show.

    Returns one of :data:`CLEARED`, :data:`CLEAR_UNKNOWN` (no such run),
    :data:`CLEAR_ACTIVE` (still live — stop it first), or :data:`CLEAR_FAILED`
    (the record could not be removed). ``CLEAR_FAILED` is its own outcome, not a
    quiet success: if :func:`runs.forget` cannot unlink the file — an unwritable
    state dir — the record survives and the run would reappear on the next read,
    so reporting "cleared" would be a lie.
    """
    run = runs.load(run_id)
    if run is None:
        return CLEAR_UNKNOWN
    if not run.is_terminal:
        return CLEAR_ACTIVE
    return CLEARED if runs.forget(run.id) else CLEAR_FAILED
