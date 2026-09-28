"""The cross-repo test contract, rules 1 and 3: hermetic tests, one summary line.

Scaffolded by project-init (PI-1044) and refreshed by ``project-init upgrade``; put
your own fixtures in ``tests/conftest.py``, which pytest loads beside this one.

Rule 1: no test reads or writes the real home, so a verdict never depends on who
ran it. HOME (with Windows' USERPROFILE, HOMEDRIVE and HOMEPATH), the XDG dirs
and CLAUDE_CONFIG_DIR move to a throwaway directory when pytest imports this
file: before it collects a test module or runs a fixture of any scope. Every test
then gets a directory of its own, TMPDIR included; TMPDIR moves per test only,
because pytest keeps its own temporary directories under it. Toolchain caches
(uv, cargo, rustup, go, bun) keep their real locations: they hold content, not
configuration, and a cold cache would turn every ``uv run`` into a download.

Rule 3: every run ends with ``<project>: N passed, M failed`` (an error counts
as failed, a skip as neither), the line a fleet runner adds up. pytest's own
exit code is unchanged: 0 all passed, 1 a test failed, 5 nothing collected.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping


def _toolchain_env(real_home: Path, env: Mapping[str, str]) -> dict[str, str]:
    """Each toolchain's cache and install dirs as the real home places them today."""
    cache = Path(env.get("XDG_CACHE_HOME") or real_home / ".cache")
    data = Path(env.get("XDG_DATA_HOME") or real_home / ".local" / "share")
    go_cache = (
        real_home / "Library" / "Caches" / "go-build"
        if sys.platform == "darwin"
        else cache / "go-build"
    )
    return {
        "UV_CACHE_DIR": str(cache / "uv"),
        "UV_PYTHON_INSTALL_DIR": str(data / "uv" / "python"),
        "UV_TOOL_DIR": str(data / "uv" / "tools"),
        "CARGO_HOME": str(real_home / ".cargo"),
        "RUSTUP_HOME": str(real_home / ".rustup"),
        "GOPATH": str(real_home / "go"),
        "GOCACHE": str(go_cache),
        "BUN_INSTALL": str(real_home / ".bun"),
    }


def _home_env(root: Path) -> dict[str, Path]:
    """The home and config variables, each pointed inside *root*."""
    home = root / "home"
    return {
        "HOME": home,
        # Windows: Path.home() reads USERPROFILE, then HOMEDRIVE + HOMEPATH, never HOME.
        "USERPROFILE": home,
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_DATA_HOME": home / ".local" / "share",
        "XDG_CACHE_HOME": home / ".cache",
        "XDG_STATE_HOME": home / ".local" / "state",
        "CLAUDE_CONFIG_DIR": root / "claude-config",
    }


def _redirect(env: Mapping[str, Path], patch: pytest.MonkeyPatch) -> None:
    """Create each directory in *env* and point its variable at it."""
    for name, path in env.items():
        path.mkdir(parents=True, exist_ok=True)
        patch.setenv(name, str(path))
    home = env["HOME"]
    patch.setenv("HOMEDRIVE", home.drive)
    patch.setenv("HOMEPATH", str(home)[len(home.drive) :])


def _hermetic_session() -> tuple[pytest.MonkeyPatch, Path]:
    """Move home for the whole run, reading the real one first for the toolchain caches."""
    patch = pytest.MonkeyPatch()
    root = Path(tempfile.mkdtemp(prefix="test-contract-"))
    # pytest_unconfigure removes it; this covers a run that never configures (--version).
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    for name, value in _toolchain_env(Path.home(), os.environ).items():
        if name not in os.environ:
            patch.setenv(name, value)
    _redirect(_home_env(root), patch)
    return patch, root


# At import, not in pytest_configure: pytest imports tests/conftest.py before
# configure, and test modules at collection. xdist workers inherit the moved home
# and the real toolchain paths, then move to a home of their own.
_SESSION_PATCH, _SESSION_ROOT = _hermetic_session()


def pytest_unconfigure() -> None:
    """Give the process its environment back and remove the run's home."""
    _SESSION_PATCH.undo()
    shutil.rmtree(_SESSION_ROOT, ignore_errors=True)


@pytest.fixture(autouse=True)
def _test_contract_hermetic_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A dir of its own, not tmp_path: a test that lists its tmp_path must not find these in it.
    root = tmp_path_factory.mktemp("hermetic")
    env = {**_home_env(root), "TMPDIR": root / "tmp"}
    _redirect(env, monkeypatch)
    # tempfile caches its dir on first use, so the variable alone would not move it.
    monkeypatch.setattr(tempfile, "tempdir", str(env["TMPDIR"]))


def _suite_name(config: pytest.Config) -> str:
    """The project's name from pyproject.toml, else its directory's, in the line's charset."""
    name = config.rootpath.name
    try:
        import tomllib  # Python 3.11+; on 3.10 the name falls back to the directory's

        with (config.rootpath / "pyproject.toml").open("rb") as handle:
            name = str(tomllib.load(handle).get("project", {}).get("name") or name)
    except (ImportError, OSError, ValueError):
        name = config.rootpath.name
    return re.sub(r"[^A-Za-z0-9._-]", "-", name) or "tests"


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    """Print the contract's summary line: ``<project>: N passed, M failed``.

    Args:
        terminalreporter: pytest's reporter, which holds the run's results.
        config: the run's config, for the project's name.
    """
    stats = terminalreporter.stats
    passed = len(stats.get("passed", []))
    failed = len(stats.get("failed", [])) + len(stats.get("error", []))
    terminalreporter.write_line(f"{_suite_name(config)}: {passed} passed, {failed} failed")
