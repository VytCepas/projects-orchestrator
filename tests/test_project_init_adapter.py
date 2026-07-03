"""project-init adapter: release-tag parsing and offline degradation."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_project

from projects_orchestrator.adapters.project_init import (
    latest_upstream_version,
    parse_release_tag,
    trigger_upgrade,
)
from projects_orchestrator.descriptor import load_descriptor


def test_parse_release_tag_reads_version() -> None:
    assert parse_release_tag(json.dumps({"tagName": "v0.6.0"})) == (0, 6, 0)


def test_parse_release_tag_tolerates_no_v_prefix() -> None:
    assert parse_release_tag(json.dumps({"tagName": "0.6.0"})) == (0, 6, 0)


def test_parse_release_tag_non_semver_is_none() -> None:
    assert parse_release_tag(json.dumps({"tagName": "nightly"})) is None


def test_parse_release_tag_garbage_is_none() -> None:
    assert parse_release_tag("not json") is None


def test_latest_upstream_version_degrades_offline(tmp_path: Path) -> None:
    assert latest_upstream_version(tmp_path, timeout=10.0) is None


def test_trigger_upgrade_degrades_to_failed_offline(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    result = trigger_upgrade(load_descriptor(fleet_dir / "alpha"), timeout=10.0)
    assert result == "failed"
