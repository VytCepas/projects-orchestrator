"""Campaigns — one declarative file that fans an agent across the fleet, safely.

``work --where scaffold=none "apply project-init" --apply`` already rolls a task
across every matching project. A **campaign** is that same fan-out made into a
named, versioned, re-runnable artifact — and wraps it in the two safeguards a
one-off command cannot give you:

**It canaries.** By default a campaign runs exactly ONE project and stops. You
read that single PR, and only then does ``--apply`` fan out to the rest. Proving
the task on one repo before spending forty agents' worth of tokens on it is the
whole point — a task that is subtly wrong is wrong on all forty at once.

**Its progress is DERIVED, never tracked.** There is no campaign state file that
can drift from reality. "What is still outstanding" is computed every run from
two sources that already exist: the selector (which projects match) and the runs
store (which of those already have work in flight or a PR up). Re-running a
campaign therefore picks up exactly what remains — and a campaign whose selector
matches nothing is *done*, not an error. As merged PRs move projects out of the
selector (``scaffold=none`` stops matching once the scaffold lands), the campaign
converges to empty on its own.

The execution loop enforces the policy: at most ``max_concurrent`` runs at once,
and a run that outlives ``timeout`` is killed — not left burning tokens. Nothing
here raises (ADR-003): a malformed file is a :class:`CampaignError` the CLI turns
into a nonzero exit, and a run whose record vanishes mid-flight resolves to
``failed`` rather than hanging the loop forever. **Silence is not success.**
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from projects_orchestrator import runs, selector
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.fleet import ProjectSnapshot

#: The campaign-file schema versions this code understands. A file that declares
#: any other version is refused rather than best-guessed — a v2 field silently
#: ignored by a v1 reader is exactly the kind of quiet misbehaviour a rollout
#: cannot afford.
SUPPORTED_VERSIONS = frozenset({1})

#: Report formats a campaign may request via ``policy.output``.
OUTPUT_FORMATS = frozenset({"text", "json"})

#: A run in one of these states means the campaign has ALREADY handled that
#: project and must not launch a second run on it. ``pr-opened`` counts — the
#: work is delivered and awaiting review; re-launching would open a duplicate PR.
#: ``needs-human`` counts — a person must act, and a fresh agent cannot. A
#: ``failed`` or ``abandoned`` run does NOT count: re-running the campaign is how
#: you retry it.
_HANDLED_STATES = frozenset({runs.QUEUED, runs.RUNNING, runs.PR_OPENED, runs.NEEDS_HUMAN})

_DEFAULT_TIMEOUT = 1800.0
_DEFAULT_POLL_INTERVAL = 2.0


class CampaignError(ValueError):
    """A campaign file that cannot be honoured — bad schema, field, or selector."""


@dataclass(frozen=True)
class Policy:
    """How a campaign is allowed to run.

    Attributes:
        max_concurrent: Most runs in flight at once during ``--apply`` fan-out.
        timeout: Per-run wall-clock budget in seconds; a run that exceeds it is
            killed. This is the campaign's own cap, independent of (and able to
            pre-empt) the agent subprocess's internal timeout.
        include_plain_repos: Whether discovery should include git repos with no
            project-init scaffold. A ``scaffold=none`` campaign is meaningless
            without this — the repos it targets are invisible to discovery by
            default, so its target list would be silently empty.
        output: Preferred report format (``text`` or ``json``).
    """

    max_concurrent: int = 1
    timeout: float = _DEFAULT_TIMEOUT
    include_plain_repos: bool = False
    output: str = "text"


@dataclass(frozen=True)
class Campaign:
    """A declarative fleet task: who to run on, what to do, and under what policy.

    Attributes:
        name: Human label, and the identity a re-run reconciles against.
        select: Selector expressions (:mod:`selector`), combined with AND.
        task: The instruction handed to each agent — the same for every project.
        policy: Concurrency, timeout, and discovery knobs.
        version: The campaign-file schema version (see :data:`SUPPORTED_VERSIONS`).
    """

    name: str
    select: tuple[str, ...]
    task: str
    policy: Policy = field(default_factory=Policy)
    version: int = 1


# --- Loading and validation ----------------------------------------------------


def _require(raw: dict[str, Any], key: str) -> Any:
    value = raw.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        message = f"campaign is missing required field '{key}'"
        raise CampaignError(message)
    return value


def _reject_unknown(raw: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    """Refuse unknown keys loudly.

    A typo'd key (``tasks:`` for ``task:``) that were silently ignored would
    leave the real field at its default — an empty task, a canary that does
    nothing — and read as success. Allow-list the keys; reject the rest.
    """
    unknown = sorted(set(raw) - allowed)
    if unknown:
        message = (
            f"unknown {where} field(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}"
        )
        raise CampaignError(message)


def _parse_selectors(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str):
        exprs: tuple[str, ...] = (raw,)
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        exprs = tuple(raw)
    else:
        message = "'select' must be a selector string or a list of them"
        raise CampaignError(message)
    if not exprs:
        message = "'select' must not be empty — a campaign with no filter targets the whole fleet by accident"
        raise CampaignError(message)
    # Validate NOW, so a bad selector fails at load in the operator's shell rather
    # than deep inside a fan-out that has already launched real agents.
    try:
        selector.parse(exprs)
    except selector.SelectorError as exc:
        raise CampaignError(str(exc)) from exc
    return exprs


def _parse_policy(raw: object) -> Policy:
    if raw is None:
        return Policy()
    if not isinstance(raw, dict):
        message = "'policy' must be a mapping"
        raise CampaignError(message)
    _reject_unknown(
        raw, frozenset({"max_concurrent", "timeout", "include_plain_repos", "output"}), "policy"
    )
    max_concurrent = _coerce_positive_int(raw.get("max_concurrent", 1), "max_concurrent")
    timeout = _coerce_positive_float(raw.get("timeout", _DEFAULT_TIMEOUT), "timeout")
    output = str(raw.get("output", "text"))
    if output not in OUTPUT_FORMATS:
        message = f"'output' must be one of {', '.join(sorted(OUTPUT_FORMATS))}, not '{output}'"
        raise CampaignError(message)
    return Policy(
        max_concurrent=max_concurrent,
        timeout=timeout,
        include_plain_repos=bool(raw.get("include_plain_repos", False)),
        output=output,
    )


def _coerce_positive_int(value: object, field_name: str) -> int:
    # `bool` is a subclass of `int`; `max_concurrent: true` is a mistake, not 1.
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        message = f"'{field_name}' must be a positive integer"
        raise CampaignError(message)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        message = f"'{field_name}' must be a positive integer"
        raise CampaignError(message) from exc
    if result < 1:
        message = f"'{field_name}' must be at least 1, not {result}"
        raise CampaignError(message)
    return result


def _coerce_positive_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        message = f"'{field_name}' must be a positive number"
        raise CampaignError(message)
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        message = f"'{field_name}' must be a positive number"
        raise CampaignError(message) from exc
    if result <= 0:
        message = f"'{field_name}' must be greater than 0, not {result}"
        raise CampaignError(message)
    return result


def _targets_unscaffolded(select: tuple[str, ...]) -> bool:
    """Whether the selector asks for repos with no scaffold (``scaffold=none``)."""
    return any(
        term.field == "scaffold" and term.op == "=" and term.value == "none"
        for term in selector.parse(select)  # already validated by _parse_selectors
    )


def parse_campaign(raw: object) -> Campaign:
    """Build a :class:`Campaign` from a decoded document; raise on anything off."""
    if not isinstance(raw, dict):
        message = "a campaign file must be a YAML mapping"
        raise CampaignError(message)
    _reject_unknown(raw, frozenset({"version", "name", "select", "task", "policy"}), "campaign")
    version = _coerce_positive_int(raw.get("version", 1), "version")
    if version not in SUPPORTED_VERSIONS:
        message = f"unsupported campaign version {version}; this build understands {sorted(SUPPORTED_VERSIONS)}"
        raise CampaignError(message)
    name = str(_require(raw, "name"))
    select = _parse_selectors(_require(raw, "select"))
    task = str(_require(raw, "task"))
    policy = _parse_policy(raw.get("policy"))
    # A `scaffold=none` campaign targets repos with no project-init — which are
    # exactly the repos discovery hides unless `include_plain_repos` is set. Left
    # unset, the campaign matches nothing and reports "done", a silent no-op on
    # the rollout's whole point. Refuse it here rather than let it lie later.
    if _targets_unscaffolded(select) and not policy.include_plain_repos:
        message = (
            "a campaign selecting 'scaffold=none' must set 'policy.include_plain_repos: true' — "
            "the unscaffolded repos it targets are invisible to discovery otherwise, so it "
            "would silently match nothing and report done"
        )
        raise CampaignError(message)
    return Campaign(name=name, select=select, task=task, policy=policy, version=version)


def load_campaign(path: Path) -> Campaign:
    """Read and validate a campaign file; raise :class:`CampaignError` on any fault.

    Every failure mode — unreadable file, non-YAML, missing field, bad selector —
    becomes one exception type the caller turns into a nonzero exit, so a broken
    campaign never launches a single agent.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        message = f"cannot read campaign file {path}: {exc}"
        raise CampaignError(message) from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        message = f"campaign file {path} is not valid YAML: {exc}"
        raise CampaignError(message) from exc
    return parse_campaign(raw)


