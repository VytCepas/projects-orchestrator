"""Tests for run-command inference, the supervisor, and the fleet snapshot."""

from __future__ import annotations

import time

import pytest

from projects_orchestrator.cockpit import snapshot
from projects_orchestrator.guard import LaunchRefusedError
from projects_orchestrator.runcommands import plan_for
from projects_orchestrator.supervisor import Supervisor


@pytest.fixture
def project_root(tmp_path):
    """A scan root holding one marked project with a justfile."""
    proj = tmp_path / "demo"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "project-init.md").write_text("# Project: demo\n", encoding="utf-8")
    (proj / "justfile").write_text("dev:\n\techo hi\ntest:\n\techo ok\n", encoding="utf-8")
    return tmp_path


def test_plan_prefers_justfile_run(project_root):
    assert plan_for(project_root / "demo").run == "just dev"


def test_plan_reads_test_recipe(project_root):
    assert plan_for(project_root / "demo").test == "just test"


def test_plan_reports_source(project_root):
    assert plan_for(project_root / "demo").source == "justfile"


def test_supervisor_starts_process(tmp_path):
    sup = Supervisor()
    assert sup.start("job", "sleep 5", tmp_path) is True


def test_supervisor_refuses_launch_when_memory_low(tmp_path):
    sup = Supervisor(available=lambda: 0, min_free_bytes=1024)
    with pytest.raises(LaunchRefusedError):
        sup.start("job", "sleep 5", tmp_path)


def test_supervisor_reports_running(tmp_path):
    sup = Supervisor()
    sup.start("job", "sleep 5", tmp_path)
    assert sup.status("job") == "running"


def test_supervisor_stops_process(tmp_path):
    sup = Supervisor()
    sup.start("job", "sleep 30", tmp_path)
    sup.stop("job")
    assert sup.status("job") == "exited"


def test_supervisor_captures_output(tmp_path):
    sup = Supervisor()
    sup.start("job", "echo hello-cockpit", tmp_path)
    time.sleep(0.3)
    assert "hello-cockpit" in sup.logs("job")


def test_snapshot_marks_supervised_running(project_root):
    sup = Supervisor()
    sup.start("demo", "sleep 5", project_root / "demo")
    view = next(v for v in snapshot(project_root, sup) if v["name"] == "demo")
    assert view["running"] is True
