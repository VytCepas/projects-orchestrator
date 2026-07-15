"""Per-project deploy/runtime status, driven by the contract-v2 deploy block.

The orchestrator is otherwise blind to runtime: it cannot see what revision
of a ``delivery: service`` project is deployed or whether its health check
passes. This adapter fills that gap **read-only**: probes go through the
owning platform CLI (``flyctl``, ``gcloud``) via the shared timeout-bounded
runner, health is a bounded stdlib HTTP GET, and no code path here ever
issues a mutating cloud command — mutations stay in review-gated CI
(ADR-012 credential separation).

``deploy: none`` (or no deploy block at all) short-circuits at zero cost:
no subprocess, no network. Everything else degrades to ``unknown`` exactly
like :mod:`~projects_orchestrator.status` — a missing CLI or offline probe
is a cell, never an exception.

Results map to :class:`~projects_orchestrator.checks.CheckResult` (task
``cloud``) so the ``status`` table shows last-known cloud state offline;
only the explicit ``cloud-status`` command makes the calls.
"""

from __future__ import annotations

import json
import shlex
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from projects_orchestrator.adapters.gitlab import provider_is_gitlab
from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import DEPLOY_NONE, ProjectDescriptor
from projects_orchestrator.runner import RunResult, run_command

STATE_NONE = "none"
STATE_DEPLOYED = "deployed"
STATE_STOPPED = "stopped"
STATE_UNKNOWN = "unknown"

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"

# Cloud control plane (ADR-005). Actions are *dispatched* to the child's own
# workflow_dispatch pipeline — the orchestrator never holds cloud credentials
# or runs a platform mutation itself. Credentials stay in review-gated CI
# (ADR-012). The default workflow name is the convention a child unlocks by
# shipping it; ``deploy.workflow`` overrides it.
DEPLOY_ACTIONS = ("deploy", "rollback", "restart")
DEFAULT_DEPLOY_WORKFLOW = "deploy.yml"

DISPATCH_PLANNED = "planned"
DISPATCH_DISPATCHED = "dispatched"
DISPATCH_FAILED = "failed"
DISPATCH_SKIPPED = "skipped"
# The child ships no workflow to dispatch. Distinct from `failed` on purpose: it
# is a permanent, structural fact ("this project cannot be deployed this way"),
# not a transient one ("gh was offline"). Retrying will never help.
DISPATCH_NO_WORKFLOW = "no-workflow"

# Settlement of a dispatched deploy (ADR-005's deferred poll-until-settled).
# A dispatch confirms only that a run was QUEUED; --wait follows it to a verdict.
# The states are asymmetric on purpose — only ``succeeded`` is good news, and the
# three not-good outcomes are kept distinct because an operator mid-incident needs
# to tell "it failed" from "it is still running" from "I could not confirm".
SETTLE_SUCCEEDED = "succeeded"
SETTLE_FAILED = "failed"
# Dispatched and observed running, but not finished before --wait gave up. The
# deploy may yet succeed; we simply stopped watching. NOT a failure.
SETTLE_TIMED_OUT = "timed-out"
# The dispatch returned OK but no new run ever appeared to us. We cannot confirm
# anything — reported as its own state and NEVER as success. Silence is not success.
SETTLE_UNCONFIRMED = "unconfirmed"
# `gh` missing, unauthenticated, or offline during polling — like every other
# read path here, an unreachable forge is ``unknown``, not a verdict.
SETTLE_UNKNOWN = "unknown"
# Waiting is a GitHub-only capability (it follows a `gh` run). A GitLab project
# says so rather than silently returning as though the wait had happened.
SETTLE_UNSUPPORTED = "unsupported"

