"""Diff the live GCP inventory against the fleet — what does no repo govern?

An *orphan* is a resource running in the cloud that no repository in the fleet
accounts for: a service someone deployed and forgot, or one whose repo was never
brought under management. It is the raw material for the project-init rollout
(#122) — an orphan gets a repo, gets project-init, and becomes fleet.

The join is by name AND region. A resource is accounted for when either its
display name matches a fleet project's own name (region-agnostic — a repo governs
resources it named, wherever they run), or its (display name, location) matches a
service project's (deploy app, deploy region). The region is part of the key
because Cloud Run service names are not unique across regions: a stray ``web-svc``
in another region must not be hidden by the fleet's legitimate ``web-svc`` in its
declared one. Matching is exact and case-insensitive rather than fuzzy — a
substring rule would let a project named ``api`` silently "account for" every
resource with ``api`` in its name, hiding real orphans.

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


#: The sentinel region for a match that ignores location — a bare project-name
#: match, which governs a resource wherever it runs.
_ANY_REGION = ""


def accounted_keys(fleet: Fleet) -> frozenset[tuple[str, str]]:
    """Every (name, region) the fleet accounts for, lower-cased.

    Each project contributes ``(name, _ANY_REGION)`` — its repo name governs any
    same-named resource regardless of region. A service project ALSO contributes
    ``(deploy app, deploy region)``, a region-qualified key: the app name only
    accounts for a resource in the declared region. Blank apps (non-service
    projects) add no app key — they own no cloud service to match against.
    """
    keys: set[tuple[str, str]] = set()
    for descriptor in fleet.descriptors:
        keys.add((descriptor.name.lower(), _ANY_REGION))
        deploy = descriptor.deploy
        if deploy is not None and deploy.app.strip():
            keys.add((deploy.app.strip().lower(), deploy.region.strip().lower()))
    return frozenset(keys)


def _is_accounted(resource: GcpResource, keys: frozenset[tuple[str, str]]) -> bool:
    """Whether ``keys`` accounts for ``resource`` — by bare name, or name+region."""
    name = resource.display_name.strip().lower()
    location = resource.location.strip().lower()
    return (name, _ANY_REGION) in keys or (name, location) in keys


def find_orphans(fleet: Fleet, resources: list[GcpResource] | None) -> OrphanReport:
    """Report resources no fleet project accounts for; never raises.

    ``resources`` is the output of :func:`~adapters.gcp.search_resources`: a list,
    or ``None`` when the scan could not run. ``None`` yields an UNKNOWN report
    (``scanned=False``) — never an empty-orphan report, which would falsely say the
    estate is fully governed when in truth it was never looked at.
    """
    if resources is None:
        return OrphanReport(orphans=(), scanned=False)
    keys = accounted_keys(fleet)
    orphans = tuple(r for r in resources if not _is_accounted(r, keys))
    return OrphanReport(orphans=orphans, scanned=True)
