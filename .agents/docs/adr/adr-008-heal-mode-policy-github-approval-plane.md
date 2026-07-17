# ADR-008: Heal-mode policy, with GitHub as the notification and approval plane

- Status: accepted
- Date: 2026-07-17

## Context and Problem Statement

ADR-006 gave the fleet one heal behavior: spawn a scoped coding agent, verify
the fix, open a draft PR. That is the right ceiling, but not always the right
default — some projects the operator wants *told about*, not *edited by an
agent*, and an unattended scheduled pass needs that choice made per project,
in advance, not per incident. Separately, the epic this serves (#162) needs a
channel that notifies the operator and carries their approval back. How should
heal-mode be selected, and over what plane do notifications and approvals flow?

## Considered Options

- One global heal mode per invocation, no per-project say
- Per-project mode declared in the child's descriptor, overriding a run-wide
  default
- A custom notification/approval surface (webhook inbox, Telegram bot, new
  authenticated endpoints on the dashboard)
- GitHub as the single notification and approval plane

## Decision Outcome

Chosen: **per-project override on a run-wide default**, and **GitHub as the
single notification and approval plane**.

**Mode policy.** `heal --mode fix|notify` sets the run-wide default (`fix` is
today's ADR-006 behavior and stays the default; the scheduled unit exposes it
as `PO_HEAL_MODE`). A project may declare `heal.mode` in its descriptor, and
the declaration **wins in either direction** — a child that says "never
auto-fix me" is obeyed even in a fix-mode fleet pass, exactly as the per-run
budget (PO-150) is policy the run carries rather than a constant. `notify`
stops at the diagnosis: no worktree, no agent, no spend — the result names
what failed and what to do next. Notify-mode projects therefore never consume
`--limit`, which caps *paid* attempts only. An unknown declared mode is
ignored with a warning, never guessed: a typo must not silently switch a
project between "spends money and opens PRs" and "tells me and stops".

**The plane.** Everything the operator must see or decide flows through
GitHub, which they already watch from Gmail and their phone:

- *Fix mode notifies by PR*: the healed branch's PR is the notification
  (promotion to ready + review request → GitHub email; #165).
- *Notify mode notifies by issue*: deduplicated GitHub issues carry the
  diagnosis (#164).
- *Approval is a merge* — never a custom endpoint, token, or chat button. The
  dashboard's mutating actions stay loopback-bound (PO-156); production
  credentials stay in review-gated CI (ADR-012 upstream); no new inbound
  surface is added anywhere.

### Consequences

- Good: one inbox, one approval gesture, zero new attack surface; the policy
  is auditable in each child's own config; unattended spend is only ever
  consumed by projects that opted into (or defaulted to) fix mode.
- Good: future watchers (GCP alerts, #167) can join the same plane by filing
  issues, without touching the approval story.
- Bad: notification latency is GitHub's email latency; operators who mute
  GitHub notifications silence the fleet too.
- Bad: `heal.mode` rides in the child descriptor ahead of the frozen contract
  v2 surface — it is feature-detected like `ci.status_url`, and must be
  raised upstream with project-init before the next contract revision.
