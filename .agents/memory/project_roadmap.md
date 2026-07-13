---
name: Roadmap and interface direction
description: The four-direction plan, its status, and the decisions guiding follow-up work
type: project
---

# Roadmap and interface direction

- **Fact:** The 2026-07-05 effort delivered four directions — core hardening (#51), the live `serve` dashboard (#52), interface unification (#53, ADR-004), and governance breadth (#54: `notify`, `audit --digest`, `history`, GitLab adapter). All merged.
  **Why:** "Realize the core, then build the interface" was the agreed sequence — an interface is a projection of the data model, so the contract was stabilized before the surfaces grew.
  **How to apply:** Build new surfaces on the shared view-model (ADR-004); don't add a fifth parallel projection. Keep new commands read-only and never-raise per ADR-003.

- **Fact:** Descriptor contract v2 (`deploy`, `observability.path`, `hooks.expected`) is live: project-init emits `project_init_contract_version: 2` and the orchestrator parses it. The doc is frozen at `docs/reference/descriptor-contract-v2.md` (PO-91). project-init also relocated its canonical tree `.claude/`→`.agents/` (PI-627); `descriptor.resolve_config` reads `.agents/` first, `.claude/` as a legacy fallback (PO-88).
  **Why:** The contract is co-owned upstream; it is now stable and emitted, so the boundary is documented as live rather than proposed.
  **How to apply:** Validate golden fixtures against project-init's shipped `descriptor.schema.json` (packaged via project-init#786; PO #90). When adding a contract surface, read it via the resolved scaffold root, never a hardcoded `.claude/` or `.agents/` prefix.

- **Fact:** Persistent state lives under `$XDG_STATE_HOME/projects-orchestrator/`: supervisor run-state, `audit-digest.json` (digest deltas), and `history.jsonl` (append-only check trends, bounded by `MAX_ENTRIES`).
  **Why:** Trends and digests need durable state the last-known cache can't hold; keeping it under XDG (not the repo) keeps it machine-local.
  **How to apply:** New persisted state goes under the same XDG dir with atomic writes (temp + replace) and never-raise load; isolate `XDG_STATE_HOME` in tests.

- **Fact:** Open follow-up #64 — a sparkline trend column in the fleet table — is deferred because it threads `history` data through the shared snapshot view-model that ADR-004 consolidated.
  **Why:** It touches every surface, so it deserves its own PR with golden-output tests rather than riding along with the history persistence work.
  **How to apply:** Read the history log once per fleet render (not per project — avoid N+1), add `Trend` to `COLUMNS` + `snapshot_row`, let every surface inherit it via `fleet_rows`/`cell_status`.
