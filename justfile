# justfile — the canonical command interface (scaffolded by project-init).
# `just --list` shows every recipe. Recipes are thin wrappers — logic lives
# in the tools and their configs, never in this file.

# install/sync dev dependencies (PEP 735 dependency-group; add tools with `uv add --dev`)
setup:
    uv sync --group dev

# lint project code (docstring + complexity gates per ruff.toml)
lint:
    uv run ruff check .

# auto-format project code
format:
    uv run ruff format .

# run the test suite (xdist pulled in on demand so -n works without declaring it)
test:
    uv run --with pytest-xdist pytest -n auto --tb=short -q

# tests with the coverage gate (CI runs this when pytest-cov is installed)
test-cov:
    uv run --with pytest-xdist --with pytest-cov pytest -n auto --tb=short -q --cov --cov-fail-under=70

# serve the docs site locally
docs:
    uv run --with mkdocs-material --with "mkdocstrings[python]" mkdocs serve

# scan staged changes for secrets (same scan as the pre-commit git hook)
scan:
    gitleaks git --pre-commit --staged --redact --no-banner --verbose

# what CI runs
ci: lint test

# regenerate .claude/docs/CODE_MAP.md (low-token "what does what" map; read before grepping)
code-map:
    uv run python .claude/scripts/gen_code_map.py
