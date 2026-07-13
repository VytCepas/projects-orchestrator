"""Is the vendored project-init contract still the contract upstream ships?

The contract tests (``tests/test_contract.py``) are a tripwire against a
*producer* change — but only against the producer copy **vendored here**: the
golden fixture and `descriptor.schema.json` under ``tests/fixtures/project_init/``.
If project-init changes the contract and nobody re-vendors, those tests keep
passing on a stale copy and the drift ships silently. That is the exact failure
the tripwire was built to prevent, one level up.

This closes the loop: compare the vendored copies against what project-init
actually ships today, and say so out loud. The comparison itself is pure and
offline (:func:`compare`); fetching is injected, so the unit tests never touch
the network and CI's scheduled job is the only thing that does.

Never raises: an unreachable upstream is ``unknown``, not a failure — a flaky
network must not look like a contract change (epic #68 / #106).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Where project-init publishes the machine-readable contract it ships
# (VytCepas/project-init#603, packaged via #786).
UPSTREAM_SCHEMA_URL = (
    "https://raw.githubusercontent.com/VytCepas/project-init/main/schemas/descriptor.schema.json"
)

FRESH = "fresh"
STALE = "stale"
UNKNOWN = "unknown"

_TIMEOUT = 20.0

# A fetcher: given a URL, return the body. Injectable so tests stay offline.
Fetcher = Callable[[str], str]


@dataclass(frozen=True)
class Drift:
    """One divergence between the vendored contract and what upstream ships.

    Attributes:
        surface: What drifted (``schema`` | ``fixture-version``).
        detail: Human-readable description of the difference.
    """

    surface: str
    detail: str


@dataclass(frozen=True)
class FreshnessReport:
    """Whether the vendored contract still matches upstream.

    Attributes:
        status: ``fresh`` (no drift) | ``stale`` (re-vendor needed) |
            ``unknown`` (upstream unreachable — NOT a failure).
        drifts: Every divergence found; empty when fresh or unknown.
    """

    status: str = UNKNOWN
    drifts: tuple[Drift, ...] = ()


def _urllib_fetch(url: str) -> str:
    """Default fetcher: GET ``url`` and return the body as text."""
    request = urllib.request.Request(url, headers={"accept": "application/json"})  # noqa: S310 — constant https URL
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310
        return str(response.read().decode("utf-8", errors="replace"))


# Keys that carry no contract: prose and provenance. Everything else — `type`,
# `enum`, `items`, `required`, `pattern` — is load-bearing, because a reader that
# expects a list and gets a string breaks just as hard as one whose field vanished.
_COSMETIC = frozenset({"description", "title", "$id", "$schema", "$comment", "examples"})


def _contract(node: Any) -> Any:
    """Strip prose/provenance from a schema, keeping everything that constrains data (pure).

    Comparing raw schemas would fire on a reworded description; comparing only
    field *names* would miss a retype — ``hooks.expected`` going from a list of
    strings to something else keeps its name and silently breaks every reader.
    So: drop the cosmetic keys, keep the rest.
    """
    if isinstance(node, dict):
        return {k: _contract(v) for k, v in sorted(node.items()) if k not in _COSMETIC}
    if isinstance(node, list):
        return [_contract(item) for item in node]
    return node


def _blocks(schema: Any) -> dict[str, Any]:
    """The top-level blocks of a descriptor schema, contract-normalised (pure)."""
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {str(block): _contract(spec) for block, spec in properties.items()}


def _fields(block_spec: Any) -> dict[str, Any]:
    """A block's fields, contract-normalised (pure)."""
    fields = block_spec.get("properties") if isinstance(block_spec, dict) else None
    return {str(f): spec for f, spec in fields.items()} if isinstance(fields, dict) else {}


def _retyped(block: str, ours: dict[str, Any], theirs: dict[str, Any]) -> list[Drift]:
    """Fields that kept their name but changed shape — the silent contract break (pure)."""
    return [
        Drift(
            "schema",
            f"upstream RETYPED {block}.{field}: the reader still expects the old shape",
        )
        for field in sorted(set(ours) & set(theirs))
        if ours[field] != theirs[field]
    ]


