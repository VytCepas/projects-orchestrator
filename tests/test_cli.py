"""Smoke tests for the projects-orchestrator CLI."""

from __future__ import annotations

import pytest

from projects_orchestrator import __version__
from projects_orchestrator.__main__ import main


def test_version_is_set():
    assert __version__


def test_main_runs():
    assert main([]) == 0


def test_version_flag_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_status_command_exits_zero(tmp_path):
    assert main(["status", "--root", str(tmp_path)]) == 0


def test_json_command_exits_zero(tmp_path):
    assert main(["json", "--root", str(tmp_path)]) == 0


def test_html_command_writes_file(tmp_path):
    out = tmp_path / "dash.html"
    main(["html", "--root", str(tmp_path), "-o", str(out)])
    assert out.exists()


def test_run_unknown_project_returns_not_found(tmp_path):
    assert main(["run", "--root", str(tmp_path), "ghost"]) == 2


def test_test_unknown_project_returns_not_found(tmp_path):
    assert main(["test", "--root", str(tmp_path), "ghost"]) == 2