_FLY_COMMAND = "flyctl status --json"
_CLOUD_RUN_COMMAND = "gcloud run services describe {app} --region {region} --format=json"
_DISPATCH_COMMAND = "gh workflow run {workflow} -f action={action}"
# A GitLab child has no `gh` to dispatch with. trigger_upgrade already branches
# on the forge; without the same branch here, a GitLab service project reports a
# clean `planned` dry run and then fails at --apply with `gh` shouting into a
# repo it cannot resolve — structurally undeployable, and nothing said so.
_GITLAB_DISPATCH_COMMAND = "glab ci run --variables action:{action}"
# List recent runs of one workflow, newest first, with the fields we need to
# identify our run (databaseId) and read its verdict (status, conclusion, url).
_RUN_LIST_COMMAND = (
    "gh run list --workflow {workflow} --limit 20 --json databaseId,status,conclusion,url,createdAt"
)

_PROBE_TIMEOUT = 20.0
_HEALTH_TIMEOUT = 5.0
_DISPATCH_TIMEOUT = 20.0
# Poll defaults for --wait. A deploy is minutes, not seconds, so the ceiling is
# generous and the cadence unhurried — a tight poll would just rate-limit `gh`.
_WAIT_TIMEOUT = 900.0
_WAIT_POLL_INTERVAL = 10.0
_WAIT_LIST_TIMEOUT = 20.0


@dataclass(frozen=True)
class CloudStatus:
    """One project's deploy/runtime state.

    Attributes:
        project: Project name.
        target: Declared deploy target (``none`` when undeclared).
        state: ``none`` | ``deployed`` | ``stopped`` | ``unknown``.
        revision: Deployed revision/version, when the platform reports one.
        health: ``healthy`` | ``unhealthy`` | ``unknown``; empty when the
            project declares no health URL.
    """

    project: str
    target: str = DEPLOY_NONE
    state: str = STATE_NONE
    revision: str = ""
    health: str = ""


def _loads(stdout: str) -> Any:
    """Parse JSON stdout, returning ``None`` on any problem."""
    try:
        return json.loads(stdout)
    except (ValueError, TypeError):
        return None


def parse_fly_status(stdout: str) -> tuple[str, str]:
    """Map ``flyctl status --json`` output to (state, revision) (pure).

    Args:
        stdout: JSON object from ``flyctl status``.

    Returns:
        The app state and version; anything unparseable is ``unknown``.
    """
    data = _loads(stdout)
    if not isinstance(data, dict):
        return STATE_UNKNOWN, ""
    revision = str(data.get("Version") or "")
    status = str(data.get("Status") or "").lower()
    if data.get("Deployed") is True or status in {"deployed", "running"}:
        return STATE_DEPLOYED, revision
    if status in {"suspended", "stopped"}:
        return STATE_STOPPED, revision
    return STATE_UNKNOWN, revision


def parse_cloud_run_status(stdout: str) -> tuple[str, str]:
    """Map ``gcloud run services describe`` output to (state, revision) (pure).

    Args:
        stdout: JSON object from ``gcloud run services describe``.

    Returns:
        ``deployed`` when the Ready condition is true, ``stopped`` when it is
        explicitly false, else ``unknown`` — plus the latest ready revision.
    """
    data = _loads(stdout)
    if not isinstance(data, dict):
        return STATE_UNKNOWN, ""
    status = data.get("status")
    if not isinstance(status, dict):
        return STATE_UNKNOWN, ""
    revision = str(status.get("latestReadyRevisionName") or "")
    for condition in status.get("conditions") or []:
        if isinstance(condition, dict) and condition.get("type") == "Ready":
            if condition.get("status") == "True":
                return STATE_DEPLOYED, revision
            if condition.get("status") == "False":
                return STATE_STOPPED, revision
    return STATE_UNKNOWN, revision


def probe_health(url: str, timeout: float = _HEALTH_TIMEOUT) -> str:
    """HTTP GET a health URL; never raises.

    Args:
        url: The health-check URL (http/https only).
        timeout: Socket timeout in seconds.

    Returns:
        ``healthy`` (2xx/3xx), ``unhealthy`` (HTTP error status), or
        ``unknown`` (unreachable, timeout, or a non-HTTP scheme).
    """
    if not url.startswith(("http://", "https://")):
        return STATE_UNKNOWN
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 — scheme checked above; descriptor-declared health URL
            return HEALTHY if response.status < 400 else UNHEALTHY
    except urllib.error.HTTPError:
        return UNHEALTHY
    except (urllib.error.URLError, OSError, ValueError):
        return STATE_UNKNOWN


