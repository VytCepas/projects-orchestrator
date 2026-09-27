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

# argv logged per call. `run` drops uv's own flags and runs the script; `python
# find <env>` names the env's interpreter the way uv does; `tool
# install` builds a real venv (FAKE_UV_LAYOUT=windows: a Lib/ + Scripts/ one whose
# interpreter reports those paths), copies the package, and writes the dist-info a
# build of the fixture pyproject would. FAKE_UV_CORRUPT makes the copy differ.
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
    env = {k: v for k, v in os.environ.items() if k not in {"CLAUDECODE", "VIRTUAL_ENV"}}
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


def test_just_install_runs_the_script_in_the_project_venv(box: Box) -> None:
    # `--no-project` sets no Python floor, so uv may pick an interpreter with no tomllib.
    subprocess.run(["just", "install"], cwd=box.repo, env=box.env, capture_output=True, check=False)
    runs = [line.split() for line in box.log.read_text().splitlines() if line.startswith("run ")]
    assert "--no-project" not in runs[0]


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


def test_install_check_ignores_the_venv_uv_run_puts_first_on_path(box: Box, tmp_path: Path) -> None:
    # `uv run` runs the script in the project's venv and prepends that venv's bin,
    # whose dev entrypoint is not what a shell runs.
    _install_faithfully(box)
    venv = tmp_path / "devvenv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    stub = _stub(venv / "bin")
    box.env["PATH"] = f"{stub.parent}{os.pathsep}{box.env['PATH']}"
    assert _script(box, "--check", python=str(venv / "bin" / "python")).returncode == 0


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
