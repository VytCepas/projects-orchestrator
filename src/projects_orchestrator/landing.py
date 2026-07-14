"""The write boundary — an agent run's work leaves here as a draft PR, or not at all.

Every mutation an agent run makes to a child repo passes through this module.
There are exactly two sanctioned writes:

1. push a **new, non-protected branch**, and
2. open a **draft pull request** from it.

Nothing else. No push to the default branch, no force-push, no merge, no tag —
not because the caller happens not to ask, but because this module refuses.

**Why it is enforced here and not by the child.** A project-init'd repo ships a
``pre-push`` hook that blocks pushes to main, and leaning on it is tempting. But
the first campaign this system exists to run — rolling project-init across an
unscaffolded estate — targets *precisely the repos that do not have that hook
yet*. The child's guard is absent exactly where the blast radius is highest. **A
guard that is missing whenever it matters is not a guard** (ADR-007 §3), so the
tests here run against a repo with no hooks at all.

**Why draft.** A ready-for-review PR is one click and one distracted moment from
merged, and some repos auto-merge on green. Draft is the state that says "a
machine wrote this and no human has looked at it yet", which is the truth.

Never raises (ADR-003): a refused ref, a missing remote, or an absent ``gh``
degrades to a typed failure the caller renders.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.runner import RunResult

_GIT_TIMEOUT = 30.0

#: Refs an agent run may never write to, whatever it is asked. `main`/`master` are
#: the obvious ones; `HEAD` and `@` are the ones someone reaches for when being
#: clever. The repo's *actual* default branch is resolved separately and added to
#: this set, because a child is free to call its trunk anything at all.
_ALWAYS_PROTECTED = frozenset({"main", "master", "trunk", "develop", "HEAD", "@"})

REFUSED = "refused"
PUSH_FAILED = "push_failed"
PR_FAILED = "pr_failed"
LANDED = "landed"


@dataclass(frozen=True)
class Landing:
    """The outcome of trying to land a run's work.

    Attributes:
        status: :data:`LANDED`, :data:`REFUSED`, :data:`PUSH_FAILED`, or
            :data:`PR_FAILED`.
        pr_url: The draft PR, when one was opened.
        detail: Why it did not land. Always populated on failure — a refusal with
            no reason is indistinguishable from a bug.
    """

    status: str
    pr_url: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Whether the work is now sitting in a draft PR."""
        return self.status == LANDED


def _run_argv(args: list[str], cwd: Path, timeout: float = _GIT_TIMEOUT) -> RunResult:
    """Run one ``git``/``gh`` subcommand via argv, never through a shell."""
    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell; never concatenated
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RunResult(
            command=" ".join(args),
            returncode=None,
            error=str(exc),
            duration=time.monotonic() - start,
        )
    return RunResult(
        command=" ".join(args),
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration=time.monotonic() - start,
    )


def default_branch(repo: Path) -> str:
    """Resolve the repo's *actual* default branch; ``""`` when it cannot be.

    A child is free to call its trunk anything, so ``main``/``master`` is a guess
    rather than an answer, and this asks the repo instead of assuming.

    It deliberately does **not** fall back to ``init.defaultBranch``. That setting
    is global and describes what branch *new* repos are given — not what this one
    actually uses. A machine whose global default is ``main``, pointed at a child
    whose trunk is ``production``, would be told "the default is main", and
    :func:`is_protected` would then wave a push to ``production`` straight through.
    **A confidently wrong answer is worse than no answer**, because no answer still
    refuses everything in :data:`_ALWAYS_PROTECTED`.
    """
    local = _run_argv(["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=repo)
    if local.ok and local.stdout.strip():
        return local.stdout.strip().rsplit("/", 1)[-1]

    # origin/HEAD is only populated by `clone` (and by `remote set-head`), so a
    # repo that was `init`-ed and given a remote by hand has none. Ask the remote.
    remote = _run_argv(["git", "ls-remote", "--symref", "origin", "HEAD"], cwd=repo)
    for line in remote.stdout.splitlines():
        if line.startswith("ref:"):
            return line.split()[1].rsplit("/", 1)[-1]
    return ""


def is_protected(branch: str, repo_default: str = "") -> bool:
    """Whether ``branch`` is a ref an agent run may never write to (pure).

    Refuses the always-protected names, the repo's own default branch, and
    anything that is not a plain branch name — a value containing a slash-prefixed
    ref path, a flag, or whitespace is not a branch we created, and the only safe
    response to a ref we do not recognise is *no*.
    """
    candidate = branch.strip()
    if not candidate:
        return True
    if candidate.startswith("-"):
        return True  # a flag wearing a branch's clothes
    if candidate in _ALWAYS_PROTECTED:
        return True
    if repo_default and candidate == repo_default:
        return True
    return candidate.startswith(("refs/", "origin/")) or any(c.isspace() for c in candidate)


def push_branch(worktree: Path, branch: str, repo_default: str = "") -> Landing:
    """Push one agent branch to ``origin``; refuse anything else.

    Refusal is the point. The caller could be a bug, a crafted project name, or a
    future verb that has not thought about this — and none of those get to push
    main.
    """
    if is_protected(branch, repo_default):
        return Landing(
            REFUSED,
            detail=(
                f"refusing to push '{branch}': an agent run may only push a new, "
                "non-protected branch (ADR-007 §3)"
            ),
        )
    # Note what is NOT here: a substring scan for "--force"/"-f" in the branch
    # name. That was the first thing written, and it was wrong — `-f` is a
    # substring of any branch whose random suffix begins with `f`
    # (`heal/lint-alpha-f1a2b3c4`), so the boundary refused roughly one in
    # sixteen of its own legitimate branches. A guard that randomly blocks real
    # work gets deleted by the third person who hits it, and then there is no
    # guard at all.
    #
    # The concern it was reaching for is real but structural, and is handled
    # structurally: `is_protected` rejects any name starting with `-` or
    # containing whitespace (so a flag cannot masquerade as a branch), and `--`
    # below terminates git's option parsing outright. No `--force` appears in
    # this argv because none is written — an agent run creates history, it does
    # not rewrite it.
    pushed = _run_argv(["git", "push", "--set-upstream", "origin", "--", branch], cwd=worktree)
    if not pushed.ok:
        return Landing(PUSH_FAILED, detail=pushed.stderr.strip()[-300:] or "git push failed")
    return Landing(LANDED)


def open_draft_pr(worktree: Path, branch: str, title: str, body: str) -> Landing:
    """Open a **draft** PR from ``branch``; never a ready-for-review one.

    ``--draft`` is not a nicety. A ready PR is one click and one distracted moment
    from merged, and a repo with auto-merge-on-green would land an agent's work
    with no human in the loop at all — which is the entire thing this system
    promises not to do.
    """
    args = [
        "gh",
        "pr",
        "create",
        "--draft",
        "--head",
        branch,
        "--title",
        title,
        "--body",
        body,
    ]
    result = _run_argv(args, cwd=worktree)
    if not result.ok:
        return Landing(PR_FAILED, detail=result.stderr.strip()[-300:] or "gh pr create failed")
    url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return Landing(LANDED, pr_url=url)