# --- Built-in campaigns --------------------------------------------------------

#: The instruction the project-init rollout hands every agent. It says WHAT to
#: achieve, not how to push it: the briefing already forbids committing, pushing,
#: and merging, and the orchestrator lands the result (ADR-007 §3).
_PROJECT_INIT_TASK = (
    "Apply the project-init scaffold so this repository conforms to the fleet's "
    "contract-v1: add the .agents/ descriptor (config.yaml with the project name, "
    "language, and per-task commands), the CI workflow, the language toolchain gates "
    "(lint, typecheck, tests with a coverage floor), and the git hooks. Then make "
    "`just ci` pass, fixing whatever lint, type, or test failures the new gates surface. "
    "Keep the change scoped to the scaffold and the fixes its own gates require."
)

#: Named campaigns shipped with the tool, so ``campaign project-init`` works with
#: no file to author. ``project-init`` is THE estate rollout (#122): it selects the
#: unscaffolded repos — which is why it must opt discovery into seeing plain repos —
#: and is self-terminating, because a project drops out of ``scaffold=none`` the
#: moment its scaffold PR merges.
BUILTIN_CAMPAIGNS: dict[str, Campaign] = {
    "project-init": Campaign(
        name="project-init",
        select=("scaffold=none",),
        task=_PROJECT_INIT_TASK,
        policy=Policy(max_concurrent=1, include_plain_repos=True),
    ),
}


