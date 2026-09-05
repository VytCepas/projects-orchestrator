"""Scaffold drift and hook-installation health per project.

project-init records a ``scaffold.manifest`` (file → SHA-256) in every
child's ``.claude/config.yaml``. Comparing the working tree against those
hashes answers "has this project locally diverged from its scaffold?"
offline and without project-init installed. Hook health answers "are the
repo's git hooks actually active in this clone?" — enforcement that exists
only in ``.github/hooks/`` but not ``.git/hooks/`` is enforcement that
never runs. Both never raise.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from projects_orchestrator.descriptor import ProjectDescriptor

HOOKS_SOURCE_DIR = Path(".github/hooks")

_MAX_HASH_BYTES = 4_194_304


@dataclass(frozen=True)
class DriftReport:
    """How far one project's tree is from its recorded scaffold.

    Attributes:
        project: Project name.
        status: ``clean`` | ``drift`` | ``no-manifest``.
        modified: Scaffolded files whose content hash changed.
        missing: Scaffolded files that no longer exist.
        total: Number of files the manifest covers.
    """

    project: str
    status: str
    modified: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    total: int = 0

    @property
    def summary(self) -> str:
        """Compact cell text: ``none``, ``n files``, or ``-``."""
        if self.status == "no-manifest":
            return "-"
        changed = len(self.modified) + len(self.missing)
        return "none" if changed == 0 else f"{changed} file{'s' if changed != 1 else ''}"


def _load_manifest(config_path: Path) -> dict[str, str]:
    """Read ``scaffold.manifest`` from the project config; empty on failure."""
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(raw, dict):
        return {}
    scaffold = raw.get("scaffold")
    manifest = scaffold.get("manifest") if isinstance(scaffold, dict) else None
    if not isinstance(manifest, dict):
        return {}
    return {str(k): str(v) for k, v in manifest.items()}


def _sha256(path: Path) -> str | None:
    """Hash one file; ``None`` when unreadable or implausibly large."""
    try:
        if path.stat().st_size > _MAX_HASH_BYTES:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def compute_drift(descriptor: ProjectDescriptor) -> DriftReport:
    """Compare a project's tree against its scaffold manifest; never raises.

    Args:
        descriptor: The project to inspect.

    Returns:
        The drift report; a project without a manifest is ``no-manifest``
        (older contract), not an error.
    """
    manifest = _load_manifest(descriptor.config_path)
    if not manifest:
        return DriftReport(project=descriptor.name, status="no-manifest")

    modified: list[str] = []
    missing: list[str] = []
    for relpath, expected in sorted(manifest.items()):
        target = descriptor.path / relpath
        if not target.is_file():
            missing.append(relpath)
            continue
        actual = _sha256(target)
        # A tracked file we cannot hash (too large or unreadable) cannot match
        # the recorded SHA-256 of a scaffold file — treat it as drifted rather
        # than silently "clean", which would hide a real divergence.
        if actual != expected.lower():
            modified.append(relpath)

    status = "clean" if not modified and not missing else "drift"
    return DriftReport(
        project=descriptor.name,
        status=status,
        modified=tuple(modified),
        missing=tuple(missing),
        total=len(manifest),
    )


def _hooks_dir(project: Path) -> Path:
    """Resolve where git will look for this checkout's hooks.

    In an ordinary clone that is ``.git/hooks``. In a WORKTREE it is not:
    ``.git`` is a FILE holding ``gitdir: <admin dir>``, and git resolves
    ``hooks`` to the SHARED common dir, not to the per-worktree admin dir. The
    old code assumed the directory form, so ``<worktree>/.git/hooks`` never
    existed and every worktree reported ``missing``.

    That was a false negative, not a conservative one — measured 2026-09-05 on
    three berths (``estate-wt-access``, ``estate-wt-mcp``,
    ``gtm-wt-bite-consent``), each reporting ``missing`` while
    ``git hook run commit-msg`` inside it rejected a malformed message with
    exit 1 and accepted a valid one with exit 0. A health check that reads red
    on a healthy tree is the class §2.11 names: it gets ignored, and then it
    protects nothing.

    ``commondir`` is git's own answer to "where is the shared dir", so it is
    READ rather than reconstructed by counting ``..`` segments — the admin dir
    layout is git's to change, and a hand-built relative path would go wrong
    silently.

    KNOWN LIMIT, stated rather than hidden: ``core.hooksPath`` is not consulted,
    so a repo that relocates its hooks still reads ``missing``. No repo in this
    fleet sets it (checked 2026-09-05); reading git config here would mean
    shelling out from a module that is otherwise pure filesystem, and the
    honest fix is to add it when a project actually needs it.
    """
    dot_git = project / ".git"
    if dot_git.is_dir():
        return dot_git / "hooks"
    try:
        pointer = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        # Unreadable or absent: fall back to the ordinary layout so a
        # non-repo answers exactly as it did before.
        return dot_git / "hooks"
    if not pointer.startswith("gitdir:"):
        return dot_git / "hooks"
    admin = Path(pointer[len("gitdir:") :].strip())
    if not admin.is_absolute():
        admin = project / admin
    try:
        common_rel = (admin / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        # A gitdir with no commondir is a plain relocated .git (``git init
        # --separate-git-dir``), not a worktree — its hooks live under it.
        return admin / "hooks"
    return (admin / common_rel) / "hooks"


def hook_health(descriptor: ProjectDescriptor) -> str:
    """Report whether the project's git hooks are installed in its clone.

    Args:
        descriptor: The project to inspect.

    Returns:
        ``ok`` (all shipped hooks present in ``.git/hooks/``), ``partial``,
        ``missing``, or ``-`` when the project ships no hooks. The shipped
        set comes from the contract-v2 ``hooks.expected`` list when declared,
        else from globbing ``.github/hooks/``.
    """
    declared = list(descriptor.hooks_expected)
    if not declared:
        source_dir = descriptor.path / HOOKS_SOURCE_DIR
        try:
            declared = sorted(p.name for p in source_dir.iterdir() if p.is_file())
        except OSError:
            declared = []
    if not declared:
        return "-"
    installed_dir = _hooks_dir(descriptor.path)
    installed = sum(1 for name in declared if (installed_dir / name).is_file())
    if installed == len(declared):
        return "ok"
    return "partial" if installed else "missing"
