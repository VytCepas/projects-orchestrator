# projects-orchestrator

Cross-project orchestration layer for agentic development.

A root orchestrator that coordinates work across multiple projects scaffolded
with [project-init](https://github.com/VytCepas/project-init).

## Status

Early. The orchestrator kernel — discovering project-init projects and reading
their descriptor contract — is in place (see
[ADR-003](.claude/docs/adr/adr-003-descriptor-registry-core.md)). Cross-project
memory aggregation and task scheduling land in follow-ups.

## Usage

```sh
# index every project-init project under a directory
projects-orchestrator discover ~/code

# inspect one project's descriptor in detail
projects-orchestrator show my-service ~/code
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
