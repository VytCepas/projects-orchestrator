"""Autonomous fix dispatch — spawn a scoped coding agent to repair a failing gate.

The engine can *detect* a failing lint/test gate (``checks.py``) but not
repair it. This closes that loop for the two gates every project declares
locally: given a project with a cached failure, it cuts a **throwaway git
worktree**, hands a scoped coding agent (the ``claude`` CLI, headless) exactly
the failing command and its last-known error, re-runs the gate *in that
worktree* to verify the fix, and — only on a verified pass — commits, pushes,
and opens a PR. Nothing ever reaches the default branch without a human
merging it (ADR-006).

The agent never works in the operator's clone. It used to: heal originally ran
``git checkout -B`` in ``descriptor.path`` and restored the branch in a
``finally``, which meant a fleet-wide heal branch-switched every project out
from under whoever was working in them, and a SIGKILL mid-run stranded a clone
on a ``heal/`` branch with an agent's edits in the tree. :mod:`worktree` closes
that; see its docstring for why a ``finally`` was never a sufficient guarantee.
(ADR-007 §3 supersedes ADR-006 §2, which specified the old checkout.)

Because the run is now isolated, heal no longer cares whether your clone is
dirty — you can heal a project while you are mid-edit in it.

Like the rest of the engine, this never raises: a worktree git refuses, an agent
that can't fix the gate, or a push/PR failure all degrade to a
:class:`HealResult` the caller renders, never an exception.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from projects_orchestrator import cost as cost_mod
from projects_orchestrator import landing, sandbox
from projects_orchestrator import worktree as wt
from projects_orchestrator.briefing import build_briefing, evidence_from_checks
from projects_orchestrator.checks import CheckResult, collect_checks
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.runner import RunResult

# Gates the heal loop can attempt: both are declared, locally-runnable
# commands. ci/cloud are deliberately excluded — they probe remote state a
# local agent run can't reproduce or re-verify.
HEALABLE_TASKS = ("lint", "test")

# Tasks whose scoped Bash an agent run may NEVER be granted, whatever
# HEALABLE_TASKS grows to include (ADR-007 §4). `deploy` mutates production;
# `cloud` reaches the data plane. The scoped-Bash builder refuses these by name,
# so a future well-meaning `HEALABLE_TASKS = ("lint", "test", "deploy")` cannot
# silently hand an agent a production deploy — it is caught here, not in review.
_FORBIDDEN_AGENT_TASKS = frozenset({"deploy", "cloud", "release", "publish"})

AGENT_TIMEOUT = 900.0  # a real fix may take several tool calls
GIT_TIMEOUT = 30.0
_MAX_BUDGET_USD = "2.00"
# No bare Bash: an unattended agent (esp. the scheduled trigger, no human
# watching) would otherwise be able to run anything, not just work in this
# project's directory. _agent_allowed_tools scopes Bash to exactly the
# project's own declared lint/test commands (already ADR-003-trusted
# strings) so the agent can re-run them to verify, and nothing else.
_BASE_TOOLS = ("Edit", "Write", "Read", "Grep", "Glob")

# Heal-mode policy (ADR-008). `fix` is today's full loop: agent, verify, draft
# PR. `notify` stops at the diagnosis — report what failed and what to do next,
# spend nothing, touch nothing. The run sets the default; a project's declared
# `heal.mode` overrides it (a child that says "never auto-fix me" is obeyed even
# when the fleet pass runs in fix mode).
MODE_FIX = "fix"
MODE_NOTIFY = "notify"
HEAL_MODES = (MODE_FIX, MODE_NOTIFY)

NO_FAILURES = "no_action"
# NOTE: there is deliberately no "worktree dirty" outcome any more. Heal used to
# refuse on a dirty clone because it was about to `checkout -B` *in* it. It now
# cuts its own worktree, so the operator's working state is irrelevant — you can
# heal a project while you are mid-edit in it.
WORKTREE_FAILED = "worktree_failed"
BRANCH_FAILED = "branch_failed"
AGENT_FAILED = "agent_failed"
VERIFY_FAILED = "verify_failed"
PUSH_FAILED = "push_failed"
PR_FAILED = "pr_failed"
FIXED = "fixed"
NOTIFIED = "notified"

# Outcomes reached WITHOUT ever launching the agent: nothing was failing, the
# policy said notify-only, or a worktree could not be cut. Their cost is None
# because no run happened — not because a run went unmetered — so a fleet spend
# total must exclude them rather than count them as unmetered runs (which would
# falsely warn spend is higher).
_PRE_AGENT_STATUSES = frozenset({NO_FAILURES, NOTIFIED, WORKTREE_FAILED})


@dataclass(frozen=True)
class AgentOutcome:
    """Result of one scoped coding-agent invocation.

    Attributes:
        ok: Whether the agent process completed (exit 0); says nothing
            about whether the fix is correct — that is re-verified by
            re-running the failing gate.
        summary: The agent's final reply, or an error description.
        cost: What the agent run actually cost, when the CLI metered it;
            ``None`` when the run was unmetered (a killed/timed-out run, or an
            injected fake). ``None`` is deliberately not ``$0`` — an unattended
            heal that spends real money must report what it spent, and a failed
            run may still have cost something before it gave up.
    """

    ok: bool
    summary: str = ""
    cost: cost_mod.RunCost | None = None


@dataclass(frozen=True)
class PrOutcome:
    """Result of opening a pull request for a healed branch.

    Attributes:
        ok: Whether the PR was created.
        url: The PR URL, when created.
        detail: Failure explanation, when not.
    """

    ok: bool
    url: str = ""
    detail: str = ""


@dataclass(frozen=True)
class HealResult:
    """Outcome of one heal attempt on one project.

    Attributes:
        project: Project name.
        status: One of the module-level status constants.
        branch: The heal branch, once created (empty before then).
        pr_url: The opened PR's URL, only set when ``status == FIXED``.
        detail: Human-readable explanation.
        worktree: Where the agent's work was left, set only when the run FAILED
            and its worktree was therefore retained. Empty on success (the
            worktree is removed) — so a non-empty value means "there is evidence
            on disk, go look at it".
        cost: What the healing agent run cost, when metered; ``None`` when
            unmetered or when no agent ran (a no-op or a worktree that could not
            be cut). Carried on every outcome that reached the agent — including
            the failures, which can still have spent money before giving up.
    """

    project: str
    status: str
    branch: str = ""
    pr_url: str = ""
    detail: str = ""
    worktree: str = ""
    cost: cost_mod.RunCost | None = None


AgentRun = Callable[[ProjectDescriptor, str], AgentOutcome]
OpenPr = Callable[[ProjectDescriptor, str, tuple[str, ...]], PrOutcome]


def _run_argv(args: list[str], cwd: Path, timeout: float = GIT_TIMEOUT) -> RunResult:
    """Run one ``git``/``gh`` subcommand via argv, never through a shell.

    Unlike ``runner.run_command`` (shell strings — fine for a project's own
    declared tooling commands, per ADR-003), the arguments here include
    values this module does not fully control (``descriptor.name``, a
    branch built from it). Passing them as separate argv elements means
    there is no shell to interpret shell metacharacters in a crafted
    project name — never raises, degrading to a failed :class:`RunResult`.
    """
    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell; args are not concatenated into a command string
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RunResult(
            command=" ".join(args),
            returncode=None,
            error=str(exc),
            duration=time.monotonic() - start,
        )
    return RunResult(
        command=" ".join(args),
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration=time.monotonic() - start,
    )


def pending_failures(cached: dict[str, CheckResult]) -> tuple[CheckResult, ...]:
    """Return the project's cached failures among the healable tasks (pure).

    Args:
        cached: This project's ``{task: CheckResult}`` slice of the checks cache.

    Returns:
        The failing healable results, in no particular order.
    """
    return tuple(
        result
        for task, result in cached.items()
        if task in HEALABLE_TASKS and result.status == "fail"
    )


def build_heal_prompt(descriptor: ProjectDescriptor, failing: tuple[CheckResult, ...]) -> str:
    """Render the fix-scoping prompt handed to the coding agent (pure).

    A thin adapter over :func:`briefing.build_briefing`. Heal is now one *kind* of
    agent run among several, and the briefing — the task, why you are here, and
    the output contract — is the same shape for all of them. Keeping heal's own
    copy would mean the prompt-injection fencing and the "do not commit" contract
    get hardened in one place and quietly not in the other.

    Args:
        descriptor: The project being healed (the agent runs in its directory).
        failing: The cached failures to fix.

    Returns:
        A prompt naming exactly the failing command(s) and the last-known error,
        instructing a minimal, scoped fix with no commit.
    """
    gates = ", ".join(sorted({result.task for result in failing}))
    return build_briefing(
        descriptor,
        task=(
            f"The {gates} gate(s) are failing. Fix them with the smallest correct "
            "change, then stop — do not commit."
        ),
        evidence=evidence_from_checks(descriptor, failing),
    )


def _agent_allowed_tools(descriptor: ProjectDescriptor) -> str:
    """Build the ``--allowedTools`` value: edits everywhere, Bash scoped tight (pure).

    Bash is allowed only for the project's own declared ``lint``/``test``
    commands — the same strings ADR-003 already trusts enough to execute
    unattended (``checks.py``) — so the agent can re-run them to verify
    progress without gaining a general shell.

    Args:
        descriptor: The project being healed.

    Returns:
        A comma-separated ``--allowedTools`` value.
    """
    scoped_bash = tuple(
        f"Bash({command})"
        for task in HEALABLE_TASKS
        if task not in _FORBIDDEN_AGENT_TASKS
        if (command := descriptor.tooling.get(task, "").strip())
    )
    return ",".join((*_BASE_TOOLS, *scoped_bash))


def _decode_agent_output(stdout: str) -> tuple[str, cost_mod.RunCost | None]:
    """Pull the agent's final reply and its metered cost from the CLI's JSON.

    Both come from the one ``--output-format json`` object: ``result`` is the
    agent's final message, ``total_cost_usd``/``usage`` are what it spent. A
    payload that is not that object (truncated, or a plain-text tail) degrades to
    the raw stdout tail and *no* cost — an unparseable run is unmetered, not free.
    """
    try:
        payload = json.loads(stdout)
    except ValueError:
        return stdout.strip()[-500:], None
    if not isinstance(payload, dict):
        return stdout.strip()[-500:], None
    return str(payload.get("result", "")), cost_mod.from_payload(payload)


def _default_agent_run(descriptor: ProjectDescriptor, prompt: str) -> AgentOutcome:
    """Invoke the ``claude`` CLI headless, scoped to ``descriptor.path``.

    Runs with ``acceptEdits`` (auto-accepts file edits; everything else —
    including any Bash call outside the scoped lint/test allowlist — is
    denied rather than bypassed) so an unattended run neither stalls on a
    tool-use prompt nor gets a general shell. The PR gate (the caller only
    pushes/opens a PR after re-verifying the fix; ADR-006) remains the
    backstop for a bad *file* edit; the allowlist is the backstop for the
    agent process itself.
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
        _agent_allowed_tools(descriptor),
        "--max-budget-usd",
        _MAX_BUDGET_USD,
    ]
    # ADR-007 §4: the agent runs with the data plane scrubbed OUT of its
    # environment. The --allowedTools list stops the CLI from running gcloud;
    # this stops the credentials existing at all, so nothing the agent reaches —
    # an MCP server, a hook, a tool we did not foresee — can find them. A fresh,
    # per-run HOME is part of that: scrubbing GOOGLE_APPLICATION_CREDENTIALS is
    # useless if HOME still points at ~/.config/gcloud. The directory is
    # ephemeral — nothing the agent writes to its config home is worth keeping.
    try:
        with tempfile.TemporaryDirectory(prefix="po-agent-home-") as home:
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell; scoped to the project's own directory
                command,
                cwd=descriptor.path,
                capture_output=True,
                text=True,
                timeout=AGENT_TIMEOUT,
                check=False,
                env=sandbox.agent_env(home=home),
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return AgentOutcome(ok=False, summary=str(exc))
    result_text, run_cost = _decode_agent_output(proc.stdout)
    if proc.returncode != 0:
        return AgentOutcome(
            ok=False,
            summary=proc.stderr.strip()[-500:] or "agent exited non-zero",
            cost=run_cost,
        )
    return AgentOutcome(ok=True, summary=result_text, cost=run_cost)


