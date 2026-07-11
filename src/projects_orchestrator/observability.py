"""Read guard/usage events across the fleet — the observability layer.

Child projects' guards (``prod_guard``, ``package_guard``) self-log to
``.claude/observability/usage.jsonl``. Nothing read that signal before: what
was blocked, where, and how often is exactly the governance telemetry a
fleet controller should aggregate. This module reads that JSONL contract
across the fleet, degrading like the rest of the engine: a missing
directory is an empty result with a warning, malformed lines are skipped
and counted, and nothing here ever raises.

The log directory comes from the contract-v2 ``observability.path`` field
when the child declares it, falling back to the ``.claude/observability/``
convention for v1 children.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.descriptor import ProjectDescriptor

OBSERVABILITY_CONVENTION = Path(".claude/observability")

USAGE_FILENAME = "usage.jsonl"

_MAX_FILE_BYTES = 1_048_576


@dataclass(frozen=True)
class GuardEvent:
    """One normalized guard/usage event from one project.

    Attributes:
        project: Owning project name.
        timestamp: ISO-8601 event time (empty when the line omitted it).
        hook: Guard that fired (``prod_guard``, ``package_guard``, …).
        action: What the guard did (``ask`` | ``block`` | ``allow`` | …).
        command: The command the guard evaluated, when recorded.
        session: Agent session id the event belongs to, when recorded — lets
            the fleet group a run's events across projects (empty when absent).
    """

    project: str
    timestamp: str = ""
    hook: str = "unknown"
    action: str = "unknown"
    command: str = ""
    session: str = ""


@dataclass(frozen=True)
class ProjectEvents:
    """Everything one project's observability log recorded.

    Attributes:
        project: Project name.
        path: The usage log that was read (or would have been).
        events: Parsed events in file (chronological) order.
        warnings: Non-fatal read problems (missing log, malformed lines).
    """

    project: str
    path: Path
    events: tuple[GuardEvent, ...] = ()
    warnings: tuple[str, ...] = ()


def observability_dir(descriptor: ProjectDescriptor) -> Path:
    """Resolve a project's observability directory.

    Args:
        descriptor: The project to resolve for.

    Returns:
        The contract-v2 declared path when present, else the
        ``.claude/observability/`` convention under the project root.
    """
    if descriptor.observability_path is not None:
        return descriptor.observability_path
    # Undeclared (or v1): the log dir lives beside the descriptor — under
    # ``.agents/`` on a PI-627 scaffold, ``.claude/`` on a legacy one.
    return descriptor.path / descriptor.config_root / OBSERVABILITY_CONVENTION.name


def parse_event(line: str, project: str) -> GuardEvent | None:
    """Normalize one JSONL line into a :class:`GuardEvent` (pure).

    Args:
        line: One line of ``usage.jsonl``.
        project: Owning project name.

    Returns:
        The event, or ``None`` when the line is not a JSON object. Field
        aliases are tolerated (``ts``/``timestamp``, and
        ``action``/``decision``/``event`` — project-init's guards log the
        outcome under ``event``, which the reader previously dropped so every
        row read ``action=unknown``).
    """
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    return GuardEvent(
        project=project,
        timestamp=str(entry.get("ts") or entry.get("timestamp") or ""),
        hook=str(entry.get("hook") or "unknown"),
        action=str(entry.get("action") or entry.get("decision") or entry.get("event") or "unknown"),
        command=str(entry.get("command") or ""),
        session=str(entry.get("session") or ""),
    )


def load_events(descriptor: ProjectDescriptor) -> ProjectEvents:
    """Read one project's usage log; never raises.

    Args:
        descriptor: The project whose events to load.

    Returns:
        The project's events; a missing or oversized log yields an empty
        result with a warning, malformed lines are skipped and counted.
    """
    path = observability_dir(descriptor) / USAGE_FILENAME
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return ProjectEvents(
                project=descriptor.name, path=path, warnings=("usage log too large to read",)
            )
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ProjectEvents(
            project=descriptor.name, path=path, warnings=("no observability log",)
        )

    events: list[GuardEvent] = []
    malformed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        event = parse_event(line, descriptor.name)
        if event is None:
            malformed += 1
        else:
            events.append(event)

    warnings: list[str] = []
    if malformed:
        warnings.append(f"{malformed} malformed line(s) skipped")
    unparseable = count_unparseable_timestamps(tuple(events))
    if unparseable:
        warnings.append(f"{unparseable} event(s) with an unparseable timestamp")
    return ProjectEvents(
        project=descriptor.name, path=path, events=tuple(events), warnings=tuple(warnings)
    )


def filter_since(events: tuple[GuardEvent, ...], since: str) -> tuple[GuardEvent, ...]:
    """Keep events at or after an ISO-8601 instant (pure).

    Args:
        events: Events to filter.
        since: ISO-8601 lower bound; empty keeps everything.

    Returns:
        The filtered events; an unparseable bound keeps everything (never
        silently hides data), events without a timestamp are dropped when
        a bound is given.
    """
    cutoff = _parse_instant(since)
    if cutoff is None:
        return events
    kept: list[GuardEvent] = []
    for event in events:
        stamp = _parse_instant(event.timestamp)
        if stamp is not None and stamp >= cutoff:
            kept.append(event)
    return tuple(kept)


def _parse_instant(value: str) -> _dt.datetime | None:
    """Parse an ISO-8601 or epoch-seconds instant to aware UTC; ``None`` if not.

    Guards may log ISO-8601 (``2026-07-04T10:00:00Z``) or a raw epoch-seconds
    number; accept both. A naive ISO stamp is assumed UTC (documented in the
    contract). Anything else is unparseable.
    """
    text = value.strip()
    if not text:
        return None
    try:
        stamp = _dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            return _dt.datetime.fromtimestamp(float(text), tz=_dt.UTC)
        except (ValueError, OverflowError, OSError):
            return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=_dt.UTC)


def count_unparseable_timestamps(events: tuple[GuardEvent, ...]) -> int:
    """Count events carrying a non-empty timestamp that does not parse (pure)."""
    return sum(
        1 for event in events if event.timestamp.strip() and _parse_instant(event.timestamp) is None
    )
