"""This suite runs under the cross-repo test contract's hermetic fixture (#316).

The root ``conftest.py`` is the one project-init scaffolds (PI-1044): every test
gets a HOME, XDG dirs, CLAUDE_CONFIG_DIR and TMPDIR of its own, and HOME moves
before collection, so an import-time read or a broader-scoped fixture is covered
too. The account's home comes from the password database, which no environment
variable can move.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
#: Read at collection, before any function-scoped fixture has run.
_HOME_AT_IMPORT = Path.home()
_REPO_ROOT = Path(__file__).resolve().parents[1]


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


# --- #323: UV_TOOL_DIR / UV_PYTHON_INSTALL_DIR are install roots, not caches --
#
# The running suite already imported the real conftest.py once, so its module
# top-level `_hermetic_session()` call cannot be re-triggered in-process to try
# a different starting HOME/preset. Each check below instead imports the real
# repo-root conftest.py fresh, in its own subprocess with a controlled, fake
# "real home" — the exact path a runner's shell exercises, never this host's
# actual home.


def _import_conftest(tmp_path: Path, env: dict[str, str]) -> dict[str, str]:
    """Import repo-root conftest.py in a clean subprocess; report what it leaves
    UV's env vars pointing at, and where a simulated install write landed."""
    script = tmp_path / "_import_conftest.py"
    script.write_text(
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(_REPO_ROOT)!r})\n"
        "import conftest\n"
        "tool_bin = os.path.join(os.environ['UV_TOOL_DIR'], 'demo-tool', 'bin', 'demo-tool')\n"
        "os.makedirs(os.path.dirname(tool_bin), exist_ok=True)\n"
        "open(tool_bin, 'w').close()\n"
        "py_bin = os.path.join(\n"
        "    os.environ['UV_PYTHON_INSTALL_DIR'], 'cpython-3.12', 'bin', 'python3'\n"
        ")\n"
        "os.makedirs(os.path.dirname(py_bin), exist_ok=True)\n"
        "open(py_bin, 'w').close()\n"
        "print(json.dumps({\n"
        "    'session_root': str(conftest._SESSION_ROOT),\n"
        "    'UV_TOOL_DIR': os.environ.get('UV_TOOL_DIR', ''),\n"
        "    'UV_PYTHON_INSTALL_DIR': os.environ.get('UV_PYTHON_INSTALL_DIR', ''),\n"
        "    'UV_CACHE_DIR': os.environ.get('UV_CACHE_DIR', ''),\n"
        "    'tool_bin': tool_bin,\n"
        "    'py_bin': py_bin,\n"
        "}))\n",
        encoding="utf-8",
    )
    subprocess_tmp = tmp_path / "subprocess-tmp"
    subprocess_tmp.mkdir(exist_ok=True)
    full_env = {"PATH": os.environ.get("PATH", ""), "TMPDIR": str(subprocess_tmp), **env}
    proc = subprocess.run(
        [sys.executable, str(script)],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_uv_install_shaped_write_lands_in_the_sandbox(tmp_path: Path) -> None:
    """#323: a `uv tool install` / `uv python install` write must land under the
    session root, never under the real home — only UV_CACHE_DIR, a real
    download cache, is allowed to keep its real spot."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    result = _import_conftest(tmp_path, {"HOME": str(real_home)})
    session_root = Path(result["session_root"])
    tool_bin = Path(result["tool_bin"])
    py_bin = Path(result["py_bin"])
    assert tool_bin.is_relative_to(session_root), result
    assert py_bin.is_relative_to(session_root), result
    assert not tool_bin.is_relative_to(real_home), result
    assert not py_bin.is_relative_to(real_home), result
    assert not any(real_home.rglob("demo-tool")), "a uv tool install must not touch the real home"
    assert not any(real_home.rglob("cpython-3.12")), "nor must a uv python install"


def test_a_runners_preset_uv_install_dirs_are_still_isolated(tmp_path: Path) -> None:
    """#323: the docstring promises UV_TOOL_DIR/UV_PYTHON_INSTALL_DIR are
    always isolated, even over a runner's own exported value — unlike a plain
    cache var (UV_CACHE_DIR), whose preset the fixture leaves alone."""
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    preset_tools = tmp_path / "runner-tools"
    preset_pythons = tmp_path / "runner-pythons"
    preset_cache = tmp_path / "runner-cache"
    result = _import_conftest(
        tmp_path,
        {
            "HOME": str(real_home),
            "UV_TOOL_DIR": str(preset_tools),
            "UV_PYTHON_INSTALL_DIR": str(preset_pythons),
            "UV_CACHE_DIR": str(preset_cache),
        },
    )
    session_root = Path(result["session_root"])
    assert result["UV_TOOL_DIR"] != str(preset_tools), result
    assert result["UV_PYTHON_INSTALL_DIR"] != str(preset_pythons), result
    assert Path(result["UV_TOOL_DIR"]).is_relative_to(session_root), result
    assert Path(result["UV_PYTHON_INSTALL_DIR"]).is_relative_to(session_root), result
    assert result["UV_CACHE_DIR"] == str(preset_cache), (
        "a plain cache var keeps a runner's override"
    )
