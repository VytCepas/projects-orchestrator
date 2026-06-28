---
description: Python environment, tooling, and test conventions
globs: ["**/*.py", "pyproject.toml", "uv.lock"]
alwaysApply: false
---

## Python environment

Uses [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync                           # install deps
uv run <command>                  # run in the project venv
uv run ruff check .               # lint
uv run ruff format .              # format
uv run pytest -n auto -q          # tests (parallel mode, requires pytest-xdist)
uv run pytest -q --tb=short       # tests (single-threaded fallback)
```

**Test Optimization**: Use `pytest -n auto` in CI to parallelize tests across CPU cores (30-50% faster). Requires `pytest-xdist` in dev dependencies. See `ci.yml.tmpl` for a full optimized CI config.

## Test conventions

- One assertion per test; name: `test_<unit>_<scenario>`
- External services (DB, API) use a real instance, not a mock