def collect_cloud(descriptor: ProjectDescriptor, timeout: float = _PROBE_TIMEOUT) -> CloudStatus:
    """Probe one project's deploy state and health; never raises.

    Args:
        descriptor: The project to probe (platform CLIs run in its directory).
        timeout: Per-command timeout in seconds.

    Returns:
        A :class:`CloudStatus`; ``deploy: none`` (or no block) returns
        immediately with no subprocess or network call.
    """
    deploy = descriptor.deploy
    if deploy is None or deploy.target == DEPLOY_NONE:
        return CloudStatus(project=descriptor.name)

    if deploy.target == "fly":
        result = run_command(_FLY_COMMAND, cwd=descriptor.path, timeout=timeout)
        state, revision = parse_fly_status(result.stdout) if result.ok else (STATE_UNKNOWN, "")
    elif deploy.target == "cloud-run":
        # deploy.app/region are descriptor data, not vetted commands: quote
        # them so a hostile child config can't inject shell into the
        # nominally read-only cloud-status probe.
        command = _CLOUD_RUN_COMMAND.format(
            app=shlex.quote(deploy.app), region=shlex.quote(deploy.region)
        )
        result = run_command(command, cwd=descriptor.path, timeout=timeout)
        state, revision = (
            parse_cloud_run_status(result.stdout) if result.ok else (STATE_UNKNOWN, "")
        )
    else:
        state, revision = STATE_UNKNOWN, ""

    health = probe_health(deploy.health_url) if deploy.health_url else ""
    return CloudStatus(
        project=descriptor.name,
        target=deploy.target,
        state=state,
        revision=revision,
        health=health,
    )


def as_check_results(status: CloudStatus, checked_at: str) -> list[CheckResult]:
    """Adapt a :class:`CloudStatus` into one cacheable ``cloud`` check result.

    Args:
        status: The probed cloud state.
        checked_at: ISO-8601 timestamp to stamp the result with.

    Returns:
        A single ``cloud`` result whose status renders directly as the
        fleet-table cell: ``none`` | ``pass`` | ``fail`` | ``unknown``.
    """
    if status.target == DEPLOY_NONE:
        cell = STATE_NONE
    elif status.health == UNHEALTHY or status.state == STATE_STOPPED:
        cell = "fail"
    elif status.state == STATE_DEPLOYED:
        cell = "pass"
    else:
        cell = STATE_UNKNOWN
    detail = " ".join(part for part in (status.revision, status.health) if part)
    return [
        CheckResult(
            project=status.project,
            task="cloud",
            status=cell,
            detail=detail,
            checked_at=checked_at,
        )
    ]


def deploy_workflow_relpath(descriptor: ProjectDescriptor, workflow: str = "") -> Path:
    """Where the child's deploy workflow lives, per its forge."""
    name = workflow or (descriptor.deploy.workflow if descriptor.deploy else "")
    name = name or DEFAULT_DEPLOY_WORKFLOW
    root = Path(".gitlab") if provider_is_gitlab(descriptor) else Path(".github/workflows")
    return root / name


def has_deploy_workflow(descriptor: ProjectDescriptor) -> bool:
    """Whether the child actually ships the deploy workflow a dispatch would target.

    The counterpart of
    :func:`~projects_orchestrator.adapters.project_init.has_upgrade_workflow`,
    and for the same reason: a missing workflow should be *diagnosable* (via
    ``doctor``, and on the dry run) rather than surfacing as a silent dispatch
    failure the first time someone reaches for ``--apply`` in an incident.
    """
    return (descriptor.path / deploy_workflow_relpath(descriptor)).is_file()


def _failure_detail(result: RunResult) -> str:
    """Say *why* a dispatch failed, in one line (pure)."""
    if result.timed_out:
        return f"dispatch timed out after {result.duration:.0f}s"
    if result.error:
        return result.error
    stderr = " ".join(result.stderr.split())
    return stderr[:200] if stderr else f"dispatch exited {result.returncode}"


