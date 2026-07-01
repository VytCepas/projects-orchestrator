"""Tests for running tasks inside a project's directory."""

from __future__ import annotations

from pathlib import Path

from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.runner import run_in_project


def _descriptor(root: Path):
    return load_descriptor(root / ".claude" / "config.yaml")


def test_run_returns_zero_exit_for_true(make_project):
    result = run_in_project(_descriptor(make_project("alpha")), ["true"])
    assert result.exit_code == 0


def test_run_ok_true_for_success(make_project):
    result = run_in_project(_descriptor(make_project("alpha")), ["true"])
    assert result.ok is True


def test_run_ok_false_for_failure(make_project):
    result = run_in_project(_descriptor(make_project("alpha")), ["false"])
    assert result.ok is False


def test_run_captures_stdout(make_project):
    result = run_in_project(_descriptor(make_project("alpha")), ["echo", "hello"])
    assert "hello" in result.output


def test_run_executes_in_project_root(make_project):
    root = make_project("alpha")
    run_in_project(_descriptor(root), ["touch", "marker"])
    assert (root / "marker").is_file()


def test_run_records_project_name(make_project):
    result = run_in_project(_descriptor(make_project("alpha")), ["true"])
    assert result.project == "alpha"


def test_run_times_out(make_project):
    result = run_in_project(_descriptor(make_project("alpha")), ["sleep", "5"], timeout=0.1)
    assert result.ok is False


def test_run_reports_missing_binary_without_raising(make_project):
    result = run_in_project(_descriptor(make_project("alpha")), ["definitely-not-a-binary"])
    assert result.ok is False
