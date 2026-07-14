"""Read-only GCP inventory — list what is live, and never touch it.

This adapter exists to answer one question: *what is running in the cloud that no
repo in the fleet governs?* Answering it must be **incapable of changing
anything**. So the module has exactly one command — ``gcloud asset
search-all-resources`` — and it is a pure read: no create, no delete, no deploy,
no IAM change. The write path does not exist here to be misused.

**The pessimistic contract (ADR-003) is the whole point.** An inventory scan that
cannot run — ``gcloud`` absent, unauthenticated, timed out, or returning garbage —
must NOT return "no resources". "I found nothing" and "I could not look" are
different facts, and conflating them is how an orphan-hunt reports a clean estate
while a forgotten service quietly bills. A failed scan returns ``None`` (unknown),
never an empty list, and every caller is built to keep that distinction.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.runner import run_command

#: The ONLY gcloud verb this module runs. `search-all-resources` is read-only — it
#: reads the Cloud Asset inventory and mutates nothing. It is a module constant, so
#: a test can assert the adapter never issues anything else (and a reviewer can see
#: the whole cloud surface of this module in one line).
#:
#: The ``--scope`` is REQUIRED, not optional: without it gcloud silently inventories
#: only the currently-configured project, and a fleet whose estate spans other
#: projects/folders/orgs would then have "no orphans" declared from a partial scan.
#: The scope names what "the estate" is — ``projects/<id>``, ``folders/<id>``, or
#: ``organizations/<id>`` — so completeness is the operator's explicit claim, not a
#: silent accident of gcloud config.
_SEARCH_TEMPLATE = "gcloud asset search-all-resources --scope={scope} --format=json"

_SCAN_TIMEOUT = 60.0


@dataclass(frozen=True)
class GcpResource:
    """One live GCP resource, as returned by the asset inventory.

    Attributes:
        name: Full resource name (the ``//service/.../resource`` path).
        asset_type: e.g. ``run.googleapis.com/Service`` or ``storage/Bucket``.
        display_name: The short name — a Cloud Run service's name, a bucket's id.
        project: The owning project id/number, when the inventory reports one.
        location: The resource's region/zone (``global`` or empty when it has
            none). Cloud Run service names are not unique across regions, so the
            location is part of a resource's identity for the orphan match.
    """

    name: str
    asset_type: str
    display_name: str
    project: str = ""
    location: str = ""


def _parse(raw: object) -> list[GcpResource] | None:
    """Build resources from decoded JSON; ``None`` if it is not a resource list.

    A payload that is not a JSON array is not "zero resources" — it is a scan that
    did not return an inventory, so it degrades to unknown like any other failure.
    """
    if not isinstance(raw, list):
        return None
    resources: list[GcpResource] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        resources.append(
            GcpResource(
                name=str(item.get("name", "")),
                asset_type=str(item.get("assetType", "")),
                display_name=str(item.get("displayName", "")),
                project=str(item.get("project", "")),
                location=str(item.get("location", "")),
            )
        )
    return resources


def search_command(scope: str) -> str:
    """The exact read-only command for a scope (scope is shell-quoted)."""
    return _SEARCH_TEMPLATE.format(scope=shlex.quote(scope))


def search_resources(scope: str, *, timeout: float = _SCAN_TIMEOUT) -> list[GcpResource] | None:
    """List live GCP resources in ``scope`` via the read-only asset inventory.

    ``scope`` bounds what "the estate" is — ``projects/<id>``, ``folders/<id>``, or
    ``organizations/<id>`` — and is required: an unscoped scan silently covers only
    the configured project, which a multi-project fleet must never mistake for a
    complete inventory.

    Returns the resources, or ``None`` when the scan could not run or its output
    could not be trusted (``gcloud`` missing, unauthenticated, timed out, or
    non-JSON). ``None`` is *unknown*, distinct from an empty list — a caller must
    never read a failed scan as "no resources", or an unauthenticated run would
    report every service accounted for. Never raises.
    """
    result = run_command(search_command(scope), cwd=Path.cwd(), timeout=timeout)
    if not result.ok:
        return None
    try:
        raw = json.loads(result.stdout)
    except ValueError:
        return None
    return _parse(raw)
