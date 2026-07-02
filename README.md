# projects-orchestrator

Cross-project orchestration layer for agentic development.

A root orchestrator that coordinates work across multiple projects scaffolded
with [project-init](https://github.com/VytCepas/project-init).

## Status

Early. In place:

- **Kernel** — discover project-init projects and read their descriptor contract
  ([ADR-003](.claude/docs/adr/adr-003-descriptor-registry-core.md)).
- **Monitor + runner + TUI** — per-project git health and running tasks across
  the fleet, with an interactive overview
  ([ADR-004](.claude/docs/adr/adr-004-fleet-monitor-and-runner.md)).

Cross-project memory aggregation and CI/gate-health rollups land in follow-ups.

## Usage

```sh
# interactive fleet overview (health, branches, run tasks) — the main entry point
projects-orchestrator tui ~/code

# index every project-init project under a directory
projects-orchestrator discover ~/code

# inspect one project's descriptor in detail
projects-orchestrator show my-service ~/code

# git health/status for every project
projects-orchestrator status ~/code

# run a task in one project or across the whole fleet
projects-orchestrator run "just lint" ~/code --all
projects-orchestrator run "just test" ~/code --project my-service
```

## Development

```sh
just setup    # uv sync --group dev
just test     # run the test suite
just lint     # ruff check
just ci       # what CI runs (lint + test)
```

Or directly:

```sh
uv sync --group dev
uv run projects-orchestrator --version
```

## License

Apache-2.0 — see [LICENSE](LICENSE).
