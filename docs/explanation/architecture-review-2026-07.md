# Architecture review — projects-orchestrator (2026-07-02)

Full-project review: the project-init integration, every scaffold layer
(workflows, scripts, hooks, code, docs, memory), the ticket backlog, and an
improvement plan toward the stated goal — **one controllable interface that
governs and monitors all child projects**.

## 1. What this repo is today

Two very different layers coexist:

- **Process harness (mature).** A project-init 0.5.2 scaffold with a complete
  single-repo GitHub lifecycle: issue templates + metadata validation, board
  automation to GitHub Project #7, branch/PR naming gates, CI with a coverage
  gate, gitleaks, release automation, lifecycle scripts (`.claude/scripts/`),
  and agent guards (`dag_workflow.py`, `prod_guard.py`) delivered via the
  `project-init-workflow` plugin.
- **Product (stub).** `src/projects_orchestrator/` is a `--version`-only CLI
  with 3 smoke tests. No descriptor registry, no monitor, no runner, no TUI,
  no multi-repo code exists yet. `README.md` says so explicitly.

The gap matters because the backlog (epic #21) already references modules
(`status.py`, `descriptor`, `runner`) and decisions ("ADR-003 descriptor
registry", "ADR-004 monitor/runner/TUI") **that exist nowhere in the repo** —
the tickets were written against a design that was never committed.

## 2. project-init integration review

How this repo is coupled to [VytCepas/project-init](https://github.com/VytCepas/project-init):

| Surface | Mechanism | State |
|---|---|---|
| Claude hooks | `project-init-workflow` + `project-init-lifecycle` plugins via `enabledPlugins` in `.claude/settings.json` | wired; depends on plugin marketplace being reachable/trusted on each machine |
| Skills | plugin-provided; mirrored to `.agents/skills/` for Codex/others | present |
| Scaffold record | `.claude/config.yaml` (`scaffold.manifest` SHA-256 map) + `.claude/.upgrade-base.json` (pristine file snapshot) | present; drives drift detection |
| Descriptor contract | `config.yaml` `project_init_contract_version: 1`, memory tier/paths; `.claude/CAPABILITIES.md` | present — this is the machine-readable surface a root orchestrator reads |
| Upgrade channel | `.github/workflows/project-init-upgrade.yml` re-renders from upstream `main` and opens a PR | **manual-only and partially broken** (below) |
| Git enforcement | `.github/hooks/{pre-commit,commit-msg,pre-push}` installed via `install_hooks.sh` | scripts exist; **not installed in this clone** — enforcement is opt-in per clone |

### Findings — integration

1. **Upgrade PR #4 is stranded.** The 0.5.2 → 0.5.3 upgrade PR is open with
   *zero CI checks reported* (PRs opened with `GITHUB_TOKEN` don't trigger
   workflows; the file documents that `UPGRADE_PR_TOKEN` PAT is required,
   `project-init-upgrade.yml:9-12`). It also fixes two top issues found below
   (hardcoded board number, `ci-gate` required check). Merging it should be
   the first action.
2. **Weekly upgrade schedule is commented out**
   (`project-init-upgrade.yml:15-18`) — the integration never runs unless a
   human clicks *Run workflow*, so child repos will silently drift from
   upstream.
3. **Upgrades track upstream `main`, unpinned, with `--apply --accept-new all`**
   (`:48-49`). Any upstream breakage lands unreviewed in the render. Should
   pin to release tags and drop `--accept-new all` for consent-gated additions.
4. **Duplicate-PR guard is check-then-act** (`:65-70`) — two concurrent runs
   can open two upgrade PRs. Add a `concurrency:` group.
5. **Hook enforcement is plugin- and clone-dependent.** No `hooks` block in
   `settings.json` (by design — plugin supplies them), and git hooks require
   a one-time `install_hooks.sh` per clone (not run in fresh clones, incl.
   remote/CI sessions). The only unconditional boundary is CI. This matches
   ADR-007's stated philosophy but should be stated as an explicit trust
   model: **CI is the boundary; everything client-side is advisory.**

## 3. Layer-by-layer findings

### 3.1 GitHub workflows (CI/automation layer)

- **`issue-validation.yml` fails on every issue event** (9/9 recent runs red).
  Two independent bugs:
  - No `GH_REPO: ${{ github.repository }}` env and no checkout, so every
    `gh issue edit/comment` dies with `fatal: not a git repository`
    (`validate-pr.yml:21-22` does this correctly). The needs-info
    label/comment feedback loop has never reached an issue.
  - Type-label taxonomy mismatch: the validator requires
    `{feature, bug, chore, documentation, test}` (`issue-validation.yml:38`),
    but live issues carry GitHub's default `enhancement` label — so even
    complete tickets (#14–#21) exit 1.
- **Board project number hardcoded**: `PROJECT_NUMBER: 7`
  (`board-automation.yml:33`); `config.yaml:15 github_project_number: 7`
  claims to be the source of truth but **nothing reads it** (also
  `${PROJECT_NUMBER:-7}` literals in `create_issue.sh:418`,
  `setup_github.sh:155`). PR #4 fixes this.
- **Required-check contexts don't match rendered names.**
  `setup_github.sh:66-71` requires `CI / Lint and test`, but that job is a
  matrix — GitHub reports `Lint and test (3.11)` etc., so the required check
  can hang forever. PR #4's `ci-gate` job is the right fix. `review/decision`
  is required but only posted after a review event — a PR with no review has
  a permanently missing required check (chicken-and-egg;
  `monitor_pr.sh:153` works around it).
- **No `concurrency:` groups anywhere** — concurrent board-automation runs do
  read-modify-write on the same board (lost updates).
- **Board item lookup caps at `items(first: 100)`**
  (`board-automation.yml:93,115`, same in `create_issue.sh`) — breaks
  silently past 100 board items. Needs pagination for a fleet-scale board.
- Minor: `board-automation.yml` silently no-ops when `PROJECT_TOKEN` is
  unset; `.gitleaks.toml:12-15` comment describes a different path than it
  allowlists; `renovate.json` pins action digests while `release.yml:70-71`
  relies on a version tag for its custom manager.

### 3.2 Lifecycle scripts and guards

- Well-built single-repo harness: shims → `_py.sh` → `dag_workflow.py`
  (lifecycle DAG + command guard), `monitor_pr.sh` (poll/merge/escalate),
  `setup_github.sh` (governance provisioning), `create_issue.sh` (metadata +
  board field sync + native sub-issue linking).
- **Everything is single-repo.** Each script resolves the current checkout's
  repo; there is no repo list, fan-out, or aggregation. The only cross-repo
  primitive today is `create_issue.sh --parent owner/repo#N` (native
  sub-issue linking across repos, `create_issue.sh:243-258,589`) — a good
  seed for parent-orchestrator → child hierarchies.
- `base_branch()` hardcoded to `main` (`gh_host.sh:68-70`); board owner
  assumed == repo owner (`create_issue.sh:470-473`) so org-level boards
  spanning repos are invisible; `monitor_pr.sh` org-profile `BLOCKED` path
  dead-ends (`monitor_pr.sh:138-143,294`); `prod_guard`'s `rm -rf` rule
  misses `./`-relative and `$HOME`-var forms (guardrail, not boundary — fine
  per ADR-012, but worth noting).
- `install_hooks.sh` copies hooks but ignores a configured `core.hooksPath`
  (husky et al. would silently disable them).

### 3.3 Code, docs, memory

- CLI stub + 3 smoke tests; the 70 % coverage gate is trivially met and
  currently measures nothing meaningful.
- `just ci` runs `lint test` locally but real CI runs `test-cov` — local
  gate is weaker than CI (`justfile:34` vs `ci.yml:124-132`).
- `just code-map` and the "read CODE_MAP.md before grepping" guidance point
  at a file that **doesn't exist** (never generated/committed).
- Diátaxis `docs/` are all "No X yet" stubs; memory files
  (`project_context.md`, `user_role.md`, `feedback_conventions.md`) are
  empty templates — zero captured project knowledge after two working
  sessions (decisions like the quality-stack triage and the epic design
  live only in closed issues/chat).
- ADR trail is broken: only ADR-002 exists locally; ADR-001 is referenced by
  `gen_code_map.py:5`; issues reference ADR-003/ADR-004; `mkdocs.yml`/
  `config.yaml` reference upstream project-init ADRs with overlapping
  numbers (a namespace collision worth disambiguating, e.g. `PI-ADR-nnn`
  vs local `ADR-nnn`).

### 3.4 Tickets / board

- **#5–#13** (mypy, rust/ts/go strictness, shellcheck, mutmut, semgrep,
  supply-chain guard, fresh-context reviewer) — all closed `not_planned`,
  no comments. Presumably triaged upstream to project-init, but there is no
  cross-reference recorded on any of them; the rationale is unrecoverable
  from the repo.
- **Epic #21 with #14–#20** — well-structured (dependency order, acceptance
  criteria, metadata blocks) but:
  - children are markdown checkboxes, **not native sub-issues**, so the
    board/epic can't roll up progress (and `create_issue.sh --parent`
    exists precisely for this);
  - they carry `enhancement` instead of the scripted `feature` label —
    created outside the scripted lifecycle, which is why the (broken)
    validator also flags them;
  - they cite ADR-003/ADR-004 and `status.py`/`descriptor`/`runner` that
    don't exist — the design must be committed as ADRs before
    implementation starts, or the epic's references dangle.

## 4. Improvement plan — toward one controllable interface

### Phase 0 — make the existing governance actually work (hours)

1. Merge **PR #4** (close/re-push if checks won't trigger; set
   `UPGRADE_PR_TOKEN` PAT so future upgrade PRs get CI).
