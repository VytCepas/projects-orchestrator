# justfile — the canonical command interface (scaffolded by project-init).
# `just --list` shows every recipe. Recipes are thin wrappers — logic lives
# in the tools and their configs, never in this file.

# install/sync dev dependencies (PEP 735 dependency-group; add tools with `uv add --dev`)
setup:
    uv sync --group dev

# lint project code (docstring + complexity gates per ruff.toml)
lint:
    uv run ruff check .
    find .claude -name '*.sh' -exec shellcheck -S error -x {} +
    find .claude -name '*.sh' -exec shfmt -d -i 2 {} +

# static type check (strict mode per mypy.ini; add mypy with `uv add --dev mypy`)
# no-op on a fresh scaffold with no src/ yet — mypy errors on a missing path
typecheck:
    if [ -d src ]; then uv run --with "mypy>=1.10" --with types-PyYAML mypy src/; else echo "No src/ directory yet — nothing to type-check."; fi

# auto-format project code
format:
    uv run ruff format .

# run the test suite (xdist pulled in on demand so -n works without declaring it)
test:
    uv run --with pytest-xdist pytest -n auto --tb=short -q

# tests with the coverage gate (CI always runs this, not `test` — PI-569).
# without src/ yet, still run the plain test suite (tests/ may exist before
# src/ does) — only the coverage instrumentation/threshold is skipped, since
# 0% coverage on zero application code would trip --cov-fail-under before any
# code exists. Silently skipping pytest entirely here would let a real test
# failure through `just ci` unnoticed.
test-cov:
    if [ -d src ]; then uv run --with pytest-xdist --with pytest-cov pytest -n auto --tb=short -q --cov=src --cov-fail-under=70; else uv run --with pytest-xdist pytest -n auto --tb=short -q; fi

# mutation testing on core logic (slow; requires [tool.mutmut] source_paths
# in your own pyproject.toml, scoped to deterministic pure-logic modules —
# skip I/O-heavy code, hooks, template renderers). CI runs this nightly.
test-mutation:
    uv run --with mutmut mutmut run
    uv run --with mutmut mutmut export-cicd-stats

# dependency vulnerability scan against known CVEs/advisories (PI-568).
# complements package_guard.py, which only blocks installing a package that
# doesn't exist or looks typosquatted — this catches a real, correctly-
# spelled dependency with a known vulnerability already in the lockfile.
audit:
    uv run --with pip-audit pip-audit

# generate a CycloneDX SBOM of the runtime dependency tree (#574). release.yml
# attaches this to GitHub Releases; run locally on demand. --no-dev so it
# reflects what ships; uvx keeps cyclonedx-py out of the scanned .venv.
sbom:
    uv sync --no-dev
    uvx --from cyclonedx-bom cyclonedx-py environment .venv -o sbom.cdx.json

# dependency license compliance scan (#579) — fail on copyleft (GPL/AGPL; also
# LGPL, since --partial-match is substring-based). Tune the --fail-on deny-list
# to your policy. Non-blocking in CI initially (see ci.yml's license-scan job).
license:
    uv run --with pip-licenses pip-licenses --from=mixed --fail-on "GPL;AGPL" --partial-match

# property-based tests with Hypothesis (#580). Property tests are opt-in per file
# (import hypothesis); this recipe makes Hypothesis available without adding it as
# a permanent dependency. Pattern/tooling, NOT a blocking gate.
fuzz:
    uv run --with hypothesis --with pytest-xdist pytest -n auto --tb=short -q

# what CI runs
ci: lint typecheck test-cov audit

# serve the docs site locally
docs:
    uv run --with mkdocs-material --with "mkdocstrings[python]" mkdocs serve

# scan staged changes for secrets (same scan as the pre-commit git hook)
scan:
    gitleaks git --pre-commit --staged --redact --no-banner --verbose

# regenerate .claude/docs/CODE_MAP.md (low-token "what does what" map; read before grepping)
code-map:
    uv run python .claude/scripts/gen_code_map.py
