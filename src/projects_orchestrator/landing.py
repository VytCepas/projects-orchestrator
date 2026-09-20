"""The write boundary — an agent run's work leaves here as a draft PR, or not at all.

Every mutation an agent run makes to a child repo passes through this module.
There are exactly two sanctioned writes:

1. push a **new, non-protected branch**, and
2. open a **draft pull request** from it.

Nothing else. No push to the default branch, no force-push, no merge, no tag —
not because the caller happens not to ask, but because this module refuses.

**Notification writes.** Notify-mode heal (ADR-008, #164) reports a failing gate
as a GitHub issue on the failing project, and closes it once the gate passes.
Those writes live here too, so every write heal can cause stays in one module,
and they are refused on the same principle: this module opens only an issue
whose body carries its own marker, and closes only an issue whose body, re-read
from GitHub at close time, still carries that marker. It never edits, reopens
or closes an issue a person filed.

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

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.runner import RunResult

_log = logging.getLogger(__name__)

_GIT_TIMEOUT = 30.0

#: A branch name we are willing to push, defined by ALLOWLIST rather than by
#: blocklisting the bad shapes we happen to think of — the blocklist approach is
#: exactly what produced the `-f`-substring flake. Slashes are allowed (agent
#: branches are `heal/...`, `work/...`), but a COLON is not: `heal/x:main` is a
#: refspec `src:dst`, and `--` ends git's *option* parsing, not its *refspec*
#: parsing, so a value with a colon can update an arbitrary remote ref. Nothing
#: here can be read as a flag, a refspec, a ref path, or whitespace.
_VALID_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")

#: Refs an agent run may never write to, whatever it is asked. `main`/`master` are
#: the obvious ones; `HEAD` and `@` are the ones someone reaches for when being
#: clever. The repo's *actual* default branch is resolved separately and added to
#: this set, because a child is free to call its trunk anything at all.
_ALWAYS_PROTECTED = frozenset({"main", "master", "trunk", "develop", "HEAD", "@"})

REFUSED = "refused"
COMMIT_FAILED = "commit_failed"
NOTHING_TO_COMMIT = "nothing_to_commit"
PUSH_FAILED = "push_failed"
PR_FAILED = "pr_failed"
ISSUE_FAILED = "issue_failed"
LANDED = "landed"


@dataclass(frozen=True)
class Landing:
    """The outcome of trying to land a run's work.

    Attributes:
        status: :data:`LANDED`, :data:`REFUSED`, :data:`PUSH_FAILED`,
            :data:`PR_FAILED`, or (for an issue write) :data:`ISSUE_FAILED`.
        pr_url: The draft PR, when one was opened; for an issue write, the
            issue's URL.
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
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        # ValueError: an argv element holding a NUL byte is refused before exec.
        # Gate output reaches argv through an issue body, so it degrades here
        # like any other failed launch rather than escaping the never-raise
        # engine (ADR-003).
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


def _why(result: RunResult, fallback: str) -> str:
    """The tail of what a failed command said, or ``fallback`` when it said nothing."""
    return (result.stderr or result.error or "").strip()[-300:] or fallback


#: A remote URL's authority and path, in the two shapes git accepts. They are
#: separate patterns because a colon means different things in each: in
#: `ssh://host:22/owner/name` it introduces a PORT, and in scp-style
#: `git@host:owner/name` it separates the host from the path. One pattern that
#: treats every colon alike cannot read `https://ghes.example:8443/acme/alpha`
#: (Codex on #296, verified: the old pattern returned no match at all).
#:
#: The host is NOT pinned to ``github.com``: this system is host-aware by
#: decision (project-init ADR-013, spike #254), covering GHE.com and GitHub
#: Enterprise Server, and a pattern that only knew ``github.com`` would refuse
#: every write on an Enterprise child — *after* the branch had already been
#: pushed.
_AUTHORITY = r"[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]"
_SCHEME_URL = re.compile(
    rf"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?:[^@/]+@)?"
    rf"(?P<host>{_AUTHORITY})(?P<port>:[0-9]{{1,5}})?/(?P<path>.+)\Z"
)
#: The only schemes whose port is the port ``gh`` should dial. `--repo
#: host:2222/owner/name` makes gh request `https://host:2222/api/graphql`, so
#: carrying an `ssh://…:2222` transport port into the answer points every write
#: at the SSH daemon (Codex on #296 — and at a test case of mine that blessed it).
_API_SCHEMES = frozenset({"http", "https"})
#: scp-style takes no port — `git@host:8443/owner` means the PATH `8443/owner`.
#: The path's leading `[^/]` is what keeps the two patterns order-independent: it
#: is the reason `ssh://git@host/owner/name` cannot also parse as scp with the
#: authority `ssh` and the path `//git@host/…`. Swapping the two is therefore an
#: equivalent mutant, and only while that character stays.
_SCP_URL = re.compile(rf"(?:[^@/:]+@)?(?P<authority>{_AUTHORITY}):(?P<path>[^/].*)\Z")

