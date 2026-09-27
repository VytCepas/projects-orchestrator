#!/usr/bin/env python3
"""Install this checkout as the machine's ``projects-orchestrator`` uv tool, or check it (#317).

``just install`` is a dry run. It prints the source, commit, target and command,
and writes nothing. It exits 1 when ``--apply`` would refuse, unless the only
reason is a Claude Code session. ``just install --apply`` runs ``uv tool install --reinstall
<repo>``, and only from a clean ``main`` in sync with ``origin/main``.
``just install --check`` compares the installed package files with HEAD and
exits 1 on drift, naming each file.

The command on PATH runs uv's copy of the checkout, never the tree, and
``--version`` does not move between releases (#191), so a pull without a
reinstall used to leave a stale build that nothing reported.

The layout is uv's own. ``uv tool dir`` holds one env per tool,
``<env>/uv-receipt.toml`` records the source and entrypoints, and the package
sits in ``<env>/lib/python3.X/site-packages``. The tree-to-wheel mapping is read
from pyproject's hatch config, never restated here.

Exit codes: 0 ok, 1 refused, failed or drifted, 2 usage. Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import TextIO

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL = "projects-orchestrator"
_BASE = "main"
_SESSION = "inside a Claude Code session: run `just install --apply` from a terminal"


class RefusedError(Exception):
    """A precondition failed. The message says which one, and how to fix it."""


def _run(argv: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, env=env)  # noqa: S603 — fixed argv, no shell


def _git_proc(*args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(_REPO_ROOT), *args])


def _git(*args: str, strip: bool = True) -> str:
    proc = _git_proc(*args)
    if proc.returncode != 0:
        raise RefusedError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip() if strip else proc.stdout


def _uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        raise RefusedError("uv is not on PATH: https://docs.astral.sh/uv/")
    return uv


def _uv_dir(*extra: str) -> Path:
    proc = _run([_uv(), "tool", "dir", *extra])
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RefusedError(f"`uv tool dir {' '.join(extra)}` failed: {proc.stderr.strip()}")
    return Path(proc.stdout.strip())


def blob_id(data: bytes) -> str:
    """Return the git blob id of *data*, so installed bytes compare with ``git ls-tree``."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 — git's id, not security


def wheel_layout(root: Path) -> list[tuple[str, str]]:
    """Return (tree prefix, site-packages prefix) pairs from pyproject's hatch wheel config."""
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = data.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {})
    wheel = wheel.get("wheel", {})
    pairs = [(pkg, Path(pkg).name) for pkg in wheel.get("packages", [])]
    pairs += [(str(src), str(dst)) for src, dst in wheel.get("force-include", {}).items()]
    if not pairs:
        raise RefusedError("pyproject.toml has no hatch wheel layout, so the tree cannot be mapped")
    return pairs


def expected_files(root: Path, commit: str) -> dict[str, tuple[str, str]]:
    """Map each installed path the commit should produce to (blob id, tree path)."""
    layout = wheel_layout(root)
    out = _git("ls-tree", "-r", "-z", "--full-tree", commit, "--", *(src for src, _ in layout))
    files: dict[str, tuple[str, str]] = {}
    for entry in filter(None, out.split("\0")):
        meta, path = entry.split("\t", 1)
        _mode, kind, sha = meta.split()
        if kind != "blob":
            continue
        for src, dst in layout:
            src, dst = src.rstrip("/"), dst.rstrip("/")
            if path == src or path.startswith(src + "/"):
                files[dst + path[len(src) :]] = (sha, path)
                break
    return files


def site_packages(env: Path) -> Path:
    """Return the tool env's single site-packages directory."""
    found = sorted(env.glob("lib/python3*/site-packages"))
    if len(found) != 1:
        raise RefusedError(f"expected one site-packages under {env}, found {len(found)}")
    return found[0]


def installed_files(site: Path, tops: set[str]) -> dict[str, str]:
    """Map each installed package file (bytecode caches excluded) to its blob id."""
    files: dict[str, str] = {}
    for top in sorted(tops):
        for path in sorted((site / top).rglob("*")):
            if "__pycache__" in path.parts or not path.is_file():
                continue
            files[path.relative_to(site).as_posix()] = blob_id(path.read_bytes())
    return files


def receipt_problems(env: Path) -> list[str]:
    """Check the receipt names this checkout and its entrypoint links into the env."""
    receipt = env / "uv-receipt.toml"
    if not receipt.is_file():
        return [f"no uv-receipt.toml in {env}"]
    tool = tomllib.loads(receipt.read_text(encoding="utf-8")).get("tool", {})
    problems: list[str] = []
    reqs = [r for r in tool.get("requirements", []) if r.get("name") == _TOOL]
    source = reqs[0].get("directory") if reqs else None
    if source is None or Path(source).resolve() != _REPO_ROOT:
        problems.append(
            f"source: installed from {source or reqs}, not this checkout ({_REPO_ROOT})"
        )
    for ep in tool.get("entrypoints", []):
        link, want = Path(ep.get("install-path", "")), env / "bin" / ep.get("name", "")
        if not link.exists() or link.resolve() != want.resolve():
            problems.append(f"entrypoint: {link} does not resolve to {want}")
    return problems


