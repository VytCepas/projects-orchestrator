# How-to guides

Goal-oriented recipes for someone already working with the project: "How do
I X?" Each guide solves one real problem, assumes basic familiarity, and
skips teaching.

- [Run the audit digest on a schedule](scheduled-audit-digest.md) — report only
  what changed in the fleet, daily, with an optional Slack sink.
- [Heal the fleet on a schedule](scheduled-heal.md) — open PRs that fix red
  lint/test gates, unattended, with a hard cap on how much one pass may spend.
- [Run the dashboard as a service](serve-dashboard.md) — the live fleet view
  always on, loopback-bound, restarting itself, reachable from a phone over a
  tunnel.
