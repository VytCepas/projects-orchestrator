#!/usr/bin/env python3
"""Install this checkout as the machine's ``projects-orchestrator`` uv tool, or check it (#317).

``just install`` is a dry run. It prints the source, commit, target and command,
and writes nothing. It exits 1 when ``--apply`` would refuse, unless the only
reason is a Claude Code session. ``just install --apply`` runs ``uv tool install
--reinstall <repo>``, and only from a clean ``main`` in sync with ``origin/main``.
``just install --check`` compares the installed package files with HEAD and
exits 1 on drift, naming each file.

The command on PATH runs uv's copy of the checkout, never the tree, and
``--version`` does not move between releases (#191), so a pull without a
reinstall used to leave a stale build that nothing reported.

The layout is uv's own. ``uv tool dir`` holds one env per tool, and
``<env>/uv-receipt.toml`` records the source and entrypoints. Where the env keeps
its packages and scripts is asked of the env's own interpreter, so no platform
layout is assumed. The tree-to-wheel mapping and the metadata a build must carry
are read from pyproject, never restated here.

Exit codes: 0 ok, 1 refused, failed or drifted, 2 usage. Stdlib only.
"""

from __future__ import annotations

import argparse
import filecmp
import io
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tomllib
from email.parser import Parser
from pathlib import Path
from typing import Any, TextIO

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL = "projects-orchestrator"
_BASE = "main"
_SESSION = "inside a Claude Code session: run `just install --apply` from a terminal"
_ENV_PATHS = (
    "import json, sysconfig; "
    "print(json.dumps({k: sysconfig.get_path(k) for k in ('purelib', 'scripts')}))"
)
_REQ = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?([^;]*)(?:;(.*))?$")
_EXTRA = re.compile(r"^(?:\((.*)\)and)?extra=='([^']*)'$")
_HATCH_VERSION = re.compile(r"(?im)^(__version__|VERSION) *= *(['\"])v?(?P<version>.+?)\2")


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


def blob_ids(paths: list[Path]) -> list[str]:
    """Return each file's git blob id, in order, from the repo's own ``hash-object``.

    ``--no-filters`` hashes the installed bytes as they are: with filters, an
    attribute such as ``*.sh text eol=lf`` turns a CRLF copy back into a match.
    """
    if not paths:
        return []
    argv = ["git", "-C", str(_REPO_ROOT), "hash-object", "--no-filters", "--stdin-paths"]
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv,
        input="".join(f"{path}\n" for path in paths),
        capture_output=True,
        text=True,
        check=False,
    )
    ids = proc.stdout.split()
    if proc.returncode != 0 or len(ids) != len(paths):
        raise RefusedError(f"git hash-object failed: {proc.stderr.strip()}")
    return ids


def checkout_bytes(commit: str, pathspecs: list[str]) -> dict[str, bytes]:
    """Map each file under *pathspecs* to the bytes a checkout of *commit* writes.

    ``git archive`` applies checkout's eol conversion and filters, so a text file
    that ``core.autocrlf`` made CRLF on disk, and so in the wheel, matches its LF
    blob here, while a CRLF copy of an ``eol=lf`` file still does not.
    """
    argv = ["git", "-C", str(_REPO_ROOT), "archive", "--format=tar", commit, "--", *pathspecs]
    proc = subprocess.run(argv, capture_output=True, check=False)  # noqa: S603 — fixed argv
    if proc.returncode != 0:
        raise RefusedError(f"git archive failed: {proc.stderr.decode(errors='replace').strip()}")
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        for member in tar:
            data = tar.extractfile(member) if member.isfile() else None
            if data is not None:
                files[member.name] = data.read()
    return files


def wheel_layout(project_toml: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (tree prefix, site-packages prefix) pairs from pyproject's hatch wheel config."""
    wheel = project_toml.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {})
    wheel = wheel.get("wheel", {})
    pairs = [(pkg, Path(pkg).name) for pkg in wheel.get("packages", [])]
    pairs += [(str(src), str(dst)) for src, dst in wheel.get("force-include", {}).items()]
    if not pairs:
        raise RefusedError("pyproject.toml has no hatch wheel layout, so the tree cannot be mapped")
    return pairs


def expected_files(project_toml: dict[str, Any], commit: str) -> dict[str, tuple[str, str]]:
    """Map each installed path the commit should produce to (blob id, tree path)."""
    layout = wheel_layout(project_toml)
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