def _default_open_pr(
    descriptor: ProjectDescriptor, branch: str, tasks: tuple[str, ...]
) -> PrOutcome:
    """Open a DRAFT PR for a pushed heal branch, via the write boundary.

    Draft, not ready-for-review: a ready PR is one click — or one auto-merge-on-
    green rule — away from landing an agent's work with no human in the loop, which
    is the one thing this system promises not to do.
    """
    title = f"fix: repair failing {', '.join(tasks)} (automated)"
    body = (
        "Opened automatically by projects-orchestrator's heal command after a scoped "
        f"agent fixed and verified: {', '.join(tasks)}. Review before merging."
    )
    landed = landing.open_draft_pr(descriptor.path, branch, title, body)
    if not landed.ok:
        return PrOutcome(ok=False, detail=landed.detail)
    return PrOutcome(ok=True, url=landed.pr_url)


def _why_failed(result: RunResult) -> str:
    """Explain a failed git subcommand, wherever it chose to explain itself.

    Reading only ``stderr`` loses the two cases that matter most, and loses them
    as an **empty string** — a BRANCH_FAILED with no reason, which is the least
    actionable thing this can report.

    ``git commit`` with nothing staged exits 1 and writes "nothing to commit,
    working tree clean" to **stdout**. That is not an obscure edge: it is what
    happens whenever the agent declared success without changing a file, so the
    one message that would have told the operator *why* was the one dropped. And
    a timeout or OSError never ran git at all — both streams are empty and the
    reason is in ``error``.
    """
    reason = result.stderr.strip() or result.stdout.strip() or (result.error or "").strip()
    return (reason or f"{result.command} failed with no output")[-300:]


