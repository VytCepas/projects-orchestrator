"""`just install`: the gated route that puts this checkout on PATH as a uv tool (#317).

Every test runs against a throwaway box: a checkout with its own bare `origin`,
a uv tool dir and bin dir under tmp_path, and a fake `uv` first on PATH that
logs its argv and fakes an install by copying the package. Nothing here can
reach the developer's real uv tool dir: the script finds uv through PATH, and
the fake answers `uv tool dir` with the sandbox.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / ".agents" / "scripts" / "install_tool.py"
_PKG = "projects_orchestrator"
_TOOL = "projects-orchestrator"
_REAL_UV = shutil.which("uv")  # read before any test puts the fake first on PATH

# argv logged per call. `run` drops uv's own flags and runs the script; `python
# find <env>` names the env's interpreter the way uv does; `tool
# install` builds a real venv (FAKE_UV_LAYOUT=windows: a Lib/ + Scripts/ one whose
# interpreter reports those paths), copies the package with its modes, and writes
# the dist-info a build of the fixture pyproject would, RECORD included.
# FAKE_UV_CORRUPT makes the copy differ.
_FAKE_UV = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_UV_LOG"
if [ "$1" = run ]; then
  while [ $# -gt 0 ] && [ "$1" != python ]; do shift; done
  shift; exec "$FAKE_PYTHON" "$@"
fi
if [ "$1 $2" = "python find" ]; then
  for p in "$3/bin/python" "$3/Scripts/python.exe"; do
    if [ -x "$p" ]; then printf '%s\\n' "$p"; exit 0; fi
  done
  echo "error: No interpreter found in directory $3" >&2; exit 2
fi
if [ "$1 $2" = "tool dir" ]; then
  if [ "${3:-}" = --bin ]; then printf '%s\\n' "$UV_TOOL_BIN_DIR"; else printf '%s\\n' "$UV_TOOL_DIR"; fi
  exit 0
fi
if [ "$1 $2 $3" = "tool install --reinstall" ]; then
  [ "${FAKE_UV_INSTALL_RC:-0}" = 0 ] || exit "$FAKE_UV_INSTALL_RC"
  env="$UV_TOOL_DIR/projects-orchestrator"; rm -rf "$env"; mkdir -p "$UV_TOOL_BIN_DIR"
  if [ "${FAKE_UV_LAYOUT:-}" = windows ]; then
    site="$env/Lib/site-packages"; scripts="$env/Scripts"; mkdir -p "$site" "$scripts"
    cat > "$scripts/python.exe" <<EOF
#!/bin/sh
echo '{"purelib": "$site", "scripts": "$scripts"}'
EOF
    chmod +x "$scripts/python.exe"
  else
    "$FAKE_PYTHON" -m venv --without-pip "$env"
    site="$("$env/bin/python" -I -B -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
    scripts="$env/bin"
  fi
  cp -R "$4/src/projects_orchestrator" "$site/"
  [ -z "${FAKE_UV_CORRUPT:-}" ] || printf '# stale\\n' >> "$site/projects_orchestrator/cli.py"
  dist="$site/projects_orchestrator-0.0.0.dist-info"; mkdir -p "$dist"
  printf '%s\\n' 'Metadata-Version: 2.4' 'Name: projects-orchestrator' 'Version: 0.0.0' \\
    'Requires-Python: >=3.11' 'Requires-Dist: pyyaml>=6.0' 'Provides-Extra: tui' \\
    "Requires-Dist: textual>=0.86; extra == 'tui'" > "$dist/METADATA"
  printf '%s\\n' '[console_scripts]' 'projects-orchestrator = projects_orchestrator.cli:main' \\
    > "$dist/entry_points.txt"
  # uv's RECORD: each installed file relative to site-packages, the script outside it.
  (cd "$site" && find projects_orchestrator "${dist##*/}" -type f) | sed 's/$/,,/' > "$dist/RECORD"
  printf '../../../bin/projects-orchestrator,,\\n' >> "$dist/RECORD"
  printf '#!/bin/sh\\n' > "$scripts/projects-orchestrator"; chmod +x "$scripts/projects-orchestrator"
  ln -sf "$scripts/projects-orchestrator" "$UV_TOOL_BIN_DIR/projects-orchestrator"
  printf '[tool]\\nrequirements = [{ name = "projects-orchestrator", directory = "%s" }]\\n' "$4" > "$env/uv-receipt.toml"
  printf 'entrypoints = [{ name = "projects-orchestrator", install-path = "%s" }]\\n' \\
    "$UV_TOOL_BIN_DIR/projects-orchestrator" >> "$env/uv-receipt.toml"
  exit 0
fi
exit 99
"""

_PYPROJECT = """\
[project]
name = "projects-orchestrator"
version = "0.0.0"
requires-python = ">=3.11"
dependencies = ["pyyaml>=6.0"]

[project.optional-dependencies]
tui = ["textual>=0.86"]

[project.scripts]
projects-orchestrator = "projects_orchestrator.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/projects_orchestrator"]
"""