#: ``owner/name``, in the character set GitHub allows for each, so nothing parsed
#: out of a remote can be read by ``gh`` as a flag or a path.
_OWNER_NAME = re.compile(
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9-]*)/(?P<name>[A-Za-z0-9._-]+?)(?:\.git)?/?\Z"
)


def _authority_and_path(url: str) -> tuple[str, str]:
    """The host ``gh`` should be given and the path after it; ``("", "")`` for neither shape."""
    with_scheme = _SCHEME_URL.match(url)
    if with_scheme:
        port = with_scheme["port"] or ""
        if with_scheme["scheme"].lower() not in _API_SCHEMES:
            port = ""
        return with_scheme["host"] + port, with_scheme["path"]
    scp = _SCP_URL.match(url)
    return (scp["authority"], scp["path"]) if scp else ("", "")


def origin_repo(repo: Path) -> str:
    """``host/owner/name`` for ``repo``'s ``origin`` remote; ``""`` when there is none.

    Every ``gh`` write this module makes names its repository explicitly, because
    ``gh``'s own answer is not ``origin``. In a clone with a second remote and no
    ``gh repo set-default``, ``gh`` resolves the base repository to ``upstream``
    (measured with gh 2.98.0: in a clone whose ``origin`` was this project and
    whose ``upstream`` was another, ``gh issue list`` returned the *other* repo's
    issues). A fork clone healed by this system would therefore have opened its
    draft PRs, and filed and closed its notify-mode issues, on somebody else's
    repository — the one place the blast radius is not ours to take (#286).

    The authority travels with the answer rather than being assumed or dropped,
    port included (``gh`` keeps it as the HTTP Host — verified: ``--repo
    localhost:8443/foo/bar`` reaches ``https://localhost:8443/api/graphql``,
    where a malformed value is rejected at argument parsing instead). A lookalike
    host is therefore preserved, not silently read as ``github.com``, and an
    Enterprise child keeps working. What this does NOT do is decide which forge a
    host belongs to — ``gh`` knows which hosts it is configured for and fails
    loudly on one it does not, and guessing from the hostname would be the
    confident-wrong answer this module refuses elsewhere.

    ``""`` is a refusal, not a default: a caller that fell back to ``gh``'s own
    resolution would reintroduce exactly the behaviour this exists to prevent. A
    local path has neither of the two shapes and is refused by that alone.
    """
    remote = _run_argv(["git", "remote", "get-url", "origin"], cwd=repo)
    if not remote.ok:
        _log.warning(
            "cannot read origin in %s: %s", repo, _why(remote, "git remote get-url failed")
        )
        return ""
    authority, path = _authority_and_path(remote.stdout.strip())
    owner_name = _OWNER_NAME.match(path)
    if not (authority and owner_name):
        return ""
    return f"{authority}/{owner_name['owner']}/{owner_name['name']}"


