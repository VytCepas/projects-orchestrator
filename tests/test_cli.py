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


def test_discover_lists_project_name(make_project, capsys):
    root = make_project("alpha").parent
    main(["discover", str(root)])
    assert "alpha" in capsys.readouterr().out


def test_discover_reports_empty_root(tmp_path, capsys):
    main(["discover", str(tmp_path)])
    assert "No project-init projects" in capsys.readouterr().out


def test_discover_exits_zero(make_project):
    root = make_project("alpha").parent
    assert main(["discover", str(root)]) == 0


def test_show_prints_project_root(make_project, capsys):
    root = make_project("alpha").parent
    main(["show", "alpha", str(root)])
    assert "root:" in capsys.readouterr().out


def test_show_unknown_project_exits_one(make_project):
    root = make_project("alpha").parent
    assert main(["show", "missing", str(root)]) == 1
