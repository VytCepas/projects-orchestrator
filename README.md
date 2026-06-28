# projects-orchestrator

Cross-project orchestration layer for agentic development.

A root orchestrator that coordinates work across multiple projects scaffolded
with [project-init](https://github.com/VytCepas/project-init).

## Status

Early scaffold. The package currently ships a CLI stub; orchestration logic
lands in follow-ups.

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