def _not_github(repo: Path) -> Landing:
    return Landing(
        REFUSED,
        detail=(
            f"refusing to write to GitHub from {repo}: its 'origin' remote is not a "
            "repository gh can be pointed at, and gh would pick a base repository of its own"
        ),
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
        return _strip_ref_prefix(local.stdout.strip())

    # origin/HEAD is only populated by `clone` (and by `remote set-head`), so a
    # repo that was `init`-ed and given a remote by hand has none. Ask the remote.
    remote = _run_argv(["git", "ls-remote", "--symref", "origin", "HEAD"], cwd=repo)
    for line in remote.stdout.splitlines():
        if line.startswith("ref:"):
            return _strip_ref_prefix(line.split()[1])
    return ""


def _strip_ref_prefix(ref: str) -> str:
    """Drop the fixed ``refs/…/`` prefix, keeping the WHOLE branch name.

    Not ``rsplit('/', 1)`` — a default branch may itself contain a slash
    (``release/2026``), and taking only the last path component would return
    ``2026``. :func:`is_protected` would then fail to recognise the real default
    and let a push to it through. Strip the KNOWN prefix, keep everything after.
    """
    for prefix in ("refs/remotes/origin/", "refs/heads/", "refs/remotes/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


def is_protected(branch: str, repo_default: str = "") -> bool:
    """Whether ``branch`` is a ref an agent run may never write to (pure).

    Defined by ALLOWLIST: anything that is not a syntactically valid plain branch
    name is protected, then the always-protected names and the repo's own default
    are refused on top. The allowlist is the point — a blocklist of bad shapes is
    what produced the ``-f``-substring flake, and it is one clever ref syntax away
    from being wrong again. In particular ``heal/x:main`` is refused here because
    the colon fails :data:`_VALID_BRANCH`, before it can reach git as a refspec.
    """
    candidate = branch.strip()
    if not _VALID_BRANCH.match(candidate):
        return True  # not a plain branch name → not something we created → no
    if ".." in candidate or candidate.endswith(".lock"):
        return True  # git refspec/lock syntax that slips past a char-class check
    if candidate.startswith(("refs/", "heads/", "remotes/", "origin/")):
        return True  # a ref PATH, not a branch — slashes are legal, so match by prefix
    if candidate in _ALWAYS_PROTECTED:
        return True
    return bool(repo_default) and candidate == repo_default


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
    # Defence in depth. `is_protected` already refused anything that is not a
    # plain branch name, but the refspec is ALSO fully qualified on both sides
    # rather than passing the bare name: `refs/heads/<b>:refs/heads/<b>` can only
    # ever create/update the branch `<b>`, whereas a bare `heal/x:main` is a
    # `src:dst` refspec that updates `main`. `--` ends git's *option* parsing, not
    # its *refspec* parsing, so it does not help here — the qualification does.
    # No `--force`: an agent run creates history, it does not rewrite it.
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    pushed = _run_argv(["git", "push", "--set-upstream", "origin", "--", refspec], cwd=worktree)
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
    target = origin_repo(worktree)
    if not target:
        return _not_github(worktree)
    args = [
        "gh",
        "pr",
        "create",
        "--repo",
        target,
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


def commit_all(worktree: Path, message: str) -> Landing:
    """Stage and commit everything the agent changed; report the outcome.

    The briefing tells the agent NOT to commit — the harness owns the commit
    (ADR-007 §3) — so the agent's edits sit uncommitted in the worktree. No gate of
    the orchestrator's runs first: this commits whatever the agent left (#255).
    Skipping this and pushing straight away sends only the branch ref cut from
    ``HEAD``: an empty diff, and a PR with nothing in it (or a ``gh pr create``
    that fails for having no commits). The commit is not optional plumbing; it is
    how the agent's work actually reaches the branch.

    A worktree with no changes returns :data:`NOTHING_TO_COMMIT` rather than a
    misleading success — an agent that changed nothing has not produced a PR-worthy
    result, and committing ``--allow-empty`` would paper over that.
    """
    if not _run_argv(["git", "add", "-A"], cwd=worktree).ok:
        return Landing(COMMIT_FAILED, detail="git add failed")
    status = _run_argv(["git", "status", "--porcelain"], cwd=worktree)
    if status.ok and not status.stdout.strip():
        return Landing(NOTHING_TO_COMMIT, detail="the agent made no changes")
    committed = _run_argv(["git", "commit", "-m", message], cwd=worktree)
    if not committed.ok:
        return Landing(COMMIT_FAILED, detail=committed.stderr.strip()[-300:] or "git commit failed")
    return Landing(LANDED)


# --- Notification writes: issues this module filed, and only those (#164) -------

#: The identity of a notify-mode finding, as it is written into the issue body.
#: A key is ``<project>/<gate>``, both parts restricted to characters that cannot
#: close the HTML comment or smuggle a second marker into it.
_KEY_PART = r"[A-Za-z0-9._-]+"
_MARKER = re.compile(rf"<!-- projects-orchestrator:heal-notify key=({_KEY_PART}/{_KEY_PART}) -->")
_VALID_KEY = re.compile(rf"{_KEY_PART}/{_KEY_PART}\Z")

#: How many open issues one read may return. A repo with more open issues than
#: this cannot be deduplicated from one page, so the read reports "unknown"
#: rather than a partial list that would let a duplicate through.
ISSUE_LIST_LIMIT = 500


@dataclass(frozen=True)
class OwnIssue:
    """An open issue this module filed, recognised by its marker.

    Attributes:
        number: The issue number in its repository.
        key: The finding it reports, ``<project>/<gate>``.
        url: The issue's web URL.
    """

    number: int
    key: str
    url: str = ""


def issue_marker(key: str) -> str:
    """The hidden marker that identifies a finding's issue; ``""`` for an unsafe key (pure)."""
    if not _VALID_KEY.match(key):
        return ""
    return f"<!-- projects-orchestrator:heal-notify key={key} -->"


def marker_key(body: str) -> str:
    """The finding key an issue body carries, or ``""`` when it carries none (pure)."""
    match = _MARKER.search(body or "")
    return match.group(1) if match else ""


def own_open_issues(repo: Path, limit: int = ISSUE_LIST_LIMIT) -> tuple[OwnIssue, ...] | None:
    """The open issues in ``repo``'s GitHub repository that carry a finding marker.

    Only issues opened by the account ``gh`` is signed in as are read. On a public
    repository anyone can open an issue, and one that pasted a marker would
    otherwise suppress the real report and later be closed as if it were ours.

    Returns ``None`` when the answer is unknown: ``gh`` failed, its output did not
    parse, or the page was full, so an issue beyond it could be missed. Unknown
    is not "none": a caller that read ``None`` as "no open issues" would file a
    duplicate every pass.
    """
    target = origin_repo(repo)
    if not target:
        _log.warning("%s: origin is not a GitHub repository, so its issues cannot be read", repo)
        return None
    listed = _run_argv(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            target,
            "--state",
            "open",
            "--author",
            "@me",
            "--limit",
            str(limit),
            "--json",
            "number,url,body",
        ],
        cwd=repo,
    )
    if not listed.ok:
        _log.warning("gh issue list failed in %s: %s", repo, _why(listed, "no output"))
        return None
    try:
        rows = json.loads(listed.stdout or "[]")
    except ValueError as exc:
        _log.warning("gh issue list returned unparseable JSON in %s: %r", repo, exc)
        return None
    if not isinstance(rows, list):
        _log.warning("gh issue list returned %s, not a list, in %s", type(rows).__name__, repo)
        return None
    if len(rows) >= limit:
        _log.warning(
            "%s has at least %d open issues; one page cannot rule out a duplicate", repo, limit
        )
        return None
    issues: list[OwnIssue] = []
    for row in rows:
        if not isinstance(row, dict):
            continue  # expected: a row gh did not shape as an object carries no marker
        key = marker_key(str(row.get("body") or ""))
        number = row.get("number")
        if key and isinstance(number, int):
            issues.append(OwnIssue(number=number, key=key, url=str(row.get("url") or "")))
    return tuple(issues)


def open_issue(repo: Path, title: str, body: str) -> Landing:
    """File one issue in ``repo``'s GitHub repository; refuse a body without a marker.

    The marker is what lets a later pass find this issue again, deduplicate
    against it, and close it. An issue filed without one could never be closed by
    the pass that opened it, so it is refused rather than orphaned.
    """
    if not marker_key(body):
        return Landing(REFUSED, detail="refusing to file an issue that carries no finding marker")
    target = origin_repo(repo)
    if not target:
        return _not_github(repo)
    result = _run_argv(
        ["gh", "issue", "create", "--repo", target, "--title", title, "--body", body], cwd=repo
    )
    if not result.ok:
        return Landing(ISSUE_FAILED, detail=_why(result, "gh issue create failed"))
    url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return Landing(LANDED, pr_url=url)


def close_own_issue(repo: Path, number: int, key: str, comment: str) -> Landing:
    """Comment on and close issue ``number``, only if it is still open and still ours.

    The body is re-read at close time rather than trusted from the earlier list:
    between the two, a person may have edited the issue into their own report, and
    closing that would be closing someone else's issue.
    """
    target = origin_repo(repo)
    if not target:
        return _not_github(repo)
    viewed = _run_argv(
        ["gh", "issue", "view", str(number), "--repo", target, "--json", "state,body"], cwd=repo
    )
    if not viewed.ok:
        return Landing(ISSUE_FAILED, detail=_why(viewed, "gh issue view failed"))
    try:
        issue = json.loads(viewed.stdout or "{}")
    except ValueError as exc:
        _log.warning("gh issue view #%d returned unparseable JSON in %s: %r", number, repo, exc)
        return Landing(ISSUE_FAILED, detail=f"gh issue view #{number} returned unparseable JSON")
    if not isinstance(issue, dict) or str(issue.get("state", "")).upper() != "OPEN":
        return Landing(REFUSED, detail=f"issue #{number} is no longer open")
    if marker_key(str(issue.get("body") or "")) != key:
        return Landing(REFUSED, detail=f"issue #{number} no longer carries the marker for {key}")
    closed = _run_argv(
        [
            "gh",
            "issue",
            "close",
            str(number),
            "--repo",
            target,
            "--reason",
            "completed",
            "--comment",
            comment,
        ],
        cwd=repo,
    )
    if not closed.ok:
        return Landing(ISSUE_FAILED, detail=_why(closed, "gh issue close failed"))
    return Landing(LANDED)
