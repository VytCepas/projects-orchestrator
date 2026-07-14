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

- **Fact:** The controller is deterministic. `/ask` (`ask.py`) is **implemented** but opt-in and off by default — it needs `ORCHESTRATOR_ASK_MODEL` + `ANTHROPIC_API_KEY`. It resolves a question to an *existing* intent, which the deterministic dispatcher then executes; it may PROPOSE a `work` run but never launches one (#124).
  **Why:** Owner decision (2026-07-02 session): governance requires predictable, auditable actions. The seam was filled without weakening that — a model chooses among intents, it does not generate commands.
  **How to apply:** New controller capabilities = new intents in `parse_command`/`dispatch` with table-driven tests; never free-form command generation. Anything that launches an agent run stays behind an explicit human verb.
