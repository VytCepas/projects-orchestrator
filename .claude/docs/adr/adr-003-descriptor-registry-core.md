# ADR-003: Descriptor-contract registry as the orchestrator kernel

- Status: accepted
- Date: 2026-07-01

## Context and Problem Statement

projects-orchestrator is the "root orchestrator" that coordinates work across
projects scaffolded by project-init. Every downstream capability — cross-project
memory aggregation, fleet-wide task scheduling, health rollups — first needs one
thing: a reliable way to *discover* those projects and *introspect* them. How
should the orchestrator learn what projects exist and what each one is?

## Considered Options

- **Read each project's `.claude/config.yaml` descriptor contract** (the schema
  project-init already stamps with `project_init_contract_version`).
- **Maintain a separate central manifest** the orchestrator owns and updates.
- **Shell out to project-init** to introspect each project on demand.

## Decision Outcome

Chosen option: **read the descriptor contract**. project-init already writes a
stable, versioned descriptor into every project it generates (`config.yaml`
`project_init_contract_version: 1`, plus resolved memory tier/paths). Parsing it
directly makes the project the single source of truth, needs no central state to
keep in sync, and adds no dependency on project-init being installed at runtime.

The core is deliberately thin:

- `descriptor.load_descriptor()` → typed `ProjectDescriptor` / `MemoryDescriptor`
  (name, language, delivery, memory surface, contract version, MCPs, raw config).
- `registry.discover_projects()` walks a tree for `**/.claude/config.yaml`;
  `Registry` indexes the results with `get` / `by_language` lookups.
- A malformed descriptor is *skipped*, never fatal — one broken project must not
  blind the orchestrator to the rest of the fleet.
- CLI: `discover <root>` and `show <name> <root>`.

## Consequences

- Good: no central manifest to drift; each project owns its own truth.
- Good: contract is versioned, so the orchestrator can evolve parsing per
  `contract_version` (absent ⇒ v0) without breaking older projects.
- Good: the typed descriptor is the seam every later feature builds on.
- Bad: adds a `pyyaml` runtime dependency (config is YAML; stdlib has no parser).
- Bad: discovery is filesystem-glob based, so very large trees pay a scan cost;
  a cached index can be layered on later if it matters.
