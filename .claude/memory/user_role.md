---
name: User role
description: Who the developer is, their background, how to tailor responses
type: user
---

# User role

- **Fact:** The user (`vyt.cepas.ve@gmail.com`) owns and develops both this repo
  and its companion [`project-init`](https://github.com/VytCepas/project-init) —
  a two-tool agentic-dev ecosystem where project-init scaffolds repos and this
  orchestrator governs the fleet.
  **Why:** Requests often span the boundary between the two tools (contract,
  upgrades, drift), and "the fleet" means project-init-scaffolded siblings.
  **How to apply:** Treat the descriptor contract as the interface between the
  two; keep this repo a *reader* of it (ADR-003), and propose contract changes
  upstream rather than working around them per-project.

- **Fact:** Strongly prefers autonomous, decisive execution and broad delegation
  ("do everything stronger and better", "close other stuff, clean old stuff").
  **Why:** Values momentum; expects the agent to scope, act, verify, and
  reconcile rather than seek step-by-step approval.
  **How to apply:** Take initiative and finish the whole task (code + tests +
  lint + PR). Reserve questions for genuinely irreversible or ambiguous
  outward-facing actions (closing issues/PRs, deleting history) — then offer
  concrete, grounded options with a recommendation.

- **Fact:** Holds a high quality bar: strict mechanical gates (ruff with
  ARG/S/RUF/PERF/PTH/RET/BLE, complexity caps, docstrings), TDD, deterministic
  tooling, and a clean project free of stale/misleading artifacts.
  **Why:** "Prose rules decay; lint gates do not" — quality is enforced by the
  toolchain, not by convention.
  **How to apply:** Fix the code, never loosen the gate; write failing tests
  first; keep the engine never-raise and UI-free; remove dead/outdated docs
  instead of letting them rot.