@dataclass
class Box:
    repo: Path
    origin: Path
    tools: Path
    bin: Path
    uv: Path
    log: Path
    env: dict[str, str]

    @property
    def site(self) -> Path:
        return next((self.tools / "projects-orchestrator").glob("**/site-packages")) / _PKG

    @property
    def scripts(self) -> Path:
        return self.tools / "projects-orchestrator" / "bin"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _make_box(tmp_path: Path, object_format: str = "sha1") -> Box:
    fmt = f"--object-format={object_format}"
    origin = tmp_path / "origin.git"
    repo = tmp_path / "checkout"
    (repo / "src" / _PKG).mkdir(parents=True)
    (repo / "src" / _PKG / "__init__.py").write_text('__version__ = "0.0.0"\n')
    (repo / "src" / _PKG / "cli.py").write_text("def main() -> int:\n    return 0\n")
    (repo / "src" / _PKG / "run.sh").write_text("#!/bin/sh\necho run\n")
    (repo / "src" / _PKG / "run.sh").chmod(0o755)  # a 100755 blob the install must keep
    (repo / "pyproject.toml").write_text(_PYPROJECT)
    (repo / ".agents" / "scripts").mkdir(parents=True)
    shutil.copy2(_SCRIPT, repo / ".agents" / "scripts" / "install_tool.py")
    shutil.copy2(_REPO / "justfile", repo / "justfile")
    subprocess.run(["git", "init", "-q", "--bare", fmt, "-b", "main", str(origin)], check=True)
    _git(repo.parent, "init", "-q", fmt, "-b", "main", str(repo))
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _commit(repo, "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uv").write_text(_FAKE_UV)
    (fake / "uv").chmod(0o755)
    skip = {"CLAUDECODE", "VIRTUAL_ENV", "UV_RUN_RECURSION_DEPTH"}  # set by a `uv run pytest`
    env = {k: v for k, v in os.environ.items() if k not in skip}
    # The sandbox bin dir, and no real `projects-orchestrator` (the developer's, or
    # the test venv's) for the PATH check to find instead.
    rest = [d for d in os.environ["PATH"].split(os.pathsep) if not (Path(d) / _TOOL).exists()]
    env |= {
        "PATH": os.pathsep.join([str(fake), str(tmp_path / "bin"), *rest]),
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
        "FAKE_UV_LOG": str(tmp_path / "uv.log"),
        "FAKE_PYTHON": sys.executable,
    }
    return Box(
        repo, origin, tmp_path / "tools", tmp_path / "bin", fake / "uv", tmp_path / "uv.log", env
    )


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return _make_box(tmp_path)


@pytest.fixture
def sha256_box(tmp_path: Path) -> Box:
    return _make_box(tmp_path, "sha256")


def _script(
    box: Box, *args: str, cwd: Path | None = None, python: str = sys.executable
) -> subprocess.CompletedProcess[str]:
    script = (cwd or box.repo) / ".agents" / "scripts" / "install_tool.py"
    return subprocess.run(
        [python, str(script), *args],
        env=box.env,
        capture_output=True,
        text=True,
        check=False,
    )


def _installs(box: Box) -> list[str]:
    """Every `uv tool install` the fake uv was asked to run."""
    lines = box.log.read_text().splitlines() if box.log.exists() else []
    return [line for line in lines if line.startswith("tool install")]


def _plan(out: str) -> dict[str, str]:
    """The dry run's `  <label>    <value>` lines, by label."""
    rows = [
        line.split(maxsplit=1)
        for line in out.splitlines()
        if line.startswith("  ") and not line.startswith("   ")
    ]
    return {row[0]: row[1] for row in rows if len(row) == 2}


def _items(out: str) -> list[str]:
    """The `    - <item>` lines a refusal or a drift report lists."""
    return [line[6:] for line in out.splitlines() if line.startswith("    - ")]


def _change_pyproject(box: Box, old: str, new: str) -> None:
    """Commit a pyproject-only change, the kind no package file shows."""
    path = box.repo / "pyproject.toml"
    text = path.read_text()
    assert text.count(old) == 1
    path.write_text(text.replace(old, new))
    _commit(box.repo, "pyproject only")


def _stub(where: Path) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    stub = where / _TOOL
    stub.write_text("#!/bin/sh\necho shadow\n")
    stub.chmod(0o755)
    return stub


def _install_faithfully(box: Box) -> None:
    subprocess.run(
        [str(box.uv), "tool", "install", "--reinstall", str(box.repo.resolve())],
        env=box.env,
        check=True,
    )


# --- The dry run --------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("source", lambda b: str(b.repo.resolve())),
        ("commit", lambda b: f"{_git(b.repo, 'rev-parse', 'HEAD')} (main)"),
        (
            "target",
            lambda b: (
                f"{b.tools / 'projects-orchestrator'}, entrypoint {b.bin / 'projects-orchestrator'}"
            ),
        ),
        ("command", lambda b: f"{b.uv} tool install --reinstall {b.repo.resolve()}"),
    ],
)
def test_install_dry_run_prints_the_plan_line(box: Box, label: str, expected) -> None:
    assert _plan(_script(box).stdout).get(label) == expected(box)


def test_install_dry_run_runs_no_install(box: Box) -> None:
    _script(box)
    assert _installs(box) == []


def test_install_dry_run_creates_no_tool_env(box: Box) -> None:
    _script(box)
    assert not box.tools.exists()


def test_install_dry_run_on_a_clean_synced_main_says_apply_would_proceed(box: Box) -> None:
    assert _script(box).stdout.splitlines()[-1].startswith("  --apply would proceed")