2. Fix `issue-validation.yml`: add `GH_REPO`, align the label set with
   `create_issue.sh` (`feature`, not `enhancement`), and relabel #14–#21.
3. Convert #14–#20 into **native sub-issues of #21**.
4. Write **ADR-003 (descriptor registry)** and **ADR-004
   (monitor/runner/TUI)** so the epic's references resolve; adopt a
   convention for citing upstream project-init ADRs.
5. Re-enable the weekly `project-init-upgrade` schedule; pin to release
   tags; add `concurrency:` groups to upgrade + board workflows.
6. Housekeeping: generate/commit `CODE_MAP.md` (or drop the guidance until
   there's code), align `just ci` with CI (`test-cov`), fill the three
   memory templates, record the #5–#13 triage rationale.

### Phase 1 — fleet registry (the missing foundation, before epic #21)

The epic starts at "checks engine" but nothing defines *which projects*
exist. Add the layer the scaffold already anticipates:

- `descriptor.py` — read a child's `.claude/config.yaml` (contract v1:
  name, language, tooling commands, memory tier/path, delivery) +
  `CAPABILITIES.md`. This is ADR-003's subject.
- `registry.py` — a root-level `projects.yaml` (or a scan root) listing
  child repos/paths; resolve → list of descriptors. Local paths first;
  `owner/repo` remotes later.
