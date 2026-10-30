"""GitHub adapter: pure gh-output parsers, cache mapping, offline degradation."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_project

from projects_orchestrator.adapters.github import (
    GithubStatus,
    as_check_results,
    collect_github,
    parse_ci_conclusion,
    parse_pr_count,
)
from projects_orchestrator.descriptor import load_descriptor


def test_parse_ci_conclusion_success_is_pass() -> None:
    payload = json.dumps([{"status": "completed", "conclusion": "success"}])
    assert parse_ci_conclusion(payload) == "pass"


def test_parse_ci_conclusion_failure_is_fail() -> None:
    payload = json.dumps([{"status": "completed", "conclusion": "failure"}])
    assert parse_ci_conclusion(payload) == "fail"


def test_parse_ci_conclusion_in_progress_is_running() -> None:
    payload = json.dumps([{"status": "in_progress", "conclusion": None}])
    assert parse_ci_conclusion(payload) == "running"


def test_parse_ci_conclusion_empty_is_unknown() -> None:
    assert parse_ci_conclusion("[]") == "unknown"


def test_parse_ci_conclusion_garbage_is_unknown() -> None:
    assert parse_ci_conclusion("not json") == "unknown"


def test_parse_pr_count_counts_entries() -> None:
    assert parse_pr_count(json.dumps([{"number": 1}, {"number": 2}])) == 2


def test_parse_pr_count_empty_is_zero() -> None:
    assert parse_pr_count("[]") == 0


def test_parse_pr_count_garbage_is_none() -> None:
    assert parse_pr_count("not json") is None


def test_pr_command_overrides_gh_default_page_size() -> None:
    # Without --limit, gh caps the list at 30, silently undercounting backlogs.
    from projects_orchestrator.adapters.github import _PR_COMMAND

    assert "--limit" in _PR_COMMAND


def test_as_check_results_maps_known_pr_count() -> None:
    results = as_check_results(GithubStatus("alpha", ci="pass", open_prs=3), "2026-07-03T00:00:00")
    prs = next(r for r in results if r.task == "prs")
    assert prs.detail == "3"


def test_as_check_results_marks_unknown_pr_count() -> None:
    results = as_check_results(
        GithubStatus("alpha", ci="unknown", open_prs=None), "2026-07-03T00:00:00"
    )
    prs = next(r for r in results if r.task == "prs")
    assert prs.status == "unknown"


def test_collect_github_degrades_to_unknown_offline(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    status = collect_github(load_descriptor(fleet_dir / "alpha"), timeout=10.0)
    assert status.ci == "unknown"


def test_collect_github_pr_count_none_offline(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    status = collect_github(load_descriptor(fleet_dir / "alpha"), timeout=10.0)
    assert status.open_prs is None
