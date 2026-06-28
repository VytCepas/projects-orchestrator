# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

- Preferred: use GitHub's private vulnerability reporting on this
  repository (Security tab → "Report a vulnerability").

- Or contact the maintainer directly: @VytCepas

You should receive an acknowledgement within a few business days. Please
include reproduction steps and the affected version or commit.

## Supported versions

| Version | Supported |
|---|---|
| latest release / `main` | ✅ |
| older releases | ❌ |

## Scope notes

Secrets must never be committed — the scaffolded pre-commit hook runs a
gitleaks scan, and CI re-checks. If you find a leaked credential in the
history, report it privately first so it can be rotated before disclosure.
