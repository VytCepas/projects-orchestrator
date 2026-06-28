# Issue metadata

Issues carry planning metadata in two places.

## GitHub labels

Use labels for values that workflows and project boards can read.

- Type labels: `feature`, `bug`, `chore`, `documentation`, `test`
- Priority labels: `priority:high`, `priority:medium`, `priority:low`
- Size labels: `size:XS`, `size:S`, `size:M`, `size:L`, `size:XL`
- Scale labels: `scale:epic` (a large parent initiative) or `scale:task` (a focused leaf — the default). Keep an epic's child tickets `scale:task` and sized `S`/`M`, so PRs and agent context stay small.
- Area labels are repository-specific. Use existing labels only; do not invent new area labels from agent context.

`create_issue.sh` creates missing priority, size, and scale labels when the token has permission. If label creation fails, the issue is still created and the value remains in the markdown body.

## Markdown body

Use the markdown body for context GitHub does not model portably:

- references to issues, PRs, ADRs, docs, designs, logs, or external links
- dependencies, blocked-by, parent, and follow-up relationships
- acceptance criteria
- implementation notes and affected areas
- Definition of Ready
- Definition of Done

GitHub Projects fields are synced opportunistically by `board-automation.yml`. Missing fields or options are logged and skipped so issue creation does not fail in a new repository.
