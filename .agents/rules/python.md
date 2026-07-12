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
uv run ruff format .              # WRITES formatting; `ruff format --check .` is the gate,
                                  # part of `just lint` — CI runs it (#726)
uv run --with "mypy>=1.10" mypy src/  # type check (strict mode, per mypy.ini)
uv run pytest -n auto -q          # tests (parallel mode, requires pytest-xdist)
uv run pytest -q --tb=short       # tests (single-threaded fallback)
just test-cov                     # tests + coverage gate (threshold per justfile) — CI always runs this
just audit                        # dependency CVE/advisory scan (pip-audit) — CI always runs this
just sbom                         # CycloneDX SBOM of runtime deps (#574) — release.yml attaches it to Releases
just license                      # dependency license scan (#579) — deny GPL/AGPL; tune the recipe's --fail-on list
```

ruff lints; it does not type-check. `just typecheck` (mypy, strict) is a separate
gate — type errors do not surface as ruff findings.

ruff's `select` also covers `RUF`/`PERF`/`PTH`/`RET`/`ARG`/`A`/`S` — Ruff-native
rules, perf anti-patterns, pathlib-over-os.path, return-statement clarity,
unused arguments, builtin shadowing, and bandit-derived security checks
(cheap and instant; complements Semgrep's CI-only SAST rather than
duplicating it). `S` is exempted under `tests/**` — plain `assert` is the
point of a test, not a vulnerability.

**Test Optimization**: Use `pytest -n auto` in CI to parallelize tests across CPU cores (30-50% faster). Requires `pytest-xdist` in dev dependencies. See `ci.yml.tmpl` for a full optimized CI config.

## Test conventions

- One assertion per test; name: `test_<unit>_<scenario>`
- External services (DB, API) use a real instance, not a mock
- **Prove a guard can fail** (AGENTS.md): `just test-mutation` (mutmut) automates
  the break-it check — it mutates your code and reports which mutants your tests
  fail to kill. A surviving mutant is a test that passes on broken code. Coverage
  is not this: a suite can hit 100% line coverage and kill ~0% of mutants, which
  is why the nightly CI mutation job scores kill-rate, not coverage (it enforces
  an 80%-kill threshold within that scheduled run; it is non-blocking for PRs).

## Property-based testing (Hypothesis, #580)

Opt-in per file, run with `just fuzz` (which provides Hypothesis). It generates
edge-case inputs a hand-written test wouldn't think to try:

```python
from hypothesis import given, strategies as st

@given(st.integers(min_value=-1000, max_value=0), st.integers(min_value=1, max_value=1000), st.integers())
def test_clamp_stays_within_bounds(lo, hi, x):
    assert lo <= clamp(x, lo, hi) <= hi   # a true invariant; Hypothesis probes x < lo
```

Pattern/tooling, **not** a blocking gate — property tests live alongside unit
tests and complement mutation testing (which checks existing tests) and the
coverage floor (which checks how much is exercised).

**When it runs:** the CI `fuzz` job is schedule-only (nightly) and non-blocking —
never on a PR (#727), the same placement mutation testing already uses.
Hypothesis draws fresh seeds each run, so nightly explores inputs a per-PR run
would only repeat. Run `just fuzz` locally at will.
