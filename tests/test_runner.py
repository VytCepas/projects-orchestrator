"""Bounded subprocess runner: failures are data, timeouts kill the whole tree."""

from __future__ import annotations

import time
from pathlib import Path

from projects_orchestrator.runner import run_command


def test_run_command_success_captures_stdout(tmp_path: Path) -> None:
    assert run_command("echo hi", tmp_path).stdout.strip() == "hi"


def test_run_command_nonzero_is_not_ok(tmp_path: Path) -> None:
    assert not run_command("exit 3", tmp_path).ok


def test_run_command_missing_binary_reports_error(tmp_path: Path) -> None:
    # A bad cwd surfaces as an OS-level error rather than an exception.
    result = run_command("true", tmp_path / "does-not-exist")
    assert result.error is not None


def test_run_command_timeout_sets_flag(tmp_path: Path) -> None:
    assert run_command("sleep 5", tmp_path, timeout=0.2).timed_out


def test_run_command_timeout_kills_grandchildren(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    # The shell backgrounds a sleep-then-touch grandchild and waits on it. A
    # timeout that only killed the shell would let the grandchild survive and
    # create the marker; killing the process group prevents it.
    result = run_command(f"(sleep 3; touch {marker}) & wait", tmp_path, timeout=0.5)
    assert result.timed_out
    time.sleep(3.5)
    assert not marker.exists()
