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
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import DEPLOY_NONE, ProjectDescriptor
from projects_orchestrator.runner import run_command

STATE_NONE = "none"
STATE_DEPLOYED = "deployed"
STATE_STOPPED = "stopped"
STATE_UNKNOWN = "unknown"

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"

_FLY_COMMAND = "flyctl status --json"
_CLOUD_RUN_COMMAND = "gcloud run services describe {app} --region {region} --format=json"

_PROBE_TIMEOUT = 20.0
_HEALTH_TIMEOUT = 5.0


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
