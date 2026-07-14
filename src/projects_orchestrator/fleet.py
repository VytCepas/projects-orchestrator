"""Aggregate everything the engine knows into one fleet view.

A :class:`ProjectSnapshot` joins descriptor + git status + last-known check
results + memory summary for one project; ``fleet_rows`` turns snapshots
into plain dict rows so every surface (table, JSON, TUI) renders the same
truth. Row building is pure and unit-testable.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.cache import load_results
from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import ProjectDescriptor, parse_scaffold_version
from projects_orchestrator.drift import DriftReport, compute_drift, hook_health
from projects_orchestrator.history import load_history, primary_trend
from projects_orchestrator.memory import ProjectMemory, load_project_memory
from projects_orchestrator.pool import map_ordered
from projects_orchestrator.registry import Fleet
from projects_orchestrator.status import ProjectStatus, collect_status
from projects_orchestrator.supervisor import RunState, running_state

COLUMNS = (
    "Project",
    "Health",
    "Branch",
    "Sync",
    "Scaffold",
    "Latest",
    "Contract",
    "Drift",
    "Hooks",
    "Lint",
    "Tests",
    "CI",
    "PRs",
    "Cloud",
    "Runnable",
    "Running",
    "Memory",
    "Checked",
    "Trend",
)


@dataclass(frozen=True)
class ProjectSnapshot:
    """Everything known about one project at a point in time.

    Attributes:
        descriptor: Static self-description (contract v1).
        status: Git health.
        checks: Last-known check results by task (from the cache or a run).
        memory: The project's memory summary.
        drift: Divergence from the recorded scaffold manifest.
        hooks: Git-hook installation health (``ok``/``partial``/``missing``/``-``).
        run_state: Live supervised-process state, or ``None`` when not running.
        trend: Primary-gate history sparkline (``++x+``); empty when no history.
    """

    descriptor: ProjectDescriptor
    status: ProjectStatus
    checks: dict[str, CheckResult]
    memory: ProjectMemory
    drift: DriftReport
    hooks: str
    run_state: RunState | None = None
    trend: str = ""


def collect_snapshot(
    descriptor: ProjectDescriptor,
    cached: dict[str, CheckResult] | None = None,
    trend: str = "",
) -> ProjectSnapshot:
    """Join all knowledge for one project; never raises.

    Args:
        descriptor: The project to snapshot.
        cached: Last-known check results for the project, if any.
        trend: Pre-computed primary-gate history sparkline (the history log is
            read once per fleet render, not once per project — see
            :func:`fleet_snapshots`).

    Returns:
        The joined snapshot.
    """
    return ProjectSnapshot(
        descriptor=descriptor,
        status=collect_status(descriptor),
        checks=dict(cached or {}),
        memory=load_project_memory(descriptor),
        drift=compute_drift(descriptor),
        hooks=hook_health(descriptor),
        run_state=running_state(descriptor),
        trend=trend,
    )


def fleet_snapshots(fleet: Fleet, cache_file: Path | None = None) -> list[ProjectSnapshot]:
    """Snapshot every project in the fleet, joining the checks cache.

    Snapshots are collected on a bounded thread pool (the work is git/
    filesystem-bound), so fleet wall-clock tracks the slowest project.

    Args:
        fleet: The discovered fleet.
        cache_file: Checks-cache override (None = default location).

    Returns:
        One snapshot per project, in fleet (name) order.
    """
    cache = load_results(cache_file)
    history = load_history()  # read once per fleet render, sliced per project below
    return map_ordered(
        lambda d: collect_snapshot(d, cache.get(d.name), primary_trend(history, d.name)),
        fleet.descriptors,
    )


def humanize_age(iso_timestamp: str, now: _dt.datetime | None = None) -> str:
    """Render an ISO timestamp as a short age like ``5m`` or ``2d``.

    Args:
        iso_timestamp: ISO-8601 timestamp (empty means never).
        now: Clock override for tests.

    Returns:
        ``never`` for empty/unparseable input, else a compact age.
    """
    if not iso_timestamp:
        return "never"
    try:
        then = _dt.datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return "never"
    if then.tzinfo is None:
        then = then.replace(tzinfo=_dt.UTC)
    now = now or _dt.datetime.now(tz=_dt.UTC)
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _sync_cell(status: ProjectStatus) -> str:
    """Render ahead/behind as ``↑n↓m`` (``-`` when unknown/no upstream)."""
    if status.ahead is None and status.behind is None:
        return "-"
    parts = []
    if status.ahead:
        parts.append(f"↑{status.ahead}")
    if status.behind:
        parts.append(f"↓{status.behind}")
    return "".join(parts) or "="


def newest_scaffold_version(snapshots: list[ProjectSnapshot]) -> tuple[int, ...] | None:
    """Return the newest comparable scaffold version across the fleet.

    Args:
        snapshots: The fleet's snapshots.

    Returns:
        The maximum parseable ``project_init_version``, or ``None`` when no
        project declares a comparable version.
    """
    versions = [
        version
        for version in (
            parse_scaffold_version(s.descriptor.project_init_version) for s in snapshots
        )
        if version is not None
    ]
    return max(versions) if versions else None


def _latest_cell(snapshot: ProjectSnapshot, newest: tuple[int, ...] | None) -> str:
    """Render scaffold freshness vs the fleet's newest (``-`` when unknown)."""
    version = parse_scaffold_version(snapshot.descriptor.project_init_version)
    if version is None or newest is None:
        return "-"
    return "=" if version >= newest else "behind"