@dataclass(frozen=True)
class DeployDispatch:
    """The outcome of one cloud action for one project (ADR-005).

    Attributes:
        project: Project name.
        action: Requested action (``deploy`` | ``rollback`` | ``restart``).
        workflow: The child workflow that would be / was dispatched; empty when
            skipped.
        status: ``planned`` (dry run, nothing dispatched) | ``dispatched`` |
            ``failed`` (gh missing/offline/no such workflow) | ``skipped``
            (no deploy target, or an unknown action).
        detail: Human-readable note (why it was skipped, or the dry-run hint).
    """

    project: str
    action: str
    workflow: str = ""
    status: str = DISPATCH_SKIPPED
    detail: str = ""


def trigger_deploy(
    descriptor: ProjectDescriptor,
    action: str = "deploy",
    *,
    apply: bool = False,
    timeout: float = _DISPATCH_TIMEOUT,
) -> DeployDispatch:
    """Dispatch a child's deploy workflow for a cloud action; never raises.

    Read-only toward the child tree and the cloud: this only *dispatches* the
    child's own ``workflow_dispatch`` pipeline (``deploy.workflow``, default
    ``deploy.yml``) with an ``action`` input. The mutation runs in the child's
    CI, where production credentials live — the orchestrator holds none and
    runs no platform command itself (ADR-005 / ADR-012).

    Args:
        descriptor: The service project to act on.
        action: One of :data:`DEPLOY_ACTIONS`.
        apply: When ``False`` (the default) nothing is dispatched — the result
            is ``planned`` and no subprocess runs, so any surface can call it
            safely. Only ``apply=True`` fires ``gh workflow run``.
        timeout: Command timeout in seconds.

    Returns:
        A :class:`DeployDispatch`. ``skipped`` for a non-service project (no
        deploy target) or an unknown action; ``no-workflow`` when the child ships
        no such workflow to dispatch; ``planned`` on a dry run;
        ``dispatched``/``failed`` once applied. A ``failed`` result always
        carries *why* in ``detail``.
    """
    deploy = descriptor.deploy
    if deploy is None or deploy.target == DEPLOY_NONE:
        return DeployDispatch(project=descriptor.name, action=action, detail="no deploy target")
    if action not in DEPLOY_ACTIONS:
        return DeployDispatch(
            project=descriptor.name,
            action=action,
            detail=f"unknown action (expected {', '.join(DEPLOY_ACTIONS)})",
        )
    workflow = deploy.workflow or DEFAULT_DEPLOY_WORKFLOW
    # Pre-flight, mirroring trigger_upgrade's has_upgrade_workflow gate: a child
    # that ships no such workflow can never be deployed, and saying so up front —
    # on the DRY RUN, before anyone commits to --apply — beats reporting a plan
    # that cannot execute and only discovering it mid-incident.
    if not has_deploy_workflow(descriptor):
        return DeployDispatch(
            project=descriptor.name,
            action=action,
            workflow=workflow,
            status=DISPATCH_NO_WORKFLOW,
            detail=f"child ships no {deploy_workflow_relpath(descriptor, workflow)}",
        )
    if not apply:
        return DeployDispatch(
            project=descriptor.name,
            action=action,
            workflow=workflow,
            status=DISPATCH_PLANNED,
            detail="dry run — pass --apply to dispatch",
        )
    # workflow/action are descriptor/enum data, but quote defensively so a
    # hostile child config can never inject shell into the dispatch command.
    template = _GITLAB_DISPATCH_COMMAND if provider_is_gitlab(descriptor) else _DISPATCH_COMMAND
    command = template.format(workflow=shlex.quote(workflow), action=shlex.quote(action))
    result = run_command(command, cwd=descriptor.path, timeout=timeout)
    return DeployDispatch(
        project=descriptor.name,
        action=action,
        workflow=workflow,
        status=DISPATCH_DISPATCHED if result.ok else DISPATCH_FAILED,
        # Without this, every failure mode — CLI missing, not authenticated,
        # network down, workflow input mismatch — renders as a bare `failed`.
        # An operator mid-rollback cannot tell "impossible" from "retry".
        detail="" if result.ok else _failure_detail(result),
    )


