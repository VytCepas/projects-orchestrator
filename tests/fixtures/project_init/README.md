# project-init golden fixtures

These are **real, generated** project-init output — not hand-authored copies —
used by `tests/test_contract.py` as the producer→consumer contract tripwire
(epic #68, WS1 / #69). If project-init changes the descriptor shape in a way
that breaks what the orchestrator reads, the contract test fails here instead
of on a user's fleet.

| File | What | Contract surface |
|---|---|---|
| `config.v1.yaml` | a scaffolded `.claude/config.yaml` | the descriptor `descriptor.py` parses |
| `capabilities.v1.md` | a scaffolded `.claude/CAPABILITIES.md` | the capability inventory `capabilities.py` parses (ADR-025 §3) |
| `scaffold_result.v1.json` | `project-init … --json` stdout (target path sanitized) | the `--json` registration seam (#510) |

## How to refresh (pin to a project-init version)

Generated with **project-init 1.0.0** via:

```sh
project-init <target> \
  --preset auto --name demo-service --description "golden fixture for the orchestrator contract test" \
  --language python --delivery service --deploy cloud-run --observability \
  --lifecycle github --owner VytCepas --license apache-2.0 \
  --non-interactive --json
```

Then copy `<target>/.claude/config.yaml` here as `config.v1.yaml` and the JSON
stdout as `scaffold_result.v1.json` (sanitize the absolute `target` path).

When project-init emits **contract v2** (VytCepas/project-init#604), add a
`config.v2.yaml` fixture and extend the contract test to assert the orchestrator
reads the `deploy` block / `observability.path` / `hooks.expected` / `run_command`.
The shared JSON Schema (VytCepas/project-init#603) will eventually validate these
fixtures directly.