def test_install_dry_run_lists_what_apply_would_refuse(box: Box) -> None:
    _git(box.repo, "checkout", "-q", "-b", "feat/x")
    assert _items(_script(box).stdout) == [
        "on 'feat/x', not main: an unmerged branch is unreviewed text"
    ]


def test_install_dry_run_exits_0_on_a_clean_synced_main(box: Box) -> None:
    assert _script(box).returncode == 0


def test_install_dry_run_exits_1_when_apply_would_refuse(box: Box) -> None:
    # A caller that plans on the dry run must see the refusal before anything is written.
    _git(box.repo, "checkout", "-q", "-b", "feat/x")
    assert _script(box).returncode == 1


def test_install_dry_run_exits_0_when_a_session_is_the_only_reason(box: Box) -> None:
    # Dry runs are expected inside a session, and deploy checks for one itself.
    box.env["CLAUDECODE"] = "1"
    assert _script(box).returncode == 0


def test_install_dry_run_still_lists_a_session_as_a_reason(box: Box) -> None:
    box.env["CLAUDECODE"] = "1"
    assert _items(_script(box).stdout) == [
        "inside a Claude Code session: run `just install --apply` from a terminal"
    ]


def test_install_dry_run_exits_1_when_a_session_is_not_the_only_reason(box: Box) -> None:
    box.env["CLAUDECODE"] = "1"
    _git(box.repo, "checkout", "-q", "-b", "feat/x")
    assert _script(box).returncode == 1


def test_just_install_with_no_flag_is_the_dry_run(box: Box) -> None:
    done = subprocess.run(
        ["just", "install"], cwd=box.repo, env=box.env, capture_output=True, text=True, check=False
    )
    assert (
        done.stdout.splitlines()[0] == "projects-orchestrator box install: DRY RUN, nothing written"
    )


def test_just_install_runs_uv_without_python_downloads(box: Box) -> None:
    # A dry run that fetched an interpreter into uv's cache would not be "nothing written".
    subprocess.run(["just", "install"], cwd=box.repo, env=box.env, capture_output=True, check=False)
    runs = [line.split() for line in box.log.read_text().splitlines() if line.startswith("run ")]
    assert "--no-python-downloads" in runs[0]


def test_just_install_asks_uv_for_a_python_with_tomllib(box: Box) -> None:
    # `--no-project` sets no Python floor, so the recipe states the script's own.
    subprocess.run(["just", "install"], cwd=box.repo, env=box.env, capture_output=True, check=False)
    runs = [line.split() for line in box.log.read_text().splitlines() if line.startswith("run ")]
    assert runs[0][runs[0].index("--python") + 1] == ">=3.11"


def _offline_real_uv(box: Box, tmp_path: Path) -> None:
    """The real uv first on PATH, offline, with an empty cache: a sync can only fail."""
    assert _REAL_UV
    dirs = [str(Path(_REAL_UV).parent), str(Path(sys.executable).parent), box.env["PATH"]]
    box.env |= {
        "PATH": os.pathsep.join(dirs),
        "UV_OFFLINE": "1",
        "UV_CACHE_DIR": str(tmp_path / "empty-uv-cache"),
    }


_NEEDS_JUST_AND_UV = pytest.mark.skipif(
    shutil.which("just") is None or _REAL_UV is None, reason="just or uv missing"
)
_READ_ONLY_MODES = pytest.mark.parametrize(
    ("args", "ran"),
    [((), "DRY RUN, nothing written"), (("--check",), "not installed:")],
    ids=["dry-run", "check"],
)