def compare_schema(vendored: Any, upstream: Any) -> list[Drift]:
    """Diff two descriptor schemas by contract (pure).

    Catches three distinct breakages: a block/field upstream **dropped** (the
    reader reads something nobody emits), one it **added** (a surface the reader
    is blind to), and one it **retyped** (same name, different shape — the
    nastiest, because every name-based check calls it fresh).
    """
    ours, theirs = _blocks(vendored), _blocks(upstream)
    if not theirs:
        return []
    drifts: list[Drift] = [
        Drift("schema", f"upstream added block '{block}' — the reader never sees it")
        for block in sorted(set(theirs) - set(ours))
    ]
    drifts += [
        Drift("schema", f"upstream dropped block '{block}' — the reader still reads it")
        for block in sorted(set(ours) - set(theirs))
    ]
    for block in sorted(set(ours) & set(theirs)):
        our_fields, their_fields = _fields(ours[block]), _fields(theirs[block])
        added = sorted(set(their_fields) - set(our_fields))
        removed = sorted(set(our_fields) - set(their_fields))
        if added:
            drifts.append(Drift("schema", f"upstream added {block}.{{{', '.join(added)}}}"))
        if removed:
            drifts.append(
                Drift(
                    "schema", f"upstream dropped {block}.{{{', '.join(removed)}}} — still read here"
                )
            )
        drifts += _retyped(block, our_fields, their_fields)
        # A block whose own shape changed (its `type`, its `required` list) while
        # its fields stayed put — e.g. deploy going string-or-object → object-only.
        if not added and not removed and _block_shape(ours[block]) != _block_shape(theirs[block]):
            drifts.append(Drift("schema", f"upstream changed the shape of block '{block}'"))
    return drifts


def _block_shape(block_spec: Any) -> Any:
    """A block's own constraints, ignoring its fields (pure)."""
    if not isinstance(block_spec, dict):
        return block_spec
    return {k: v for k, v in block_spec.items() if k != "properties"}


def _version(text: str) -> tuple[int, ...] | None:
    """Parse a dotted version into a comparable tuple; ``None`` when it isn't one."""
    parts = text.strip().lstrip("v").split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def compare_fixture_version(pinned: str, upstream: str) -> list[Drift]:
    """Flag a golden fixture generated by an OLDER project-init than ships today (pure).

    Strictly ordered, not merely unequal. A fixture *ahead* of the newest release
    is the normal state right after re-vendoring from an unreleased ``main``, and
    calling that "stale" would fire this job on every such window — the fastest
    way to teach everyone to ignore it. Only a fixture that has fallen *behind*
    upstream means the tripwire is guarding a contract nobody ships any more.
    """
    ours, theirs = _version(pinned), _version(upstream)
    if ours is None or theirs is None or ours >= theirs:
        return []
    return [
        Drift(
            "fixture-version",
            f"golden fixture was generated with project-init {pinned}; upstream ships {upstream}",
        )
    ]


def compare(
    vendored_schema: Any, upstream_schema: Any, pinned_version: str, upstream_version: str
) -> FreshnessReport:
    """Build the full freshness report (pure).

    Three states, and the distinction between the last two is the whole point:

    - ``stale``  — something we *did* fetch proves the vendored copy diverged.
    - ``fresh``  — both halves were fetched, and both match.
    - ``unknown`` — at least one half never arrived. NOT ``fresh``: a report that
      says "the vendored contract matches upstream" when the schema fetch failed
      is a lie, and it would mask a real drift until someone happened to look.
      Nor ``stale``: a flaky network is not a contract change, and a job that
      cried wolf on every blip gets muted within a week.

    Drift found from a source that *did* arrive still wins — a half-outage that
    already proves staleness should say so, not hide behind ``unknown``.
    """
    drifts = tuple(
        compare_schema(vendored_schema, upstream_schema)
        + compare_fixture_version(pinned_version, upstream_version)
    )
    if drifts:
        return FreshnessReport(status=STALE, drifts=drifts)
    if upstream_schema is None or not upstream_version:
        return FreshnessReport(status=UNKNOWN)
    return FreshnessReport(status=FRESH)


def render(report: FreshnessReport) -> str:
    """Render a freshness report as text (pure)."""
    if report.status == UNKNOWN:
        return "contract freshness: unknown — could not reach upstream project-init"
    if report.status == FRESH:
        return "contract freshness: fresh — the vendored contract matches upstream"
    lines = [f"contract freshness: STALE — {len(report.drifts)} divergence(s) from upstream"]
    lines.extend(f"  [{d.surface}] {d.detail}" for d in report.drifts)
    lines.append("")
    lines.append("Re-vendor: see tests/fixtures/project_init/README.md")
    return "\n".join(lines)


def load_vendored_schema(path: Path) -> Any:
    """Read the vendored schema; ``None`` when unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def fetch_upstream_schema(fetch: Fetcher | None = None) -> Any:
    """Fetch project-init's shipped schema; ``None`` on any problem (never raises)."""
    fetcher = fetch or _urllib_fetch
    try:
        return json.loads(fetcher(UPSTREAM_SCHEMA_URL))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
