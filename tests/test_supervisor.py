"""Supervisor: real detached processes started, watched, and stopped."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from conftest import make_project

from projects_orchestrator.__main__ import main
from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.fleet import fleet_rows, fleet_snapshots
from projects_orchestrator.registry import FleetConfig, discover
from projects_orchestrator.supervisor import logs, running_state, start, stop


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _runnable(fleet_dir: Path, command: str = "sleep 30"):
    return load_descriptor(make_project(fleet_dir, "alpha", tooling={"run": command}))


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_start_reports_pid(fleet_dir: Path) -> None:
    descriptor = _runnable(fleet_dir)
    try:
        assert "started (pid " in start(descriptor)
    finally:
        stop(descriptor)


def test_start_records_live_state(fleet_dir: Path) -> None:
    descriptor = _runnable(fleet_dir)
    start(descriptor)
    try:
        assert running_state(descriptor) is not None
    finally:
        stop(descriptor)


def test_start_twice_reports_already_running(fleet_dir: Path) -> None:
    descriptor = _runnable(fleet_dir)
    start(descriptor)
    try:
        assert "already running" in start(descriptor)
    finally:
        stop(descriptor)


def test_start_without_run_command_is_friendly(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha", tooling={"lint": "true"}))
    assert "no run_command declared" in start(descriptor)


def test_stop_kills_the_process(fleet_dir: Path) -> None:
    descriptor = _runnable(fleet_dir)
    state = None
    start(descriptor)
    state = running_state(descriptor)
    stop(descriptor)
    assert _wait_dead(state.pid)


def test_stop_clears_state(fleet_dir: Path) -> None:
    descriptor = _runnable(fleet_dir)
    start(descriptor)
    stop(descriptor)
    assert running_state(descriptor) is None


def test_stop_when_not_running_is_friendly(fleet_dir: Path) -> None:
    descriptor = _runnable(fleet_dir)
    assert stop(descriptor) == "alpha: not running"


def test_stale_pid_reads_as_not_running(fleet_dir: Path) -> None:
    descriptor = _runnable(fleet_dir, command="sleep 0.05")
    start(descriptor)
    subprocess.run(["sleep", "0.3"], check=True)
    assert running_state(descriptor) is None


def test_start_records_process_start_ticks(fleet_dir: Path) -> None:
    descriptor = _runnable(fleet_dir)
    start(descriptor)
    try:
        state = running_state(descriptor)
        assert state is not None
        assert state.start_ticks is not None  # recorded on Linux /proc
    finally:
        stop(descriptor)


def test_running_state_detects_pid_reuse(fleet_dir: Path) -> None:
    import json
    import signal

    from projects_orchestrator.supervisor import _state_file

    descriptor = _runnable(fleet_dir, command="sleep 30")
    start(descriptor)
    state = running_state(descriptor)
    assert state is not None and state.start_ticks is not None
    real_pid = state.pid
    try:
        # Simulate the pid having been recycled to a different process: the pid
        # is still live, but its recorded start time no longer matches.
        state_file = _state_file("alpha")
        data = json.loads(state_file.read_text(encoding="utf-8"))
        data["start_ticks"] = data["start_ticks"] + 1
        state_file.write_text(json.dumps(data), encoding="utf-8")
        assert running_state(descriptor) is None
    finally:
        os.kill(real_pid, signal.SIGKILL)


def test_logs_capture_run_output(fleet_dir: Path) -> None:
    descriptor = _runnable(fleet_dir, command="echo hello-from-run; sleep 20")
    start(descriptor)
    try:
        time.sleep(0.3)
        assert any("hello-from-run" in line for line in logs(descriptor))
    finally:
        stop(descriptor)


def test_logs_without_start_is_friendly(fleet_dir: Path) -> None:
    descriptor = _runnable(fleet_dir)
    assert logs(descriptor) == ["alpha: no run log (never started?)"]


def test_running_column_defaults_to_dash(fleet_dir: Path, tmp_path: Path) -> None:
    make_project(fleet_dir, "alpha")
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    rows = fleet_rows(fleet_snapshots(fleet, tmp_path / "checks.json"))
    assert rows[0]["Running"] == "-"


def test_running_column_shows_uptime(fleet_dir: Path, tmp_path: Path) -> None:
    descriptor = _runnable(fleet_dir)
    start(descriptor)
    try:
        fleet = discover(FleetConfig(roots=(fleet_dir,)))
        rows = fleet_rows(fleet_snapshots(fleet, tmp_path / "checks.json"))
        assert rows[0]["Running"].startswith("up ")
    finally:
        stop(descriptor)


def test_cli_start_exits_zero(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"run": "sleep 30"})
    try:
        assert main(["start", "alpha", "--root", str(fleet_dir)]) == 0
    finally:
        main(["stop", "alpha", "--root", str(fleet_dir)])


def test_cli_stop_reports_stopped(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha", tooling={"run": "sleep 30"})
    main(["start", "alpha", "--root", str(fleet_dir)])
    main(["stop", "alpha", "--root", str(fleet_dir)])
    assert "stopped" in capsys.readouterr().out


def test_cli_start_without_run_command_exits_1(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    assert main(["start", "alpha", "--root", str(fleet_dir)]) == 1


def test_cli_logs_tails_output(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha", tooling={"run": "echo cli-log-line; sleep 20"})
    main(["start", "alpha", "--root", str(fleet_dir)])
    try:
        time.sleep(0.3)
        capsys.readouterr()
        main(["logs", "alpha", "--root", str(fleet_dir), "-n", "5"])
        assert "cli-log-line" in capsys.readouterr().out
    finally:
        main(["stop", "alpha", "--root", str(fleet_dir)])


def test_cli_unknown_project_exits_2(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"run": "sleep 1"})
    assert main(["start", "nope", "--root", str(fleet_dir)]) == 2
