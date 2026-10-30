"""Checks engine: declared gates become pass/fail/skip data, never exceptions."""

from __future__ import annotations

from pathlib import Path

from conftest import make_project

from projects_orchestrator.checks import collect_checks, run_check
from projects_orchestrator.descriptor import load_descriptor


def _descriptor(fleet_dir: Path, tooling: dict[str, str]):
    return load_descriptor(make_project(fleet_dir, "alpha", tooling=tooling))


def test_run_check_passing_command_is_pass(fleet_dir: Path) -> None:
    descriptor = _descriptor(fleet_dir, {"lint": "true"})
    assert run_check(descriptor, "lint").status == "pass"


def test_run_check_failing_command_is_fail(fleet_dir: Path) -> None:
    descriptor = _descriptor(fleet_dir, {"lint": "false"})
    assert run_check(descriptor, "lint").status == "fail"


def test_run_check_undeclared_task_is_skip(fleet_dir: Path) -> None:
    descriptor = _descriptor(fleet_dir, {"lint": "true"})
    assert run_check(descriptor, "test").status == "skip"


def test_run_check_missing_binary_is_fail(fleet_dir: Path) -> None:
    descriptor = _descriptor(fleet_dir, {"lint": "definitely-not-a-binary-xyz"})
    assert run_check(descriptor, "lint").status == "fail"


def test_run_check_timeout_is_fail(fleet_dir: Path) -> None:
    descriptor = _descriptor(fleet_dir, {"lint": "sleep 5"})
    assert run_check(descriptor, "lint", timeout=0.2).status == "fail"


def test_run_check_timeout_detail_mentions_timeout(fleet_dir: Path) -> None:
    descriptor = _descriptor(fleet_dir, {"lint": "sleep 5"})
    assert "timed out" in run_check(descriptor, "lint", timeout=0.2).detail


def test_run_check_failure_detail_carries_output(fleet_dir: Path) -> None:
    descriptor = _descriptor(fleet_dir, {"lint": "echo broken-gate >&2; false"})
    assert "broken-gate" in run_check(descriptor, "lint").detail


def test_run_check_records_timestamp(fleet_dir: Path) -> None:
    descriptor = _descriptor(fleet_dir, {"lint": "true"})
    assert run_check(descriptor, "lint").checked_at != ""


def test_collect_checks_returns_one_result_per_task(fleet_dir: Path) -> None:
    descriptor = _descriptor(fleet_dir, {"lint": "true"})
    assert [r.task for r in collect_checks(descriptor)] == ["lint", "test"]
