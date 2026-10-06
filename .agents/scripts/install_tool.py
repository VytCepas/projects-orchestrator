#!/usr/bin/env python3
"""Install this checkout as the machine's ``projects-orchestrator`` uv tool, or check it (#317).

``just install`` is a dry run. It prints the source, commit, target and command,
and writes nothing. It exits 1 when ``--apply`` would refuse, unless the only
reason is a Claude Code session. ``just install --apply`` runs ``uv tool install
--reinstall <repo>``, and only from a clean ``main`` in sync with ``origin/main``.
``just install --check`` compares the installed package files, and their exec
bits, with HEAD and exits 1 on drift, naming each file. The files read are those
HEAD maps into the wheel plus every one the installed build's RECORD lists.

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
import csv
import filecmp
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib
from email.parser import Parser
from pathlib import Path, PurePosixPath
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
_FILE_MODES = ("100644", "100755")  # git's regular files; a symlink or submodule has no exec bit
_HATCH_VERSION = re.compile(r"(?im)^(__version__|VERSION) *= *(['\"])v?(?P<version>.+?)\2")
# `git ls-files -v` tags a plain entry H, skip-worktree S, and assume-unchanged in lower case.
_HIDDEN = {"S": "skip-worktree", "h": "assume-unchanged", "s": "skip-worktree, assume-unchanged"}


class RefusedError(Exception):
    """A precondition failed. The message says which one, and how to fix it."""


class UnreadableError(Exception):
    """An installed dist-info file is missing or unreadable. The message names it and why."""


def _dist_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        why = "not UTF-8" if isinstance(exc, UnicodeDecodeError) else exc.strerror or repr(exc)
        raise UnreadableError(f"{path.parent.name}/{path.name} cannot be read ({why})") from exc


def _run(argv: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, env=env)  # noqa: S603 — fixed argv, no shell


def _git_proc(*args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(_REPO_ROOT), *args])


def _git(*args: str, strip: bool = True) -> str:
    proc = _git_proc(*args)
    if proc.returncode != 0:
        raise RefusedError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip() if strip else proc.stdout


def _status_without_stat_cache() -> list[str]:
    """`git status --porcelain` lines, with every tracked file hashed instead of stat-trusted.

    A cleared skip-worktree or assume-unchanged flag leaves the entry's stat from before
    the edit, and git trusts it when size and whole-second mtime still match: a same-size
    edit made in that second reads clean (#331). A throwaway index built from HEAD holds
    no stat, so status must read the bytes.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = os.environ | {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        for argv in (["read-tree", "HEAD"], ["status", "--porcelain", "--untracked-files=all"]):
            proc = _run(["git", "-C", str(_REPO_ROOT), *argv], env=env)
            if proc.returncode != 0:
                raise RefusedError(f"git {' '.join(argv)} failed: {proc.stderr.strip()}")
        return proc.stdout.splitlines()


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


def ignored_problems(project_toml: dict[str, Any]) -> list[str]:
    """Name each ignored file under a path the wheel packages: git status never lists one.

    force-include ships every file under its path, and a package walk skips only what the
    root ``.gitignore`` names (project-init#1047). hatchling never ships ``__pycache__``.
    """
    layout = [src for src, _ in wheel_layout(project_toml)]
    argv = ["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--", *layout]
    listed = _git(*argv, strip=False).split("\0")
    found = [p for p in listed if p and "__pycache__" not in PurePosixPath(p).parts]
    if not found:
        return []
    shown = "\n      ".join(found[:10] + (["..."] if len(found) > 10 else []))
    paths = shlex.join(layout)
    return [
        "ignored files the wheel packages would be installed unreviewed. Preview with "
        f"`git clean -ndX -- {paths}`, then delete them with `git clean -fdX -- {paths}`:"
        f"\n      {shown}"
    ]


def expected_files(project_toml: dict[str, Any], commit: str) -> dict[str, tuple[str, str, str]]:
    """Map each installed path the commit should produce to (blob id, tree path, tree mode)."""
    layout = wheel_layout(project_toml)
    out = _git("ls-tree", "-r", "-z", "--full-tree", commit, "--", *(src for src, _ in layout))
    files: dict[str, tuple[str, str, str]] = {}
    for entry in filter(None, out.split("\0")):
        meta, path = entry.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind != "blob":
            continue
        for src, dst in layout:
            src, dst = src.rstrip("/"), dst.rstrip("/")
            if path == src or path.startswith(src + "/"):
                files[dst + path[len(src) :]] = (sha, path, mode)
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
    """Map each file under the top-level names *tops* (bytecode caches excluded) to its blob id."""
    paths = [
        path
        for top in sorted(tops)
        for path in ([site / top] if (site / top).is_file() else sorted((site / top).rglob("*")))
        if "__pycache__" not in path.parts and path.is_file()
    ]
    ids = blob_ids(paths)
    return {path.relative_to(site).as_posix(): i for path, i in zip(paths, ids, strict=True)}


def _dists(site: Path, project_toml: dict[str, Any]) -> list[Path]:
    name = re.sub(r"[-_.]+", "_", project_toml.get("project", {}).get("name", "")).lower()
    return sorted(site.glob(f"{name}-*.dist-info"))


def record_tops(dists: list[Path]) -> tuple[set[str], list[str]]:
    """Return the top-level site-packages names each dist-info's RECORD owns, and problems.

    A build from before HEAD dropped a wheel mapping left files that HEAD's layout
    never names, and RECORD is where the installed build lists them. Its own
    dist-info and anything outside site-packages (the scripts) are left out.
    """
    tops: set[str] = set()
    problems: list[str] = []
    for dist in dists:
        record = dist / "RECORD"
        if not record.is_file():
            problems.append(f"record: {dist.name} has no RECORD, so what it installed is unknown")
            continue
        try:
            text = _dist_text(record)
        except UnreadableError as exc:
            problems.append(f"record: {exc}, so what it installed is unknown")
            continue
        for row in csv.reader(io.StringIO(text)):
            path = PurePosixPath(row[0]) if row else None
            if path is None or path.is_absolute() or not path.parts:
                continue
            if path.parts[0] not in ("..", dist.name):
                tops.add(path.parts[0])
    return tops, problems


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


def caller_path() -> list[str]:
    """Return PATH as the caller's shell had it, before ``uv run`` prepended to it.

    Measured on uv 0.11: each ``uv run`` level bumps ``UV_RUN_RECURSION_DEPTH``
    and prepends the running interpreter's scripts dir once, even when the caller
    had that venv active and first already. ``VIRTUAL_ENV`` cannot tell the two
    apart, as uv sets it for a ``.venv`` it only found. So drop at most that many
    leading copies of this interpreter's scripts dir, and keep a caller's own.
    """
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    depth = os.environ.get("UV_RUN_RECURSION_DEPTH", "")
    own = Path(sysconfig.get_path("scripts")).resolve()
    for _ in range(int(depth) if depth.isdigit() else 0):
        if not dirs or not dirs[0] or Path(dirs[0]).resolve() != own:
            break
        dirs = dirs[1:]
    return dirs


def path_problems(scripts: Path) -> list[str]:
    """Check the ``_TOOL`` that the caller's PATH selects is the tool env's, and name any shadow."""
    found = shutil.which(_TOOL, path=os.pathsep.join(caller_path()))
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
    meta = Parser().parsestr(_dist_text(dist / "METADATA"))
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
    for raw in (_dist_text(points) if points.exists() else "").splitlines():
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
    dists = _dists(site, project_toml)
    if len(dists) != 1:
        return [f"metadata: expected one {name}-*.dist-info in {site}, found {len(dists)}"]
    want_one, want = _pyproject_side(project_toml, commit)
    try:
        have_one, have = _installed_side(dists[0])
    except UnreadableError as exc:
        return [f"metadata: {exc}, so the installed metadata is not compared"]
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
    owned, record_drift = record_tops(_dists(site, project_toml))
    installed = installed_files(site, owned | {p.split("/", 1)[0] for p in expected})
    drift = receipt_problems(env, scripts) + path_problems(scripts)
    drift += metadata_problems(site, project_toml, head) + record_drift
    # Not the blob is not yet modified: a CRLF checkout (core.autocrlf) builds CRLF files.
    suspect = {dest for dest, (sha, _, _) in expected.items() if installed.get(dest, sha) != sha}
    layout = [src for src, _ in wheel_layout(project_toml)]
    checkout = checkout_bytes(head, layout) if suspect else {}
    for dest, (_, src, mode) in sorted(expected.items()):
        if dest not in installed:
            drift.append(f"missing: {dest} (tree: {src})")
            continue
        if dest in suspect and checkout.get(src) != (site / dest).read_bytes():
            drift.append(f"modified: {dest} (tree: {src})")
        perms = (site / dest).stat().st_mode & 0o777
        # The scaffolder gives an output the exec bit its template has (any of 0o111).
        if os.name != "nt" and mode in _FILE_MODES and bool(perms & 0o111) != (mode == "100755"):
            drift.append(f"mode: {dest} installed {perms:o}, tree {mode} (tree: {src})")
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
    # status skips a skip-worktree or assume-unchanged file; uv builds it (project-init#1047).
    entries = _git("ls-files", "-v", "-z", strip=False).split("\0")
    hidden = [f"{_HIDDEN.get(e[0], e[0])}: {e[2:]}" for e in entries if e and e[0] != "H"]
    hidden_paths = {e[2:] for e in entries if e and e[0] != "H"}
    # Unstripped: porcelain's first column is a space for an unstaged change.
    dirty = [
        line
        for line in _status_without_stat_cache()
        if line[3:] not in hidden_paths  # named by the hidden-files refusal instead
    ]
    if dirty:
        shown = "\n      ".join(dirty[:10] + (["..."] if len(dirty) > 10 else []))
        problems.append(f"uncommitted changes would be installed unreviewed:\n      {shown}")
    if hidden:
        shown = "\n      ".join(hidden[:10] + (["..."] if len(hidden) > 10 else []))
        problems.append(
            "files git status skips would be installed unreviewed. Clear each flag with "
            "`git update-index --no-skip-worktree -- <file>` or "
            f"`git update-index --no-assume-unchanged -- <file>`, one call per flag:\n      {shown}"
        )
    # HEAD's layout: a pyproject on disk that differs from it is refused above.
    problems += ignored_problems(tomllib.loads(_git("show", "HEAD:pyproject.toml")))
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