def env_paths(env: Path) -> tuple[Path, Path]:
    """Return the tool env's (purelib, scripts), as its own interpreter reports them.

    Exact on every platform: no ``lib/python3*`` or ``bin`` guessed here.
    """
    found = _run([_uv(), "python", "find", str(env)])
    if found.returncode != 0 or not found.stdout.strip():
        raise RefusedError(f"`uv python find {env}` failed: {found.stderr.strip()}")
    argv = [found.stdout.strip(), "-I", "-B", "-c", _ENV_PATHS]
    proc = _run(argv)
    try:
        paths = json.loads(proc.stdout)
        return Path(paths["purelib"]), Path(paths["scripts"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RefusedError(f"{argv[0]} did not report its paths: {proc.stderr.strip()}") from exc


def installed_files(site: Path, tops: set[str]) -> dict[str, str]:
    """Map each installed package file (bytecode caches excluded) to its blob id."""
    paths = [
        path
        for top in sorted(tops)
        for path in sorted((site / top).rglob("*"))
        if "__pycache__" not in path.parts and path.is_file()
    ]
    ids = blob_ids(paths)
    return {path.relative_to(site).as_posix(): i for path, i in zip(paths, ids, strict=True)}


def _same_entry(link: Path, want: Path) -> bool:
    """True when *link* is *want* through a symlink, or a byte-identical copy (Windows)."""
    if not link.exists() or not want.exists():
        return False
    return link.resolve() == want.resolve() or filecmp.cmp(link, want, shallow=False)


def receipt_problems(env: Path, scripts: Path) -> list[str]:
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
        link = Path(ep.get("install-path", ""))
        if not _same_entry(link, scripts / link.name):
            problems.append(f"entrypoint: {link} does not resolve to {scripts / link.name}")
    return problems


def path_problems(scripts: Path) -> list[str]:
    """Check the ``_TOOL`` that PATH selects is the tool env's, and name any shadow."""
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    if sys.prefix != sys.base_prefix:  # under `uv run`, this repo's own venv is first on PATH
        own = Path(sysconfig.get_path("scripts")).resolve()
        dirs = [d for d in dirs if d and Path(d).resolve() != own]
    found = shutil.which(_TOOL, path=os.pathsep.join(dirs))
    if found is None:
        return [f"PATH: no {_TOOL} on PATH"]
    want = scripts / Path(found).name
    if not _same_entry(Path(found), want):
        return [f"PATH: {_TOOL} runs {found}, which shadows {want}"]
    return []


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _specs(spec: str) -> str:
    return ",".join(sorted(s for s in "".join(spec.split()).split(",") if s))


def _req_key(req: str, extra: str = "") -> str:
    """Normalise one requirement as PEP 503/508 compare it, for either side of the diff."""
    match = _REQ.match(req)
    if match is None:
        return f"unparsed {req!r}"
    name, extras, spec, marker = match.groups()
    extras_n = ",".join(sorted(_norm(e) for e in (extras or "").split(",") if e.strip()))
    marker_n = "".join((marker or "").split()).replace('"', "'")
    return f"{_norm(name)}[{extras_n}]{_specs(spec)}; {marker_n}; extra={extra}"


def _installed_req_key(value: str) -> str:
    """Normalise a Requires-Dist line, splitting off the ``extra == '...'`` the backend adds."""
    req, _, marker = value.partition(";")
    match = _EXTRA.match("".join(marker.split()).replace('"', "'"))
    if match is None:
        return _req_key(value)
    inner, extra = match.groups()
    return _req_key(f"{req};{inner}" if inner else req, _norm(extra))


def _dynamic_version(project_toml: dict[str, Any], commit: str) -> str:
    """Read a hatch ``path`` version source at *commit*, with hatch's default pattern."""
    source = project_toml.get("tool", {}).get("hatch", {}).get("version", {}).get("path")
    if not source:
        return "(dynamic, not a hatch path source)"
    match = _HATCH_VERSION.search(_git("show", f"{commit}:{source}"))
    return match.group("version") if match else f"(no version in {source})"


# A side of the metadata diff: single fields as (normalised, original), and the
# rest as normalised key -> the original text a drift line prints.
_Side = tuple[dict[str, tuple[str, str]], dict[str, str]]
_EP_KIND = {"console_scripts": "console script", "gui_scripts": "gui script"}


def _entry_point(group: str, name: str, value: str) -> tuple[str, str]:
    kind = _EP_KIND.get(group, f"entry point [{group}]")
    return (
        f"[{group}] {name.strip()} = {''.join(value.split())}",
        f"{kind} {name.strip()} = {value.strip()}",
    )


def _pyproject_side(project_toml: dict[str, Any], commit: str) -> _Side:
    project = project_toml.get("project", {})
    if "version" in project.get("dynamic", []):
        version = _dynamic_version(project_toml, commit)
    else:
        version = str(project.get("version", ""))
    name, python = project.get("name", ""), project.get("requires-python", "")
    singles = {
        "Name": (_norm(name), name),
        "Version": (version, version),
        "Requires-Python": (_specs(python), python),
    }
    rest = {_req_key(r): f"dependency {r}" for r in project.get("dependencies", [])}
    for extra, deps in project.get("optional-dependencies", {}).items():
        rest[f"extra {_norm(extra)}"] = f"extra {extra}"
        rest |= {_req_key(r, _norm(extra)): f"dependency {r} (extra {extra})" for r in deps}
    groups = {"console_scripts": project.get("scripts", {})}
    groups["gui_scripts"] = project.get("gui-scripts", {})
    groups.update(project.get("entry-points", {}))
    for group, points in groups.items():
        rest |= dict(_entry_point(group, n, str(v)) for n, v in points.items())
    return singles, rest


def _installed_side(dist: Path) -> _Side:
    meta = Parser().parsestr((dist / "METADATA").read_text(encoding="utf-8"))
    name, version = meta.get("Name", ""), meta.get("Version", "")
    python = meta.get("Requires-Python", "")
    singles = {
        "Name": (_norm(name), name),
        "Version": (version, version),
        "Requires-Python": (_specs(python), python),
    }
    rest = {_installed_req_key(v): f"dependency {v}" for v in meta.get_all("Requires-Dist", [])}
    rest |= {f"extra {_norm(e)}": f"extra {e}" for e in meta.get_all("Provides-Extra", [])}
    points = dist / "entry_points.txt"
    group = ""
    for raw in (points.read_text(encoding="utf-8") if points.is_file() else "").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1].strip()
        elif group and "=" in line and not line.startswith(("#", ";")):
            rest |= dict([_entry_point(group, *line.split("=", 1))])
    return singles, rest


def metadata_problems(site: Path, project_toml: dict[str, Any], commit: str) -> list[str]:
    """Diff the installed dist-info (version, requirements, entry points) against pyproject.

    Compared normalised, as the build backend rewrites names, quotes and specifier
    order; reported in the original spelling, one line per difference.
    """
    name = re.sub(r"[-_.]+", "_", project_toml.get("project", {}).get("name", "")).lower()
    dists = sorted(site.glob(f"{name}-*.dist-info"))
    if len(dists) != 1:
        return [f"metadata: expected one {name}-*.dist-info in {site}, found {len(dists)}"]
    (want_one, want), (have_one, have) = (
        _pyproject_side(project_toml, commit),
        _installed_side(dists[0]),
    )
    problems = [
        f"metadata: {field} installed {have_one[field][1] or '(none)'}, "
        f"pyproject says {want_one[field][1] or '(none)'}"
        for field in want_one
        if want_one[field][0] != have_one[field][0]
    ]
    problems += [
        f"metadata: {want[k]} is in pyproject, not installed"
        for k in sorted(want.keys() - have.keys())
    ]
    problems += [
        f"metadata: {have[k]} is installed, not in pyproject"
        for k in sorted(have.keys() - want.keys())
    ]
    return problems


def check(env: Path) -> tuple[list[str], int]:
    """Return (one line per drift between the installed tool and HEAD, files compared)."""
    if not env.is_dir():
        return [f"not installed: {env} does not exist (run `just install --apply`)"], 0
    head = _git("rev-parse", "HEAD")
    project_toml = tomllib.loads(_git("show", f"{head}:pyproject.toml"))
    expected = expected_files(project_toml, head)
    site, scripts = env_paths(env)
    installed = installed_files(site, {p.split("/", 1)[0] for p in expected})
    drift = receipt_problems(env, scripts) + path_problems(scripts)
    drift += metadata_problems(site, project_toml, head)
    # Not the blob is not yet modified: a CRLF checkout (core.autocrlf) builds CRLF files.
    suspect = {dest for dest, (sha, _) in expected.items() if installed.get(dest, sha) != sha}
    layout = [src for src, _ in wheel_layout(project_toml)]
    checkout = checkout_bytes(head, layout) if suspect else {}
    for dest, (_, src) in sorted(expected.items()):
        if dest not in installed:
            drift.append(f"missing: {dest} (tree: {src})")
        elif dest in suspect and checkout.get(src) != (site / dest).read_bytes():
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
