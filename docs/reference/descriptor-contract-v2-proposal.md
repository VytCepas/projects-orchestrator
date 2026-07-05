# Descriptor contract v2 — proposal

Status: **proposed** — the field list below is what this orchestrator needs
from [project-init](https://github.com/VytCepas/project-init) beyond
[contract v1](descriptor-contract-v1.md). The contract itself is owned
upstream; this page tracks the proposal and documents exactly what the
orchestrator already parses when a child declares
`project.project_init_contract_version: 2`. File the upstream issue against
project-init and record its link here once opened.

## Invariants (unchanged from v1)

- **Additive only.** Every v1 surface keeps its meaning; a v1 reader that
  ignores the new blocks keeps working. The orchestrator likewise ignores the
  v2 blocks on configs declaring contract version `< 2` — exactly as a v1
  reader would.
- **Machine-generated.** The scaffold renders these fields; humans edit them
  through project-init, not by hand.
- **Hash-covered.** `.claude/config.yaml` stays covered by
  `scaffold.manifest`, so drift detection sees contract edits.
- **Read-only.** The orchestrator never writes any of this (ADR-003).

## New surfaces

### `tooling.run_command`

Already representable in v1 (any `<task>_command` is read); v2 makes it a
*named, expected* field so the `Runnable` column and the controller `run`
verb rest on contract rather than convention.

### `deploy:` — runtime identity for `delivery: service` projects

```yaml
deploy:
  target: fly          # none | cloud-run | fly | k8s | …
  app: my-service      # app/service name at the target
  region: fra          # when the platform needs one
  health_url: https://my-service.example/healthz
  workflow: deploy.yml # the child's workflow_dispatch deploy pipeline
```

Consumed by `adapters/cloud.py` (`cloud-status` command + `Cloud` column for
reads; the `deploy` command for actions):

| Key | Type | Consumed as | Missing → |
|---|---|---|---|
| `deploy.target` | string | which platform CLI probes run (`flyctl`, `gcloud`) | `none` (no probe, zero cost) |
| `deploy.app` | string | app name interpolated into the probe command | empty |
| `deploy.region` | string | region interpolated into the probe command | empty |
| `deploy.health_url` | string (http/https) | bounded stdlib GET → `healthy`/`unhealthy`/`unknown` | no health probe |
| `deploy.workflow` | string | the child workflow the `deploy` command dispatches for cloud actions | `deploy.yml` (convention) |

`deploy: none` — or omitting the block — stays valid and free: the adapter
short-circuits with no subprocess and no network call.

**Reads are read-only; actions are dispatch-only.** The status probes above
never mutate. The `deploy` command (`deploy`/`rollback`/`restart`) does *not*
run `flyctl`/`gcloud` locally either — it dispatches the child's own
`workflow_dispatch` pipeline (`deploy.workflow`) with an `action` input, so
production credentials stay in the child's review-gated CI and never enter the
orchestrator. This is the same dispatch pattern as `upgrade-plan --apply`, and
it is dry-run by default (only `--apply` fires). See
[ADR-005](../../.claude/docs/adr/adr-005-cloud-control-plane.md).

### `observability.path` — where guard/usage logs live

```yaml
observability:
  path: .claude/observability
```

Consumed by `observability.py` (`events` command). Today the location is an
undocumented convention; v2 names it so fleet ingestion doesn't guess. When
undeclared (or at v1) the orchestrator falls back to
`.claude/observability/`. A declared `path` that escapes the project root
(`../…` or absolute) is ignored with a warning — the orchestrator only ever
reads inside the project it governs. The log itself stays `usage.jsonl`, one
JSON object per line; the reader tolerates `ts`/`timestamp` and
`action`/`decision` aliases, skips (and counts) malformed lines, and counts
events whose timestamp is present but unparseable.

**Timestamps.** Event and `--since` instants may be ISO-8601
(`2026-07-04T10:00:00Z`) or raw epoch-seconds; a naive ISO stamp is read as
UTC. This is the pinned timestamp contract for the log.

### `hooks.expected` — the git hooks the scaffold ships

```yaml
hooks:
  expected: [pre-commit, commit-msg, pre-push]
```

Consumed by `drift.hook_health` (the `Hooks` column and `doctor`). With the
list declared, hook health checks the *contract* — are these exact hooks
installed in `.git/hooks/`? — instead of globbing `.github/hooks/`, which
conflates "what the scaffold ships" with "what happens to be in the tree".
Undeclared (or v1) keeps the globbing fallback.

## Orchestrator implementation state

`descriptor.py` parses all of the above behind `contract_version >= 2`
(`DeployConfig`, `observability_path`, `hooks_expected`), with synthetic-v2
config tests in `tests/test_descriptor.py`. v0/v1 children are unaffected —
the fields stay at their empty defaults and every consumer falls back to the
v1 behavior.

**Read-surface hardening (orchestrator-side, done).** `memory_path` and
`observability.path` are clamped under the project root (an escaping value is
rejected with a warning, never read); `doctor` warns on a `contract_version`
newer than the orchestrator understands (`CONTRACT_VERSION_MAX`) instead of
claiming full conformance; the observability timestamp format is pinned above.

**Remaining before this doc is frozen (upstream, project-init).** project-init
must actually emit `project_init_contract_version: 2` with these blocks, and
should constrain `deploy.app`/`region` to `^[A-Za-z0-9._-]+$` at scaffold time
(the orchestrator already `shlex.quote`s them defensively). File that upstream
issue and record its link here; then rename this page to
`descriptor-contract-v2.md`.