def _just_install(box: Box, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["just", "install", *args],
        cwd=box.repo,
        env=box.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@_NEEDS_JUST_AND_UV
@pytest.mark.parametrize("args", [(), ("--check",)], ids=["dry-run", "check"])
def test_just_install_creates_no_venv(box: Box, tmp_path: Path, args: tuple[str, ...]) -> None:
    # A project `uv run` syncs first: it creates .venv and installs into it.
    _offline_real_uv(box, tmp_path)
    _just_install(box, *args)
    assert not (box.repo / ".venv").exists()


@_NEEDS_JUST_AND_UV
@_READ_ONLY_MODES
def test_just_install_runs_offline_from_a_fresh_checkout(
    box: Box, tmp_path: Path, args: tuple[str, ...], ran: str
) -> None:
    _offline_real_uv(box, tmp_path)
    done = _just_install(box, *args)
    assert ran in done.stdout + done.stderr


def test_just_install_passes_the_script_exit_code_through(box: Box) -> None:
    done = subprocess.run(
        ["just", "install", "--bogus"], cwd=box.repo, env=box.env, capture_output=True, check=False
    )
    assert done.returncode == 2


# --- --apply refuses anything but the main worktree on a clean, synced main ----


def _on_a_branch(box: Box) -> None:
    _git(box.repo, "checkout", "-q", "-b", "feat/x")


def _detached(box: Box) -> None:
    _git(box.repo, "checkout", "-q", "--detach")


def _dirty(box: Box) -> None:
    (box.repo / "src" / _PKG / "cli.py").write_text("def main() -> int:\n    return 1\n")


def _untracked(box: Box) -> None:
    (box.repo / "src" / _PKG / "new.py").write_text("")


def _ahead(box: Box) -> None:
    (box.repo / "src" / _PKG / "cli.py").write_text("# unpushed\n")
    _commit(box.repo, "unpushed")


def _behind(box: Box) -> None:
    other = box.repo.parent / "other"
    _git(box.repo.parent, "clone", "-q", str(box.origin), str(other))
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    (other / "src" / _PKG / "cli.py").write_text("# merged elsewhere\n")
    _commit(other, "merged elsewhere")
    _git(other, "push", "-q", "origin", "main")


def _no_origin(box: Box) -> None:
    _git(box.repo, "remote", "set-url", "origin", str(box.repo.parent / "gone.git"))


def _in_a_session(box: Box) -> None:
    box.env["CLAUDECODE"] = "1"


# ls-files -v label -> the update-index flags that set it. Set one call per flag:
# update-index applies only one of the two when both are given in one call.
_HIDING = {
    "skip-worktree": ("--skip-worktree",),
    "assume-unchanged": ("--assume-unchanged",),
    "skip-worktree, assume-unchanged": ("--skip-worktree", "--assume-unchanged"),
}
_HIDDEN_REFUSAL = (
    "files git status skips would be installed unreviewed. Clear each flag with "
    "`git update-index --no-skip-worktree -- <file>` or "
    "`git update-index --no-assume-unchanged -- <file>`, one call per flag:"
)


def _hide(box: Box, label: str) -> None:
    """Edit a packaged file that git status no longer checks (project-init#1047 review)."""
    for flag in _HIDING[label]:
        _git(box.repo, "update-index", flag, f"src/{_PKG}/cli.py")
    (box.repo / "src" / _PKG / "cli.py").write_text("def main() -> int:\n    return 1\n")
    assert _git(box.repo, "status", "--porcelain", "--untracked-files=all") == ""


# Every place an ignore rule can live. Each hides the file from git status.
_IGNORE_FILES = (".gitignore", f"src/{_PKG}/.gitignore", ".git/info/exclude")
_CLEAN = f"git clean -fdX -- src/{_PKG}"
_IGNORED_REFUSAL = (
    "ignored files the wheel packages would be installed unreviewed. Preview with "
    f"`git clean -ndX -- src/{_PKG}`, then delete them with `{_CLEAN}`:"
)


def _ignore(box: Box, rules: str, where: str) -> None:
    path = box.repo / where
    path.write_text(path.read_text() + rules if path.exists() else rules)
    if not where.startswith(".git/"):
        _commit(box.repo, "ignore")
        _git(box.repo, "push", "-q")


def _ignored(box: Box, where: str = ".git/info/exclude") -> None:
    """Leave an ignored file where the wheel packages it, and a bytecode cache it never does."""
    _ignore(box, "__pycache__/\n", ".git/info/exclude")
    _ignore(box, "*.local\n", where)
    (box.repo / "src" / _PKG / "extra.local").write_text("unreviewed\n")
    (box.repo / "src" / _PKG / "__pycache__").mkdir()
    (box.repo / "src" / _PKG / "__pycache__" / "cli.cpython-313.pyc").write_bytes(b"\0")
    assert _git(box.repo, "status", "--porcelain", "--untracked-files=all") == ""


_REFUSALS = {
    "branch": (_on_a_branch, "on 'feat/x', not main"),
    "detached": (_detached, "on 'detached HEAD', not main"),
    # Porcelain's leading space is kept: " M", not "M".
    "dirty": (
        _dirty,
        f"uncommitted changes would be installed unreviewed:\n       M src/{_PKG}/cli.py",
    ),
    "untracked": (_untracked, "uncommitted changes would be installed unreviewed:\n      ?? src/"),
    "ahead": (_ahead, "not in sync with origin/main (ahead 1, behind 0)"),
    "behind": (_behind, "not in sync with origin/main (ahead 0, behind 1)"),
    "no-origin": (_no_origin, "cannot fetch origin/main"),
    "session": (_in_a_session, "inside a Claude Code session"),
    "ignored": (_ignored, f"{_IGNORED_REFUSAL}\n      src/{_PKG}/extra.local"),
    **{
        label: (
            lambda box, label=label: _hide(box, label),
            f"{_HIDDEN_REFUSAL}\n      {label}: src/{_PKG}/cli.py",
        )
        for label in _HIDING
    },
}


def _refusal_text(stderr: str) -> str:
    """The refusal's items, rejoined so a multi-line item reads as printed."""
    head, _, rest = stderr.partition("\n")
    return rest if head == "refusing --apply:" else ""


@pytest.mark.parametrize("scenario", sorted(_REFUSALS))
def test_install_apply_refuses(box: Box, scenario: str) -> None:
    _REFUSALS[scenario][0](box)
    assert _script(box, "--apply").returncode == 1


@pytest.mark.parametrize("scenario", sorted(_REFUSALS))
def test_install_apply_refusal_installs_nothing(box: Box, scenario: str) -> None:
    _REFUSALS[scenario][0](box)
    _script(box, "--apply")
    assert _installs(box) == []


@pytest.mark.parametrize("scenario", sorted(_REFUSALS))
def test_install_apply_refusal_names_the_reason(box: Box, scenario: str) -> None:
    setup, reason = _REFUSALS[scenario]
    setup(box)
    assert f"    - {reason}" in _refusal_text(_script(box, "--apply").stderr)


@pytest.mark.parametrize("label", sorted(_HIDING))
def test_install_dry_run_exits_1_on_an_edit_git_status_skips(box: Box, label: str) -> None:
    _hide(box, label)
    assert _script(box).returncode == 1


@pytest.mark.parametrize("label", sorted(_HIDING))
def test_install_dry_run_lists_an_edit_git_status_skips(box: Box, label: str) -> None:
    _hide(box, label)
    shown = f"    - {_HIDDEN_REFUSAL}\n      {label}: src/{_PKG}/cli.py\n"
    assert shown in _script(box).stdout


@pytest.mark.parametrize("label", sorted(_HIDING))
def test_install_apply_names_the_commands_that_clear_the_flags(box: Box, label: str) -> None:
    # Run as the refusal says, one call per flag; status then sees the edit.
    _hide(box, label)
    for flag in _HIDING[label]:
        _git(box.repo, "update-index", f"--no-{flag[2:]}", "--", f"src/{_PKG}/cli.py")
    assert _items(_script(box, "--apply").stderr) == [
        "uncommitted changes would be installed unreviewed:"
    ]


@pytest.mark.parametrize("where", _IGNORE_FILES)
def test_install_apply_names_only_the_ignored_file_the_wheel_packages(box: Box, where: str) -> None:
    """status reads clean, yet the build ships the ignored file (project-init#1047 review)."""
    _ignored(box, where)
    assert _items(_script(box, "--apply").stderr) == [_IGNORED_REFUSAL]
    assert f"    - {_IGNORED_REFUSAL}\n      src/{_PKG}/extra.local\n" in _script(box).stdout


@pytest.mark.parametrize("where", _IGNORE_FILES)
def test_install_dry_run_proceeds_once_the_named_command_ran(box: Box, where: str) -> None:
    _ignored(box, where)
    subprocess.run(_CLEAN.split(), cwd=box.repo, check=True, capture_output=True)
    dry = _script(box)
    assert dry.returncode == 0, dry.stdout


def test_install_ignored_file_check_reads_the_packaged_paths_from_pyproject(box: Box) -> None:
    _ignore(box, "*.local\n", ".git/info/exclude")
    (box.repo / "assets").mkdir()
    (box.repo / "assets" / "extra.local").write_text("unreviewed\n")
    assert _script(box).returncode == 0  # not packaged, so no reason to refuse
    forced = '[tool.hatch.build.targets.wheel.force-include]\n"assets" = "projects_orchestrator/assets"\n'
    _change_pyproject(
        box, "[tool.hatch.build.targets.wheel]\n", forced + "\n[tool.hatch.build.targets.wheel]\n"
    )
    _git(box.repo, "push", "-q")
    dry = _script(box)
    assert dry.returncode == 1 and "\n      assets/extra.local\n" in dry.stdout, dry.stdout


def _linked_worktree(box: Box) -> Path:
    # --force lets a second worktree hold `main`, so every OTHER gate passes there.
    wt = box.repo.parent / "wt"
    _git(box.repo, "worktree", "add", "-q", "--force", str(wt), "main")
    return wt


def test_install_apply_refuses_a_linked_worktree(box: Box) -> None:
    wt = _linked_worktree(box)
    assert _items(_script(box, "--apply", cwd=wt).stderr) == [
        f"linked worktree: install from the main checkout, {box.repo.resolve()}"
    ]


def test_install_apply_from_a_linked_worktree_installs_nothing(box: Box) -> None:
    _script(box, "--apply", cwd=_linked_worktree(box))
    assert _installs(box) == []


# --- --apply on a clean, synced main -----------------------------------------


def test_install_apply_reinstalls_the_checkout(box: Box) -> None:
    _script(box, "--apply")
    assert _installs(box) == [f"tool install --reinstall {box.repo.resolve()}"]


def test_install_apply_verifies_what_it_installed(box: Box) -> None:
    assert _script(box, "--apply").returncode == 0


def test_install_apply_fails_when_uv_fails(box: Box) -> None:
    box.env["FAKE_UV_INSTALL_RC"] = "3"
    assert _script(box, "--apply").returncode == 1


def test_install_apply_names_a_failed_uv_run_as_the_failure(box: Box) -> None:
    # Not only the check that follows: that would report "not installed" instead.
    box.env["FAKE_UV_INSTALL_RC"] = "3"
    assert _script(box, "--apply").stderr.splitlines()[0] == "failed:"


def test_install_apply_fails_when_the_build_does_not_match_head(box: Box) -> None:
    box.env["FAKE_UV_CORRUPT"] = "1"
    assert _script(box, "--apply").returncode == 1


# --- --check -------------------------------------------------------------------


def test_install_check_passes_on_a_faithful_install(box: Box) -> None:
    _install_faithfully(box)
    assert _script(box, "--check").returncode == 0


def test_install_check_fails_on_a_modified_installed_file(box: Box) -> None:
    _install_faithfully(box)
    (box.site / "cli.py").write_text("# edited in place\n")
    assert _script(box, "--check").returncode == 1


def test_install_check_names_a_modified_installed_file(box: Box) -> None:
    _install_faithfully(box)
    (box.site / "cli.py").write_text("# edited in place\n")
    assert _items(_script(box, "--check").stderr) == [
        f"modified: {_PKG}/cli.py (tree: src/{_PKG}/cli.py)"
    ]


def test_install_check_names_a_missing_installed_file(box: Box) -> None:
    _install_faithfully(box)
    (box.site / "cli.py").unlink()
    assert _items(_script(box, "--check").stderr) == [
        f"missing: {_PKG}/cli.py (tree: src/{_PKG}/cli.py)"
    ]


def test_install_check_names_an_installed_file_the_tree_dropped(box: Box) -> None:
    _install_faithfully(box)
    (box.site / "stale.py").write_text("")
    assert _items(_script(box, "--check").stderr) == [f"not in tree: {_PKG}/stale.py"]


def test_install_check_reports_a_commit_the_build_lacks(box: Box) -> None:
    # The case #317 exists for: pulled, never reinstalled.
    _install_faithfully(box)
    (box.repo / "src" / _PKG / "cli.py").write_text("# merged fix\n")
    _commit(box.repo, "merged fix")
    assert _items(_script(box, "--check").stderr) == [
        f"modified: {_PKG}/cli.py (tree: src/{_PKG}/cli.py)"
    ]


def test_install_check_compares_head_not_the_working_tree(box: Box) -> None:
    _install_faithfully(box)
    (box.repo / "src" / _PKG / "cli.py").write_text("# uncommitted edit\n")
    assert _script(box, "--check").returncode == 0


def test_install_check_passes_on_a_faithful_install_in_a_sha256_repository(
    sha256_box: Box,
) -> None:
    # git computes the blob ids, so they follow the repository's object format.
    _install_faithfully(sha256_box)
    assert _script(sha256_box, "--check").returncode == 0


def test_install_check_names_a_modified_installed_file_in_a_sha256_repository(
    sha256_box: Box,
) -> None:
    _install_faithfully(sha256_box)
    (sha256_box.site / "cli.py").write_text("# edited in place\n")
    assert _items(_script(sha256_box, "--check").stderr) == [
        f"modified: {_PKG}/cli.py (tree: src/{_PKG}/cli.py)"
    ]


def _autocrlf_checkout(box: Box) -> None:
    """Git for Windows' defaults: `* text=auto` committed, core.autocrlf=true, a fresh checkout.

    The wheel copies the working tree, so it is CRLF while the committed blobs are LF.
    """
    (box.repo / ".gitattributes").write_text("* text=auto\n*.sh text eol=lf\n")
    (box.repo / "src" / _PKG / "hook.sh").write_text("#!/bin/sh\necho hi\n")
    _commit(box.repo, "text=auto")
    _git(box.repo, "push", "-q", "origin", "main")
    _git(box.repo, "config", "core.autocrlf", "true")
    for rel in _git(box.repo, "ls-files").splitlines():
        (box.repo / rel).unlink()
    _git(box.repo, "checkout", "--", ".")
    assert b"\r\n" in (box.repo / "src" / _PKG / "cli.py").read_bytes()
    assert b"\r\n" not in (box.repo / "src" / _PKG / "hook.sh").read_bytes()


def test_install_check_passes_an_install_built_from_a_crlf_checkout(box: Box) -> None:
    _autocrlf_checkout(box)
    _install_faithfully(box)
    assert _items(_script(box, "--check").stderr) == []


def test_install_apply_verifies_an_install_from_a_crlf_checkout(box: Box) -> None:
    _autocrlf_checkout(box)
    assert _script(box, "--apply").returncode == 0


def test_install_check_still_names_crlf_in_an_eol_lf_file_under_autocrlf(box: Box) -> None:
    # A checkout writes `*.sh text eol=lf` as LF even here, so CRLF is not its form.
    _autocrlf_checkout(box)
    _install_faithfully(box)
    hook = box.site / "hook.sh"
    hook.write_bytes(hook.read_bytes().replace(b"\n", b"\r\n"))
    assert _items(_script(box, "--check").stderr) == [
        f"modified: {_PKG}/hook.sh (tree: src/{_PKG}/hook.sh)"
    ]


def test_install_check_still_names_an_edit_under_autocrlf(box: Box) -> None:
    _autocrlf_checkout(box)
    _install_faithfully(box)
    (box.site / "cli.py").write_bytes(b"def main() -> int:\r\n    return 1\r\n")
    assert _items(_script(box, "--check").stderr) == [
        f"modified: {_PKG}/cli.py (tree: src/{_PKG}/cli.py)"
    ]


@pytest.mark.parametrize(
    ("name", "mode", "want"),
    [
        ("run.sh", 0o644, "installed 644, tree 100755"),
        ("cli.py", 0o755, "installed 755, tree 100644"),
    ],
    ids=["exec-bit-dropped", "exec-bit-added"],
)
def test_install_check_names_an_installed_file_whose_exec_bit_differs(
    box: Box, name: str, mode: int, want: str
) -> None:
    # The same blob with another exec bit: a scaffold copies a file's exec bit along.
    _install_faithfully(box)
    (box.site / name).chmod(mode)
    assert _items(_script(box, "--check").stderr) == [
        f"mode: {_PKG}/{name} {want} (tree: src/{_PKG}/{name})"
    ]


def test_install_check_names_an_exec_bit_only_head_changed(box: Box) -> None:
    _install_faithfully(box)
    _git(box.repo, "update-index", "--chmod=-x", f"src/{_PKG}/run.sh")
    _git(box.repo, "commit", "-q", "-m", "run.sh is not executable")
    assert _items(_script(box, "--check").stderr) == [
        f"mode: {_PKG}/run.sh installed 755, tree 100644 (tree: src/{_PKG}/run.sh)"
    ]


def test_install_check_names_what_the_installed_record_owns(box: Box) -> None:
    # A build from before HEAD dropped a wheel mapping left files HEAD never names.
    _install_faithfully(box)
    site = box.site.parent
    (site / "retired_pkg").mkdir()
    (site / "retired_pkg" / "old.py").write_text("stale\n")
    (site / "retired.py").write_text("stale\n")
    record = site / f"{_PKG}-0.0.0.dist-info" / "RECORD"
    record.write_text(record.read_text() + "retired_pkg/old.py,,\nretired.py,,\n")
    assert _items(_script(box, "--check").stderr) == [
        "not in tree: retired.py",
        "not in tree: retired_pkg/old.py",
    ]


def test_install_check_names_an_install_without_a_record(box: Box) -> None:
    _install_faithfully(box)
    (box.site.parent / f"{_PKG}-0.0.0.dist-info" / "RECORD").unlink()
    assert _items(_script(box, "--check").stderr) == [
        f"record: {_PKG}-0.0.0.dist-info has no RECORD, so what it installed is unknown"
    ]


_DIST = f"{_PKG}-0.0.0.dist-info"
_UNCOMPARED = ", so the installed metadata is not compared"


@pytest.mark.parametrize(
    ("name", "damage", "line"),
    [
        ("METADATA", None, f"{_DIST}/METADATA cannot be read (No such file or directory)"),
        ("METADATA", b"\xff\xfe", f"{_DIST}/METADATA cannot be read (not UTF-8)"),
        ("entry_points.txt", b"\xff", f"{_DIST}/entry_points.txt cannot be read (not UTF-8)"),
        ("RECORD", b"\xff", f"{_DIST}/RECORD cannot be read (not UTF-8)"),
    ],
    ids=["no-metadata", "binary-metadata", "binary-entry-points", "binary-record"],
)
def test_install_check_reports_an_unreadable_dist_info_file_and_carries_on(
    box: Box, name: str, damage: bytes | None, line: str
) -> None:
    # One drift line, never a traceback, and the file comparison still runs (#318 review).
    _install_faithfully(box)
    path = box.site.parent / _DIST / name
    if damage is None:
        path.unlink()
    else:
        path.write_bytes(damage)
    (box.site / "cli.py").write_text("# edited in place\n")
    first = (
        f"record: {line}, so what it installed is unknown"
        if name == "RECORD"
        else f"metadata: {line}{_UNCOMPARED}"
    )
    assert _items(_script(box, "--check").stderr) == [
        first,
        f"modified: {_PKG}/cli.py (tree: src/{_PKG}/cli.py)",
    ]


def test_install_check_ignores_bytecode_caches(box: Box) -> None:
    _install_faithfully(box)
    (box.site / "__pycache__").mkdir()
    (box.site / "__pycache__" / "cli.cpython-313.pyc").write_bytes(b"\0")
    assert _script(box, "--check").returncode == 0


def test_install_check_reports_not_installed(box: Box) -> None:
    assert _items(_script(box, "--check").stderr) == [
        f"not installed: {box.tools / 'projects-orchestrator'} does not exist (run `just install --apply`)"
    ]


def test_install_check_reports_a_build_from_another_checkout(box: Box) -> None:
    _install_faithfully(box)
    receipt = box.tools / "projects-orchestrator" / "uv-receipt.toml"
    receipt.write_text(receipt.read_text().replace(str(box.repo.resolve()), "/elsewhere"))
    assert _items(_script(box, "--check").stderr) == [
        f"source: installed from /elsewhere, not this checkout ({box.repo.resolve()})"
    ]


def test_install_check_reports_a_broken_entrypoint(box: Box) -> None:
    _install_faithfully(box)
    (box.bin / _TOOL).unlink()
    assert _items(_script(box, "--check").stderr) == [
        f"entrypoint: {box.bin / _TOOL} does not resolve to {box.scripts / _TOOL}",
        f"PATH: no {_TOOL} on PATH",
    ]


# --- --check: the command PATH selects -----------------------------------------


def test_install_check_names_a_command_on_path_that_shadows_the_tool(
    box: Box, tmp_path: Path
) -> None:
    _install_faithfully(box)
    stub = _stub(tmp_path / "shadow")
    box.env["PATH"] = f"{stub.parent}{os.pathsep}{box.env['PATH']}"
    assert _items(_script(box, "--check").stderr) == [
        f"PATH: {_TOOL} runs {stub}, which shadows {box.scripts / _TOOL}"
    ]


def _venv_with_the_tool(venv: Path) -> Path:
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    return _stub(venv / "bin")


def _uv_run_check(box: Box, cwd: Path, *, nested: bool = False) -> subprocess.CompletedProcess[str]:
    """`--check` the way the recipe runs it: under the real `uv run --no-project`."""
    assert _REAL_UV
    run = [_REAL_UV, "run", "--no-python-downloads", "--no-project", "--python", ">=3.11"]
    script = box.repo / ".agents" / "scripts" / "install_tool.py"
    return subprocess.run(
        [*run, *(run if nested else []), "python", str(script), "--check"],
        cwd=cwd,
        env=box.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.skipif(_REAL_UV is None, reason="uv missing")
@pytest.mark.parametrize("nested", [False, True], ids=["uv-run", "uv-run-in-uv-run"])
def test_install_check_ignores_the_venv_uv_run_puts_first_on_path(
    box: Box, tmp_path: Path, nested: bool
) -> None:
    # `uv run` finds the cwd's .venv, never activated, and prepends its bin to PATH:
    # its dev entrypoint is not what a shell runs.
    _install_faithfully(box)
    _venv_with_the_tool(tmp_path / "work" / ".venv")
    done = _uv_run_check(box, tmp_path / "work", nested=nested)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.skipif(_REAL_UV is None, reason="uv missing")
def test_install_check_names_the_tool_in_a_venv_the_caller_activated(
    box: Box, tmp_path: Path
) -> None:
    # An activated venv was first on the shell's PATH before `uv run` prepended it again.
    _install_faithfully(box)
    stub = _venv_with_the_tool(tmp_path / "active")
    box.env |= {
        "VIRTUAL_ENV": str(tmp_path / "active"),
        "PATH": f"{stub.parent}{os.pathsep}{box.env['PATH']}",
    }
    assert _items(_uv_run_check(box, tmp_path).stderr) == [
        f"PATH: {_TOOL} runs {stub}, which shadows {box.scripts / _TOOL}"
    ]


def test_install_check_reports_the_tool_missing_from_path(box: Box) -> None:
    _install_faithfully(box)
    dirs = box.env["PATH"].split(os.pathsep)
    box.env["PATH"] = os.pathsep.join(d for d in dirs if d != str(box.bin))
    assert _items(_script(box, "--check").stderr) == [f"PATH: no {_TOOL} on PATH"]


# --- --check: the installed metadata against HEAD's pyproject ------------------


def test_install_check_reports_a_dependency_only_heads_pyproject_has(box: Box) -> None:
    _install_faithfully(box)
    _change_pyproject(box, '["pyyaml>=6.0"]', '["pyyaml>=6.0", "rich>=13"]')
    assert _items(_script(box, "--check").stderr) == [
        "metadata: dependency rich>=13 is in pyproject, not installed"
    ]


def test_install_check_reports_a_console_script_head_changed(box: Box) -> None:
    _install_faithfully(box)
    _change_pyproject(box, '"projects_orchestrator.cli:main"', '"projects_orchestrator.cli:run"')
    assert _items(_script(box, "--check").stderr) == [
        "metadata: console script projects-orchestrator ="
        " projects_orchestrator.cli:run is in pyproject, not installed",
        "metadata: console script projects-orchestrator ="
        " projects_orchestrator.cli:main is installed, not in pyproject",
    ]


def test_install_check_reports_a_version_only_heads_pyproject_bumped(box: Box) -> None:
    _install_faithfully(box)
    _change_pyproject(box, 'version = "0.0.0"', 'version = "0.0.1"')
    assert _items(_script(box, "--check").stderr) == [
        "metadata: Version installed 0.0.0, pyproject says 0.0.1",
    ]


def _dynamic_version(box: Box) -> None:
    # This repo's own layout: hatch reads the version out of the package's __init__.py.
    path = box.repo / "pyproject.toml"
    text = path.read_text().replace('version = "0.0.0"\n', 'dynamic = ["version"]\n')
    hatch = '[tool.hatch.version]\npath = "src/projects_orchestrator/__init__.py"\n\n'
    path.write_text(text.replace("[tool.hatch.build", hatch + "[tool.hatch.build"))
    _commit(box.repo, "dynamic version")


def test_install_check_passes_a_dynamic_version_hatch_reads_from_a_file(box: Box) -> None:
    _dynamic_version(box)
    _install_faithfully(box)
    assert _script(box, "--check").returncode == 0


def test_install_check_reports_a_dynamic_version_head_bumped(box: Box) -> None:
    _dynamic_version(box)
    _install_faithfully(box)
    (box.repo / "src" / _PKG / "__init__.py").write_text('__version__ = "0.0.1"\n')
    _commit(box.repo, "bump")
    assert _items(_script(box, "--check").stderr) == [
        "metadata: Version installed 0.0.0, pyproject says 0.0.1",
        f"modified: {_PKG}/__init__.py (tree: src/{_PKG}/__init__.py)",
    ]


# --- --check: the env's layout comes from its own interpreter -------------------


def test_install_check_asks_the_env_interpreter_for_its_layout(box: Box) -> None:
    # A Windows-shaped env (Lib/site-packages, Scripts/) that no POSIX glob would find.
    box.env["FAKE_UV_LAYOUT"] = "windows"
    _install_faithfully(box)
    assert _script(box, "--check").returncode == 0


# --- Usage --------------------------------------------------------------------


def test_install_unknown_flag_is_a_usage_error(box: Box) -> None:
    assert _script(box, "--bogus").returncode == 2


def test_install_apply_and_check_together_is_a_usage_error(box: Box) -> None:
    assert _script(box, "--apply", "--check").returncode == 2
