# Testing Strategy

## Principles

- **Test-driven** — write failing tests before implementation. Tests define the contract.
- **No mocks for external systems** — integration tests hit real instances (DB, APIs). Mocks mask divergence between test and production.
- **One assertion per test** — narrow tests pinpoint failures immediately.
- **Name tests descriptively** — `test_<unit>_<scenario>` (e.g. `test_auth_rejects_expired_token`)

## Test boundaries

| What to test | How |
|---|---|
| Business logic | Unit tests, no I/O |
| DB queries | Integration tests against real DB |
| External APIs | Integration tests or contract tests |
| UI behaviour | E2E tests (Playwright if configured) |

## Running tests

See `.claude/project-init.md` for the test command for this project's language/runtime.

## Coverage

Coverage is a signal, not a target. Aim for high coverage of critical paths; don't write tests just to hit a number.
