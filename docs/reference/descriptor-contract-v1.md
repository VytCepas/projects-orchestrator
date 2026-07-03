# Descriptor contract v1

The orchestrator is a **reader** of the contract that
[project-init](https://github.com/VytCepas/project-init) scaffolds into every
child project. This page pins the exact surfaces the engine consumes today, so
the coupling between the two tools is a **named, versioned boundary** rather
than implicit knowledge spread across the code.

Anything not listed here is *not* read by the orchestrator and carries no
compatibility promise. Everything listed here should stay stable within
contract version `1`; a breaking change is a new contract version, surfaced in
the `Contract` column of `projects-orchestrator status`.

> Scope: the orchestrator only ever *reads* these files. It never writes the
> contract, and it degrades (never raises) when any surface is missing or
> malformed — see [ADR-003](../../.claude/docs/adr/adr-003-fleet-engine.md).

## 1. `.claude/config.yaml` — the descriptor

Parsed by `descriptor.py`. A directory is considered a project-init project iff
this file exists and is readable.

| Key | Type | Consumed as | Missing → |
|---|---|---|---|
| `project.name` | string | project name | directory name |
| `project.project_init_contract_version` | int | `Contract` column; `0`/absent means "predates the contract" | `0` (`none`) |
| `project.project_init_version` | string (`MAJOR.MINOR.PATCH`) | `Scaffold` column + `Latest` staleness compare | `unknown` (not comparable) |
| `language` | string | descriptor `language` | `unknown` |
| `delivery` | string (`library`/`service`/`prototype`) | descriptor `delivery` | `unknown` |
| `memory.tier` | int (`0`–`3`) | memory tier | `0` |
| `memory.memory_path` | string (repo-relative) | memory directory location | `.claude/memory` |
| `tooling.<task>_command` | string (shell) | the gate run for `<task>` (`lint`, `test`, `run`, …) | task is `skip` |

Notes:

- **Tooling.** Only keys ending in `_command` with a non-empty string value are
  read; the `_command` suffix is stripped to form the task name. An undeclared
  gate is never guessed — it is skipped.
- **Version format.** `project_init_version` is compared by splitting on `.`
  into integer components (`0.5.2` → `(0, 5, 2)`). Any non-integer component
  (e.g. `1.2.0-rc1`) makes the version *not comparable* — the `Latest` cell
  renders `-` rather than guessing an order. Staleness is judged relative to
  the **newest comparable version in the fleet**, so it is fully offline.

## 2. `scaffold.manifest` — drift detection

Read by `drift.py` from the **same `config.yaml`**, under `scaffold.manifest`:

```yaml
scaffold:
  manifest:
    <repo-relative-path>: <sha-256-hex>
    ...
```

The engine hashes each listed file in the working tree and compares. A project
with no `scaffold.manifest` reports `no-manifest` (not an error). This is what
lets drift detection fall out of the existing contract for free.

## 3. Git hooks — enforcement health

Read by `drift.py`. Hooks shipped in `.github/hooks/` are compared against
`.git/hooks/` in the child's clone:

- `ok` — every shipped hook is installed
- `partial` / `missing` — some / none installed
- `-` — the project ships no hooks

Enforcement that exists in `.github/hooks/` but was never installed into
`.git/hooks/` never actually runs — so install state is a first-class signal.

## 4. `.claude/memory/*.md` — fleet memory

Read and searched by `memory.py`. Each fact file carries YAML frontmatter:

```markdown
---
name: <title>
description: <one-line summary>
type: user | feedback | project | reference
---

<markdown body>
```

- Index/schema files (`MEMORY.md`, `SCHEMA.md`, `README.md`) are treated as the
  index, not as facts.
- `name` falls back to the file stem, `description` to empty, `type` to
  `unknown` when frontmatter is absent — malformed files degrade to untyped
  entries rather than failing the read.

## Compatibility policy

- Fields in this document are the v1 surface. The orchestrator pins to
  `project_init_contract_version: 1`.
- project-init may **add** fields freely; the orchestrator ignores unknown keys.
- **Removing or repurposing** any field above is a breaking change and requires
  a new contract version. Consumers must never reach into project-init
  *implementation* details (specific script internals, undocumented file
  layouts) — only the surfaces listed here.
