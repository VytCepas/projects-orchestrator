# project-init golden fixtures

These are **real, generated** project-init output — not hand-authored copies —
used by `tests/test_contract.py` as the producer→consumer contract tripwire
(epic #68, WS1 / #69). If project-init changes the descriptor shape in a way
that breaks what the orchestrator reads, the contract test fails here instead
of on a user's fleet.

| File | What | Contract surface |
|---|---|---|
| `config.v1.yaml` | a legacy `.claude/config.yaml` (pre-PI-627) | the descriptor `descriptor.py` parses |
| `capabilities.v1.md` | a scaffolded `.claude/CAPABILITIES.md` | the capability inventory `capabilities.py` parses (ADR-025 §3) |
| `scaffold_result.v1.json` | `project-init … --json` stdout (target path sanitized) | the `--json` registration seam (#510) |
| `config.v2.yaml` | a current `.agents/config.yaml` (PI-627, contract v2) | the descriptor + the `deploy`/`observability.path`/`hooks.expected` blocks |
| `capabilities.v2.md` | a current `.agents/CAPABILITIES.md` | the capability inventory |
| `scaffold_result.v2.json` | current `project-init … --json` stdout (target sanitized) | the `--json` seam (now targets `.agents/config.yaml`) |

The **v1** fixtures pin the legacy `.claude/` layout the orchestrator still
reads as a fallback; the **v2** fixtures pin the current `.agents/` layout and
the contract-v2 shape. Both are guarded by `tests/test_contract.py`.

## How to refresh (pin to a project-init version)

The v2 fixtures were generated with **project-init 1.0.1** via:

```sh
project-init <target> \
  --preset auto --name demo-service --description "golden fixture for the orchestrator contract test" \
  --language python --delivery service --deploy cloud-run --observability \
  --lifecycle github --owner VytCepas --license apache-2.0 \
  --non-interactive --json
```

Then copy `<target>/.agents/config.yaml` → `config.v2.yaml`,
`<target>/.agents/CAPABILITIES.md` → `capabilities.v2.md`, and the JSON stdout →
`scaffold_result.v2.json` (sanitize the absolute `target` path). The v1 fixtures
were generated the same way with an older project-init that wrote `.claude/`.

## Vendored schemas (`schemas/`)

`schemas/descriptor.schema.json` and `schemas/usage-event.schema.json` are
vendored copies of project-init's shipped machine schemas (VytCepas/project-init#603,
packaged as a consumable via #786). `tests/test_contract.py` validates the v2
fixture (and a sample usage event) against them, so a schema-level drift the
reader-based tripwire could miss still fails CI.

**Refresh** these alongside the config/capabilities fixtures — copy
`<project-init>/schemas/*.json` here — whenever project-init bumps the contract.

## `config.v2_ci.yaml`

A current scaffold (**project-init 1.1.6 + VytCepas/project-init#828**) that
additionally carries the optional `ci:` block — the non-forge CI status endpoint
the `status_url` adapter reads. Generated with the same command as `config.v2.yaml`
above; nothing about it is hand-written, so a producer that drops or renames the
block fails `tests/test_contract.py` rather than silently disabling the adapter.

`ci.status_url` is **additive within contract v2** — the version does not move —
so the fixture also pins that a default scaffold leaves it empty and keeps
falling back to `gh`/`glab`. Fold it into `config.v2.yaml` and delete this file
once the fixtures are regenerated against a project-init release that includes
#828.
Once the orchestrator depends on a released project-init that includes #786, this
vendoring can be replaced by `project_init.schema.load_descriptor_schema()`.
