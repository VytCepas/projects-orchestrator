# ADR-006: Autonomous Heal — Scoped Agent Dispatch for Failing Gates

- Status: accepted
- Date: 2026-07-05

## Context and Problem Statement

ADR-003 fixed the orchestrator as a contract *reader*: it detects a failing
lint/test gate but has no way to repair one. An operator governing several
projects wants failures fixed without manually opening each child repo —
ideally triggered from the same failure data the engine already collects
(`checks.py`/`cache.py`), and eventually on a schedule so it runs unattended.
How can the orchestrator dispatch real code changes into a child project
without breaking the never-write invariant that makes the rest of the engine
safe to run against a fleet of repos the operator doesn't want silently
mutated?

## Considered Options

- No change — heal stays a human-only action; the orchestrator only reports.
- Autonomous fix that commits and pushes straight to the child's default branch.
- Autonomous fix that lands as a **pull request**, gated on a re-verified pass,
  never touching the default branch directly.

## Decision Outcome

Chosen option: **PR-gated autonomous fix** (`heal.py`, wired as the `heal
<project>` controller verb). Concretely:

1. **Scope is narrow and explicit.** Only cached `lint`/`test` failures
   (`HEALABLE_TASKS`) are eligible — the two gates that are locally
   runnable and locally re-verifiable. `ci`/`cloud` failures depend on
   remote state a local run can't reproduce, so they're excluded from this
   loop entirely.
2. **The fix runs on a dedicated branch, never in place.** `heal_project`
   requires a clean worktree, checks out `heal/<tasks>-<project>`, and
   restores the original branch on every exit path (success or failure) —
   the rest of the engine (`status.py`, the checks cache's `head` stamp)
   assumes one stable "current" branch per project, and heal must not
   violate that between commands.
3. **A scoped coding agent edits files; the orchestrator does everything
   else.** The agent (the `claude` CLI, headless, `bypassPermissions`, tools
   limited to `Bash,Edit,Write,Read,Grep,Glob`) is told exactly which
   command is failing and its last-known error, and is explicitly told not
   to commit. `heal.py` re-runs the same declared gate(s) after the agent
   returns; only a **verified pass** is committed.
4. **Nothing reaches the default branch unreviewed.** A verified fix is
   pushed to its heal branch and a PR is opened (`gh pr create`) — never a
   direct push to the child's default branch, regardless of how the loop
   is triggered (on-demand or scheduled). This is the safety net that makes
   `bypassPermissions` an acceptable trade-off for the agent step: a bad
   autonomous fix produces, at worst, a PR nobody merges.
5. **Every external effect is injectable and never raises**, consistent with
   ADR-003: `agent_run` and `open_pr` are swappable (tests inject fakes; no
   live agent or GitHub call ever runs in CI), and a dirty worktree,
   failed checkout, agent failure, failed re-verification, or failed
   push/PR all degrade to a typed `HealResult` rather than an exception.
6. **Every git/gh call is argv-only, never a shell string.** `descriptor.name`
   (and the branch derived from it) comes from the child's own
   `.claude/config.yaml` — unlike `tooling.*_command`, ADR-003's "trusted
   shell string" trust level was never extended to it. `_run_argv` runs
   `git`/`gh` via `subprocess.run` with a plain argv list (no shell), so a
   crafted project name cannot be interpreted as shell code at any of the
   checkout/commit/push/PR/branch-restore call sites.

### Consequences

- Good: closes the detect-but-can't-fix gap without weakening the
  contract-reader boundary for every other command — `heal` is the one
  explicit, opt-in exception, and it still can't merge anything itself.
- Good: the same `HealResult`/`render_heal_result` shape is reusable by a
  future scheduled trigger (a periodic job that runs `checks`/`audit` and
  calls `heal_project` on any failing project) without new plumbing.
- Bad: `bypassPermissions` means the agent step has full tool access within
  the child's directory for the duration of one run — mitigated by the
  PR gate, but a real trust boundary the operator should know about before
  enabling `heal` on a project they don't fully own.
- Bad: `ci`/`cloud` failures still require a human — accepted for now
  rather than guessing at fixes for state the local run can't observe.
