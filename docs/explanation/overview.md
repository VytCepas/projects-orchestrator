# Overview: what the orchestrator does and how it relates to project-init

A single-page tour of the whole system — the capabilities, the
[project-init](https://github.com/VytCepas/project-init) relationship, and the
architecture that ties them together. Read this first for the mental model,
then follow the links into the reference tables and ADRs for precision.

A rendered, self-contained visual version of this page lives at
[`docs/overview.html`](../overview.html) (open it in a browser).

## What it is

**projects-orchestrator is a root orchestrator: one control surface for a whole
fleet of repositories scaffolded with project-init.** It reads each project's
machine-readable self-description, runs that project's *own* declared gates,
watches CI / cloud / drift / memory across the fleet, and renders the result
through four interchangeable surfaces — all without ever writing back into the
projects it governs.

Three properties define the system and recur through every module:

- **Read-only.** The orchestrator only ever *reads* each child's descriptor,
  memory, and manifest. It never authors the contract; mutations stay in
  review-gated CI. The coupling to project-init is a named, versioned boundary,
  not shared internals.
- **Never-raise.** Every module that touches a subprocess, the filesystem, or
  YAML returns degraded data instead of raising. A half-scaffolded, non-git, or
  timed-out project becomes an `unknown`/`fail` cell — the fleet view always
  renders. See [ADR-003](../../.claude/docs/adr/adr-003-fleet-engine.md).
- **Offline-first.** Check outcomes persist per `(project, task)` under
  `$XDG_CACHE_HOME`. The status table answers "does it pass, and how fresh is
  that?" without re-running anything; only `ci` and `cloud-status` make network
  calls.

## Capabilities, grouped by intent

Every data command accepts `--json` for external monitors, and exit codes are
meaningful (`checks` exits non-zero on a failed gate, `drift` on divergence,
`doctor` on non-conformance, `upgrade-plan` when a project is behind upstream).

### 1. Discover & observe

| Command | Does | Module |
|---|---|---|
| `projects` | List discovered projects. Membership comes from `fleet.yaml` (scan roots + explicit paths) or the `~/projects/<name>` sibling convention — anything with a `.claude/config.yaml` counts. | `registry.py` |
| `status` | The signature fleet table: health · branch · sync · scaffold version & freshness · contract version · drift · hook-install state · lint · tests · CI · open PRs · cloud · memory facts · check freshness. | `fleet.py`, `status.py` |
| `snapshot --json / --html` | Full machine-readable fleet state, or one self-contained HTML dashboard. | `html.py` |
| `serve` | Live, auto-refreshing web dashboard (`--host`, `--port`). | `server.py` |
| `tui` | Textual terminal UI with a per-project detail drill-in (descriptor, checks, commits, memory). | `tui.py`, `detail.py` |

### 2. Run & supervise

| Command | Does | Module |
|---|---|---|
| `checks [project] --jobs N --changed-only` | Run each project's *declared* lint/test gates — never a guessed command. Fans out across a bounded pool; `--changed-only` trusts a cached pass for a clean worktree still at the same HEAD. | `checks.py`, `cache.py`, `pool.py` |
| `start` / `stop` / `logs` | Launch a project's `run_command` detached and logged, terminate the supervised process, or tail its output. Supervision state (pid, uptime, log) lives under `$XDG_STATE_HOME` and feeds the status table. | `supervisor.py`, `runner.py` |

### 3. Govern & converge

| Command | Does | Module |
|---|---|---|
| `doctor [project]` | Diagnose conformance to descriptor contract v1; warns on a contract version newer than the orchestrator understands. | `doctor.py` |
| `drift [project]` | Hash every file in the recorded `scaffold.manifest` and compare against the tree; also report whether shipped git hooks are installed in `.git/hooks/`. | `drift.py` |
| `audit [project] --markdown --digest` | One governance report composing doctor's conformance, scaffold drift, a memory-schema lint, and check freshness. `--digest` renders a delta vs the last run. | `audit.py`, `digest.py` |
| `upgrade-plan --apply` | Compare each child's scaffold version against the newest upstream project-init release; `--apply` dispatches the child's `project-init-upgrade.yml` workflow. | `upgrade.py`, `adapters/project_init.py` |
| `notify [project] --webhook` | Threshold alerts — CI red, drift, uninstalled hooks, cloud down — printed or pushed to a webhook. | `notify.py` |
| `history <project>` | Per-task check-history trend as a sparkline, plus pass/fail transitions. | `history.py` |

### 4. Reach across repos & ask

| Command | Does | Module |
|---|---|---|
| `ci [project]` | Latest CI conclusion + open PR/MR count via `gh` (or `glab` for GitLab hosts). Cached so the status table reads it offline. | `adapters/github.py`, `adapters/gitlab.py` |
| `cloud-status [project]` | Deploy/runtime state for `delivery: service` projects — Fly / Cloud Run / k8s revision + a bounded health-URL probe, all read-only (contract-v2 deploy block). | `adapters/cloud.py` |
| `events [project] --since` | Guard/usage events from each project's observability log (`usage.jsonl`), tolerant of field/timestamp aliases. | `observability.py` |
| `memory <query>` | Search every project's `.claude/memory/*.md` fact files at once — the "all-knowing" layer that makes the fleet's remembered context queryable from one prompt. | `memory.py` |
| `controller` / `/ask` | A deterministic command REPL — one control point, no LLM. `/ask` is an opt-in seam: a model may only *select among existing intents*; the dispatcher still executes them. | `controller.py`, `ask.py` |

## How it interacts with project-init

project-init and the orchestrator are coupled only through a named, versioned
**descriptor contract** — a boundary that lets each tool evolve independently.

```
   project-init  ──(scaffolds)──▶  child repo  ──(reads contract)──▶  orchestrator
   the author                      .claude/config.yaml               the reader
                                    scaffold.manifest                 (never writes back)
                                    .claude/memory/*.md
                                    git hooks
```

- **project-init is the author.** It scaffolds `.claude/config.yaml` into every
  new project, records a `scaffold.manifest` of file hashes, ships git hooks and
  a structured `.claude/memory/`, and cuts releases the fleet measures freshness
  against.
- **The orchestrator is the reader.** It parses the descriptor into a
  `ProjectDescriptor`, runs the project's *declared* gates (nothing guessed),
  diffs the tree against the recorded manifest (drift), and searches every
  project's memory as one corpus.

The exact surfaces read are pinned so the coupling is explicit rather than
implicit knowledge spread through the code:

- **[Descriptor contract v1](../reference/descriptor-contract-v1.md)** — the
  pinned surfaces: `config.yaml` (`project.name`, contract version,
  `project_init_version`, `language`, `delivery`, `memory.tier`/`path`,
  `tooling.*_command`), `scaffold.manifest`, git hooks, and `.claude/memory/*.md`.
- **[Descriptor contract v2 (proposal)](../reference/descriptor-contract-v2-proposal.md)**
  — additive, opt-in blocks a child unlocks with
  `project_init_contract_version: 2`: `tooling.run_command`, a `deploy:` block,
  `observability.path`, and `hooks.expected`.

The compatibility policy is strict: project-init may **add** fields freely (the
orchestrator ignores unknown keys), but **removing or repurposing** any pinned
field is a breaking change requiring a new contract version — surfaced in the
`Contract` column of `status`. A declared path that escapes the project root is
rejected with a warning; the orchestrator only ever reads inside the project it
governs.

## Infrastructure & architecture

A never-raise engine feeds one shared view-model; every interface is a
projection of it, so no two surfaces can disagree. Cross-repo I/O is quarantined
in adapters, and per-project work fans out over a bounded pool.

- **Core engine — reads contracts, never raises.** `descriptor.py` parses the
  config; `memory.py`, `drift.py`, `status.py`, and `checks.py` read the rest.
  Every subprocess flows through one timeout-bounded `runner.py`; unparseable
  input becomes defaults + warnings, not an exception.
- **Shared view-model — one truth, four surfaces.** `fleet.py` joins descriptor
  + git status + cached checks + memory into a `ProjectSnapshot`. A single
  ordered `COLUMNS` set, the pure `snapshot_row`, and the presentation-free
  `cell_status` classifier decide *what text every cell holds* and *whether it
  reads good / warn / bad* — in exactly one place. The CLI table, `--json`, the
  TUI, and the web dashboard all read that same dict by column name. See
  [Interfaces: one view-model, four surfaces](interfaces.md).
- **Adapters — cross-repo I/O, quarantined.** The only modules that reach the
  network sit behind a uniform seam: `github` / `gitlab` (CI + PRs), `cloud`
  (deploy state), and `project_init` (upstream releases + upgrade dispatch).
  Each still never raises and caches its result so the fleet view stays offline.
- **Concurrency & state.** `pool.py` maps per-project work over a bounded thread
  pool (`min(8, cpu)`), preserving order and short-circuiting a single item to a
  plain loop — fleet-wide wall-clock becomes *slowest project*, not *sum of
  projects*. `cache.py` persists last-known results under `$XDG_CACHE_HOME`; a
  corrupt cache reads as empty.

### Governing principles

1. **Contract reader, not author** — no parallel metadata format is introduced.
2. **The fleet view must render** no matter what state a child is in.
3. **Gates are the child's own** declared commands — undeclared is skipped, never guessed.
4. **Only `ci` and `cloud-status` touch the network**; every other command reads the cache.
5. **Adding a column** is one entry in `COLUMNS` + a value in `snapshot_row` — every surface inherits it.
6. **The controller is LLM-free** — `/ask` may only choose among existing intents; the dispatcher executes them.

See [ADR-003: Fleet Engine](../../.claude/docs/adr/adr-003-fleet-engine.md) for
the decision record behind this shape.
