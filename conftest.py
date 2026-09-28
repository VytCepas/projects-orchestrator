"""The cross-repo test contract, rules 1 and 3: hermetic tests, one summary line.

Scaffolded by project-init (PI-1044) and refreshed by ``project-init upgrade``; put
your own fixtures in ``tests/conftest.py``, which pytest loads beside this one.

Rule 1: every test runs with HOME, the XDG dirs, CLAUDE_CONFIG_DIR and TMPDIR
inside a throwaway directory of its own, so no test reads or writes the real
home and a verdict never depends on who ran it. Toolchain caches (uv, cargo,
rustup, go, bun) keep their real locations: they hold content, not
configuration, and a cold cache would turn every ``uv run`` into a download.

Rule 3: every run ends with ``<project>: N passed, M failed`` (an error counts
as failed, a skip as neither), the line a fleet runner adds up. pytest's own
exit code is unchanged: 0 all passed, 1 a test failed, 5 nothing collected.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping

# Read once, at import: before any test has redirected HOME.
_REAL_HOME = Path.home()


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


@pytest.fixture(autouse=True)
def _test_contract_hermetic_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A dir of its own, not tmp_path: a test that lists its tmp_path must not find these in it.
    root = tmp_path_factory.mktemp("hermetic")
    for name, value in _toolchain_env(_REAL_HOME, os.environ).items():
        if name not in os.environ:
            monkeypatch.setenv(name, value)
    home = root / "home"
    redirected = {
        "HOME": home,
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_DATA_HOME": home / ".local" / "share",
        "XDG_CACHE_HOME": home / ".cache",
        "XDG_STATE_HOME": home / ".local" / "state",
        "CLAUDE_CONFIG_DIR": root / "claude-config",
        "TMPDIR": root / "tmp",
    }
    for name, path in redirected.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(path))
    # tempfile caches its dir on first use, so the variable alone would not move it.
    monkeypatch.setattr(tempfile, "tempdir", str(redirected["TMPDIR"]))


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
