"""The cross-repo test contract, rules 1 and 3: hermetic tests, one summary line.

Scaffolded by project-init (PI-1044) and refreshed by ``project-init upgrade``; put
your own fixtures in ``tests/conftest.py``, which pytest loads beside this one.

Rule 1: no test reads or writes the real home, so a verdict never depends on who
ran it. HOME, the XDG dirs and CLAUDE_CONFIG_DIR move to a throwaway directory
when pytest imports this file: before it collects a test module or runs a
fixture of any scope. Every test then gets a directory of its own, TMPDIR (with
TEMP and TMP, which Python's tempfile and some tools read instead) included; these move
per test only, because pytest keeps its own temporary directories under
TMPDIR. Toolchain *caches* (uv, cargo, rustup, go, bun) keep their real
locations: they hold content, not configuration, and a cold cache would turn
every ``uv run`` into a download. *Install roots* — UV_PYTHON_INSTALL_DIR,
UV_TOOL_DIR, GOPATH and BUN_INSTALL, with the bin dirs UV_PYTHON_BIN_DIR,
UV_TOOL_BIN_DIR and XDG_BIN_HOME — move inside the throwaway root instead,
even when the runner exports them: they are where a test's own
`uv python install`/`uv tool install`/`go install`/`bun install -g` would
write, so leaving them real would let a test install into it (#1062). An
inherited GOBIN is dropped rather than moved, so `go install` writes to the
isolated GOPATH/bin: Go refuses a cross-compiled install while GOBIN is set
(#1069).
GOMODCACHE and bun's install cache are pinned to their real locations
explicitly (under the runner's own GOPATH/BUN_INSTALL, if exported), so
isolating GOPATH/BUN_INSTALL does not accidentally cool them — both otherwise
default to a path *under* the var each command just moved.
CARGO_HOME and RUSTUP_HOME are further exceptions that cannot just point at
the real thing even for their cache half: cargo has no separate env var for
its cache the way UV_CACHE_DIR splits from uv's config, so CARGO_HOME governs
config.toml and credentials.toml (real registry tokens) as well as the
registry/git download caches, and rustup keeps settings.toml (the default
toolchain and other mutable preferences) directly under RUSTUP_HOME beside its
toolchains/downloads caches. The test run's CARGO_HOME and RUSTUP_HOME (set
once per session, in ``_hermetic_session``) are therefore their own throwaway
directories, with only the download caches — registry/ and git/ for Cargo,
toolchains/ and downloads/ for rustup — symlinked back to the real ones (or an
inherited CARGO_HOME/RUSTUP_HOME's, if the runner already exported one):
config and credentials are absent from them. An xdist worker inherits the
controller's CARGO_HOME/RUSTUP_HOME as that source, so its links resolve
through the controller's to the real caches (#1062).

Also at import: the variables git exports into a hook (GIT_DIR, and more from a
linked worktree) are dropped, so a test's ``git -C <tmp>`` acts on <tmp>, never
on the repository whose pre-push hook ran pytest (#1065).

Rule 3: every run ends with ``<project>: N passed, M failed`` (an error counts
as failed, a skip as neither), the line a fleet runner adds up, printed after
pytest's own final summary line so a reader taking "the last line" gets it.
pytest's own exit code is unchanged: 0 all passed, 1 a test failed, 5 nothing
collected.

Also: ``pytest -n auto`` starts no more workers than this process's cgroup has
CPUs and memory for. xdist's answer is the CPU count, which in a runner
container is the HOST's: 8 workers in a 1-CPU/1-GiB runner were OOM-killed and
every run failed with "node down" (PR #1104). Without cgroup v2 files (macOS,
cgroup v1) the count is xdist's; a number in PYTEST_XDIST_AUTO_NUM_WORKERS, or
an explicit ``-n N``, is never changed.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping


def _cargo_home(root: Path, source: Path) -> str:
    """The test run's CARGO_HOME: only *source*'s registry/git *caches* symlinked in.

    *source* is the CARGO_HOME this process inherited, else ``~/.cargo``
    (#1062). config.toml and credentials.toml are absent from the result —
    cargo reads none of the real ones through this variable. A symlink that
    cannot be made is skipped: isolation still holds, the cache is just cold.
    """
    fake = root / "cargo-home"
    fake.mkdir(parents=True, exist_ok=True)
    for name in ("registry", "git"):
        real, link = source / name, fake / name
        if real.is_dir() and not link.exists():
            with contextlib.suppress(OSError):
                link.symlink_to(real, target_is_directory=True)
    return str(fake)


def _rustup_home(root: Path, source: Path) -> str:
    """The test run's RUSTUP_HOME: only *source*'s toolchains/downloads *caches* symlinked in.

    *source* is the RUSTUP_HOME this process inherited, else ``~/.rustup``
    (#1062). settings.toml (the default toolchain and other rustup
    preferences) is mutable state, not a cache, and is absent from the result
    — rustup reads and writes none of the real one through this variable. A
    symlink that cannot be made is skipped: isolation still holds, the cache
    is just cold.
    """
    fake = root / "rustup-home"
    fake.mkdir(parents=True, exist_ok=True)
    for name in ("toolchains", "downloads"):
        real, link = source / name, fake / name
        if real.is_dir() and not link.exists():
            with contextlib.suppress(OSError):
                link.symlink_to(real, target_is_directory=True)
    return str(fake)


def _toolchain_env(
    real_home: Path, cargo_src: Path, rustup_src: Path, env: Mapping[str, str], root: Path
) -> dict[str, str]:
    """Each toolchain's install roots (inside *root*) and its shared caches (real).

    Verified on this machine: ``UV_CACHE_DIR``/``UV_PYTHON_INSTALL_DIR``/
    ``UV_TOOL_DIR`` against ``uv python install --help``, ``uv tool dir --help``
    and ``uv cache dir --help`` (uv 0.11.28); ``GOPATH``/``GOCACHE``/
    ``GOMODCACHE`` against ``go help environment`` and ``go env`` (go 1.27.1,
    installed via Homebrew for this check — GOMODCACHE defaults to
    ``$GOPATH/pkg/mod``, so it must be pinned once GOPATH moves); ``BUN_INSTALL``
    and ``BUN_INSTALL_CACHE_DIR`` empirically (bun 1.4.2: the cache defaults to
    ``$BUN_INSTALL/install/cache``, confirmed by pointing each var in turn at a
    scratch dir and reading back ``bun pm cache``).
    """
    cache = Path(env.get("XDG_CACHE_HOME") or real_home / ".cache")
    # GOMODCACHE defaults to the first GOPATH entry's pkg/mod, bun's cache to BUN_INSTALL's.
    gopath = next((p for p in env.get("GOPATH", "").split(os.pathsep) if p), "")
    bun = Path(env.get("BUN_INSTALL") or real_home / ".bun")
    go_cache = (
        real_home / "Library" / "Caches" / "go-build"
        if sys.platform == "darwin"
        else cache / "go-build"
    )
    return {
        "UV_CACHE_DIR": str(cache / "uv"),
        "UV_PYTHON_INSTALL_DIR": str(root / "uv-python"),
        "UV_PYTHON_BIN_DIR": str(root / "uv-python-bin"),
        "UV_TOOL_DIR": str(root / "uv-tools"),
        "UV_TOOL_BIN_DIR": str(root / "uv-tools-bin"),
        "CARGO_HOME": _cargo_home(root, cargo_src),
        "RUSTUP_HOME": _rustup_home(root, rustup_src),
        "GOPATH": str(root / "go"),
        "GOCACHE": str(go_cache),
        "GOMODCACHE": str(Path(gopath or real_home / "go") / "pkg" / "mod"),
        "BUN_INSTALL": str(root / "bun"),
        "BUN_INSTALL_CACHE_DIR": str(bun / "install" / "cache"),
    }


def _home_env(root: Path) -> dict[str, Path]:
    """The home and config variables, each pointed inside *root*."""
    home = root / "home"
    return {
        "HOME": home,
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_DATA_HOME": home / ".local" / "share",
        "XDG_CACHE_HOME": home / ".cache",
        "XDG_STATE_HOME": home / ".local" / "state",
        # Not in the XDG spec, but uv installs executables there when it is set.
        "XDG_BIN_HOME": home / ".local" / "bin",
        "CLAUDE_CONFIG_DIR": root / "claude-config",
    }


def _redirect(env: Mapping[str, Path], patch: pytest.MonkeyPatch) -> None:
    """Create each directory in *env* and point its variable at it."""
    for name, path in env.items():
        path.mkdir(parents=True, exist_ok=True)
        patch.setenv(name, str(path))


# Config homes (CARGO_HOME, RUSTUP_HOME: #1056 review) and install roots (#1062)
# replace a runner's own export; only a cache keeps the runner's override.
_ALWAYS_ISOLATED = {
    "CARGO_HOME",
    "RUSTUP_HOME",
    "UV_PYTHON_INSTALL_DIR",
    "UV_PYTHON_BIN_DIR",
    "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR",
    "GOPATH",
    "BUN_INSTALL",
}

# Every name `git rev-parse --local-env-vars` prints, plus GIT_QUARANTINE_PATH; the
# scaffolded git hooks strip the same list before their gate, and tests pin both.
_GIT_HOOK_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_QUARANTINE_PATH",
    "GIT_PREFIX",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_GRAFT_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
)


def _hermetic_session() -> tuple[pytest.MonkeyPatch, Path]:
    """Move home for the whole run, reading the real one first for the toolchain caches."""
    patch = pytest.MonkeyPatch()
    for name in _GIT_HOOK_VARS:
        patch.delenv(name, raising=False)
    patch.delenv("GOBIN", raising=False)  # go install then uses the isolated GOPATH/bin (#1069)
    root = Path(tempfile.mkdtemp(prefix="test-contract-"))
    # pytest_unconfigure removes it; this covers a run that never configures (--version).
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    real_home = Path.home()
    # Absolute: a relative export means the runner's cwd, but a relative link target
    # would resolve inside the fake home and dangle (#1067 review).
    cargo_src = Path(os.environ.get("CARGO_HOME") or real_home / ".cargo").absolute()
    rustup_src = Path(os.environ.get("RUSTUP_HOME") or real_home / ".rustup").absolute()
    for name, value in _toolchain_env(real_home, cargo_src, rustup_src, os.environ, root).items():
        if name in _ALWAYS_ISOLATED or name not in os.environ:
            patch.setenv(name, value)
    _redirect(_home_env(root), patch)
    return patch, root


# At import, not in pytest_configure: pytest imports tests/conftest.py before
# configure, and test modules at collection. An xdist worker inherits the
# controller's moved home and toolchain paths, then moves to a home of its own.
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
    tmp = root / "tmp"
    # TEMP/TMP: tempfile and some tools read these instead of TMPDIR (Copilot, #1056/#1062).
    env = {**_home_env(root), "TMPDIR": tmp, "TEMP": tmp, "TMP": tmp}
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


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session: pytest.Session) -> Generator[None, object, object]:
    """Print the contract's line: ``<project>: N passed, M failed``, printed last.

    TerminalReporter calls the ``pytest_terminal_summary`` hook and its own
    ``summary_stats()`` (which prints "N passed in Ys") as two separate
    statements — so any ``pytest_terminal_summary`` hookimpl, wrapper or not, at
    any priority, always runs *before* that final line, never after it (checked
    against the installed pytest's ``_pytest/terminal.py``). ``pytest_sessionfinish``
    is different: TerminalReporter's own implementation is itself a hook wrapper,
    so a `tryfirst` wrapper here is the outermost one — its code after `yield`
    runs last of all, once TerminalReporter's has already printed its line.
    """
    result = yield
    terminalreporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminalreporter is not None:
        stats = terminalreporter.stats
        passed = len(stats.get("passed", []))
        failed = len(stats.get("failed", [])) + len(stats.get("error", []))
        name = _suite_name(session.config)
        terminalreporter.write_line(f"{name}: {passed} passed, {failed} failed")
    return result


# The cgroup v2 files the worker cap reads (PR #1104). Module globals so a test can
# point them at a fake tree.
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_SELF_CGROUP = Path("/proc/self/cgroup")
# One worker's memory: about 2x the 237 MB peak RSS of one process running a
# scaffolded repo's whole suite with coverage. A heavier suite sets a count instead.
_WORKER_BYTES = 512 * 1024 * 1024


def _cgroup_read(path: Path) -> str | None:
    """The file's text, or None when it cannot be read: that file then sets no limit."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):  # ValueError: a cgroup name that is not UTF-8
        return None


def _as_int(text: str | None) -> int | None:
    """``int(text)``, or None where int() refuses it, exactly as xdist parses its variable."""
    try:
        return int(text or "")
    except ValueError:
        return None


def _cgroup_cpus(cpu_max: str | None) -> int | None:
    """Whole CPUs a ``cpu.max`` (``<quota> <period>``) grants, at least 1; None if unlimited."""
    quota, _, period = (cpu_max or "").partition(" ")
    whole, per = _as_int(quota), _as_int(period)
    if whole is None or not per:  # "max <period>", or a value no kernel writes
        return None
    return max(1, whole // per)


def _cgroup_limits(root: Path, proc: Path) -> tuple[int | None, int | None]:
    """The tightest CPU count and memory bytes from this process's cgroup up to *root*."""
    own = (_cgroup_read(proc) or "").splitlines()
    path = next((line[3:] or "/" for line in own if line.startswith("0::")), None)
    if path is None:  # no cgroup v2 entry: cgroup v1, or not Linux
        return None, None
    level = root / path.lstrip("/")
    levels = [level, *(p for p in level.parents if p == root or root in p.parents)]
    cpus = [_cgroup_cpus(_cgroup_read(p / "cpu.max")) for p in levels]
    memory = [_as_int(_cgroup_read(p / "memory.max")) for p in levels]  # "max": None
    return (
        min((c for c in cpus if c is not None), default=None),
        min((m for m in memory if m is not None), default=None),
    )


def _worker_cap(workers: int, cpus: int | None, memory: int | None) -> int:
    """*workers* lowered to what *cpus* and *memory* hold, never below one."""
    if cpus is not None:
        workers = min(workers, cpus)
    if memory is not None:
        workers = min(workers, memory // _WORKER_BYTES)
    return max(1, workers)


@pytest.hookimpl(wrapper=True, optionalhook=True)
def pytest_xdist_auto_num_workers() -> Generator[None, int, int]:
    """Hold ``-n auto`` to the CPUs and memory the cgroup grants, not the host's CPU count.

    A PYTEST_XDIST_AUTO_NUM_WORKERS that xdist takes as the count (int() parses it)
    stands. Anything else xdist ignores, so the cap still applies. ``optionalhook``:
    under ``-p no:xdist`` this hook has no spec, and pytest would reject the file.
    """
    workers = yield
    if _as_int(os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS")) is not None:
        return workers
    return _worker_cap(workers, *_cgroup_limits(_CGROUP_ROOT, _PROC_SELF_CGROUP))
