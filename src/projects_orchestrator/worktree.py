"""Throwaway git worktrees — an agent works here, never in the operator's clone.

``heal`` originally ran its agent by checking a branch out *in the project's own
working copy* (``git checkout -B``) and restoring the operator's branch in a
``finally``. That is unsafe for a fleet tool in three ways, and this module
exists to close all three:

1. **It commandeers the operator's clone.** A fleet-wide heal branch-switches
   every project out from under whoever is working in them.
2. **A ``finally`` is not a guarantee.** A SIGKILL, an OOM, or a host that
   sleeps badly skips it, stranding the clone on a ``heal/`` branch with an
   agent's uncommitted edits in the tree.
3. **It serialises everything.** Detached runs, concurrent runs, and a run whose
   state outlives its process (ADR-007) are all impossible while a run owns HEAD.

A worktree shares the repository's object store, so cutting one is cheap: it is a
second checkout, not a second clone. Worktrees live under ``$XDG_STATE_HOME``
beside the supervisor's run state — never inside the project, whose ``.gitignore``
we do not control.

Retention is deliberately asymmetric. A **successful** run's worktree is removed:
the work is in the PR, so the checkout is redundant. A **failed** run's worktree
is *kept* — it is the only record of what the agent actually did, and deleting it
destroys the evidence at the exact moment someone needs it. Kept worktrees expire
on a clock (:func:`prune_expired`), not on sight.

Like the rest of the engine, nothing here raises (ADR-003): an unwritable state
directory, a repo that refuses the worktree, or an already-removed path degrade
to ``None``/``False``, and the caller renders that.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

_STATE_DIRNAME = "projects-orchestrator"
_WORKTREE_SUBDIR = "worktrees"

_GIT_TIMEOUT = 60.0

#: Kept (failed-run) worktrees are pruned after this long — long enough that a
#: run which failed on Friday is still there to inspect on Monday.
DEFAULT_EXPIRY_DAYS = 7

_SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class Worktree:
    """One throwaway checkout an agent runs in.

    Attributes:
        project: Project the worktree was cut from.
        path: The worktree's own directory — *not* the project's clone.
        branch: Branch checked out in it.
        repo: The originating clone, whose object store it shares.
    """

    project: str
    path: Path
    branch: str
    repo: Path


def worktree_root() -> Path:
    """Return the worktree state directory, honoring ``$XDG_STATE_HOME``."""
    base = os.environ.get("XDG_STATE_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / _STATE_DIRNAME / _WORKTREE_SUBDIR


def _run_argv(args: list[str], cwd: Path, timeout: float = _GIT_TIMEOUT) -> bool:
    """Run one subcommand via argv (never a shell); report success.

    The executable is ``args[0]``, mirroring ``heal._run_argv``: values here (a
    project name, a branch built from it) are not fully under this module's
    control, and passing them as separate argv elements means there is no shell
    to interpret metacharacters in a crafted project name.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell; never concatenated into a command string
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _git(args: list[str], cwd: Path, timeout: float = _GIT_TIMEOUT) -> bool:
    """Run one ``git`` subcommand in ``cwd``; report success (never raises)."""
    return _run_argv(["git", *args], cwd=cwd, timeout=timeout)


def run_slug() -> str:
    """Build a slug that will not collide with a concurrent run on this repo.

    The random suffix is load-bearing, not decoration: a timestamp-and-pid slug
    collides whenever one process starts two runs inside the same second, and
    :func:`create` (rightly) refuses to reuse an existing directory — so the
    second run would be turned away for no reason at all.
    """
    return f"{int(time.time())}-{os.getpid()}-{uuid4().hex[:8]}"


def create(repo: Path, project: str, branch: str, slug: str) -> Worktree | None:
    """Cut a fresh worktree from ``repo``'s HEAD; ``None`` if git refuses.

    The operator's clone is never checked out, branch-switched, or otherwise
    mutated: ``git worktree add`` writes only ``.git/worktrees/`` metadata and
    the new directory. That is the whole point of this module.

    ``-B`` resets the branch to HEAD if it already exists (matching the
    ``checkout -B`` this replaced). Git still refuses when that branch is checked
    out in *another* live worktree — which is the correct outcome: a kept
    worktree from an earlier failed run is evidence, and clobbering it to start a
    new run would destroy exactly what we chose to retain.

    Args:
        repo: The project's clone. Read from, never modified.
        project: Project name (namespaces the worktree directory).
        branch: Branch to create/reset and check out in the worktree.
        slug: Per-run directory name; see :func:`run_slug`.

    Returns:
        The :class:`Worktree`, or ``None`` when the state dir is unwritable, the
        slug is already taken, or git declines.
    """
    path = worktree_root() / project / slug
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    if path.exists():
        # Reusing a slug would silently hand the agent a stale checkout.
        return None
    if not _git(["worktree", "add", "-B", branch, str(path), "HEAD"], cwd=repo):
        return None
    return Worktree(project=project, path=path, branch=branch, repo=repo)


def remove(worktree: Worktree) -> bool:
    """Remove a worktree and deregister it; report success (never raises)."""
    if not _git(["worktree", "remove", "--force", str(worktree.path)], cwd=worktree.repo):
        # git can refuse (the repo moved, the admin entry is corrupt). Drop the
        # directory anyway — a failed cleanup must not wedge the next run — then
        # prune the dangling administrative entry it leaves behind.
        shutil.rmtree(worktree.path, ignore_errors=True)
        _git(["worktree", "prune"], cwd=worktree.repo)
    return not worktree.path.exists()


def prune_expired(repo: Path, project: str, expiry_days: int = DEFAULT_EXPIRY_DAYS) -> int:
    """Delete kept worktrees older than ``expiry_days``; return how many went.

    Retention is the point (see the module docstring), so this is a clock and not
    a sweep-on-sight: a failed run's evidence survives until it is genuinely
    stale, and only then.
    """
    cutoff = time.time() - expiry_days * _SECONDS_PER_DAY
    try:
        entries = list((worktree_root() / project).iterdir())
    except OSError:
        return 0
    pruned = 0
    for entry in entries:
        try:
            if not entry.is_dir() or entry.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        pruned += 1
    if pruned:
        _git(["worktree", "prune"], cwd=repo)
    return pruned
