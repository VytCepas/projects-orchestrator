# Secrets handling

How secrets flow through this project, and when to escalate beyond `.env`.

## The local pattern (scaffolded)

- `.env.example` documents every variable the project needs — committed,
  values empty.
- `.env` holds your real local values — gitignored, never committed.
- Loading order: shell exports win over `.env`; tools load `.env`
  explicitly (`uv run --env-file .env`, bun auto-load, or direnv).
- Enforcement: the pre-commit hook runs a gitleaks scan and CI re-scans
  the full history (ADR-007). A leaked value that reaches a commit must be
  **rotated**, not just deleted — assume it is compromised.

## When .env stops being enough

`.env` files do not scale past a single developer: no audit trail, no
rotation, manual sharing. Escalate to a secret manager when the team or
the secret count grows. Common paths, in rough order of adoption effort:

| Option | Shape | Fits when |
|---|---|---|
| [sops](https://github.com/getsops/sops) + age | encrypted files in the repo | small teams, GitOps workflows, no SaaS dependency |
| [1Password CLI](https://developer.1password.com/docs/cli/) (`op run`) | secrets injected from a shared vault | team already uses 1Password |
| [Doppler](https://docs.doppler.com/) | hosted secret manager with env sync | many environments/services, need audit + rotation |
| Cloud-native (AWS/GCP/Azure secret managers) | IAM-scoped, per-service | production workloads already on that cloud |

The scaffolder deliberately installs **none** of these — the choice is
org-specific. Whichever you pick, keep the contract: `.env.example` stays
the documentation of record for *what* variables exist; the manager owns
*values*.

## Credential separation (the actual prod-safety boundary)

The `prod_guard` hook flags destructive commands (`terraform destroy`,
`DROP DATABASE`, cloud deletes — see the `prod_guard` hook), but a
deny-list is a guardrail, not a guarantee (ADR-012). The guarantee is that
**agent sessions never hold production credentials**:

- `.env` files used in agent sessions contain dev/staging values only.
- Production credentials live in the secret manager and are injected
  exclusively into review-gated CI deploy jobs — never into a local shell
  an agent runs in.
- A guard cannot delete what the session cannot reach.

## Keeping secret files out of the transcript

Even a dev-only `.env` is worth not reading. Anything an agent reads enters
the transcript, which is re-sent on every following turn and outlives the
session that read it. Two controls, because neither covers the other:

- **`permissions.deny` in `.claude/settings.json`** closes the `Read` tool
  for `.env` and its non-example variants, private keys, `*service-account*`
  and `*credentials*` JSON, and anything under `secrets/`.
- **The `prod_guard` hook** closes the Bash route — reading, sourcing,
  copying or POSTing the file. A permission rule matches a tool's arguments,
  and Bash's single argument is an opaque command string, so the rule above
  cannot see into it.

The example files stay readable on purpose. `.env.example`, `.env.sample`,
`.env.template` and `.env.dist` are committed, carry no values, and are how
an agent learns which variables a project needs — blocking them would break
the safe half of the convention to protect nothing.

Listing, creating, deleting and `chmod`-ing a secret file are not reads and
are not flagged; neither is appending its name to `.gitignore`. If a read is
genuinely needed, add a regex to `safety.allow` in `.agents/config.yaml` —
but prefer having the value injected as an environment variable, which
leaves nothing to read.

If an allowlist entry does not take effect, the guard now says so in the
permission prompt itself rather than failing silently. Either spelling works:

```yaml
safety:
  allow: ["^cat \.env$"]     # inline
  # or
  allow:
    - "^cat \.env$"          # multi-line
```

## CI secrets

Use the platform's mechanism (GitHub Actions secrets/environments), never
files in the repo. Scope them per-environment and rotate on offboarding.
