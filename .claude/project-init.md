# Project: projects-orchestrator

> Cross-project orchestration layer for agentic development

Scaffolded with [project-init](https://github.com/VytCepas/project-init) on 2026-06-28.

| | |
|---|---|
| Language | python |
| Memory stack | auto |
| MCPs | none |

## Workflow

1. **Start of session** — read [`memory/MEMORY.md`](memory/MEMORY.md), then check [`.claude/docs/adr/`](docs/adr/) for relevant decisions.
2. **During work** — permanent decisions → [`.claude/docs/adr/`](docs/adr/); reusable facts → [`memory/`](memory/).

## Tools

| Type | Name | Purpose |
|---|---|---|
| Command | `/status` | git state, recent commits, open TODOs |
| Command | `/review` | code review of staged changes or a file |
| Command | `/plan <task>` | acceptance tests first, then implementation plan |
| Command | `/request_review` | mark PR ready for review, optional agent review |
| Script | `create_issue.sh <type> "desc"` | create typed issue, prints issue number |
| Script | `create_nojira_pr.sh <type> "desc"` | branch + push + draft PR without an issue |
| Script | `setup_github.sh [branch] [--protect]` | provision board fields and review settings; `--protect` applies baseline branch protection |
| Script | `start_issue.sh <n> <type>` | branch + draft PR |
| Script | `promote_review.sh` | mark PR ready (board card moves automatically) |
| Script | `monitor_pr.sh <n> [--merge]` | wait for CI; --merge squash-merges when clean |
| Script | `finish_pr.sh [n] [--review-cycle N]` | push branch, mark ready, monitor checks/review, and merge |
| Skill | `add_hook` | add a new deterministic hook |
| Skill | `add_command` | add a new slash command |
| Skill | `audit` | full project health scan, creates issue with findings |
| Agent | `reviewer` | code review specialist |
| Agent | `researcher` | codebase explorer |

Hooks run automatically: `github_command_guard`, `workflow_state_reminder`, `pre_commit_gate`, `post_edit_lint`, `prod_guard` — supplied by the `project-init-workflow` plugin (enabled in `.claude/settings.json` under `enabledPlugins`, not a local `hooks` block); the scripts live under the plugin's `CLAUDE_PLUGIN_ROOT`/hooks, so edit the plugin to change them. Secret scanning and lifecycle gating are enforced agent-agnostically by git hooks (gitleaks pre-commit, commit-msg, pre-push — installed via `.claude/scripts/install_hooks.sh`) and mirrored in CI; the `security-guidance` plugin provides Claude-side guidance.

**Rules** (`.claude/rules/`): per-filetype conventions loaded automatically by Claude Code when you open a matching file.

## Coding standards

- No comments unless the WHY is non-obvious.
- No premature abstractions — three similar lines before extracting.
- No error handling for impossible scenarios.
- No backwards-compatibility shims — delete removed code.
- Prefer editing existing files over creating new ones.
- `just lint` must pass before closing a task (`just --list` shows all recipes).


## TDD

Write failing tests before implementation. Tests define the contract.

1. `/plan <task>` → acceptance tests (red)
2. Commit failing tests
3. Implement until green
4. Lint and clean up

Test conventions: one assertion per test, name as `test_<unit>_<scenario>`, run with `uv run pytest`. Real DB/API instances — no mocks.


## GitHub Projects tracking

Work items are GitHub Issues; the project board is GitHub Projects (kanban with To Do / In Progress / In Review / Done columns). Board cards move automatically via `board-automation.yml` — **no manual board moves needed**.

### Lifecycle: Issue → branch → draft PR → merge

Every non-trivial task follows this exact sequence. Run the scripts; the LLM's only job is writing commit messages and fixing CI failures.

| Step | Command | What happens automatically |
|------|---------|---------------------------|
| 1. Create issue | `.claude/scripts/create_issue.sh <type> "description" --priority <priority> --area "<area>" --size <size> --acceptance "<criterion>"` | Issue created with metadata, board card → **To Do** |
| 2. Start work | `.claude/scripts/start_issue.sh <issue-n> <type>` | Branch (`<issue_type>/<project_abbr>-<issue_number>-<slug>`) + push + draft PR, board → **In Progress** |
| 3. Commit | `git add … && git commit -m "type(<KEY>-<n>): message"` | `commit-msg` hook validates format; `pre_commit_gate` auto-lints |
| 4. Push | `.claude/scripts/push_branch.sh` | CI runs; if it fails a comment is posted on the PR with `gh run view --log-failed` |
| 5. Finish PR | `.claude/scripts/finish_pr.sh <n>` | Pushes, marks ready, monitors CI/review, and merges when clean |

Steps 3–4 repeat until the work is complete. **When asked to push or finish a PR, keep going until the lifecycle is complete**: `finish_pr.sh` → fix any CI or review failures → rerun with the next review cycle until merged.

### Merge setup (one-time per repo)

Automated completion via `monitor_pr.sh --merge` works after checks pass. GitHub-native auto-merge requires two repo settings enabled by an admin:

1. **GitHub Settings → General → Allow auto-merge** — tick the checkbox
2. **Branch protection rule on `main`** — require CI status checks to pass

Without GitHub-native auto-merge, `monitor_pr.sh --merge` still works by merging explicitly after CI passes.

Run the scaffolded setup helper after creating the repository:

```bash
.claude/scripts/setup_github.sh --protect
```

It attempts to configure branch protection, required checks, conversation resolution, and Copilot code review. If GitHub does not expose a setting through the API or your token lacks permission, it prints the manual setup step.

### Branch model — single trunk

This project is **single-trunk**: feature PRs target `main` and merge by squash. That keeps history linear (one commit per PR, reusing the PR title) and the lifecycle simple. Environments are a *deploy-time* concern (config + deploy target), not a branching concern.

### Commit and PR format

| What | Format | Example |
|------|--------|---------|
| Commit message | `<type>(KEY-<n>): description` | `feat(PI-42): Add OAuth login` |
| PR title | `<type>(KEY-<n>): description` | `feat(PI-42): Add OAuth login` |
| No-issue PR | `<type>: description` (no scope) | `fix: Fix typo` |
| PR body | must include `Closes #<n>` | triggers auto-close on merge |
| Branch | `<type>/<KEY>-<n>-<slug>` | `feat/PI-42-add-oauth-login` |

Types: `feat` · `fix` · `chore` · `docs` · `test`

For minor work without an issue, use `.claude/scripts/create_nojira_pr.sh <type> "description"`.
It creates or reuses a typed `nojira` branch, pushes through `push_branch.sh`,
and opens a draft PR titled `type: description` (no scope = no linked issue).

### Rules

- **No direct commits to `main`** — all changes through PRs. The `pre-push` hook enforces this locally.
- Install hooks once after cloning:
  ```bash
  .claude/scripts/install_hooks.sh
  ```
- One issue → one branch → one PR.
- Draft PRs created immediately when work starts — not when it's done.
- PRs must pass CI (tests + lint) before merging.

### Handling CI failures

When CI fails, GitHub posts a comment on the PR. Read it, fix the code, push:
```bash
gh run view --log-failed   # full failure output
# fix the code
git add … && git commit -m "fix(<KEY>-<n>): Fix failing tests"
.claude/scripts/push_branch.sh
.claude/scripts/monitor_pr.sh <n> --merge   # wait and merge when clean
```

### Code review

`finish_pr.sh` and `monitor_pr.sh --merge` wait for GitHub's review decision and print review feedback when changes are requested. Resolve actionable comments, post a review response, push with `.claude/scripts/push_branch.sh`, then rerun `monitor_pr.sh` with the next `--review-cycle`.

`/request_review` may also invoke the local `reviewer` agent for an extra pre-merge pass; that agent review is optional, but the GitHub PR review gate is part of the normal lifecycle.

### Quick reference

```bash
.claude/scripts/create_issue.sh <type> "description" --priority high --area docs --size S --acceptance "Done when ..."  # new issue → prints issue number
.claude/scripts/start_issue.sh <n> <type>             # branch + draft PR
.claude/scripts/create_nojira_pr.sh fix "description" # no-issue branch + draft PR
.claude/scripts/setup_github.sh --protect            # one-time governance setup + branch protection
.claude/scripts/promote_review.sh                     # mark PR ready for review
.claude/scripts/monitor_pr.sh <n> --merge             # wait for CI, merge when clean
.claude/scripts/finish_pr.sh <n>                      # push, ready, monitor, merge
gh run view --log-failed                              # see what CI failed
gh issue list                                         # open issues
gh pr list                                            # open PRs
```

## Conventions

- Hooks and scripts prefer bash/python. LLM calls only for generative steps.
- `AGENTS.md` is the canonical root instruction file (the standard most agents read natively). `CLAUDE.md` redirects to it.
- Everything else lives under `.claude/`.

## Compact Instructions

When compacting, preserve:
- GitHub Issue number being worked on
- Files modified this session
- Test results (pass/fail count, failing test names)
- Unresolved errors or lint failures
- Decisions not yet committed to `.claude/docs/adr/`
