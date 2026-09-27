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

# argv logged per call; `tool install` copies the package the way a build would,
# and FAKE_UV_CORRUPT makes the copy differ from the tree it was built from.
_FAKE_UV = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_UV_LOG"
if [ "$1" = run ]; then shift 3; exec "$FAKE_PYTHON" "$@"; fi
if [ "$1 $2" = "tool dir" ]; then
  if [ "${3:-}" = --bin ]; then printf '%s\\n' "$UV_TOOL_BIN_DIR"; else printf '%s\\n' "$UV_TOOL_DIR"; fi
  exit 0
fi
if [ "$1 $2 $3" = "tool install --reinstall" ]; then
  [ "${FAKE_UV_INSTALL_RC:-0}" = 0 ] || exit "$FAKE_UV_INSTALL_RC"
  env="$UV_TOOL_DIR/projects-orchestrator"; site="$env/lib/python3.13/site-packages"
  rm -rf "$env"; mkdir -p "$site" "$env/bin" "$UV_TOOL_BIN_DIR"
  cp -R "$4/src/projects_orchestrator" "$site/"
  [ -z "${FAKE_UV_CORRUPT:-}" ] || printf '# stale\\n' >> "$site/projects_orchestrator/cli.py"
  : > "$env/bin/projects-orchestrator"
  ln -sf "$env/bin/projects-orchestrator" "$UV_TOOL_BIN_DIR/projects-orchestrator"
  printf '[tool]\\nrequirements = [{ name = "projects-orchestrator", directory = "%s" }]\\n' "$4" > "$env/uv-receipt.toml"
  printf 'entrypoints = [{ name = "projects-orchestrator", install-path = "%s" }]\\n' \\
    "$UV_TOOL_BIN_DIR/projects-orchestrator" >> "$env/uv-receipt.toml"
  exit 0
fi
exit 99
"""

_PYPROJECT = """\
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
        return self.tools / "projects-orchestrator" / "lib" / "python3.13" / "site-packages" / _PKG


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def box(tmp_path: Path) -> Box:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "checkout"
    (repo / "src" / _PKG).mkdir(parents=True)
    (repo / "src" / _PKG / "__init__.py").write_text('__version__ = "0.0.0"\n')
    (repo / "src" / _PKG / "cli.py").write_text("def main() -> int:\n    return 0\n")
    (repo / "pyproject.toml").write_text(_PYPROJECT)
    (repo / ".agents" / "scripts").mkdir(parents=True)
    shutil.copy2(_SCRIPT, repo / ".agents" / "scripts" / "install_tool.py")
    shutil.copy2(_REPO / "justfile", repo / "justfile")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    _git(repo.parent, "init", "-q", "-b", "main", str(repo))
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
    env |= {
        "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
        "FAKE_UV_LOG": str(tmp_path / "uv.log"),
        "FAKE_PYTHON": sys.executable,
    }
    return Box(
        repo, origin, tmp_path / "tools", tmp_path / "bin", fake / "uv", tmp_path / "uv.log", env
    )


def _script(box: Box, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    script = (cwd or box.repo) / ".agents" / "scripts" / "install_tool.py"
    return subprocess.run(
        [sys.executable, str(script), *args],
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


def test_just_install_with_no_flag_is_the_dry_run(box: Box) -> None:
    done = subprocess.run(
        ["just", "install"], cwd=box.repo, env=box.env, capture_output=True, text=True, check=False
    )
    assert (
        done.stdout.splitlines()[0] == "projects-orchestrator box install: DRY RUN, nothing written"
    )


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
    "dirty": (_dirty, "uncommitted changes would be installed unreviewed:"),
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
    (box.bin / "projects-orchestrator").unlink()
    link, want = box.bin / "projects-orchestrator", box.tools / "projects-orchestrator" / "bin"
    assert _items(_script(box, "--check").stderr) == [
        f"entrypoint: {link} does not resolve to {want / 'projects-orchestrator'}"
    ]


# --- Usage --------------------------------------------------------------------


def test_install_unknown_flag_is_a_usage_error(box: Box) -> None:
    assert _script(box, "--bogus").returncode == 2


def test_install_apply_and_check_together_is_a_usage_error(box: Box) -> None:
    assert _script(box, "--apply", "--check").returncode == 2