def _commit_and_land(
    descriptor: ProjectDescriptor, branch: str, tasks: tuple[str, ...], open_pr: OpenPr
) -> HealResult:
    """Commit a verified fix, push the branch, and open a PR."""
    add = _run_argv(["git", "add", "-A"], cwd=descriptor.path)
    if not add.ok:
        return HealResult(descriptor.name, BRANCH_FAILED, branch=branch, detail=_why_failed(add))
    message = f"fix: repair failing {', '.join(tasks)} (automated)"
    commit = _run_argv(["git", "commit", "-m", message], cwd=descriptor.path)
    if not commit.ok:
        return HealResult(descriptor.name, BRANCH_FAILED, branch=branch, detail=_why_failed(commit))
    # Every write leaves through the boundary (ADR-007 §3): a non-protected branch
    # and a draft PR, or nothing. Enforced HERE and not by the child's pre-push
    # hook, because the projects this system most needs to touch are the ones that
    # do not have that hook yet.
    pushed = landing.push_branch(
        descriptor.path, branch, repo_default=landing.default_branch(descriptor.path)
    )
    if not pushed.ok:
        return HealResult(descriptor.name, PUSH_FAILED, branch=branch, detail=pushed.detail)
    pr = open_pr(descriptor, branch, tasks)
    if not pr.ok:
        return HealResult(descriptor.name, PR_FAILED, branch=branch, detail=pr.detail)
    return HealResult(descriptor.name, FIXED, branch=branch, pr_url=pr.url)


