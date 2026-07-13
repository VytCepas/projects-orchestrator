# ADR-005: Cloud actions are a control plane, not a data plane

- Status: accepted
- Date: 2026-07-05

## Context and Problem Statement

The orchestrator reads deploy/runtime state today (`cloud-status`,
`adapters/cloud.py`) but cannot *act* on it — there is no way to deploy,
roll back, or restart a `delivery: service` project from the one control
surface. Adding cloud actions collides head-on with ADR-012: production
credentials belong to review-gated CI jobs, never to a shell an agent runs
in. How do we give the fleet real cloud control without ever putting cloud
credentials in the orchestrator's process?

## Considered Options

- **Direct execution.** The orchestrator runs `flyctl deploy` /
  `gcloud run deploy` / `kubectl rollout restart` itself (e.g. via a declared
  `tooling.deploy_command`). Simple, but the orchestrator's shell then needs
  production cloud credentials — exactly what ADR-012 forbids.
- **Dispatch.** The orchestrator triggers the child repo's own
  `workflow_dispatch` pipeline (`gh workflow run deploy.yml -f action=…`); the
  mutation runs in CI, where the credentials and environment protections live.
  This is already how `upgrade-plan --apply` works
  (`adapters/project_init.trigger_upgrade`).
- **No cloud actions.** Stay observe-only. Rejected: the fleet's whole value
  is being the single control point, and deploy/rollback/restart are the most
  requested actions.

## Decision Outcome

Chosen option: **dispatch**. Cloud actions are added as
`adapters/cloud.trigger_deploy(descriptor, action)` which dispatches the
child's own deploy workflow (`deploy.workflow`, defaulting to `deploy.yml`)
with an `action` input of `deploy` | `rollback` | `restart`. The orchestrator
holds no cloud credentials and runs no platform mutation itself — it *decides
and dispatches*, the child's CI *executes with the creds*. This keeps the
orchestrator a **control plane, not a data plane**, and reuses the exact
pattern ADR-003/ADR-012 already blessed for scaffold upgrades.

Two guardrails make it safe to expose from an interactive/agent surface:

1. **Dry-run by default.** `trigger_deploy` returns a `planned` result and
   makes no subprocess call unless the caller passes `apply=True`. The
   `deploy` CLI verb mirrors `upgrade-plan`: it prints what it *would*
   dispatch and does nothing until `--apply` is given.
2. **The REPL/TUI cockpit is plan-only.** The controller `deploy` verb never
   dispatches — it always reports the plan and points at `deploy --apply`.
   An agent driving the REPL cannot fire a production deploy by typing a line;
   the mutation requires the explicit, non-interactive CLI flag.

Every dangerous verb (deploy, DB migration, secret rotation) follows this same
dispatch shape: the child declares the workflow, the orchestrator fires it,
the audit trail is a CI run with logs, environments, and required reviewers.

### Consequences

- Good: real fleet-wide cloud control from one repo, with **zero** cloud
  credentials in the orchestrator — ADR-012 credential separation is preserved
  by construction, not by discipline.
- Good: consistent with `upgrade-plan --apply`; `trigger_deploy` degrades to
  `failed` offline and to `skipped` for non-service projects, exactly like the
  rest of the never-raise engine (ADR-003).
- Good: every action is auditable — it is a CI workflow run, subject to the
  child's branch/environment protections, not an opaque local command.
- Bad: the child must own a `workflow_dispatch`-enabled deploy workflow that
  accepts an `action` input; a project without one gets `failed` on `--apply`.
  This is the intended coupling — the child declares what is runnable, the
  orchestrator only triggers it.
- Bad: dispatch is fire-and-forget — `trigger_deploy` confirms the workflow was
  *queued*, not that the deploy *succeeded*. Deploy outcome is observed
  afterward through the existing read path (`cloud-status`, the `Cloud`
  column). Closing that loop (poll-until-settled) is deferred.
