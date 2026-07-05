"""Tests for the CLI action path (run/test to completion)."""

from __future__ import annotations

import pytest

from projects_orchestrator import actions
from projects_orchestrator.actions import NOT_FOUND, REFUSED, execute
from projects_orchestrator.guard import Admission


@pytest.fixture
def project_root(tmp_path):
    """A scan root with one marked project exposing trivial just recipes."""
    proj = tmp_path / "demo"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "project-init.md").write_text("# Project: demo\n", encoding="utf-8")
    (proj / "justfile").write_text("dev:\n\t@true\ntest:\n\t@exit 1\n", encoding="utf-8")
    return tmp_path


def test_execute_unknown_project_returns_not_found(tmp_path):
    assert execute(tmp_path, "ghost", "run") == NOT_FOUND


def test_execute_missing_command_returns_not_found(tmp_path):
    proj = tmp_path / "bare" / ".claude"
    proj.mkdir(parents=True)
    (proj / "project-init.md").write_text("# Project: bare\n", encoding="utf-8")
    assert execute(tmp_path, "bare", "run") == NOT_FOUND


def test_execute_runs_command_and_returns_zero(project_root, monkeypatch):
    monkeypatch.setattr(actions, "admit", lambda *a, **k: Admission(True))
    assert execute(project_root, "demo", "run") == 0


def test_execute_propagates_failure_code(project_root, monkeypatch):
    monkeypatch.setattr(actions, "admit", lambda *a, **k: Admission(True))
    assert execute(project_root, "demo", "test") == 1


def test_execute_refused_when_admission_declines(project_root, monkeypatch):
    monkeypatch.setattr(actions, "admit", lambda *a, **k: Admission(False, "low memory"))
    assert execute(project_root, "demo", "run") == REFUSED