def _notify_result(descriptor: ProjectDescriptor, failing: tuple[CheckResult, ...]) -> HealResult:
    """Render a notify-mode outcome: what failed, and what to do next (pure)."""
    tasks = ", ".join(sorted({result.task for result in failing}))
    return HealResult(
        descriptor.name,
        NOTIFIED,
        detail=(
            f"{tasks} failing — policy is notify, no agent spawned. "
            f"Fix by hand, or run: projects-orchestrator heal {descriptor.name}"
        ),
    )


def heal_project(
    descriptor: ProjectDescriptor,
    cached: dict[str, CheckResult],
    agent_run: AgentRun | None = None,
    open_pr: OpenPr | None = None,
    mode: str = MODE_FIX,
) -> HealResult:
    """Attempt to fix one project's failing lint/test gate end to end; never raises.

    Args:
        descriptor: The project to heal.
        cached: This project's ``{task: CheckResult}`` slice of the checks cache.
        agent_run: Coding-agent invocation override; ``None`` uses the real
            ``claude`` CLI. Tests inject a fake so no live agent ever runs.
        open_pr: PR-creation override; ``None`` uses the real ``gh`` CLI.
        mode: The resolved heal mode for THIS project (the caller applies the
            project's declared override — see :func:`heal_fleet`). ``notify``
            stops at the diagnosis: no worktree, no agent, no spend.

    Returns:
        A :class:`HealResult` describing what happened. The operator's clone is
        never checked out, branch-switched, or otherwise touched: all work
        happens in a throwaway worktree (:mod:`worktree`), which is removed on
        success and *kept* on failure so the agent's work can be inspected.
    """
    failing = pending_failures(cached)
    if not failing:
        return HealResult(descriptor.name, NO_FAILURES, detail="no failing lint/test gate cached")
    if mode == MODE_NOTIFY:
        return _notify_result(descriptor, failing)

    # Retention has a clock (ADR-007), and this is what makes it tick. Without a
    # caller, "expires after N days" is just a function nobody runs, and kept
    # worktrees would accumulate forever.
    wt.prune_expired(descriptor.path, descriptor.name)

    tasks = tuple(sorted({result.task for result in failing}))
    slug = wt.run_slug()
    # The branch is unique per RUN, not per project+task. A stable name would
    # deadlock against retention: a failed run's worktree is deliberately kept,
    # git will not check one branch out in two worktrees, and the next heal of
    # that project would be refused — permanently, since nothing would clear it.
    # Keeping the evidence must not cost you the ability to try again.
    branch = f"heal/{'-'.join(tasks)}-{descriptor.name}-{slug.rsplit('-', 1)[-1]}"

    tree = wt.create(repo=descriptor.path, project=descriptor.name, branch=branch, slug=slug)
    if tree is None:
        return HealResult(
            descriptor.name,
            WORKTREE_FAILED,
            branch=branch,
            detail="could not cut an isolated worktree (branch may be held by a kept failed run)",
        )

    # Everything downstream — the agent, the re-verify, the commit, the push, the
    # PR — is keyed off `descriptor.path`, so pointing a copy at the worktree
    # redirects the whole flow without touching the operator's clone.
    work = replace(descriptor, path=tree.path)
    keep = True
    try:
        outcome = (agent_run or _default_agent_run)(work, build_heal_prompt(work, failing))
        if not outcome.ok:
            return HealResult(
                descriptor.name,
                AGENT_FAILED,
                branch=branch,
                detail=outcome.summary,
                worktree=str(tree.path),
                cost=outcome.cost,
            )

        verify = collect_checks(work, tasks)
        still_failing = [result.task for result in verify if result.status != "pass"]
        if still_failing:
            return HealResult(
                descriptor.name,
                VERIFY_FAILED,
                branch=branch,
                detail=f"still failing after the agent's fix: {', '.join(still_failing)}",
                worktree=str(tree.path),
                cost=outcome.cost,
            )

        # The agent's spend is banked onto whatever the landing step produced —
        # a FIXED, a PUSH_FAILED, a PR_FAILED — so the cost of the run survives
        # into the terminal outcome regardless of how landing went.
        landed = replace(
            _commit_and_land(work, branch, tasks, open_pr or _default_open_pr),
            cost=outcome.cost,
        )
        keep = landed.status != FIXED
        if keep:
            return replace(landed, worktree=str(tree.path))
        return landed
    finally:
        # Asymmetric on purpose (ADR-007): a successful run's worktree is
        # redundant — the work is in the PR — but a failed one is the only
        # record of what the agent actually did, so it is retained and expires
        # on a clock instead. An early `return` above leaves `keep` True, which
        # is the safe default: we keep evidence rather than destroy it.
        if not keep:
            wt.remove(tree)