# --- Poll-until-settled (ADR-005 deferred consequence) -------------------------

#: gh's terminal conclusions that are anything but a clean success. ``conclusion``
#: is null while a run is in flight; only a non-null, non-``success`` value here
#: is a real failure.
_FAILED_CONCLUSIONS = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
)


@dataclass(frozen=True)
class WaitPolicy:
    """The knobs and seams for :func:`wait_for_deploy`, bundled so it stays pure.

    ``now``/``sleep`` are injectable for the same reason :class:`campaign.Seams`
    injects a clock: the poll loop must be testable without real wall-clock or a
    real ``time.sleep`` stalling the suite.

    Attributes:
        timeout: Give up waiting after this many seconds.
        poll_interval: Seconds between ``gh run list`` polls.
        now: Monotonic clock source.
        sleep: Sleep function, called between polls.
    """

    timeout: float = _WAIT_TIMEOUT
    poll_interval: float = _WAIT_POLL_INTERVAL
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class DeploySettlement:
    """What became of a dispatched deploy once we followed it (ADR-005).

    Attributes:
        project: Project name.
        workflow: The workflow whose run we watched.
        state: One of the ``SETTLE_*`` constants.
        detail: Human-readable note — the conclusion, why we could not confirm,
            or which failure mode a poll hit.
        run_url: URL of the run we watched, when we identified one.
    """

    project: str
    workflow: str
    state: str
    detail: str = ""
    run_url: str = ""

    @property
    def succeeded(self) -> bool:
        """Whether the deploy is confirmed successful — and nothing weaker."""
        return self.state == SETTLE_SUCCEEDED


def _run_records(stdout: str) -> list[dict[str, Any]]:
    """Decode ``gh run list --json`` output to a list of run dicts (pure)."""
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def newest_run_id(stdout: str) -> int | None:
    """The highest ``databaseId`` in a run list, or ``None`` (pure).

    Captured *before* dispatch, this is the high-water mark that lets
    :func:`wait_for_deploy` recognise the run the dispatch created — the one with
    an id above every run that already existed — rather than mistaking a
    pre-existing run for ours.
    """
    ids = [
        int(record["databaseId"])
        for record in _run_records(stdout)
        if isinstance(record.get("databaseId"), int)
    ]
    return max(ids) if ids else None


def resolved_workflow(descriptor: ProjectDescriptor) -> str:
    """The deploy workflow name a dispatch would use — declared, or the default.

    Exposed so a caller can name the workflow *before* dispatching (to read the
    pre-dispatch watermark) without re-deriving the default-fallback rule.
    """
    deploy = descriptor.deploy
    return (deploy.workflow if deploy else "") or DEFAULT_DEPLOY_WORKFLOW


def latest_run_id(descriptor: ProjectDescriptor, workflow: str) -> int | None:
    """The newest existing run id for ``workflow`` — the pre-dispatch watermark.

    Call this *before* dispatching so :func:`wait_for_deploy` can tell our run
    from the ones already there. ``None`` when there are no runs yet, or when
    ``gh`` cannot answer — in the latter case the wait simply has no floor and
    treats the first run it sees as ours, which is the best it can do blind.
    """
    command = _RUN_LIST_COMMAND.format(workflow=shlex.quote(workflow))
    result = run_command(command, cwd=descriptor.path, timeout=_WAIT_LIST_TIMEOUT)
    return newest_run_id(result.stdout) if result.ok else None


def _our_run(stdout: str, after_id: int) -> dict[str, Any] | None:
    """The newest run created after ``after_id`` — our dispatch's run (pure).

    Older runs (id <= ``after_id``) existed before we dispatched and are not ours,
    so they are filtered out entirely: following one would report a *previous*
    deploy's verdict as this one's, which is worse than reporting nothing.
    """
    ours = [
        record
        for record in _run_records(stdout)
        if isinstance(record.get("databaseId"), int) and int(record["databaseId"]) > after_id
    ]
    if not ours:
        return None
    return max(ours, key=lambda record: int(record["databaseId"]))


