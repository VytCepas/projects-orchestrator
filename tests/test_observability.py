"""Observability ingestion: usage.jsonl in, normalized events out, never raise."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_project, make_project_v2

from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.observability import (
    GuardEvent,
    filter_since,
    load_events,
    observability_dir,
    parse_event,
)


def _write_log(project: Path, lines: list[str], relpath: str = ".claude/observability") -> None:
    log_dir = project / relpath
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "usage.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_observability_dir_defaults_to_convention(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    assert observability_dir(descriptor) == descriptor.path / ".claude/observability"


def test_observability_dir_prefers_v2_declared_path(fleet_dir: Path) -> None:
    project = make_project_v2(fleet_dir, "alpha", observability_path="logs/agent")
    descriptor = load_descriptor(project)
    assert observability_dir(descriptor) == descriptor.path / "logs/agent"


def test_parse_event_normalizes_fields() -> None:
    line = json.dumps({"ts": "2026-07-01T10:00:00+00:00", "hook": "prod_guard", "action": "block"})
    assert parse_event(line, "alpha").hook == "prod_guard"


def test_parse_event_tolerates_aliases() -> None:
    line = json.dumps({"timestamp": "2026-07-01T10:00:00+00:00", "decision": "ask"})
    assert parse_event(line, "alpha").action == "ask"


def test_parse_event_non_object_is_none() -> None:
    assert parse_event("[1, 2]", "alpha") is None


def test_parse_event_invalid_json_is_none() -> None:
    assert parse_event("{not json", "alpha") is None


def test_load_events_missing_log_warns(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    assert load_events(descriptor).warnings == ("no observability log",)


def test_load_events_missing_log_is_empty(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    assert load_events(descriptor).events == ()


def test_load_events_reads_all_lines(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    _write_log(project, [json.dumps({"hook": "prod_guard"}), json.dumps({"hook": "pkg_guard"})])
    assert len(load_events(load_descriptor(project)).events) == 2


def test_load_events_skips_malformed_lines(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    _write_log(project, [json.dumps({"hook": "prod_guard"}), "{broken", json.dumps({})])
    assert len(load_events(load_descriptor(project)).events) == 2


def test_load_events_counts_malformed_lines_as_warning(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    _write_log(project, ["{broken", "also broken"])
    assert load_events(load_descriptor(project)).warnings == ("2 malformed line(s) skipped",)


def test_load_events_oversized_log_warns(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    _write_log(project, ["x" * 2_000_000])
    assert load_events(load_descriptor(project)).warnings == ("usage log too large to read",)


def _events(*stamps: str) -> tuple[GuardEvent, ...]:
    return tuple(GuardEvent(project="alpha", timestamp=stamp) for stamp in stamps)


def test_filter_since_keeps_events_at_or_after_bound() -> None:
    events = _events("2026-07-01T09:00:00+00:00", "2026-07-01T11:00:00+00:00")
    assert len(filter_since(events, "2026-07-01T10:00:00+00:00")) == 1


def test_filter_since_empty_bound_keeps_everything() -> None:
    events = _events("2026-07-01T09:00:00+00:00")
    assert filter_since(events, "") == events


def test_filter_since_unparseable_bound_keeps_everything() -> None:
    events = _events("2026-07-01T09:00:00+00:00")
    assert filter_since(events, "yesterday") == events


def test_filter_since_drops_events_without_timestamp() -> None:
    events = _events("")
    assert filter_since(events, "2026-07-01T10:00:00+00:00") == ()