def render_heal_result(result: HealResult) -> str:
    """Render one heal outcome as a single human-readable line (pure).

    Args:
        result: The outcome to render.

    Returns:
        A one-line summary suitable for the controller/TUI.
    """
    if result.status == FIXED:
        return f"{result.project}: fixed — PR opened at {result.pr_url} (branch {result.branch})"
    detail = f" — {result.detail}" if result.detail else ""
    branch = f" (branch {result.branch})" if result.branch else ""
    return f"{result.project}: {result.status}{detail}{branch}"


# A failing project the run deliberately did NOT attempt because the per-run
# project cap was already reached. Not a HealResult status (no heal ran) — a
# separate bucket the report names so the cap on unattended spend is never a
# silent omission (ADR-006 §Consequences).
DeferredProject = str


@dataclass(frozen=True)
class FleetHealReport:
    """Outcome of one fleet-wide heal pass — the scheduled trigger's report.

    A pass runs each failing project's heal serially, up to ``limit`` projects,
    and folds the results into one object a scheduler (or the CLI) can render or
    emit as JSON. The unattended-spend story is first-class: ``spend`` totals what
    the agents actually cost (naming the runs it could not meter rather than
    summing them as zero), and ``deferred`` names the failing projects the cap
    kept it from touching this pass.

    Attributes:
        results: One :class:`HealResult` per project actually attempted, in the
            order they were considered.
        deferred: Names of failing projects left untried because ``limit`` was
            reached — the spend cap made visible, never silently dropped.
        limit: The per-run project cap that was applied.
    """

    results: tuple[HealResult, ...]
    deferred: tuple[DeferredProject, ...] = ()
    limit: int = 0

    @property
    def fixed(self) -> tuple[HealResult, ...]:
        """The attempted heals that landed a PR."""
        return tuple(result for result in self.results if result.status == FIXED)

    @property
    def eventful(self) -> bool:
        """Whether this pass did anything a human might want to look at.

        True when it attempted at least one heal or deferred at least one project;
        False only when the fleet had no failing lint/test gate at all. This is the
        signal the scheduler's exit code carries (a quiet fleet exits 0).
        """
        return bool(self.results or self.deferred)

    @property
    def spend(self) -> cost_mod.CostTotal:
        """What the pass's agent runs cost, keeping the truly-unmetered ones visible.

        Only results whose heal actually reached the agent contribute. A
        ``WORKTREE_FAILED`` (or ``NO_FAILURES``) result has ``cost=None`` because
        *no run happened* — counting it would report it as an unmetered run and
        make the total warn "true spend is higher" when nothing was spawned. A
        run that reached the agent but could not be metered (killed/timed-out)
        still shows as unmetered, which is the honest confidence interval.
        """
        return cost_mod.total(
            result.cost for result in self.results if result.status not in _PRE_AGENT_STATUSES
        )


