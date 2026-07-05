"""project-init adapter: release-tag parsing and offline degradation."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_project

from projects_orchestrator.adapters.project_init import (
    GITHUB_UPGRADE_WORKFLOW,
    GITLAB_UPGRADE_WORKFLOW,
    has_upgrade_workflow,
    latest_upstream_version,
    parse_release_tag,
    trigger_upgrade,
    upgrade_workflow_relpath,
)
from projects_orchestrator.descriptor import load_descriptor

_GITLAB_CONFIG = (
    "project:\n  name: gl\n  project_init_contract_version: 1\n"
    "  project_init_host: gitlab.com\n"
)


def _add_workflow(project: Path, relpath: Path) -> None:
    path = project / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("name: upgrade\n", encoding="utf-8")


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


def test_trigger_upgrade_without_workflow_reports_reason(fleet_dir: Path) -> None:
    # A bare child ships no upgrade workflow — a clear reason, not a silent fail.
    make_project(fleet_dir, "alpha")
    result = trigger_upgrade(load_descriptor(fleet_dir / "alpha"), timeout=10.0)
    assert result == "no upgrade workflow"


def test_trigger_upgrade_with_github_workflow_degrades_to_failed_offline(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    _add_workflow(project, GITHUB_UPGRADE_WORKFLOW)
    result = trigger_upgrade(load_descriptor(project), timeout=10.0)
    assert result == "failed"


def test_upgrade_workflow_relpath_defaults_to_github(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    assert upgrade_workflow_relpath(descriptor) == GITHUB_UPGRADE_WORKFLOW


def test_upgrade_workflow_relpath_gitlab_for_gitlab_host(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "gl", config_text=_GITLAB_CONFIG)
    assert upgrade_workflow_relpath(load_descriptor(project)) == GITLAB_UPGRADE_WORKFLOW


def test_has_upgrade_workflow_true_when_present(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    _add_workflow(project, GITHUB_UPGRADE_WORKFLOW)
    assert has_upgrade_workflow(load_descriptor(project)) is True


def test_has_upgrade_workflow_false_when_github_file_on_gitlab_child(fleet_dir: Path) -> None:
    # A GitLab child looks for the GitLab path; a stray GitHub workflow does not count.
    project = make_project(fleet_dir, "gl", config_text=_GITLAB_CONFIG)
    _add_workflow(project, GITHUB_UPGRADE_WORKFLOW)
    assert has_upgrade_workflow(load_descriptor(project)) is False


def test_trigger_upgrade_gitlab_without_workflow_reports_reason(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "gl", config_text=_GITLAB_CONFIG)
    assert trigger_upgrade(load_descriptor(project), timeout=10.0) == "no upgrade workflow"


def test_trigger_upgrade_gitlab_with_workflow_degrades_to_failed_offline(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "gl", config_text=_GITLAB_CONFIG)
    _add_workflow(project, GITLAB_UPGRADE_WORKFLOW)
    assert trigger_upgrade(load_descriptor(project), timeout=10.0) == "failed"