def resolve(name_or_path: str) -> Campaign:
    """Resolve a built-in campaign name, or load a campaign file by path.

    Built-in names are reserved and win over a same-named file, so
    ``campaign project-init`` means the same thing from any directory. Anything
    else is treated as a path; a value that is neither is refused with the list of
    built-ins, rather than read as an empty or missing file.
    """
    if name_or_path in BUILTIN_CAMPAIGNS:
        return BUILTIN_CAMPAIGNS[name_or_path]
    path = Path(name_or_path)
    if not path.exists():
        message = (
            f"'{name_or_path}' is neither a campaign file nor a built-in campaign "
            f"({', '.join(sorted(BUILTIN_CAMPAIGNS))})"
        )
        raise CampaignError(message)
    return load_campaign(path)


# --- Deriving what is outstanding ----------------------------------------------


def _handled(campaign: Campaign, project_runs: list[runs.AgentRun]) -> bool:
    """Whether a run for THIS campaign already covers the project.

    A run is "for this campaign" when its task matches — the campaign's task is
    fixed and handed verbatim to every launch, so equality is enough to tell a
    campaign's own runs from unrelated work on the same project without adding a
    campaign-id field the run store would have to carry.
    """
    return any(r.task == campaign.task and r.state in _HANDLED_STATES for r in project_runs)


def outstanding(
    campaign: Campaign,
    snapshots: list[ProjectSnapshot],
    runs_by_project: dict[str, list[runs.AgentRun]],
) -> list[ProjectSnapshot]:
    """The projects still needing work: matched by the selector, not yet handled.

    This is the campaign's whole notion of progress, and it is derived, not
    stored: ``select`` says which projects qualify, and the runs store says which
    already have work in flight or a PR up. Nothing is written to remember it, so
    nothing can go stale.
    """
    matched = selector.select(snapshots, list(campaign.select))
    return [
        s for s in matched if not _handled(campaign, runs_by_project.get(s.descriptor.name, []))
    ]


