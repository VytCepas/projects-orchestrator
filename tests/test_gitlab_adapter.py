"""GitLab adapter: glab output parsed to CI/MR cells, degrades to unknown."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_project

from projects_orchestrator.adapters.gitlab import (
    GitlabStatus,
    as_check_results,
    collect_gitlab,
    parse_mr_count,
    parse_pipeline_status,
    provider_is_gitlab,
)
from projects_orchestrator.descriptor import load_descriptor

_GITLAB_CONFIG = (
    "project:\n  name: alpha\n  project_init_contract_version: 1\n"
    "  project_init_host: gitlab.com\nlanguage: python\n"
)


def test_parse_pipeline_success_is_pass() -> None:
    assert parse_pipeline_status(json.dumps([{"status": "success"}])) == "pass"


def test_parse_pipeline_failed_is_fail() -> None:
    assert parse_pipeline_status(json.dumps([{"status": "failed"}])) == "fail"


def test_parse_pipeline_running_is_running() -> None:
    assert parse_pipeline_status(json.dumps([{"status": "running"}])) == "running"


def test_parse_pipeline_empty_is_unknown() -> None:
    assert parse_pipeline_status("[]") == "unknown"


def test_parse_pipeline_garbage_is_unknown() -> None:
    assert parse_pipeline_status("not json") == "unknown"


def test_parse_pipeline_unknown_state_is_unknown() -> None:
    assert parse_pipeline_status(json.dumps([{"status": "manual"}])) == "unknown"


def test_parse_mr_count_counts_entries() -> None:
    assert parse_mr_count(json.dumps([{"iid": 1}, {"iid": 2}])) == 2


def test_parse_mr_count_empty_is_zero() -> None:
    assert parse_mr_count("[]") == 0


def test_parse_mr_count_garbage_is_none() -> None:
    assert parse_mr_count("nope") is None


def test_provider_is_gitlab_for_gitlab_host(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha", config_text=_GITLAB_CONFIG))
    assert provider_is_gitlab(descriptor) is True


def test_provider_is_not_gitlab_for_github_host(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    assert provider_is_gitlab(descriptor) is False


def test_as_check_results_maps_mr_count_to_prs_task() -> None:
    results = as_check_results(GitlabStatus("alpha", ci="pass", open_mrs=4), "2026-07-05T00:00:00")
    prs = next(r for r in results if r.task == "prs")
    assert prs.detail == "4"


def test_as_check_results_marks_unknown_mr_count() -> None:
    results = as_check_results(GitlabStatus("alpha", ci="pass", open_mrs=None), "2026-07-05T00:00:00")
    prs = next(r for r in results if r.task == "prs")
    assert prs.status == "unknown"


def test_collect_gitlab_degrades_to_unknown_offline(fleet_dir: Path, monkeypatch) -> None:
    # No glab on PATH → both probes fail → unknown / None, never an exception.
    monkeypatch.setenv("PATH", "")
    descriptor = load_descriptor(make_project(fleet_dir, "alpha", config_text=_GITLAB_CONFIG))
    status = collect_gitlab(descriptor)
    assert status.ci == "unknown"
    assert status.open_mrs is None
