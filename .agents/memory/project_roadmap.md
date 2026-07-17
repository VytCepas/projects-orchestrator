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

- **Fact:** The 2026-07-14 arc (PO-112 → PO-127, PRs #125–#145) built ADR-007 end to end: agent runs are isolated in throwaway worktrees, bounded to a draft PR, carry no cloud credentials, and their records outlive their processes. `work`, `campaign`, `heal`, `orphans` and `--attach` are all live. PO-146 then closed ADR-007's one stated deferral — a run now records what it cost.
  **Why:** The fleet already knew what was wrong and where; the missing verb was one that acts on it. Cost metering came last because a run must be *controllable* before it is worth making *accountable*.
  **How to apply:** New agent-facing work goes through `runs.AgentRun` + the `landing` write boundary — never a second path that can push. When surfacing cost, an unmetered run (killed/timed-out) renders as an em-dash and is counted, never summed as `$0.00`.

- **Fact:** The former ADR-deferred trio is done: deploy poll-until-settled (PO-152), scheduled heal trigger (PO-154), mutating dashboard actions (PO-156). Only **live RAG querying** (#70/#74) stays deferred, blocked upstream on project-init#605/#606 freezing the endpoint contract.
  **Why:** The 2026-07-17 session confirmed all three merged; keeping them listed as deferred would send an agent re-implementing shipped work.
  **How to apply:** Before touching RAG querying, check whether project-init#605/#606 landed.

- **Fact:** Two epics set the 2026-07-17 direction. **#162 (self-healing, operator-approved via GitHub)**: heal policy gains a `fix|notify` mode (children #163–#166) — fix mode heals, optionally verifies on a test env, then promotes the draft PR to ready and requests the operator's review; notify mode files a deduplicated issue instead. **GitHub is the notification and approval plane**: Gmail gets GitHub's own email, GitHub mobile is the phone approve surface, approval = merging the PR — no new inbound endpoints. **#167 (GCP control plane)**: least-privilege authenticated control of cloud-hosted fleet apps, orchestrator itself cloud-runnable; needs an auth-model ADR honoring ADR-012 before grooming into child tasks.
  **Why:** The operator chose "expanded option C" over webhook/Telegram approval channels — reuse GitHub's existing notification and review machinery rather than building a second approval surface; a heal only ever costs tokens when policy grants a fix mandate.
  **How to apply:** Implement #163 first (policy flag + ADR extending ADR-006); route any new approval surface through GitHub review, not custom endpoints. Groom #167's children only after its auth ADR exists.
