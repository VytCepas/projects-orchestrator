"""This suite runs under the cross-repo test contract's hermetic fixture (#316).

The root ``conftest.py`` is the one project-init scaffolds (PI-1044): every test
gets a HOME, XDG dirs, CLAUDE_CONFIG_DIR and TMPDIR of its own, and HOME moves
before collection, so an import-time read or a broader-scoped fixture is covered
too. The account's home comes from the password database, which no environment
variable can move.
"""

from __future__ import annotations

import os
import pwd
import tempfile
from pathlib import Path

import pytest

_ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
#: Read at collection, before any function-scoped fixture has run.
_HOME_AT_IMPORT = Path.home()


@pytest.fixture(scope="session")
def session_home() -> Path:
    return Path.home()


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


def test_a_read_at_import_is_not_the_real_home() -> None:
    """PR #320 review: collection imports ran before the function-scoped redirect."""
    assert _HOME_AT_IMPORT != _ACCOUNT_HOME


def test_a_session_fixture_does_not_see_the_real_home(session_home: Path) -> None:
    """PR #320 review: session fixtures ran before the function-scoped redirect."""
    assert session_home != _ACCOUNT_HOME
    assert Path.home() != session_home, "each test still gets a home of its own"