def _contract_cell(snapshot: ProjectSnapshot) -> str:
    """Render the descriptor-contract version (``none`` when unversioned)."""
    version = snapshot.descriptor.contract_version
    return f"v{version}" if version > 0 else "none"


def _ci_cell(snapshot: ProjectSnapshot) -> str:
    """Render the last-known CI conclusion (``?`` when never probed)."""
    result = snapshot.checks.get("ci")
    return result.status if result is not None else "?"


def _prs_cell(snapshot: ProjectSnapshot) -> str:
    """Render the last-known open-PR count (``?`` when unknown/never probed)."""
    result = snapshot.checks.get("prs")
    if result is None or result.status == "unknown":
        return "?"
    return result.detail or "0"


def _running_cell(snapshot: ProjectSnapshot) -> str:
    """Render the supervised-process state (``up <age>`` or ``-``)."""
    state = snapshot.run_state
    if state is None:
        return "-"
    return f"up {humanize_age(state.started_at)}"


def _cloud_cell(snapshot: ProjectSnapshot) -> str:
    """Render the last-known cloud state (``?`` when never probed)."""
    result = snapshot.checks.get("cloud")
    return result.status if result is not None else "?"


def _check_cell(snapshot: ProjectSnapshot, task: str) -> str:
    """Render one task's last-known result (``?`` when never checked)."""
    result = snapshot.checks.get(task)
    if result is None:
        return "?"
    return result.status


def _checked_cell(snapshot: ProjectSnapshot) -> str:
    """Render the freshness of the oldest known check result."""
    stamps = [r.checked_at for r in snapshot.checks.values() if r.checked_at]
    if not stamps:
        return "never"
    return humanize_age(min(stamps))


def snapshot_row(
    snapshot: ProjectSnapshot, newest: tuple[int, ...] | None = None
) -> dict[str, str]:
    """Build one table row from a snapshot (pure).

    Args:
        snapshot: The project snapshot to render.
        newest: The fleet's newest scaffold version, for the ``Latest`` cell;
            ``None`` renders it as unknown (``-``).

    Returns:
        Column → cell text, keyed by :data:`COLUMNS`.
    """
    memory_files = len(snapshot.memory.files)
    version = snapshot.descriptor.project_init_version
    return {
        "Project": snapshot.descriptor.name,
        "Health": snapshot.status.health,
        "Branch": snapshot.status.branch or "-",
        "Sync": _sync_cell(snapshot.status),
        "Scaffold": version if version != "unknown" else "-",
        "Latest": _latest_cell(snapshot, newest),
        "Contract": _contract_cell(snapshot),
        "Drift": snapshot.drift.summary,
        "Hooks": snapshot.hooks,
        "Lint": _check_cell(snapshot, "lint"),
        "Tests": _check_cell(snapshot, "test"),
        "CI": _ci_cell(snapshot),
        "PRs": _prs_cell(snapshot),
        "Cloud": _cloud_cell(snapshot),
        "Runnable": "yes" if snapshot.descriptor.has_task("run") else "-",
        "Running": _running_cell(snapshot),
        "Memory": f"{memory_files} fact{'s' if memory_files != 1 else ''}",
        "Checked": _checked_cell(snapshot),
        "Trend": snapshot.trend or "-",
    }


def fleet_rows(
    snapshots: list[ProjectSnapshot],
    reference: list[ProjectSnapshot] | None = None,
) -> list[dict[str, str]]:
    """Build all table rows (pure); resolves scaffold freshness fleet-wide.

    ``reference`` is the set the newest scaffold version is measured against —
    the WHOLE fleet, even when ``snapshots`` is a filtered subset. Without it, a
    filtered view (``status --where name=alpha``) would compute "newest" over
    only the rows it is about to show, so a selected project that is behind the
    rest of the fleet renders as current — hiding the very drift an operator
    filters down to inspect. Defaults to ``snapshots`` when the rows already are
    the whole fleet.
    """
    newest = newest_scaffold_version(reference if reference is not None else snapshots)
    return [snapshot_row(s, newest) for s in snapshots]


_GOOD_CELLS = frozenset({"pass", "clean", "ok", "none", "yes"})
_BAD_CELLS = frozenset({"fail", "missing", "unhealthy"})
_WARN_CELLS = frozenset({"dirty", "diverged", "behind", "partial", "outdated"})

GOOD = "good"
BAD = "bad"
WARN = "warn"
NEUTRAL = ""


def cell_status(value: str) -> str:
    """Classify a cell's text as ``good``/``bad``/``warn``/neutral (pure).

    The single source of truth for status colouring across every surface: the
    text table, the HTML snapshot, the TUI, and the live web dashboard all map
    this presentation-free status to their own styling (CSS class, terminal
    colour, …), so the vocabulary can never drift between them.
    """
    if value in _GOOD_CELLS or value.startswith("up "):
        return GOOD
    if value in _BAD_CELLS:
        return BAD
    if value in _WARN_CELLS:
        return WARN
    return NEUTRAL


def render_table(rows: list[dict[str, str]]) -> str:
    """Render rows as a plain aligned text table (no dependencies).

    Args:
        rows: Output of :func:`fleet_rows`.

    Returns:
        The table as a single string; a friendly line when empty.
    """
    if not rows:
        return "no projects discovered"
    widths = {c: max(len(c), *(len(r[c]) for r in rows)) for c in COLUMNS}
    header = "  ".join(c.ljust(widths[c]) for c in COLUMNS)
    divider = "  ".join("-" * widths[c] for c in COLUMNS)
    body = ("  ".join(row[c].ljust(widths[c]) for c in COLUMNS) for row in rows)
    return "\n".join((header, divider, *body))
