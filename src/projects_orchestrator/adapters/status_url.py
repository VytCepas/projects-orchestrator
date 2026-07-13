"""CI state from a declared status URL — for projects whose CI is not a forge's.

:mod:`~projects_orchestrator.adapters.github` and
:mod:`~projects_orchestrator.adapters.gitlab` cover projects whose CI *is* the
forge's. A project on Jenkins, Buildkite, Drone, or a self-hosted runner has no
`gh`/`glab` to ask, so the orchestrator could only ever report ``unknown`` for
it. Such a project declares ``ci.status_url`` in its descriptor (project-init
#828) and this adapter GETs that JSON endpoint and normalises the outcome.

Read-only and never raises, like every other adapter: a timeout, a non-2xx, a
non-JSON body, a missing field, or a value nobody recognises all degrade to
``unknown`` rather than failing the fleet render. ``unknown`` means "I could not
tell", which is honest; guessing ``pass`` would be a governance lie.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from projects_orchestrator.adapters.github import CI_FAIL, CI_RUNNING, CI_SUCCESS, CI_UNKNOWN
from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import ProjectDescriptor

_TIMEOUT = 15.0

# Keys the common CI systems report their outcome under, tried in order when the
# descriptor declares no explicit status_field: Jenkins (`result`), GitLab/most
# REST APIs (`status`), GitHub-shaped payloads (`conclusion`), Buildkite (`state`).
_STATUS_KEYS = ("result", "status", "conclusion", "state")

# Outcome vocabularies differ per system; normalise to the orchestrator's four.
# Lowercased before lookup, so `SUCCESS` and `success` both land.
_PASS = {"success", "passed", "pass", "ok", "green", "stable", "successful", "completed"}
_FAIL = {"failure", "failed", "fail", "error", "red", "broken", "unstable", "canceled", "cancelled"}
_RUNNING = {"running", "in_progress", "pending", "building", "queued", "started", "scheduled"}

# A sink: given a URL, return the response body. Injectable so tests never make
# a network call.
Fetcher = Callable[[str], str]


def _urllib_fetch(url: str) -> str:
    """Default fetcher: GET ``url`` and return the body as text."""
    request = urllib.request.Request(  # noqa: S310 — operator-declared status URL
        url, headers={"accept": "application/json"}, method="GET"
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310
        return str(response.read().decode("utf-8", errors="replace"))


# "No such key" — distinct from a key that IS there holding null. Jenkins reports
# `result: null` for a build still in flight (→ running), whereas a payload with
# no status key at all is a shape we don't understand (→ unknown). Collapsing
# both to None would report every unreadable endpoint as a live build.
_MISSING = object()


def _dig(payload: Any, dotted: str) -> Any:
    """Walk a dot-path into nested mappings; ``_MISSING`` when any step is absent."""
    current = payload
    for key in dotted.split("."):
        if not isinstance(current, dict) or key not in current:
            return _MISSING
        current = current[key]
    return current


def _raw_status(payload: Any, status_field: str) -> Any:
    """Pull the outcome value out of a decoded payload; ``_MISSING`` if there is none (pure).

    An explicit ``status_field`` wins; otherwise the well-known keys are tried in
    order.
    """
    if status_field:
        return _dig(payload, status_field)
    if not isinstance(payload, dict):
        return _MISSING
    for key in _STATUS_KEYS:
        if key in payload:
            return payload[key]
    return _MISSING


def normalise_status(raw: Any) -> str:
    """Map a CI system's outcome vocabulary onto ``pass``/``fail``/``running``/``unknown`` (pure).

    Args:
        raw: The value read out of the status payload — any type, since the
            endpoint is arbitrary.

    Returns:
        One of ``pass`` | ``fail`` | ``running`` | ``unknown``. ``None`` (a
        Jenkins build in flight) is ``running``; a bool is *not* coerced, and an
        unrecognised string is ``unknown`` rather than a guess.
    """
    if raw is None:
        return CI_RUNNING
    if not isinstance(raw, str):
        return CI_UNKNOWN
    value = raw.strip().lower()
    if value in _PASS:
        return CI_SUCCESS
    if value in _FAIL:
        return CI_FAIL
    if value in _RUNNING:
        return CI_RUNNING
    return CI_UNKNOWN


def probe_status_url(descriptor: ProjectDescriptor, fetch: Fetcher | None = None) -> str:
    """Probe a project's declared CI status endpoint; never raises.

    Args:
        descriptor: The project. Must declare ``ci.status_url`` — callers check
            ``descriptor.ci`` first (an undeclared endpoint is not this
            adapter's business).
        fetch: Fetcher override; ``None`` uses a bounded stdlib GET. Tests inject
            a fake so no network call is made.

    Returns:
        ``pass`` | ``fail`` | ``running`` | ``unknown``; every failure mode —
        unreachable, non-2xx, non-JSON, field absent, value unrecognised — is
        ``unknown``.
    """
    if descriptor.ci is None:
        return CI_UNKNOWN
    fetcher = fetch or _urllib_fetch
    try:
        body = fetcher(descriptor.ci.status_url)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return CI_UNKNOWN
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return CI_UNKNOWN
    raw = _raw_status(payload, descriptor.ci.status_field)
    if raw is _MISSING:
        return CI_UNKNOWN
    return normalise_status(raw)


def status_check_results(project: str, ci: str, checked_at: str) -> list[CheckResult]:
    """Adapt a probed CI state into cacheable check results.

    A status URL reports builds, not code review, so there is no PR/MR count to
    report. ``prs`` is emitted as ``unknown`` rather than omitted, so the fleet
    table renders ``?`` for it instead of a stale count from a previous probe.
    """
    return [
        CheckResult(project=project, task="ci", status=ci, checked_at=checked_at),
        CheckResult(
            project=project, task="prs", status=CI_UNKNOWN, detail="", checked_at=checked_at
        ),
    ]