- `status.py` — per-project git health: branch, dirty/clean, ahead/behind,
  last commit, **scaffold drift** (compare files against
  `scaffold.manifest` hashes — drift detection falls out of the existing
  contract for free) and project-init version vs upstream.

This makes issues #14–#19 implementable exactly as written.

### Phase 2 — checks engine + TUI (epic #21 as ticketed)

#14 checks engine → #15 tabbed shell → #16 overview table → #17
deterministic command controller → #18 detail drill-in → #19 CI status via
`gh` → (#20 optional `/ask`). No changes recommended to the epic's shape —
it is well-decomposed; it just needs Phase 1 under it.

### Phase 3 — fleet-level governance (new tickets to file)

1. **Fleet board aggregation** — one GitHub Project as the fleet board;
   either add child-repo issues to it (Projects v2 is cross-repo natively)
   or a `fleet board` command aggregating per-repo boards. Fix the
   board-owner assumption and 100-item pagination first.
2. **Fleet upgrade orchestration** — `projects-orchestrator upgrade --all`:
   trigger each child's `project-init-upgrade.yml` via
   `gh workflow run`, then track the resulting PRs in the Overview (columns:
   scaffold version, upgrade PR open?, CI state). Turns the current
   per-repo, manual, breakable upgrade channel into a governed fleet
   operation.
3. **Fleet health freshness + notifications** — persist check results
   (the #14 cache) to disk; surface stale/never-checked projects; optional
   `fleet report` markdown output for a scheduled run.
4. **Observability ingestion** — `prod_guard` already writes
   `.claude/observability/usage.jsonl` per project (dormant); add a reader
   that aggregates guard firings/denials across the fleet into the TUI —
   governance visibility for agent activity.
5. **Token/secret preflight** — `doctor` command validating the required
   secrets per repo (`PROJECT_TOKEN`, `UPGRADE_PR_TOKEN`, gitleaks
   license) since today missing tokens produce *silent* no-ops.

### Sequencing rationale

Phase 0 is cheap and removes actively-red automation. Phase 1 before the
epic because every epic ticket presumes a descriptor/registry that doesn't
exist. Phase 3 is where the repo finally delivers its name — and each item
builds directly on contract surfaces project-init already ships
(`config.yaml` contract v1, scaffold manifest, upgrade workflow,
sub-issue linking), so the orchestrator stays a *reader* of the existing
contract rather than inventing a parallel one.