# --- Executing a batch under the policy ----------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What became of one launched run.

    Attributes:
        run: The run's final, terminal record.
        timed_out: Whether the campaign killed it for exceeding ``policy.timeout``
            (as opposed to it failing or opening a PR on its own).
    """

    run: runs.AgentRun
    timed_out: bool = False


@dataclass(frozen=True)
class Seams:
    """The side-effecting operations execution depends on, injected for testing.

    In production these are the real fleet operations; in tests they are fakes,
    so the supervising loop can be driven to completion in microseconds with no
    process ever spawned.
    """

    launch: Callable[[ProjectDescriptor, str], runs.AgentRun]
    poll: Callable[[str], runs.AgentRun | None]
    stop: Callable[[str], runs.AgentRun | None]
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    poll_interval: float = _DEFAULT_POLL_INTERVAL


def default_seams() -> Seams:
    """The production wiring: real launches, reconciled polls, real time.

    ``work`` is imported lazily — it pulls in the whole agent stack (briefing,
    landing, sandbox), which a caller that only parses or plans a campaign has no
    need to load.
    """
    import time

    from projects_orchestrator import work

    return Seams(
        launch=work.launch,
        poll=runs.load,
        stop=work.stop,
        clock=time.monotonic,
        sleep=time.sleep,
    )


@dataclass
class _Active:
    launched: runs.AgentRun
    deadline: float


def _fill(
    campaign: Campaign,
    pending: list[ProjectDescriptor],
    active: dict[str, _Active],
    seams: Seams,
    outcomes: list[Outcome],
) -> None:
    """Launch runs until the concurrency ceiling is hit or nothing is left."""
    while pending and len(active) < campaign.policy.max_concurrent:
        descriptor = pending.pop(0)
        run = seams.launch(descriptor, campaign.task)
        if run.is_terminal:
            # Failed to even start (no worktree, no wrapper): a terminal outcome
            # with nothing to supervise.
            outcomes.append(Outcome(run=run))
        else:
            active[run.id] = _Active(launched=run, deadline=seams.clock() + campaign.policy.timeout)


def _reap(active: dict[str, _Active], seams: Seams, outcomes: list[Outcome]) -> None:
    """Retire finished runs; kill and retire any that outlived their deadline."""
    now = seams.clock()
    for run_id, state in list(active.items()):
        current = seams.poll(run_id)
        if current is None:
            # The record vanished — we cannot confirm success, so we assume the
            # pessimistic outcome rather than poll a ghost forever.
            current = replace(state.launched, state=runs.FAILED, detail="run record vanished")
        if current.is_terminal:
            outcomes.append(Outcome(run=current))
            del active[run_id]
        elif now >= state.deadline:
            killed = seams.stop(run_id) or current
            outcomes.append(Outcome(run=killed, timed_out=True))
            del active[run_id]


def execute(
    campaign: Campaign, descriptors: list[ProjectDescriptor], seams: Seams
) -> list[Outcome]:
    """Run every descriptor under the policy; return each one's outcome.

    Blocks until every launched run reaches a terminal state or is killed for
    running past ``policy.timeout``. The loop always makes progress: each turn
    either launches work (shrinking ``pending``) or, after a poll, retires at
    least the runs that have finished or timed out — so it cannot spin forever on
    a run whose process died, because reconciliation reports that as ``failed``.
    """
    pending = list(descriptors)
    active: dict[str, _Active] = {}
    outcomes: list[Outcome] = []
    while pending or active:
        _fill(campaign, pending, active, seams, outcomes)
        if active:
            seams.sleep(seams.poll_interval)
            _reap(active, seams, outcomes)
    return outcomes


# --- Reporting -----------------------------------------------------------------


@dataclass(frozen=True)
class CampaignReport:
    """The result of one campaign invocation, ready to render.

    Attributes:
        name: The campaign's name.
        outcomes: One :class:`Outcome` per run attempted this invocation.
        remaining: Outstanding targets NOT attempted this pass — non-zero after a
            canary, the count that ``--apply`` would take on.
        canary: Whether this was a canary (one project) rather than a fan-out.
    """

    name: str
    outcomes: tuple[Outcome, ...]
    remaining: int
    canary: bool

    @property
    def pr_opened(self) -> tuple[Outcome, ...]:
        """Runs that landed a draft PR."""
        return tuple(o for o in self.outcomes if o.run.state == runs.PR_OPENED)

    @property
    def failed(self) -> tuple[Outcome, ...]:
        """Runs that ended without a PR, timeouts included."""
        return tuple(o for o in self.outcomes if o.run.state != runs.PR_OPENED)

    @property
    def ok(self) -> bool:
        """Whether every attempted run opened a PR (an empty attempt is ok)."""
        return all(o.run.state == runs.PR_OPENED for o in self.outcomes)


def summarize(
    campaign: Campaign, outcomes: list[Outcome], *, remaining: int, canary: bool
) -> CampaignReport:
    """Fold a batch's outcomes into a report."""
    return CampaignReport(
        name=campaign.name, outcomes=tuple(outcomes), remaining=remaining, canary=canary
    )
