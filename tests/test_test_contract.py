"""This suite runs under the cross-repo test contract's hermetic fixture (#316).

The root ``conftest.py`` is the one project-init scaffolds (PI-1044): every test
gets a HOME, XDG dirs, CLAUDE_CONFIG_DIR and TMPDIR of its own. The account's
home comes from the password database, which no environment variable can move.
"""

from __future__ import annotations

import os
import pwd
import tempfile
from pathlib import Path

import pytest

_ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


def test_home_is_not_the_real_home() -> None:
    assert Path.home() != _ACCOUNT_HOME


def test_home_and_config_dirs_are_throwaway(tmp_path_factory: pytest.TempPathFactory) -> None:
    base = tmp_path_factory.getbasetemp()
    assert Path.home().is_relative_to(base)
    for var in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "CLAUDE_CONFIG_DIR",
    ):
        assert Path(os.environ[var]).is_relative_to(base), var


def test_tempfile_follows_the_redirected_tmpdir() -> None:
    assert tempfile.gettempdir() == os.environ["TMPDIR"]
