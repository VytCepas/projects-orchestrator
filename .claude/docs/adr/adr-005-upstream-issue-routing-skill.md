# ADR-005: Route infrastructure bugs upstream via a default-scaffolded skill

- Status: proposed
- Date: 2026-07-02

## Context and Problem Statement

Bugs in the shared machinery that `project-init` scaffolds — hooks, CI templates,
lifecycle scripts, board automation, rules, gitleaks config, the descriptor
contract, and the skills themselves — currently get filed in whatever downstream
project the user happens to be in. The same defect is then re-reported per project
and fixed inconsistently, instead of being solved once, universally, upstream.
How should a user (or agent) reporting such a bug get it to the right place?

The fix and the routing behaviour both live **upstream in `project-init`**
(`scaffold.variables.project_init_repo`, e.g. `VytCepas/project-init`), because
they must apply to every project the scaffolder generates — not just this repo.

## Considered Options

- **Default-scaffolded "report upstream issue" skill** that classifies a report
  and routes tooling-level ones to the upstream repo.
- **Manual convention only** (document "file infra bugs upstream" in AGENTS.md).
- **MCP/cross-repo automation** that files upstream directly.

## Decision Outcome

Chosen option: **a default-on, scaffolded skill**, because a skill is
agent-agnostic, discoverable, and enforced at the moment of reporting — a prose
convention decays, and cross-repo MCP automation is unavailable in most sessions
(a session is typically scoped to one repo; the raw API token is gated).

### The skill — `report-upstream-issue`

Packaging (all via `project-init`, no downstream opt-in):
- Rendered into both `.claude/skills/report-upstream-issue/SKILL.md` and
  `.agents/skills/report-upstream-issue/SKILL.md` (agent-agnostic, like the
  existing skills). Enabled by default; listed in generated `CAPABILITIES.md`.

Behaviour / rules:
1. **Classify** the report — project code/logic vs shared scaffolding/tooling/
   governance — using a heuristics + examples table (e.g. "pre-commit hook
   wrong", "board fields don't populate", "CI template fails", "a skill misfires"
   route upstream; "my endpoint 500s", "wrong business logic" stay local).
2. **Resolve the target** from `config.yaml` -> `scaffold.variables.project_init_repo`
   (and `project_init_url`). Never hardcode the repo.
3. **Match the upstream repo's own issue conventions** (see below) by inspecting a
   few recent issues — do NOT apply the downstream scaffolded metadata template.
4. **Filing method, in priority order:**
   - **Default — simple internet route:** generate a prefilled
     `https://github.com/<repo>/issues/new?title=...&body=...&labels=...` link for
     the user to review and submit. Works with zero cross-repo write access.
   - **Secondary — direct create** via the agent's GitHub tool, only when the
     session is actually scoped to the upstream repo.
5. **Project-level** bugs are filed locally through the normal `create_issue`/
   `start_task` flow.
6. Always **confirm** the target repo + title/body before filing; suggest a quick
   duplicate check against existing upstream issues.

### Upstream issue format (differs from the scaffold)

`project-init`'s own issues do **not** use the scaffolded
`### Priority/Size/Area ... Definition of Ready/Done` metadata block. Observed
format: conventional-commit title prefixes (`feat:`/`fix:`/`docs:`), plain
numeric IDs, label-based categorization (`feature`/`enhancement`/`documentation`/
`help wanted`/`good first issue`), and a lighter prose body. The skill must detect
and conform to the target repo's conventions (title style, labels, body shape),
falling back to the prefilled-link method when it lacks write access.

### Upstream tickets to file (via prefilled links)

Both are filed in `project-init` (this repo's session cannot write there):
1. `feat: scaffold self-populating board metadata (dual-format parse + auto-ensure type label)`
2. `feat: scaffold a "report upstream issue" skill (default-on) that routes tooling bugs to project-init`

## Consequences

- Good: infra defects are fixed once upstream and propagate on `project-init upgrade`.
- Good: works from any session — the default prefilled-link route needs no
  cross-repo write access or local `gh`.
- Good: agent-agnostic; the rule lives where reporting happens, not in decaying prose.
- Bad: implementation lives upstream, so it only reaches this project after the
  next scaffold upgrade.
- Bad: classification is heuristic — the skill must confirm with the user to avoid
  mis-routing a project bug upstream or vice-versa.
