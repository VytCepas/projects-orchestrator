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
