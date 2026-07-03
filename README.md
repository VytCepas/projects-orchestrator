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
projects-orchestrator checks [project]    # run each project's own lint/test gates
projects-orchestrator memory <query>      # search every project's memory files
projects-orchestrator drift [project]     # scaffold drift vs the recorded manifest
projects-orchestrator doctor [project]    # diagnose contract-v1 conformance
projects-orchestrator audit [project]     # one governance report (--markdown for a digest)
projects-orchestrator snapshot --json     # full machine-readable fleet state
projects-orchestrator controller          # deterministic command REPL
projects-orchestrator tui                 # terminal UI (needs the tui extra)
```

Every data command accepts `--json` for external monitors, and exit codes
are meaningful (`checks` exits 1 when any gate fails, `drift` when any
project diverged from its scaffold, `doctor` when any project fails
contract-v1 conformance, `audit` when anything needs attention). `audit` is
the one-shot governance report: it composes `doctor`'s conformance findings
with scaffold-drift divergence, a memory-schema lint, and check freshness
(`--markdown` renders a digest for a scheduled run). The status table tracks per project:
health · branch · sync · scaffold version · scaffold freshness (vs the
newest in the fleet) · descriptor-contract version · drift · git-hook
install state · lint · tests · memory facts · check freshness.

The contract surfaces the orchestrator reads from each project-init child are
pinned in [`docs/reference/descriptor-contract-v1.md`](docs/reference/descriptor-contract-v1.md).

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
