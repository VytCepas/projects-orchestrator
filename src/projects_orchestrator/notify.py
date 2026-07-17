"""Threshold alerts and a notifications sink — governance that reaches out.

The fleet view is pull: you look at it. Some states you want *pushed* the
moment they appear — CI going red, scaffold drift showing up, git hooks not
installed, a service turning unhealthy. :func:`fleet_alerts` distills a fleet
snapshot into a flat list of such alerts (pure, threshold-based);
:func:`post_webhook` delivers them to an HTTP endpoint (Slack-compatible JSON),
opt-in and — like the rest of the engine — never raising. The ``notify``
command wires them together for a cron/CI job.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass

from projects_orchestrator.fleet import ProjectSnapshot

CRITICAL = "critical"
WARNING = "warning"

_LEVEL_RANK = {CRITICAL: 0, WARNING: 1}

_WEBHOOK_TIMEOUT = 15.0

# A sink: given (url, json body), return the HTTP status code. Injectable so
# tests never make a network call.
Sender = Callable[[str, bytes], int]


@dataclass(frozen=True)
class Alert:
    """One threshold crossing worth pushing.

    Attributes:
        project: The project the alert is about.
        level: ``critical`` | ``warning``.
        category: Short machine slug (``ci``, ``tests``, ``drift``, …).
        message: Human-readable one-line description.
    """

    project: str
    level: str
    category: str
    message: str


def snapshot_alerts(snapshot: ProjectSnapshot) -> list[Alert]:
    """Derive the alerts for one project snapshot (pure)."""
    name = snapshot.descriptor.name
    alerts: list[Alert] = []

    def check_failed(task: str) -> bool:
        result = snapshot.checks.get(task)
        return result is not None and result.status == "fail"

    if check_failed("test"):
        alerts.append(Alert(name, CRITICAL, "tests", "tests are failing"))
    if check_failed("ci"):
        alerts.append(Alert(name, CRITICAL, "ci", "CI is red"))
    if check_failed("cloud"):
        alerts.append(Alert(name, CRITICAL, "cloud", "deployment is unhealthy"))
    if check_failed("process"):
        alerts.append(Alert(name, CRITICAL, "process", "supervised process died"))
    if check_failed("lint"):
        alerts.append(Alert(name, WARNING, "lint", "lint is failing"))
    if snapshot.drift.status == "drift":
        alerts.append(
            Alert(name, WARNING, "drift", f"{snapshot.drift.summary} diverged from scaffold")
        )
    if snapshot.hooks in ("missing", "partial"):
        alerts.append(
            Alert(name, WARNING, "hooks", f"git hooks {snapshot.hooks} — enforcement inactive")
        )
    return alerts


def fleet_alerts(snapshots: list[ProjectSnapshot]) -> list[Alert]:
    """Collect every project's alerts, most severe first (pure)."""
    alerts = [alert for snapshot in snapshots for alert in snapshot_alerts(snapshot)]
    return sorted(alerts, key=lambda a: (_LEVEL_RANK.get(a.level, 9), a.project, a.category))


def render_alerts(alerts: list[Alert]) -> str:
    """Render alerts as text lines, or a friendly all-clear (pure)."""
    if not alerts:
        return "no alerts — fleet is within thresholds"
    return "\n".join(f"[{a.level}] {a.project}: {a.message} ({a.category})" for a in alerts)


def alerts_payload(alerts: list[Alert]) -> dict[str, object]:
    """Build the JSON/webhook payload (Slack-compatible ``text`` + details)."""
    summary = (
        "no alerts — fleet is within thresholds"
        if not alerts
        else f"{len(alerts)} fleet alert(s): " + render_alerts(alerts)
    )
    return {"text": summary, "alerts": [asdict(a) for a in alerts]}


def _urllib_send(url: str, body: bytes) -> int:
    """Default sink: POST ``body`` as JSON and return the HTTP status code."""
    request = urllib.request.Request(  # noqa: S310 — operator-supplied webhook URL
        url, data=body, headers={"content-type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=_WEBHOOK_TIMEOUT) as response:  # noqa: S310
        return int(response.status)


def post_payload(url: str, payload: dict[str, object], send: Sender | None = None) -> bool:
    """Deliver any JSON payload to a webhook; report acceptance (never raises).

    The transport behind every push sink. Alerts use it via :func:`post_webhook`;
    the scheduled ``audit --digest`` posts a digest payload through it directly.

    Args:
        url: The webhook endpoint (Slack incoming-webhook compatible).
        payload: The JSON body. A top-level ``text`` key is what Slack renders.
        send: Sink override; ``None`` uses a bounded stdlib POST. Tests inject
            a fake so no network call is made.

    Returns:
        ``True`` on a 2xx response; ``False`` on any failure.
    """
    sender = send or _urllib_send
    body = json.dumps(payload).encode("utf-8")
    try:
        status = sender(url, body)
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return 200 <= status < 300


def post_webhook(url: str, alerts: list[Alert], send: Sender | None = None) -> bool:
    """Deliver alerts to a webhook; return whether it was accepted (never raises)."""
    return post_payload(url, alerts_payload(alerts), send)
