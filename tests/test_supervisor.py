"""Supervisor: real detached processes started, watched, and stopped."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from conftest import CONFIG_TEMPLATE, make_project

from projects_orchestrator.__main__ import main
from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.fleet import fleet_rows, fleet_snapshots
from projects_orchestrator.registry import FleetConfig, discover
from projects_orchestrator.supervisor import liveness_check, logs, running_state, start, stop


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


def test_mem_available_bytes_parses_meminfo(tmp_path: Path) -> None:
    from projects_orchestrator.supervisor import _mem_available_bytes

    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 100 kB\nMemAvailable: 2048 kB\n", encoding="utf-8")
    assert _mem_available_bytes(meminfo) == 2048 * 1024


def test_mem_available_bytes_off_linux_is_none(tmp_path: Path) -> None:
    from projects_orchestrator.supervisor import _mem_available_bytes

    assert _mem_available_bytes(tmp_path / "absent") is None


def test_start_refuses_below_memory_floor(fleet_dir: Path, monkeypatch) -> None:
    import projects_orchestrator.supervisor as sup

    monkeypatch.setattr(sup, "_mem_available_bytes", lambda: 1)
    assert "refusing to start" in start(_runnable(fleet_dir))


def test_start_below_floor_spawns_nothing(fleet_dir: Path, monkeypatch) -> None:
    import projects_orchestrator.supervisor as sup

    monkeypatch.setattr(sup, "_mem_available_bytes", lambda: 1)
    descriptor = _runnable(fleet_dir)
    start(descriptor)
    assert running_state(descriptor) is None


def test_start_proceeds_when_memory_unknown(fleet_dir: Path, monkeypatch) -> None:
    import projects_orchestrator.supervisor as sup

    monkeypatch.setattr(sup, "_mem_available_bytes", lambda: None)
    descriptor = _runnable(fleet_dir)
    try:
        assert "(pid " in start(descriptor)
    finally:
        stop(descriptor)


def test_cli_start_refused_exits_1(fleet_dir: Path, monkeypatch) -> None:
    import projects_orchestrator.supervisor as sup

    monkeypatch.setattr(sup, "_mem_available_bytes", lambda: 1)
    make_project(fleet_dir, "alpha", tooling={"run": "sleep 30"})
    assert main(["start", "alpha", "--root", str(fleet_dir)]) == 1


def test_start_reports_failure_when_it_cannot_record_state(
    fleet_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    import projects_orchestrator.supervisor as sup

    monkeypatch.setattr(sup, "_state_file", lambda _p: tmp_path / "no-such-dir" / "alpha.json")
    assert "failed to start" in start(_runnable(fleet_dir))


def test_start_kills_a_process_whose_state_it_could_not_record(
    fleet_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    # The process is ALREADY RUNNING when the state write happens, so reporting
    # "failed to start" and walking away strands a process nothing can find:
    # liveness is looked up via the record that just failed to write, so `stop`
    # reports "not running" while the port stays bound. The marker proves the
    # command was actually killed rather than merely reported as failed.
    import projects_orchestrator.supervisor as sup

    monkeypatch.setattr(sup, "_state_file", lambda _p: tmp_path / "no-such-dir" / "alpha.json")
    marker = tmp_path / "marker"
    start(_runnable(fleet_dir, command=f"sleep 2; touch {marker}"))
    time.sleep(3.0)
    assert not marker.exists()


# --- The project name is a CHILD repo's, not ours ----------------------------


def _hostile_named(fleet_dir: Path, name: str):
    """A project whose config.yaml declares an absolute path as its own name."""
    return load_descriptor(
        make_project(
            fleet_dir,
            "alpha",
            config_text=CONFIG_TEMPLATE.format(
                name=name, tooling='  run_command: "sleep 30"\n', memory_path=".claude/memory"
            ),
        )
    )


def test_a_hostile_project_name_cannot_write_state_outside_the_state_dir(
    fleet_dir: Path, tmp_path: Path
) -> None:
    # `state_dir() / "/tmp/owned.json"` IS `/tmp/owned.json` — an absolute
    # component discards everything to its left. `name` is read verbatim from a
    # CHILD repo's config.yaml, so a project naming itself this would have had
    # its state file written exactly where it asked (and unlinked on stop).
    hostile = str(tmp_path / "pwned")
    descriptor = _hostile_named(fleet_dir, hostile)
    try:
        start(descriptor)
        assert not Path(f"{hostile}.json").exists()
    finally:
        stop(descriptor)


def test_a_hostile_project_name_cannot_write_a_log_outside_the_state_dir(
    fleet_dir: Path, tmp_path: Path
) -> None:
    hostile = str(tmp_path / "pwned")
    descriptor = _hostile_named(fleet_dir, hostile)
    try:
        start(descriptor)
        assert not Path(f"{hostile}.log").exists()
    finally:
        stop(descriptor)


def test_a_hostile_project_name_still_starts_and_is_tracked(
    fleet_dir: Path, tmp_path: Path
) -> None:
    # Sanitising must not break the project — it is governed like any other.
    descriptor = _hostile_named(fleet_dir, str(tmp_path / "pwned"))
    try:
        start(descriptor)
        assert running_state(descriptor) is not None
    finally:
        stop(descriptor)


def test_liveness_check_is_absent_without_supervision(fleet_dir: Path) -> None:
    # Never started: no process check at all — absent, not unknown.
    descriptor = _runnable(fleet_dir)
    assert liveness_check(descriptor, "2026-07-17T00:00:00+00:00") is None


def test_liveness_check_passes_while_running(fleet_dir: Path) -> None:
    descriptor = _runnable(fleet_dir)
    start(descriptor)
    try:
        result = liveness_check(descriptor, "2026-07-17T00:00:00+00:00")
        assert result is not None and (result.task, result.status) == ("process", "pass")
    finally:
        stop(descriptor)


def test_liveness_check_fails_when_the_process_died(fleet_dir: Path) -> None:
    descriptor = _runnable(fleet_dir, command="sleep 0.05")
    start(descriptor)
    subprocess.run(["sleep", "0.3"], check=True)
    result = liveness_check(descriptor, "2026-07-17T00:00:00+00:00")
    assert result is not None and result.status == "fail"


def test_liveness_check_observes_a_death_once(fleet_dir: Path) -> None:
    # Cleanup-on-sight: the fail is recorded on the pass that notices it; the
    # next probe reads the project as unsupervised again.
    descriptor = _runnable(fleet_dir, command="sleep 0.05")
    start(descriptor)
    subprocess.run(["sleep", "0.3"], check=True)
    liveness_check(descriptor, "2026-07-17T00:00:00+00:00")
    assert liveness_check(descriptor, "2026-07-17T00:00:00+00:00") is None


def test_liveness_check_is_absent_after_a_clean_stop(fleet_dir: Path) -> None:
    descriptor = _runnable(fleet_dir)
    start(descriptor)
    stop(descriptor)
    assert liveness_check(descriptor, "2026-07-17T00:00:00+00:00") is None


def test_a_death_survives_an_intervening_status_poll(fleet_dir: Path) -> None:
    # A `status`/dashboard render between the death and the watch pass calls
    # running_state, which used to delete the only record — the alerting pass
    # then read the project as never-supervised (PR #177 review). The record
    # is now retired into a tombstone only liveness_check consumes.
    descriptor = _runnable(fleet_dir, command="sleep 0.05")
    start(descriptor)
    subprocess.run(["sleep", "0.3"], check=True)
    assert running_state(descriptor) is None  # the intervening poll
    result = liveness_check(descriptor, "2026-07-17T00:00:00+00:00")
    assert result is not None and result.status == "fail"


def test_a_restart_supersedes_an_unreported_death(fleet_dir: Path) -> None:
    # The operator who relaunched the service does not need next hour's watch
    # to tell them the previous incarnation died.
    import signal

    descriptor = _runnable(fleet_dir)
    start(descriptor)
    state = running_state(descriptor)
    assert state is not None
    os.kill(state.pid, signal.SIGKILL)
    # The unreaped child lingers as a zombie os.kill still "sees"; pid_alive
    # (via running_state) treats a zombie as dead, so poll through it.
    deadline = time.monotonic() + 5
    while running_state(descriptor) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert running_state(descriptor) is None  # death noticed, tombstoned
    start(descriptor)
    try:
        result = liveness_check(descriptor, "2026-07-17T00:00:00+00:00")
        assert result is not None and result.status == "pass"
    finally:
        stop(descriptor)
