"""Fleet upgrade planning — who is behind upstream project-init, and act on it.

``upgrade_plan`` joins each child's recorded ``project_init_version`` against
the latest upstream release into ``ok | outdated | unknown``, alongside the
local drift summary and (from the checks cache) open-PR count. It is a pure
reader; the only write path is dispatching a child's own upgrade workflow
(``adapters.project_init.trigger_upgrade``), never a direct tree edit.
"""

from __future__ import annotations

from dataclasses import dataclass

from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import ProjectDescriptor, parse_scaffold_version
from projects_orchestrator.drift import compute_drift

OK = "ok"
OUTDATED = "outdated"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class UpgradeRow:
    """One project's standing versus upstream project-init.

    Attributes:
        project: Project name.
        scaffold_version: The recorded ``project_init_version`` (display form).
        status: ``ok`` (>= upstream) | ``outdated`` (behind) | ``unknown``.
        drift: Local scaffold-drift summary (``none`` / ``n files`` / ``-``).
        open_prs: Last-known open-PR count from the cache (``?`` when unprobed).
    """

    project: str
    scaffold_version: str
    status: str
    drift: str
    open_prs: str


def plan_status(current: tuple[int, ...] | None, latest: tuple[int, ...] | None) -> str:
    """Classify a scaffold version against the latest upstream (pure).

    Args:
        current: Parsed child ``project_init_version`` (``None`` = not comparable).
        latest: Parsed latest upstream version (``None`` = unknown/offline).

    Returns:
        ``unknown`` when either side is missing, else ``ok`` when the child is
        at or ahead of upstream, ``outdated`` when behind.
    """
    if current is None or latest is None:
        return UNKNOWN
    return OK if current >= latest else OUTDATED


def _prs_cell(cached: dict[str, CheckResult] | None) -> str:
    """Render the cached open-PR count (``?`` when unknown/never probed)."""
    result = (cached or {}).get("prs")
    if result is None or result.status == UNKNOWN:
        return "?"
    return result.detail or "0"


def build_row(
    descriptor: ProjectDescriptor,
    latest: tuple[int, ...] | None,
    cached: dict[str, CheckResult] | None = None,
) -> UpgradeRow:
    """Build one upgrade-plan row for a project (never raises).

    Args:
        descriptor: The project to assess.
        latest: The latest upstream version, or ``None`` when unknown.
        cached: Last-known check results for the project, for the PR count.

    Returns:
        The composed :class:`UpgradeRow`.
    """
    current = parse_scaffold_version(descriptor.project_init_version)
    version = descriptor.project_init_version
    return UpgradeRow(
        project=descriptor.name,
        scaffold_version=version if version != "unknown" else "-",
        status=plan_status(current, latest),
        drift=compute_drift(descriptor).summary,
        open_prs=_prs_cell(cached),
    )


def upgrade_plan(
    descriptors: list[ProjectDescriptor],
    latest: tuple[int, ...] | None,
    cache: dict[str, dict[str, CheckResult]] | None = None,
) -> list[UpgradeRow]:
    """Build the whole fleet's upgrade plan (pure over its inputs).

    Args:
        descriptors: The fleet's projects.
        latest: The latest upstream version, or ``None`` when unknown.
        cache: ``{project: {task: CheckResult}}`` for PR counts.

    Returns:
        One :class:`UpgradeRow` per project, in input order.
    """
    cache = cache or {}
    return [build_row(d, latest, cache.get(d.name)) for d in descriptors]
