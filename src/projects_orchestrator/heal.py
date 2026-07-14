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
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from projects_orchestrator import worktree as wt
from projects_orchestrator.briefing import build_briefing, evidence_from_checks
from projects_orchestrator.checks import CheckResult, collect_checks
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.runner import RunResult

# Gates the heal loop can attempt: both are declared, locally-runnable
# commands. ci/cloud are deliberately excluded — they probe remote state a
# local agent run can't reproduce or re-verify.
HEALABLE_TASKS = ("lint", "test")

AGENT_TIMEOUT = 900.0  # a real fix may take several tool calls
GIT_TIMEOUT = 30.0
_MAX_BUDGET_USD = "2.00"
# No bare Bash: an unattended agent (esp. the scheduled trigger, no human
# watching) would otherwise be able to run anything, not just work in this
# project's directory. _agent_allowed_tools scopes Bash to exactly the
# project's own declared lint/test commands (already ADR-003-trusted
# strings) so the agent can re-run them to verify, and nothing else.
_BASE_TOOLS = ("Edit", "Write", "Read", "Grep", "Glob")

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


@dataclass(frozen=True)
class AgentOutcome:
    """Result of one scoped coding-agent invocation.

    Attributes:
        ok: Whether the agent process completed (exit 0); says nothing
            about whether the fix is correct — that is re-verified by
            re-running the failing gate.
        summary: The agent's final reply, or an error description.
    """

    ok: bool
    summary: str = ""


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
    """

    project: str
    status: str
    branch: str = ""
    pr_url: str = ""
    detail: str = ""
    worktree: str = ""


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
        if (command := descriptor.tooling.get(task, "").strip())
    )
    return ",".join((*_BASE_TOOLS, *scoped_bash))


def _extract_result(stdout: str) -> str:
    """Pull the ``result`` field from ``--output-format json``; raw tail otherwise."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return stdout.strip()[-500:]
    return str(payload.get("result", "")) if isinstance(payload, dict) else stdout.strip()[-500:]


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
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell; scoped to the project's own directory
            command,
            cwd=descriptor.path,
            capture_output=True,
            text=True,
            timeout=AGENT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return AgentOutcome(ok=False, summary=str(exc))
    if proc.returncode != 0:
        return AgentOutcome(ok=False, summary=proc.stderr.strip()[-500:] or "agent exited non-zero")
    return AgentOutcome(ok=True, summary=_extract_result(proc.stdout))


def _default_open_pr(
    descriptor: ProjectDescriptor, branch: str, tasks: tuple[str, ...]
) -> PrOutcome:
    """Open a PR for a pushed heal branch via ``gh``."""
    title = f"fix: repair failing {', '.join(tasks)} (automated)"
    body = (
        "Opened automatically by projects-orchestrator's heal command after a scoped "
        f"agent fixed and verified: {', '.join(tasks)}. Review before merging."
    )
    args = ["gh", "pr", "create", "--title", title, "--body", body, "--head", branch]
    result = _run_argv(args, cwd=descriptor.path, timeout=GIT_TIMEOUT)
    if not result.ok:
        return PrOutcome(ok=False, detail=result.stderr.strip()[-300:] or "gh pr create failed")
    url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return PrOutcome(ok=True, url=url)


def _commit_and_land(
    descriptor: ProjectDescriptor, branch: str, tasks: tuple[str, ...], open_pr: OpenPr
) -> HealResult:
    """Commit a verified fix, push the branch, and open a PR."""
    add = _run_argv(["git", "add", "-A"], cwd=descriptor.path)
    if not add.ok:
        return HealResult(
            descriptor.name, BRANCH_FAILED, branch=branch, detail=add.stderr.strip()[-300:]
        )
    message = f"fix: repair failing {', '.join(tasks)} (automated)"
    commit = _run_argv(["git", "commit", "-m", message], cwd=descriptor.path)
    if not commit.ok:
        return HealResult(
            descriptor.name, BRANCH_FAILED, branch=branch, detail=commit.stderr.strip()[-300:]
        )
    push = _run_argv(["git", "push", "-u", "origin", branch], cwd=descriptor.path)
    if not push.ok:
        return HealResult(
            descriptor.name, PUSH_FAILED, branch=branch, detail=push.stderr.strip()[-300:]
        )
    pr = open_pr(descriptor, branch, tasks)
    if not pr.ok:
        return HealResult(descriptor.name, PR_FAILED, branch=branch, detail=pr.detail)
    return HealResult(descriptor.name, FIXED, branch=branch, pr_url=pr.url)


def heal_project(
    descriptor: ProjectDescriptor,
    cached: dict[str, CheckResult],
    agent_run: AgentRun | None = None,
    open_pr: OpenPr | None = None,
) -> HealResult:
    """Attempt to fix one project's failing lint/test gate end to end; never raises.

    Args:
        descriptor: The project to heal.
        cached: This project's ``{task: CheckResult}`` slice of the checks cache.
        agent_run: Coding-agent invocation override; ``None`` uses the real
            ``claude`` CLI. Tests inject a fake so no live agent ever runs.
        open_pr: PR-creation override; ``None`` uses the real ``gh`` CLI.

    Returns:
        A :class:`HealResult` describing what happened. The operator's clone is
        never checked out, branch-switched, or otherwise touched: all work
        happens in a throwaway worktree (:mod:`worktree`), which is removed on
        success and *kept* on failure so the agent's work can be inspected.
    """
    failing = pending_failures(cached)
    if not failing:
        return HealResult(descriptor.name, NO_FAILURES, detail="no failing lint/test gate cached")

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
            )

        landed = _commit_and_land(work, branch, tasks, open_pr or _default_open_pr)
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
