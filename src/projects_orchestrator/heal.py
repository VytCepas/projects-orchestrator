"""Autonomous fix dispatch — spawn a scoped coding agent to repair a failing gate.

The engine can *detect* a failing lint/test gate (``checks.py``) but not
repair it. This closes that loop for the two gates every project declares
locally: given a project with a cached failure, it checks out a dedicated
branch, hands a scoped coding agent (the ``claude`` CLI, headless) exactly
the failing command and its last-known error, re-runs the gate to verify
the fix, and — only on a verified pass — commits, pushes, and opens a PR.
Nothing ever reaches the default branch without a human merging it (ADR-006).

Like the rest of the engine, this never raises: a dirty worktree, an agent
that can't fix the gate, or a push/PR failure all degrade to a
:class:`HealResult` the caller renders, never an exception.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from projects_orchestrator.checks import CheckResult, collect_checks
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.runner import run_command
from projects_orchestrator.status import collect_status

# Gates the heal loop can attempt: both are declared, locally-runnable
# commands. ci/cloud are deliberately excluded — they probe remote state a
# local agent run can't reproduce or re-verify.
HEALABLE_TASKS = ("lint", "test")

AGENT_TIMEOUT = 900.0  # a real fix may take several tool calls
GIT_TIMEOUT = 30.0
_MAX_BUDGET_USD = "2.00"
_ALLOWED_TOOLS = "Bash,Edit,Write,Read,Grep,Glob"

NO_FAILURES = "no_action"
WORKTREE_DIRTY = "worktree_dirty"
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
    """

    project: str
    status: str
    branch: str = ""
    pr_url: str = ""
    detail: str = ""


AgentRun = Callable[[ProjectDescriptor, str], AgentOutcome]
OpenPr = Callable[[ProjectDescriptor, str, tuple[str, ...]], PrOutcome]


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

    Args:
        descriptor: The project being healed (the agent runs in its directory).
        failing: The cached failures to fix.

    Returns:
        A prompt naming exactly the failing command(s) and the last-known
        error, instructing a minimal, scoped fix with no commit.
    """
    lines = [
        f"Project '{descriptor.name}' has {len(failing)} failing gate(s). Fix them with "
        "the smallest correct change; do not touch unrelated files or refactor working "
        "code. Do not create a git commit — the orchestrator commits your changes after "
        "verifying the fix.",
        "",
    ]
    for result in failing:
        command = descriptor.tooling.get(result.task, "")
        lines.append(f"- `{result.task}` runs `{command}`")
        if result.detail:
            lines.append(f"  last known failure: {result.detail}")
        lines.append("  re-run it yourself to see the full output before fixing.")
    return "\n".join(lines)


def _extract_result(stdout: str) -> str:
    """Pull the ``result`` field from ``--output-format json``; raw tail otherwise."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return stdout.strip()[-500:]
    return str(payload.get("result", "")) if isinstance(payload, dict) else stdout.strip()[-500:]


def _default_agent_run(descriptor: ProjectDescriptor, prompt: str) -> AgentOutcome:
    """Invoke the ``claude`` CLI headless, scoped to ``descriptor.path``.

    Runs with ``bypassPermissions`` so an unattended run does not stall on a
    tool-use prompt — the safety net is that nothing this produces reaches
    the default branch unreviewed (the caller only pushes/opens a PR after
    re-verifying the fix; ADR-006).
    """
    command = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
        "--allowedTools",
        _ALLOWED_TOOLS,
        "--max-budget-usd",
        _MAX_BUDGET_USD,
    ]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell; scoped to the project's own directory
            command, cwd=descriptor.path, capture_output=True, text=True, timeout=AGENT_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return AgentOutcome(ok=False, summary=str(exc))
    if proc.returncode != 0:
        return AgentOutcome(ok=False, summary=proc.stderr.strip()[-500:] or "agent exited non-zero")
    return AgentOutcome(ok=True, summary=_extract_result(proc.stdout))


def _default_open_pr(descriptor: ProjectDescriptor, branch: str, tasks: tuple[str, ...]) -> PrOutcome:
    """Open a PR for a pushed heal branch via ``gh``."""
    title = f"fix: repair failing {', '.join(tasks)} (automated)"
    body = (
        "Opened automatically by projects-orchestrator's heal command after a scoped "
        f"agent fixed and verified: {', '.join(tasks)}. Review before merging."
    )
    command = f'gh pr create --title "{title}" --body "{body}" --head {branch}'
    result = run_command(command, cwd=descriptor.path, timeout=GIT_TIMEOUT)
    if not result.ok:
        return PrOutcome(ok=False, detail=result.stderr.strip()[-300:] or "gh pr create failed")
    url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return PrOutcome(ok=True, url=url)


def _restore_branch(descriptor: ProjectDescriptor, original_branch: str) -> None:
    """Return the worktree to the branch it was on before healing started."""
    if original_branch:
        run_command(f"git checkout {original_branch}", cwd=descriptor.path, timeout=GIT_TIMEOUT)


def _commit_and_land(
    descriptor: ProjectDescriptor, branch: str, tasks: tuple[str, ...], open_pr: OpenPr
) -> HealResult:
    """Commit a verified fix, push the branch, and open a PR."""
    commit = run_command(
        f'git add -A && git commit -m "fix: repair failing {", ".join(tasks)} (automated)"',
        cwd=descriptor.path,
        timeout=GIT_TIMEOUT,
    )
    if not commit.ok:
        return HealResult(
            descriptor.name, BRANCH_FAILED, branch=branch, detail=commit.stderr.strip()[-300:]
        )
    push = run_command(f"git push -u origin {branch}", cwd=descriptor.path, timeout=GIT_TIMEOUT)
    if not push.ok:
        return HealResult(descriptor.name, PUSH_FAILED, branch=branch, detail=push.stderr.strip()[-300:])
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
        A :class:`HealResult` describing what happened. The worktree is
        always returned to its original branch before this returns.
    """
    failing = pending_failures(cached)
    if not failing:
        return HealResult(descriptor.name, NO_FAILURES, detail="no failing lint/test gate cached")

    status = collect_status(descriptor)
    if status.dirty is not False:
        return HealResult(
            descriptor.name, WORKTREE_DIRTY, detail="worktree is dirty or unreadable — refusing to heal"
        )

    original_branch = status.branch or ""
    tasks = tuple(sorted({result.task for result in failing}))
    branch = f"heal/{'-'.join(tasks)}-{descriptor.name}"

    checkout = run_command(f"git checkout -B {branch}", cwd=descriptor.path, timeout=GIT_TIMEOUT)
    if not checkout.ok:
        return HealResult(descriptor.name, BRANCH_FAILED, detail=checkout.stderr.strip()[-300:])

    try:
        outcome = (agent_run or _default_agent_run)(descriptor, build_heal_prompt(descriptor, failing))
        if not outcome.ok:
            return HealResult(descriptor.name, AGENT_FAILED, branch=branch, detail=outcome.summary)

        verify = collect_checks(descriptor, tasks)
        still_failing = [result.task for result in verify if result.status != "pass"]
        if still_failing:
            return HealResult(
                descriptor.name,
                VERIFY_FAILED,
                branch=branch,
                detail=f"still failing after the agent's fix: {', '.join(still_failing)}",
            )
        return _commit_and_land(descriptor, branch, tasks, open_pr or _default_open_pr)
    finally:
        _restore_branch(descriptor, original_branch)


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
