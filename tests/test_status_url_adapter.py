"""The generic CI adapter: a declared status URL, normalised — and never raising."""

from __future__ import annotations

import json
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import make_project

from projects_orchestrator.adapters.status_url import (
    normalise_status,
    probe_status_url,
    status_check_results,
)
from projects_orchestrator.descriptor import CiConfig, load_descriptor


def _descriptor(fleet_dir: Path, status_url: str = "http://ci.example/api", status_field: str = ""):
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    assert descriptor is not None
    return replace(descriptor, ci=CiConfig(status_url=status_url, status_field=status_field))


def _fetching(payload: object):
    return lambda _url: json.dumps(payload)


# --- Normalisation: every CI system spells the same outcome differently ---


@pytest.mark.parametrize("raw", ["SUCCESS", "success", "passed", "ok", "green", "stable"])
def test_pass_vocabularies_normalise_to_pass(raw: str) -> None:
    assert normalise_status(raw) == "pass"


@pytest.mark.parametrize("raw", ["FAILURE", "failed", "error", "red", "unstable", "cancelled"])
def test_fail_vocabularies_normalise_to_fail(raw: str) -> None:
    assert normalise_status(raw) == "fail"


@pytest.mark.parametrize("raw", ["running", "in_progress", "pending", "building", "queued"])
def test_running_vocabularies_normalise_to_running(raw: str) -> None:
    assert normalise_status(raw) == "running"


def test_null_result_is_running_not_unknown() -> None:
    # Jenkins reports `result: null` while a build is in flight. Calling that
    # `unknown` would hide a live build behind a "can't tell".
    assert normalise_status(None) == "running"


def test_unrecognised_value_is_unknown_not_a_guess() -> None:
    assert normalise_status("banana") == "unknown"


def test_non_string_value_is_unknown() -> None:
    assert normalise_status(True) == "unknown"


# --- Probing: real endpoint shapes, and every way they can go wrong ---


def test_probes_a_jenkins_shaped_payload(fleet_dir: Path) -> None:
    descriptor = _descriptor(fleet_dir)
    assert probe_status_url(descriptor, fetch=_fetching({"result": "SUCCESS"})) == "pass"


def test_probes_a_buildkite_shaped_payload(fleet_dir: Path) -> None:
    descriptor = _descriptor(fleet_dir)
    assert probe_status_url(descriptor, fetch=_fetching({"state": "failed"})) == "fail"


def test_explicit_status_field_wins_over_auto_detection(fleet_dir: Path) -> None:
    # The endpoint nests its outcome and ALSO carries a decoy top-level `status`.
    descriptor = _descriptor(fleet_dir, status_field="data.outcome")
    payload = {"status": "success", "data": {"outcome": "failed"}}
    assert probe_status_url(descriptor, fetch=_fetching(payload)) == "fail"


def test_github_shaped_run_reports_the_conclusion_not_the_lifecycle(fleet_dir: Path) -> None:
    # A GitHub-style run object carries BOTH: `status` is where the run is in its
    # lifecycle, `conclusion` is how it turned out. Reading `status: completed`
    # as an outcome reports a FAILED run as green — the exact governance lie this
    # adapter must never tell.
    descriptor = _descriptor(fleet_dir)
    payload = {"status": "completed", "conclusion": "failure"}
    assert probe_status_url(descriptor, fetch=_fetching(payload)) == "fail"


def test_github_run_still_in_flight_is_running(fleet_dir: Path) -> None:
    # conclusion is null until the run finishes.
    descriptor = _descriptor(fleet_dir)
    payload = {"status": "in_progress", "conclusion": None}
    assert probe_status_url(descriptor, fetch=_fetching(payload)) == "running"


def test_completed_alone_is_not_an_outcome(fleet_dir: Path) -> None:
    # "completed" says the run ENDED, not that it passed. With no conclusion to
    # read, `unknown` is the honest answer.
    descriptor = _descriptor(fleet_dir)
    assert probe_status_url(descriptor, fetch=_fetching({"status": "completed"})) == "unknown"


def test_gitlab_shaped_payload_still_reads_status(fleet_dir: Path) -> None:
    # GitLab has no `conclusion` key — `status` IS the outcome there. Reordering
    # the keys must not break it.
    descriptor = _descriptor(fleet_dir)
    assert probe_status_url(descriptor, fetch=_fetching({"status": "success"})) == "pass"


def test_jenkins_build_in_flight_is_running_not_unknown(fleet_dir: Path) -> None:
    # `result: null` (key present, value null) means "still building". A payload
    # with NO status key at all means "I don't understand this shape". Collapsing
    # both to None would report every unreadable endpoint as a live build.
    descriptor = _descriptor(fleet_dir)
    assert probe_status_url(descriptor, fetch=_fetching({"result": None})) == "running"


def test_missing_status_field_is_unknown(fleet_dir: Path) -> None:
    descriptor = _descriptor(fleet_dir, status_field="nope.missing")
    assert probe_status_url(descriptor, fetch=_fetching({"result": "SUCCESS"})) == "unknown"


def test_payload_without_any_known_key_is_unknown(fleet_dir: Path) -> None:
    descriptor = _descriptor(fleet_dir)
    assert probe_status_url(descriptor, fetch=_fetching({"nothing": "useful"})) == "unknown"


def test_non_json_body_is_unknown(fleet_dir: Path) -> None:
    descriptor = _descriptor(fleet_dir)
    assert (
        probe_status_url(descriptor, fetch=lambda _url: "<html>502 Bad Gateway</html>") == "unknown"
    )


def test_unreachable_endpoint_is_unknown_and_never_raises(fleet_dir: Path) -> None:
    def boom(_url: str) -> str:
        raise urllib.error.URLError("no route to host")

    assert probe_status_url(_descriptor(fleet_dir), fetch=boom) == "unknown"


def test_timeout_is_unknown_and_never_raises(fleet_dir: Path) -> None:
    def slow(_url: str) -> str:
        raise TimeoutError

    assert probe_status_url(_descriptor(fleet_dir), fetch=slow) == "unknown"


def test_project_without_a_declared_url_is_unknown(fleet_dir: Path) -> None:
    # Not this adapter's business — the forge adapters handle it.
    descriptor = load_descriptor(make_project(fleet_dir, "beta"))
    assert descriptor is not None
    assert probe_status_url(descriptor) == "unknown"


# --- Cacheable results ---


def test_check_results_carry_the_ci_status() -> None:
    results = {r.task: r for r in status_check_results("alpha", "fail", "2026-07-13T10:00:00")}
    assert results["ci"].status == "fail"


def test_check_results_report_prs_unknown_not_a_stale_count() -> None:
    # A build endpoint knows nothing about code review. Emitting `unknown`
    # (rather than omitting `prs`) keeps a previous forge probe's count from
    # lingering in the table as if it were current.
    results = {r.task: r for r in status_check_results("alpha", "pass", "2026-07-13T10:00:00")}
    assert results["prs"].status == "unknown"
