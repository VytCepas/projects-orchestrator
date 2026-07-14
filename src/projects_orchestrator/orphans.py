"""Diff the live GCP inventory against the fleet — what does no repo govern?

An *orphan* is a resource running in the cloud that no repository in the fleet
accounts for: a service someone deployed and forgot, or one whose repo was never
brought under management. It is the raw material for the project-init rollout
(#122) — an orphan gets a repo, gets project-init, and becomes fleet.

The join is by name: a resource is accounted for when its display name matches a
fleet project's deploy app (the Cloud Run service name it ships) or the project's
own name. Matching is deliberately exact and case-insensitive rather than fuzzy —
a substring rule would let a project named ``api`` silently "account for" every
resource with ``api`` in its name, hiding real orphans, which is the one failure
this report exists to prevent.

**Unknown is not empty (ADR-003).** When the scan could not run, the report says
so; it does not claim zero orphans. A blank estate and an unauthenticated scan
must never render the same.
"""

from __future__ import annotations

from dataclasses import dataclass

from projects_orchestrator.adapters.gcp import GcpResource
from projects_orchestrator.registry import Fleet


@dataclass(frozen=True)
class OrphanReport:
    """The result of an orphan scan.

    Attributes:
        orphans: Resources no fleet project accounts for (empty when the estate is
            fully governed — but only meaningful when ``scanned`` is true).
        scanned: Whether the inventory was actually read. ``False`` means the scan
            could not run, and ``orphans`` says nothing — the estate is UNKNOWN,
            not clean.
    """

    orphans: tuple[GcpResource, ...]
    scanned: bool

    @property
    def is_unknown(self) -> bool:
        """Whether the scan could not run (so the result must not read as clean)."""
        return not self.scanned


def accounted_names(fleet: Fleet) -> frozenset[str]:
    """Every name the fleet accounts for: each project's name and its deploy app.

    Lower-cased so the match is case-insensitive. Blank apps (non-service projects)
    contribute nothing — they own no cloud resource to match against.
    """
    names: set[str] = set()
    for descriptor in fleet.descriptors:
        names.add(descriptor.name.lower())
        deploy = descriptor.deploy
        if deploy is not None and deploy.app.strip():
            names.add(deploy.app.strip().lower())
    return frozenset(names)


def find_orphans(fleet: Fleet, resources: list[GcpResource] | None) -> OrphanReport:
    """Report resources no fleet project accounts for; never raises.

    ``resources`` is the output of :func:`~adapters.gcp.search_resources`: a list,
    or ``None`` when the scan could not run. ``None`` yields an UNKNOWN report
    (``scanned=False``) — never an empty-orphan report, which would falsely say the
    estate is fully governed when in truth it was never looked at.
    """
    if resources is None:
        return OrphanReport(orphans=(), scanned=False)
    accounted = accounted_names(fleet)
    orphans = tuple(r for r in resources if r.display_name.strip().lower() not in accounted)
    return OrphanReport(orphans=orphans, scanned=True)
