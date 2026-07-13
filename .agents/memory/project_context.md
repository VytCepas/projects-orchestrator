---
name: Project context
description: What the orchestrator is, its engine principles, and where fleet state lives
type: project
---

# Project context

- **Fact:** The fleet engine (descriptor/registry/status/checks/cache/memory/fleet/controller) landed 2026-07-02; design is recorded in ADR-003.
  **Why:** Ground follow-up work in the accepted architecture instead of re-deriving it.
  **How to apply:** Read `.claude/docs/adr/adr-003-fleet-engine.md` before changing engine modules; keep new engine code never-raise and UI-free.

- **Fact:** Fleet membership is defined by `fleet.yaml` (roots/projects/exclude) or defaults to scanning the parent directory for sibling checkouts.
  **Why:** Discovery is configuration, not code; users control the fleet without touching Python.
  **How to apply:** For fleet-scope changes edit `fleet.yaml` (see `fleet.yaml.example`); never hardcode project paths in the engine.

- **Fact:** Last-known check results persist in `$XDG_CACHE_HOME/projects-orchestrator/checks.json`; the status table's Lint/Tests/Checked columns read it.
  **Why:** The fleet view must show pass/fail + freshness without re-running every gate.
  **How to apply:** After changing check semantics, keep cache entries backward-readable (unknown fields are dropped, never fatal).

- **Fact:** The controller is deterministic; `/ask` is a disabled seam and any future LLM mode may only select among existing intents.
  **Why:** Owner decision (2026-07-02 session): governance requires predictable, auditable actions.
  **How to apply:** New controller capabilities = new intents in `parse_command`/`dispatch` with table-driven tests; never free-form command generation.