def check(env: Path) -> tuple[list[str], int]:
    """Return (one line per drift between the installed tool and HEAD, files compared)."""
    if not env.is_dir():
        return [f"not installed: {env} does not exist (run `just install --apply`)"], 0
    head = _git("rev-parse", "HEAD")
    expected = expected_files(_REPO_ROOT, head)
    installed = installed_files(site_packages(env), {p.split("/", 1)[0] for p in expected})
    drift = receipt_problems(env)
    for dest, (sha, src) in sorted(expected.items()):
        if dest not in installed:
            drift.append(f"missing: {dest} (tree: {src})")
        elif installed[dest] != sha:
            drift.append(f"modified: {dest} (tree: {src})")
    drift += [f"not in tree: {dest}" for dest in sorted(set(installed) - set(expected))]
    return drift, len(expected)


def apply_problems(*, fetch: bool) -> list[str]:
    """Return every reason ``--apply`` must refuse; empty means it may install."""
    problems: list[str] = []
    if os.environ.get("CLAUDECODE"):
        problems.append(_SESSION)
    git_dir = Path(_git("rev-parse", "--path-format=absolute", "--git-dir"))
    common = Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir"))
    if git_dir.resolve() != common.resolve():
        problems.append(f"linked worktree: install from the main checkout, {common.parent}")
    name = _git_proc("symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or "detached HEAD"
    if name != _BASE:
        problems.append(f"on '{name}', not {_BASE}: an unmerged branch is unreviewed text")
    # Unstripped: porcelain's first column is a space for an unstaged change.
    dirty = _git("status", "--porcelain", "--untracked-files=all", strip=False).splitlines()
    if dirty:
        shown = "\n      ".join(dirty[:10] + (["..."] if len(dirty) > 10 else []))
        problems.append(f"uncommitted changes would be installed unreviewed:\n      {shown}")
    if fetch:
        proc = _git_proc("fetch", "--quiet", "origin", _BASE)
        if proc.returncode != 0:
            problems.append(f"cannot fetch origin/{_BASE}: {proc.stderr.strip()}")
    upstream = _git_proc("rev-parse", "-q", "--verify", f"origin/{_BASE}").stdout.strip()
    if not upstream:
        problems.append(f"no origin/{_BASE} to compare with: fetch it first")
    elif upstream != _git("rev-parse", "HEAD"):
        counts = _git_proc("rev-list", "--left-right", "--count", f"HEAD...origin/{_BASE}")
        ahead, _, behind = counts.stdout.strip().partition("\t")
        problems.append(f"not in sync with origin/{_BASE} (ahead {ahead}, behind {behind})")
    return problems


def _print_plan(env: Path, argv: list[str]) -> None:
    branch = _git_proc("symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or "detached HEAD"
    print(f"  source    {_REPO_ROOT}")
    print(f"  commit    {_git('rev-parse', 'HEAD')} ({branch})")
    print(f"  target    {env}, entrypoint {_uv_dir('--bin') / _TOOL}")
    print(f"  command   {' '.join(argv)}")


def _report(lines: list[str], head: str, *, stream: TextIO | None = None) -> None:
    out = stream or sys.stderr
    print(head, file=out)
    for line in lines:
        print(f"    - {line}", file=out)


def _dry_run(env: Path, install: list[str]) -> int:
    print(f"{_TOOL} box install: DRY RUN, nothing written")
    _print_plan(env, install)
    problems = apply_problems(fetch=False)
    if problems:
        _report(problems, "  --apply would refuse:", stream=sys.stdout)
    else:
        print(f"  --apply would proceed (origin/{_BASE} as of the last fetch; --apply fetches)")
    # Non-zero so a caller planning on the dry run stops early. A session alone is only
    # a note: dry runs are expected inside one, and deploy checks for a session itself.
    return 1 if any(p != _SESSION for p in problems) else 0


def _apply(env: Path, install: list[str]) -> int:
    problems = apply_problems(fetch=True)
    if problems:
        _report(problems, "refusing --apply:")
        return 1
    _print_plan(env, install)
    child_env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    if subprocess.run(install, env=child_env, check=False).returncode != 0:  # noqa: S603
        _report([" ".join(install)], "failed:")
        return 1
    return _check(env, done="installed and verified")


def _check(env: Path, done: str = "matches") -> int:
    drift, count = check(env)
    if drift:
        _report(drift, f"drift: the installed {_TOOL} differs from {_REPO_ROOT} HEAD:")
        return 1
    head = _git("rev-parse", "--short", "HEAD")
    print(f"ok: {env} {done} {_REPO_ROOT} at {head} ({count} files)")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the dry run, ``--apply`` or ``--check``, and return the exit code."""
    parser = argparse.ArgumentParser(
        prog="just install", description="Box install of this checkout as a uv tool."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true", help="install from a clean main in sync with origin"
    )
    mode.add_argument("--check", action="store_true", help="diff the installed build against HEAD")
    args = parser.parse_args(argv)
    try:
        env = _uv_dir() / _TOOL
        if args.check:
            return _check(env)
        install = [_uv(), "tool", "install", "--reinstall", str(_REPO_ROOT)]
        return _apply(env, install) if args.apply else _dry_run(env, install)
    except RefusedError as exc:
        _report([str(exc)], "refused:")
        return 1


if __name__ == "__main__":
    sys.exit(main())
