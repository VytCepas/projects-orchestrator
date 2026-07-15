"""Agent runs — a record whose life is longer than its process.

:mod:`supervisor` tracks a process: the pid is alive or it is not. An **agent
run** cannot be modelled that way, because it has a state the process model
cannot express — *"finished, opened PR #14, awaiting your review."* That is
neither running nor dead, and it is the state a fleet operator most needs to see.

So the record outlives the process. A run reaches one of four **terminal** states
and stays there:

``pr-opened``
    The work is up for review. The process is long gone; the run is not.
``failed``
    It did not produce a PR. Its worktree is retained as evidence (ADR-007).
``needs-human``
    It hit an ambiguity. A headless agent cannot ask, so it must not guess: it
    stops, and the operator picks the run up interactively.
``abandoned``
    Someone stopped it.

**The invariant that matters most is the pessimistic one.** A run recorded as
``running`` whose process is gone — killed, OOM'd, host rebooted — never
recorded an outcome, so we do not know that it succeeded. It resolves to
``failed``. The alternative (leave it reading ``running`` forever, or infer
success from a clean exit we never observed) is how a fleet table ends up showing
green while nothing happened. **Silence is not success.**

Reads are pure and never raise (ADR-003): an unreadable state file, a truncated
JSON blob, or a pid we no longer own degrade to ``None``/``failed`` rather than
an exception.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import uuid4

from projects_orchestrator.cost import RunCost, from_record
from projects_orchestrator.naming import safe_component
from projects_orchestrator.procs import is_our_process, proc_start_ticks

_STATE_DIRNAME = "projects-orchestrator"
_RUNS_SUBDIR = "runs"

#: Default per-run spend cap in USD. A default, not a constant: a campaign sets it
#: via ``policy.max_budget_usd`` and a single run via ``work --budget``. It lives
#: on the run record (below) so the detached wrapper enforces the launcher's cap.
DEFAULT_BUDGET_USD = 5.0

QUEUED = "queued"
RUNNING = "running"
PR_OPENED = "pr-opened"
FAILED = "failed"
NEEDS_HUMAN = "needs-human"
ABANDONED = "abandoned"

#: States a run never leaves. Note ``pr-opened`` is one of them: the run is over
#: even though the *work* is not, and conflating those is how open PRs go unread.
TERMINAL = frozenset({PR_OPENED, FAILED, NEEDS_HUMAN, ABANDONED})

#: States that represent OPEN WORK an operator still owns: about to start, in
#: flight, awaiting review, or blocked on a human. A run in one of these is what
#: the fleet's Work column must never hide (#123). ``failed``/``abandoned`` are
#: excluded deliberately: they are settled outcomes the operator has already seen
#: (a failed run even keeps its worktree as evidence), not work still in the air.
OPEN_WORK = frozenset({QUEUED, RUNNING, PR_OPENED, NEEDS_HUMAN})

_DIED_WITHOUT_OUTCOME = "the run's process exited without recording an outcome"


@dataclass(frozen=True)
class AgentRun:
    """One agent run, as recorded on disk.

    Attributes:
        id: Run identifier; also its state-file and log-file stem.
        project: Project the run targets.
        task: What the agent was asked to do.
        state: One of the module-level state constants.
        started_at: UTC ISO timestamp of the launch.
        pid: Process id of the agent, when one was launched.
        start_ticks: The pid's start time, to defeat pid reuse (see :mod:`procs`).
        worktree: The isolated checkout the agent worked in.
        branch: The branch it worked on.
        log_path: File capturing the agent's output.
        pr_url: The PR it opened, only when ``state == PR_OPENED``.
        detail: Human-readable explanation — why it failed, or what it needs.
        ended_at: UTC ISO timestamp of the terminal transition, when recorded.
        cost: What the run cost, or ``None`` when it could not be metered — a
            killed or timed-out agent never reports its spend, and ``None`` says
            *we do not know*, which is not the same as free (:mod:`cost`).
        budget_usd: The spend cap this run was launched under, in USD. Set by the
            launcher (a campaign's policy, or ``work --budget``) and recorded here
            so the detached wrapper enforces the cap the launcher chose.
    """

    id: str
    project: str
    task: str
    state: str
    started_at: str = ""
    pid: int = 0
    start_ticks: int | None = None
    worktree: str = ""
    branch: str = ""
    log_path: str = ""
    pr_url: str = ""
    detail: str = ""
    ended_at: str = ""
    cost: RunCost | None = None
    budget_usd: float = DEFAULT_BUDGET_USD

    @property
    def is_terminal(self) -> bool:
        """Whether this run has finished, for any value of finished."""
        return self.state in TERMINAL


def state_dir() -> Path:
    """Return the run-state directory, honoring ``$XDG_STATE_HOME``."""
    base = os.environ.get("XDG_STATE_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / _STATE_DIRNAME / _RUNS_SUBDIR


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def new_run(project: str, task: str) -> AgentRun:
    """Mint a queued run. Nothing is launched and nothing is written yet."""
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%S")
    run_id = f"{safe_component(project)}-{stamp}-{uuid4().hex[:6]}"
    return AgentRun(id=run_id, project=project, task=task, state=QUEUED, started_at=_now())


def _run_file(run_id: str) -> Path:
    return state_dir() / f"{safe_component(run_id)}.json"


def save(run: AgentRun) -> bool:
    """Persist a run; report success (never raises).

    Written atomically — a half-written record read back by a concurrent ``list``
    would be indistinguishable from a corrupt one, and we would call a live run
    dead on the strength of a torn read.
    """
    path = _run_file(run.id)
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(asdict(run), indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return False
    return True


def _as_budget(raw: object) -> float:
    """A recorded budget, or the default when it is absent or malformed.

    A run written before budgets were recorded has no ``budget_usd`` key, and a
    corrupt one may hold a non-number or a non-positive value; all of those fall
    back to :data:`DEFAULT_BUDGET_USD` rather than an unusable cap. A run must
    never launch with a ``$0`` cap it did not ask for.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return DEFAULT_BUDGET_USD
    return float(raw)


def _parse(raw: object) -> AgentRun | None:
    """Build a run from a decoded JSON blob; ``None`` if it is not one."""
    if not isinstance(raw, dict):
        return None
    ticks = raw.get("start_ticks")
    try:
        # A non-positive pid is not a process id on POSIX, it is a broadcast
        # selector — see procs.pid_alive. A corrupt record claiming `pid: -1`
        # must not be able to describe itself as alive, so it is normalised to
        # "no pid" here as well as being rejected at the probe.
        pid = int(raw.get("pid", 0))
        return AgentRun(
            id=str(raw["id"]),
            project=str(raw["project"]),
            task=str(raw.get("task", "")),
            state=str(raw.get("state", FAILED)),
            started_at=str(raw.get("started_at", "")),
            pid=pid if pid > 0 else 0,
            start_ticks=int(ticks) if isinstance(ticks, int) else None,
            worktree=str(raw.get("worktree", "")),
            branch=str(raw.get("branch", "")),
            log_path=str(raw.get("log_path", "")),
            pr_url=str(raw.get("pr_url", "")),
            detail=str(raw.get("detail", "")),
            ended_at=str(raw.get("ended_at", "")),
            cost=from_record(raw.get("cost")),
            budget_usd=_as_budget(raw.get("budget_usd")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def resolve(run: AgentRun) -> AgentRun:
    """Reconcile a recorded run against the world; never raises.

    A terminal run is returned untouched — it already said what happened. A run
    still marked ``running`` is only *actually* running if its process is alive
    **and is still ours** (:mod:`procs` guards pid reuse). Otherwise its process
    died without recording an outcome, and it resolves to ``failed``.

    That last step is the whole point. A crashed run that keeps reading
    ``running`` is a fleet table that lies by omission, and one that reads
    ``pr-opened`` because we assumed the best is a fleet table that simply lies.
    """
    if run.is_terminal or run.state == QUEUED:
        return run
    if run.pid and is_our_process(run.pid, run.start_ticks):
        return run
    return replace(
        run,
        state=FAILED,
        detail=run.detail or _DIED_WITHOUT_OUTCOME,
        ended_at=run.ended_at or _now(),
    )


def _read(run_id: str) -> AgentRun | None:
    """Read one run exactly as recorded — **without** reconciling it.

    The distinction is load-bearing, not stylistic. :func:`resolve` turns a
    ``running`` record whose process is gone into ``failed`` — and by the time a
    caller records ``pr-opened``, the agent's process is *always* gone. So a
    ``finish`` that consulted :func:`load` would see ``failed`` (terminal), decide
    the run had already settled, and refuse to record the success. Reconciliation
    is for *reporting* what happened; it must not be consulted when *deciding*
    what happened.
    """
    try:
        raw = json.loads(_run_file(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _parse(raw)


def load(run_id: str) -> AgentRun | None:
    """Read one run, reconciled against the world; ``None`` if unreadable."""
    parsed = _read(run_id)
    return resolve(parsed) if parsed else None


def list_runs(project: str = "") -> list[AgentRun]:
    """Read every run (optionally for one project), newest first.

    Every run is reconciled, so a crashed process shows as ``failed`` here
    without anything having to have run to "clean it up" — the truth is derived,
    not maintained.
    """
    try:
        files = sorted(state_dir().glob("*.json"))
    except OSError:
        return []
    runs: list[AgentRun] = []
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # a torn or corrupt record is not a reason to fail the list
        parsed = _parse(raw)
        if parsed is None or (project and parsed.project != project):
            continue
        runs.append(resolve(parsed))
    return sorted(runs, key=lambda run: run.started_at, reverse=True)


def latest_open_run(runs: list[AgentRun]) -> AgentRun | None:
    """The most recent run that is still OPEN WORK, or ``None``.

    "Open work" is :data:`OPEN_WORK`: queued, running, awaiting review, or blocked
    on a human. Given runs newest-first (as :func:`list_runs` returns them), this
    is the run the fleet's Work column should show for a project — and returning
    ``None`` when every run has settled is how the column reads ``-`` rather than
    surfacing an old failure as if it were live work.
    """
    return next((run for run in runs if run.state in OPEN_WORK), None)


def mark_running(run: AgentRun, pid: int) -> AgentRun:
    """Record that ``run``'s agent is live under ``pid``, and persist it."""
    started = replace(run, state=RUNNING, pid=pid, start_ticks=proc_start_ticks(pid))
    save(started)
    return started


def record_cost(run: AgentRun, spent: RunCost | None) -> AgentRun:
    """Attach a run's metered cost and persist it; ``None`` leaves it unmetered.

    Call this **before** :func:`finish`. ``finish`` rebuilds the run from the
    record on disk (first writer wins), so a cost written afterwards would be
    dropped on the floor — whereas one written first is read back and carried
    into the terminal record.

    **Cost is written onto the record on disk, never onto the caller's copy.** The
    caller here is the detached wrapper, holding an ``AgentRun`` it loaded before
    the agent ran; meanwhile a racing ``work --stop`` may already have settled the
    same id to ``abandoned``. Saving our stale copy — even "just to add a cost" —
    would rewind that terminal record to ``running`` and bury the operator's stop,
    which is precisely the clobber :func:`finish` refuses to make. So we re-read,
    attach the cost to whatever is *actually* recorded, and let its state stand.

    A run that is already priced keeps its first price: same first-writer-wins
    rule, applied to the number as well as the state.

    An unknown cost is *not written as zero*. It is not written at all, and the
    run stays unmetered, which is the truth (:mod:`cost`).
    """
    if spent is None:
        return run
    base = _read(run.id) or run
    if base.cost is not None:
        return base
    priced = replace(base, cost=spent)
    save(priced)
    return priced


def finish(run: AgentRun, state: str, detail: str = "", pr_url: str = "") -> AgentRun:
    """Move a run to a terminal state and persist it.

    Args:
        run: The run to finish.
        state: A member of :data:`TERMINAL`.
        detail: Why — required in spirit for every state but ``pr-opened``.
        pr_url: The PR that was opened, for ``pr-opened``.

    Returns:
        The finished run. A non-terminal ``state`` is coerced to ``failed``
        rather than quietly recorded: an unknown outcome is not a good one.

        If the record on disk is **already terminal**, it is returned unchanged
        and nothing is written. "Terminal states never leave" has to hold against
        a *stale handle*, not merely against tidy sequential code: the caller here
        holds an ``AgentRun`` captured at launch, and something else — a cleanup
        pass, a second CLI invocation, a `--stop` racing a natural finish — may
        have settled the same id since. Writing our stale copy would let a late
        `abandoned` bury an already-recorded `pr-opened`, and the PR it names
        would then be invisible to every listing. First writer wins.
    """
    persisted = _read(run.id)
    if persisted is not None and persisted.is_terminal:
        return persisted

    settled = state if state in TERMINAL else FAILED
    ended = replace(
        persisted or run,
        state=settled,
        detail=detail or run.detail,
        pr_url=pr_url or run.pr_url,
        ended_at=_now(),
    )
    save(ended)
    return ended


def forget(run_id: str) -> bool:
    """Delete one run's record; report whether it is gone (never raises)."""
    path = _run_file(run_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return not path.exists()
