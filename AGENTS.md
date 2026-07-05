# projects-orchestrator

Cross-project orchestration layer for agentic development

Canonical instructions for **all** coding agents (and humans pairing with
them). Claude Code reads [CLAUDE.md](CLAUDE.md), which redirects here.

## Start here

Agent-neutral infrastructure lives under [`.claude/`](.claude/) — the
directory name is historical; the contents (bash lifecycle scripts, markdown
skills, memory, and docs) are readable by any agent. Harness-specific wiring is
*not* kept here: each surface you enable with `--agents` gets its own generated
config in its own directory (`.codex/`, `.agents/`, `.cursor/`, `.amp/`,
`.junie/`, `.vscode/`, plus root `.mcp.json`). Within `.claude/`, only Claude
Code's own hook configuration is Claude-specific (see "Claude Code specifics"
below); everything else is reusable by any agent.

- [`docs/explanation/overview.md`](docs/explanation/overview.md) — the whole system on one page: capabilities, the read-only project-init contract boundary, and the architecture (read this for the mental model)
- [`.claude/project-init.md`](.claude/project-init.md) — workflow, conventions, task tracking
- [`.claude/memory/MEMORY.md`](.claude/memory/MEMORY.md) — memory index (read first for context)
- [`.claude/docs/`](.claude/docs/) — system of record: ADRs and development guides
- `.claude/docs/CODE_MAP.md` — generated map of what each module does; **read before grepping** (generate/refresh with `just code-map`)

## Skills (load on demand)

Before any GitHub action (create issue, branch, push, PR, merge), check
the available skills (plugin-provided in the default scaffold; `/help` lists them) and load the relevant
skill file. Skills are plain markdown (SKILL.md) — load only the one that
matches what you are about to do. Claude loads them directly; Codex and
Antigravity read them from `.agents/skills/`. Surfaces with no skills directory
(e.g. Cursor, VS Code Copilot) — and any surface in the default plugin scaffold,
where the skill files live in the plugin rather than `.claude/skills/` — instead
follow the inline quick-references in the rules below.

> Scaffolded with [project-init](https://github.com/VytCepas/project-init) on 2026-06-28.

## Key rules for agents

- **TDD** — write failing tests before any implementation.
- **GitHub Projects** — work is tracked on the GitHub Projects board backed by GitHub Issues. Before starting non-trivial work, create or reference an issue.
- **GitHub workflow** — for any push, PR, review, or merge action, load the `github_workflow` skill. Quick ref: branch = `<type>/<KEY>-<n>-<slug>` | PR title = `type(KEY-N): desc` (no scope = no issue) | body includes `Closes #N`.
- **Commands** — the `justfile` is the canonical command surface: `just --list` shows every recipe (`setup`, `lint`, `format`, `test`, `docs`, `ci`). Prefer `just <recipe>` over raw tool invocations so every agent and CI run the same commands.
- **Lint** — `just lint` must pass before closing a task. The linter config enforces docstrings and complexity caps on project code — fix the code, don't loosen the gate.
- **Docs** — follow the Diátaxis layout in [`docs/`](docs/) (see `docs/index.md`). Record architectural decisions with the `add_adr` skill.
- **Ownership boundaries** — each tool owns exactly one concern: uv/bun own dependencies, `just` owns commands, `.env` owns environment variables. Don't blur them (no mise tasks/env, no version pins in scripts, no commands outside the justfile).
- **No secrets in code** — never hardcode API keys, tokens, or personal data. Copy `.env.example` to `.env` (gitignored) and load it explicitly; see `.claude/docs/guides/secrets.md` for the escalation path to org secret managers.
- **No prod credentials in agent sessions** — destructive infra/DB commands are flagged by the `prod_guard` hook (ask interactively, hard-block in fully autonomous mode; escape hatch: `safety.allow` in `.claude/config.yaml`). On the non-Claude surfaces (Codex/Cursor/Antigravity) the same guard fires via the shared adapter and **hard-blocks wherever the surface actually enforces the hook** (those surfaces are non-interactive, so "ask" isn't possible) — but hook enforcement is best-effort and surface-dependent (e.g. some Codex versions gate project-scoped hooks behind a one-time trust/enable step; see the advisory below), so treat it as a guardrail, not a guarantee (PI-394). The real guarantee is credential separation: production credentials belong to review-gated CI jobs, never to a shell an agent runs in (ADR-012).
- **Supply-chain package check** — `uv add`/`bun add`/`pip install`/`npm install`/`cargo add` are checked against the PyPI/npm/crates.io registry before the install runs: a name that doesn't exist (likely a typo or hallucinated dependency) or that's suspiciously close to a popular package (possible typosquat) is flagged by the `package_guard` hook, same ask/hard-block split as `prod_guard`. Network failures fail open (never blocks an install just because the registry couldn't be reached) — a guardrail, not a substitute for lockfile pinning and hash verification (PI-564).
- **Enforcement is agent-agnostic** — secret scanning (gitleaks) and lifecycle gating run as git hooks plus CI checks (`validate-pr`, `secret-scan`), binding every agent and human alike. Run `.claude/scripts/install_hooks.sh` once per clone to activate them.
- **Agent support tiers** — only the Claude Code path is functionally CI-tested. Codex: skills are discoverable under `.agents/skills/` and the command + destructive-command (`prod_guard`) + package-guard (`package_guard`) guards are wired via `.codex/hooks.json` (adapter: `.claude/hooks/agent_guard_adapter.py`) — **advisory**: some Codex versions gate project-scoped hooks behind a one-time trust/enable step before they fire, so git + CI remain the real boundary. Agent overlays are validated by contract tests on the rendered files, not by running those agents; the real security boundary for every agent is the git/CI enforcement above.


## Claude Code specifics

This section applies only when the agent is Claude Code. Other agents get
no automatic hooks — rely on the git/CI enforcement above.

- Hooks fire automatically via the `project-init-workflow` plugin (enabled in `.claude/settings.json` under `enabledPlugins`, not a local `hooks` block): `pre_commit_gate` (lint gate on commit), `github_command_guard` (steers toward lifecycle scripts), `post_edit_lint`, `workflow_state_reminder`, `prod_guard`, `package_guard`. The wired scripts live under the plugin's `CLAUDE_PLUGIN_ROOT`/hooks; `.claude/hooks/` keeps only the libraries the lifecycle scripts call (`dag_workflow.py`, `prod_guard.py`, `package_guard.py`, `_py.sh`). To see or change the wiring, edit the plugin, not `settings.json`.

- Skills are auto-discovered and invocable as `/command`s.
- Review plugins: `pr-review-toolkit@claude-plugins-official` is pre-enabled in `.claude/settings.json`; also consider `/plugin install code-review@claude-plugins-official` for full PR reviews.
