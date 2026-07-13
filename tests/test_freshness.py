"""Contract freshness: does the vendored project-init contract still match upstream?

The comparison is pure, so these run offline. The one network path
(:func:`fetch_upstream_schema`) is exercised only through an injected fetcher.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

from projects_orchestrator.freshness import (
    compare,
    compare_fixture_version,
    compare_schema,
    fetch_upstream_schema,
    load_vendored_schema,
    render,
)

_VENDORED = (
    Path(__file__).parent / "fixtures" / "project_init" / "schemas" / "descriptor.schema.json"
)


def _schema(**blocks: list[str]) -> dict:
    return {
        "title": "Agentic OS Memory Descriptor (v2)",
        "properties": {
            block: {"type": "object", "properties": {f: {"type": "string"} for f in fields}}
            for block, fields in blocks.items()
        },
    }


# --- Schema surface diffing -------------------------------------------------


def test_identical_schemas_show_no_drift() -> None:
    schema = _schema(deploy=["target", "health_url"])
    assert compare_schema(schema, schema) == []


def test_a_field_upstream_dropped_is_drift() -> None:
    # The dangerous one: the orchestrator still reads a field nobody emits.
    ours = _schema(deploy=["target", "health_url"])
    theirs = _schema(deploy=["target"])
    assert "health_url" in compare_schema(ours, theirs)[0].detail


def test_a_field_upstream_added_is_drift() -> None:
    # Less dangerous, but it means a surface the orchestrator could be reading.
    ours = _schema(deploy=["target"])
    theirs = _schema(deploy=["target", "health_url"])
    assert "health_url" in compare_schema(ours, theirs)[0].detail


def test_a_whole_new_block_is_drift() -> None:
    ours = _schema(deploy=["target"])
    theirs = _schema(deploy=["target"], ci=["status_url"])
    assert "ci" in compare_schema(ours, theirs)[0].detail


def test_a_dropped_block_is_drift() -> None:
    ours = _schema(deploy=["target"], ci=["status_url"])
    theirs = _schema(deploy=["target"])
    assert "ci" in compare_schema(ours, theirs)[0].detail


def test_cosmetic_upstream_edits_are_not_drift() -> None:
    # A reworded description or a bumped $id must not cry wolf — a job that
    # fires on noise gets muted, and then a real drift goes unread.
    ours = _schema(deploy=["target"])
    theirs = _schema(deploy=["target"])
    theirs["$id"] = "https://example.com/new-location.json"
    theirs["description"] = "reworded"
    theirs["properties"]["deploy"]["description"] = "also reworded"
    assert compare_schema(ours, theirs) == []


def test_an_unreachable_upstream_is_not_drift() -> None:
    # None (fetch failed) must never read as "upstream dropped everything".
    assert compare_schema(_schema(deploy=["target"]), None) == []


# --- Fixture version pinning ------------------------------------------------


def test_matching_fixture_version_is_fresh() -> None:
    assert compare_fixture_version("1.1.7", "1.1.7") == []


def test_older_fixture_version_is_drift() -> None:
    drifts = compare_fixture_version("1.0.1", "1.1.7")
    assert "1.0.1" in drifts[0].detail and "1.1.7" in drifts[0].detail


def test_a_fixture_ahead_of_the_latest_release_is_not_drift() -> None:
    # The normal state right after re-vendoring from an unreleased main. Calling
    # it "stale" would fire this job for the whole window before the tag lands —
    # the fastest way to teach everyone to ignore it.
    assert compare_fixture_version("1.1.7", "1.1.6") == []


def test_version_ordering_is_numeric_not_lexicographic() -> None:
    # "1.10.0" > "1.9.0" numerically, but NOT as strings — a string compare would
    # report a perfectly current fixture as stale.
    assert compare_fixture_version("1.10.0", "1.9.0") == []
    assert compare_fixture_version("1.9.0", "1.10.0") != []


def test_unknown_upstream_version_is_not_drift() -> None:
    assert compare_fixture_version("1.1.7", "") == []


def test_an_unparseable_version_is_not_drift() -> None:
    assert compare_fixture_version("1.1.7", "not-a-version") == []


# --- The composed report ----------------------------------------------------


def test_report_is_fresh_when_nothing_diverged() -> None:
    schema = _schema(deploy=["target"])
    assert compare(schema, schema, "1.1.7", "1.1.7").status == "fresh"


def test_report_is_stale_when_the_schema_diverged() -> None:
    report = compare(
        _schema(deploy=["target", "health_url"]), _schema(deploy=["target"]), "1.0.0", "1.0.0"
    )
    assert report.status == "stale"


def test_report_is_unknown_when_upstream_is_unreachable() -> None:
    # A flaky network is not a contract change. This is the difference between a
    # signal people act on and one they mute.
    assert compare(_schema(deploy=["target"]), None, "1.1.7", "").status == "unknown"


def test_stale_report_names_every_divergence() -> None:
    report = compare(
        _schema(deploy=["target", "health_url"]), _schema(deploy=["target"]), "1.0.0", "1.1.0"
    )
    assert len(report.drifts) == 2  # the dropped field AND the version pin


def test_render_stale_points_at_the_refresh_path() -> None:
    report = compare(_schema(deploy=["a"]), _schema(deploy=[]), "1.0.0", "1.0.0")
    assert "README" in render(report)


def test_render_unknown_does_not_claim_drift() -> None:
    assert "STALE" not in render(compare(_schema(), None, "1.0.0", ""))


# --- Fetching (injected, never touches the network) --------------------------


def test_fetch_returns_the_parsed_schema() -> None:
    schema = _schema(ci=["status_url"])
    assert fetch_upstream_schema(fetch=lambda _url: json.dumps(schema)) == schema


def test_fetch_degrades_to_none_when_upstream_is_down() -> None:
    def boom(_url: str) -> str:
        raise urllib.error.URLError("no route to host")

    assert fetch_upstream_schema(fetch=boom) is None


def test_fetch_degrades_to_none_on_a_non_json_body() -> None:
    assert fetch_upstream_schema(fetch=lambda _url: "<html>404</html>") is None


# --- The real vendored copy -------------------------------------------------


def test_the_vendored_schema_is_readable_and_declares_the_ci_surface() -> None:
    # Guards the check itself: if the vendored path moves, the freshness job
    # would silently compare None against upstream and report "fresh" forever.
    schema = load_vendored_schema(_VENDORED)
    assert "ci" in schema["properties"]


def test_a_missing_vendored_schema_is_none_not_a_crash() -> None:
    assert load_vendored_schema(Path("/nonexistent/descriptor.schema.json")) is None


# --- Retypes: same name, different shape — the silent contract break ---------
# A name-only comparison calls these "fresh". They are the exact class the
# contract tests exist to catch: a reader that expects a list and gets a string
# breaks just as hard as one whose field vanished.


def _typed(**fields: dict) -> dict:
    return {"properties": {"hooks": {"type": "object", "properties": fields}}}


def test_a_retyped_field_is_drift() -> None:
    ours = _typed(expected={"type": "array", "items": {"type": "string"}})
    theirs = _typed(expected={"type": "string"})
    drifts = compare_schema(ours, theirs)
    assert "RETYPED" in drifts[0].detail and "hooks.expected" in drifts[0].detail


def test_an_enum_change_is_drift() -> None:
    ours = _typed(target={"type": "string", "enum": ["fly", "cloud-run"]})
    theirs = _typed(target={"type": "string", "enum": ["fly", "cloud-run", "k8s"]})
    assert compare_schema(ours, theirs) != []


def test_an_items_type_change_is_drift() -> None:
    ours = _typed(expected={"type": "array", "items": {"type": "string"}})
    theirs = _typed(expected={"type": "array", "items": {"type": "object"}})
    assert compare_schema(ours, theirs) != []


def test_a_reworded_field_description_is_not_drift() -> None:
    # Still must not cry wolf on prose — that was the point of reducing at all.
    ours = _typed(expected={"type": "array", "description": "hooks the scaffold ships"})
    theirs = _typed(expected={"type": "array", "description": "the git hooks shipped"})
    assert compare_schema(ours, theirs) == []


def test_a_block_whose_own_shape_changed_is_drift() -> None:
    # e.g. `deploy` going from string-or-object to object-only. Field names are
    # untouched, so a name-only check sees nothing.
    ours = {"properties": {"deploy": {"type": ["string", "object"], "properties": {"t": {}}}}}
    theirs = {"properties": {"deploy": {"type": "object", "properties": {"t": {}}}}}
    assert "shape" in compare_schema(ours, theirs)[0].detail


# --- Partial upstream outages must not read as "fresh" -----------------------


def test_a_schema_fetch_failure_alone_is_unknown_not_fresh() -> None:
    # The version fetch succeeded and matches — but the schema was never checked.
    # Reporting "the vendored contract matches upstream" here is simply a lie,
    # and it would mask a real schema drift until someone happened to look.
    assert compare(_schema(deploy=["target"]), None, "1.1.7", "1.1.7").status == "unknown"


def test_a_version_fetch_failure_alone_is_unknown_not_fresh() -> None:
    schema = _schema(deploy=["target"])
    assert compare(schema, schema, "1.1.7", "").status == "unknown"


def test_drift_from_the_half_that_did_arrive_still_wins() -> None:
    # A half-outage that already proves staleness must say so, not hide behind
    # "unknown" — we know enough to act.
    report = compare(
        _schema(deploy=["target", "health_url"]), _schema(deploy=["target"]), "1.1.7", ""
    )
    assert report.status == "stale"