def heal_fleet(
    targets: Sequence[tuple[ProjectDescriptor, dict[str, CheckResult]]],
    *,
    limit: int,
    agent_run: AgentRun | None = None,
    open_pr: OpenPr | None = None,
    mode: str = MODE_FIX,
) -> FleetHealReport:
    """Heal every project with a pending lint/test failure, up to ``limit``; never raises.

    The scheduled trigger's engine (ADR-006 §Consequences). Runs **serially**: each
    heal spawns a scoped, paid coding agent, and an unattended fleet pass must not
    fan out concurrent agents that spike load and spend at once — a predictable
    one-at-a-time march is the safe default. Projects with no pending failure are
    skipped for free (they never count against ``limit``); failing projects beyond
    the cap are recorded as ``deferred`` rather than silently ignored, so the cap
    governing unattended spend is always visible in the report.

    Heal-mode policy (ADR-008): ``mode`` is the run-wide default, and a project's
    declared ``heal.mode`` overrides it — the child's word beats the run's, in
    either direction. Notify-mode projects are diagnosed for free (no worktree,
    no agent), so they never consume the ``limit`` that caps *paid* attempts.

    Args:
        targets: ``(descriptor, {task: CheckResult})`` for every project to
            consider — typically the whole fleet paired with its fresh check
            results, so heal runs against current state, not a stale cache.
        limit: Maximum number of *failing* projects to actually attempt this pass
            (a hard cap on unattended spend). Values below 1 attempt nothing.
        agent_run: Coding-agent override threaded to :func:`heal_project`; ``None``
            uses the real ``claude`` CLI. Tests inject a fake so no live agent runs.
        open_pr: PR-creation override threaded to :func:`heal_project`.
        mode: Run-wide heal mode (``fix`` | ``notify``); per-project declarations
            override it.

    Returns:
        A :class:`FleetHealReport` over the attempted projects plus deferred names.
    """
    failing = [(descriptor, cached) for descriptor, cached in targets if pending_failures(cached)]
    cap = max(limit, 0)
    results: list[HealResult] = []
    deferred: list[DeferredProject] = []
    paid_attempts = 0
    for descriptor, cached in failing:
        resolved = descriptor.heal_mode or mode
        if resolved == MODE_NOTIFY:
            results.append(heal_project(descriptor, cached, mode=MODE_NOTIFY))
            continue
        if paid_attempts >= cap:
            deferred.append(descriptor.name)
            continue
        paid_attempts += 1
        results.append(heal_project(descriptor, cached, agent_run=agent_run, open_pr=open_pr))
    return FleetHealReport(results=tuple(results), deferred=tuple(deferred), limit=limit)


def render_fleet_heal_report(report: FleetHealReport) -> str:
    """Render a fleet heal pass as human-readable lines (pure).

    Args:
        report: The pass to render.

    Returns:
        One line per attempted project, a deferred-projects line when the cap bit,
        and a closing tally with the metered spend; a friendly note when the fleet
        had nothing to heal.
    """
    if not report.eventful:
        return "heal: no failing lint/test gate in the fleet — nothing to do"
    lines = [render_heal_result(result) for result in report.results]
    if report.deferred:
        lines.append(
            f"deferred {len(report.deferred)} more (limit {report.limit}): "
            f"{', '.join(report.deferred)}"
        )
    lines.append(
        f"healed {len(report.fixed)}/{len(report.results)} attempted — "
        f"spend {cost_mod.format_total(report.spend)}"
    )
    return "\n".join(lines)
    lines = [render_heal_result(result) for result in report.results]
    if report.deferred:
        lines.append(
            f"deferred {len(report.deferred)} more (limit {report.limit}): "
            f"{', '.join(report.deferred)}"
        )
    lines.append(
        f"healed {len(report.fixed)}/{len(report.results)} attempted — "
        f"spend {cost_mod.format_total(report.spend)}"
    )
    return "\n".join(lines)
