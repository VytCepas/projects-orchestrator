# projects-orchestrator

Cross-project orchestration layer for agentic development.

A root orchestrator that coordinates work across multiple projects scaffolded
with [project-init](https://github.com/VytCepas/project-init): one interface
to see every project's health, run its gates, and search everything the
fleet remembers.

## Usage

```sh
projects-orchestrator projects            # list discovered projects
projects-orchestrator status              # fleet table: health/branch/lint/tests/memory
projects-orchestrator checks [project]    # run each project's own lint/test gates (--jobs N, --changed-only)
projects-orchestrator start <project>     # launch the project's run_command (detached, logged)
projects-orchestrator stop <project>      # terminate the supervised process
projects-orchestrator logs <project>      # tail the captured run output (-n lines)
projects-orchestrator deploy <project>    # dispatch the child's deploy workflow (--action deploy|rollback|restart; --apply)
projects-orchestrator memory <query>      # search every project's memory files
projects-orchestrator drift [project]     # scaffold drift vs the recorded manifest
projects-orchestrator doctor [project]    # diagnose contract-v1 conformance
projects-orchestrator audit [project]     # one governance report (--markdown; --digest for a delta vs last run)
projects-orchestrator hardening [project] # setup-readiness checklist with next actions
projects-orchestrator ci [project]        # latest CI conclusion + open PR/MR count (gh, or glab for gitlab.com hosts)
projects-orchestrator cloud-status [project]  # deploy/runtime status (contract-v2 deploy block)
projects-orchestrator events [project]    # guard/usage events from observability logs (--since ISO)
projects-orchestrator history <project>   # per-task check-history trend (sparkline) + pass/fail transitions
projects-orchestrator notify [project]    # threshold alerts (CI red/drift/hooks/cloud) — --webhook to push
projects-orchestrator upgrade-plan        # scaffold version vs upstream (--apply to trigger upgrades)
projects-orchestrator snapshot --json     # full machine-readable fleet state
projects-orchestrator snapshot --html -o fleet.html  # self-contained HTML dashboard
projects-orchestrator serve               # live auto-refreshing dashboard (--host, --port)
projects-orchestrator controller          # deterministic command REPL
projects-orchestrator tui                 # terminal UI (needs the tui extra)
```

Every data command accepts `--json` for external monitors, and exit codes
are meaningful (`checks` exits 1 when any gate fails, `drift` when any
project diverged from its scaffold, `doctor` when any project fails
contract-v1 conformance, `audit` when anything needs attention, `upgrade-plan`
when any project is behind upstream project-init). `audit` is
the one-shot governance report: it composes `doctor`'s conformance findings
with scaffold-drift divergence, a memory-schema lint, and check freshness
(`--markdown` renders a digest for a scheduled run). The status table tracks per project:
health · branch · sync · scaffold version · scaffold freshness (vs the
newest in the fleet) · descriptor-contract version · drift · git-hook
install state · lint · tests · CI conclusion · open PRs · cloud state ·
memory facts · check freshness. The CI/PRs/Cloud columns show the last-known
result cached by `ci`/`cloud-status` (they read the cache offline — only
those commands make network calls).

The contract surfaces the orchestrator reads from each project-init child are
pinned in [`docs/reference/descriptor-contract-v1.md`](docs/reference/descriptor-contract-v1.md);
the additive v2 surfaces (deploy block, observability path, expected hooks) are
specified in [`docs/reference/descriptor-contract-v2-proposal.md`](docs/reference/descriptor-contract-v2-proposal.md).

`checks --changed-only` trusts a cached pass only for a project whose clean
worktree is still at the same HEAD commit — note it cannot see changes in
external services your tests hit, so run a plain `checks` when that matters.
`start` supervision state (pid, uptime, log) lives under `$XDG_STATE_HOME`
and feeds the status table's `Running` column.

In the TUI, selecting a row opens the per-project Detail tab (descriptor,
last-known checks, recent commits, memory) with `l`/`t` running that
project's lint/test gates. The controller's `/ask <question>` mode is
optional and off by default: set `ORCHESTRATOR_ASK_MODEL` (a Claude model
id) and `ANTHROPIC_API_KEY` to let a model *select among the existing
commands* — the deterministic dispatcher still executes them.

### Which projects?

Copy [`fleet.yaml.example`](fleet.yaml.example) to `fleet.yaml` and list
scan roots / explicit paths. Without it, the orchestrator scans the parent
directory of the checkout — the `~/projects/<name>` sibling convention.
Anything with a `.claude/config.yaml` (project-init descriptor contract)
counts as a project; the orchestrator only ever *reads* that contract.

### Design

See `.claude/docs/adr/adr-003-fleet-engine.md`. In short: the engine never
raises (broken children degrade to `unknown`/`fail` cells, the fleet view
always renders), gates are the child's own declared commands, last-known
results persist under `$XDG_CACHE_HOME/projects-orchestrator/`, and the
controller is deterministic — `/ask` is a seam for an optional LLM mode
that may only choose among existing intents.

## Development

```sh
just setup    # uv sync --group dev
just test     # run the test suite
just lint     # ruff check
just ci       # what CI runs (lint + test)
```

Or directly:

```sh
uv sync --group dev --extra tui
uv run projects-orchestrator status
```

## License

Apache-2.0 — see [LICENSE](LICENSE).
