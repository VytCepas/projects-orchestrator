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
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import uuid4

from projects_orchestrator.procs import is_our_process, proc_start_ticks

_STATE_DIRNAME = "projects-orchestrator"
_RUNS_SUBDIR = "runs"

QUEUED = "queued"
RUNNING = "running"
PR_OPENED = "pr-opened"
FAILED = "failed"
NEEDS_HUMAN = "needs-human"
ABANDONED = "abandoned"

#: States a run never leaves. Note ``pr-opened`` is one of them: the run is over
#: even though the *work* is not, and conflating those is how open PRs go unread.
TERMINAL = frozenset({PR_OPENED, FAILED, NEEDS_HUMAN, ABANDONED})

#: A project name reaches us from a child's own config.yaml, and it becomes part
#: of a filename. Anything outside this set (a slash, a `..`) could walk out of
#: the state directory, so it is replaced rather than trusted.
_UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9._-]")

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


def safe_name(project: str) -> str:
    """Reduce a project name to something safe to put in a filename.

    Not cosmetic: the name comes from a child repo's own config, and a project
    called ``../../etc`` would otherwise write its state file outside the state
    directory entirely.
    """
    cleaned = _UNSAFE_IN_NAME.sub("-", project).strip(".-")
    return cleaned or "unnamed"


def new_run(project: str, task: str) -> AgentRun:
    """Mint a queued run. Nothing is launched and nothing is written yet."""
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%S")
    run_id = f"{safe_name(project)}-{stamp}-{uuid4().hex[:6]}"
    return AgentRun(id=run_id, project=project, task=task, state=QUEUED, started_at=_now())


def _run_file(run_id: str) -> Path:
    return state_dir() / f"{safe_name(run_id)}.json"


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


def _parse(raw: object) -> AgentRun | None:
    """Build a run from a decoded JSON blob; ``None`` if it is not one."""
    if not isinstance(raw, dict):
        return None
    ticks = raw.get("start_ticks")
    try:
        return AgentRun(
            id=str(raw["id"]),
            project=str(raw["project"]),
            task=str(raw.get("task", "")),
            state=str(raw.get("state", FAILED)),
            started_at=str(raw.get("started_at", "")),
            pid=int(raw.get("pid", 0)),
            start_ticks=int(ticks) if isinstance(ticks, int) else None,
            worktree=str(raw.get("worktree", "")),
            branch=str(raw.get("branch", "")),
            log_path=str(raw.get("log_path", "")),
            pr_url=str(raw.get("pr_url", "")),
            detail=str(raw.get("detail", "")),
            ended_at=str(raw.get("ended_at", "")),
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


def load(run_id: str) -> AgentRun | None:
    """Read one run, reconciled against the world; ``None`` if unreadable."""
    try:
        raw = json.loads(_run_file(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    parsed = _parse(raw)
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


def mark_running(run: AgentRun, pid: int) -> AgentRun:
    """Record that ``run``'s agent is live under ``pid``, and persist it."""
    started = replace(run, state=RUNNING, pid=pid, start_ticks=proc_start_ticks(pid))
    save(started)
    return started


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
    """
    settled = state if state in TERMINAL else FAILED
    ended = replace(
        run,
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