def classify_run(record: dict[str, Any]) -> str:
    """Map one gh run record to a settlement state (pure).

    Returns ``SETTLE_SUCCEEDED``/``SETTLE_FAILED`` once the run is ``completed``,
    and ``""`` while it is still in flight — the caller keeps polling on empty.
    An unrecognised ``completed`` conclusion is treated as a failure, not ignored:
    a verdict we do not understand must not be allowed to read as success.
    """
    if record.get("status") != "completed":
        return ""
    conclusion = record.get("conclusion")
    if conclusion == "success":
        return SETTLE_SUCCEEDED
    return SETTLE_FAILED


def wait_for_deploy(
    descriptor: ProjectDescriptor,
    workflow: str,
    after_id: int | None,
    policy: WaitPolicy | None = None,
) -> DeploySettlement:
    """Follow a dispatched deploy to its conclusion; never raises.

    Polls ``gh run list`` for the run whose id is above ``after_id`` (the
    pre-dispatch high-water mark) and returns when it completes. Timing and the
    clock/sleep seams come from ``policy`` (:class:`WaitPolicy`), injected so the
    loop is testable without real time — as :class:`campaign.Seams` does.

    The outcome is deliberately honest about what it did and did not see:

    - ``succeeded``/``failed`` — the run completed and we read its conclusion.
    - ``timed-out`` — we saw our run but it was still running when ``timeout``
      elapsed. The deploy may yet succeed; we stopped watching. Not a failure.
    - ``unconfirmed`` — no run above ``after_id`` ever appeared. We confirm
      nothing, and in particular we do not confirm success.
    - ``unknown`` — ``gh`` was missing/unauthenticated/offline the whole time.
    - ``unsupported`` — a GitLab project, whose runs ``gh`` cannot follow.
    """
    if provider_is_gitlab(descriptor):
        return DeploySettlement(
            project=descriptor.name,
            workflow=workflow,
            state=SETTLE_UNSUPPORTED,
            detail="--wait follows a GitHub Actions run; glab pipelines are not polled",
        )

    policy = policy or WaitPolicy()
    floor = after_id if after_id is not None else -1
    command = _RUN_LIST_COMMAND.format(workflow=shlex.quote(workflow))
    deadline = policy.now() + policy.timeout
    saw_our_run = False
    reachable = False
    while True:
        result = run_command(command, cwd=descriptor.path, timeout=_WAIT_LIST_TIMEOUT)
        if result.ok:
            reachable = True
            record = _our_run(result.stdout, floor)
            if record is not None:
                saw_our_run = True
                state = classify_run(record)
                if state:
                    return DeploySettlement(
                        project=descriptor.name,
                        workflow=workflow,
                        state=state,
                        detail=str(record.get("conclusion") or "completed"),
                        run_url=str(record.get("url") or ""),
                    )
        # Not settled yet. Stop when the budget is spent — but only AFTER a poll,
        # so a timeout shorter than one interval still checks once.
        if policy.now() >= deadline:
            return _wait_deadline(
                descriptor, workflow, saw_our_run=saw_our_run, reachable=reachable
            )
        policy.sleep(policy.poll_interval)


def _wait_deadline(
    descriptor: ProjectDescriptor, workflow: str, *, saw_our_run: bool, reachable: bool
) -> DeploySettlement:
    """The settlement when ``wait_for_deploy`` runs out of time (pure)."""
    if saw_our_run:
        state, detail = SETTLE_TIMED_OUT, "the run was still in flight when --wait gave up"
    elif reachable:
        state, detail = (
            SETTLE_UNCONFIRMED,
            "no run for this dispatch appeared before --wait gave up",
        )
    else:
        state, detail = SETTLE_UNKNOWN, "gh was unreachable throughout the wait"
    return DeploySettlement(project=descriptor.name, workflow=workflow, state=state, detail=detail)
