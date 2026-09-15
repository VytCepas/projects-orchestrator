---
id: projects-orchestrator-is-a-public-repo
event: PreToolUse
tool: Bash
when: cmd-contains git
when: cmd-contains commit
once: match
---

**This repository is PUBLIC on GitHub.** Measured 2026-09-15 with
`gh repo view --json visibility`. Almost every other tree on this box is private,
so this is one of only two repositories where a commit publishes.

Never commit, and never put in an issue, a PR body or a commit message: a client
or account name, any person's or role's email address, an absolute `/Users/...`
path, or content from the assistant's private configuration directory.

**`fleet.yaml` is machine-specific and gitignored** (`.gitignore:68`), and that is
the load-bearing half: it is the one file here that names this machine's real
layout — every repository path under the home directory, private ones included.
It is untracked on purpose. Do not add it, do not `git add -f` it, and do not
paste its contents anywhere that publishes. `fleet.yaml.example` is the shape that
may be published.

**This file is itself published**, so it names categories rather than examples.
Ask what is PUBLISHED, not what is on disk — `git grep` over tracked files is the
check. Verified clean on 2026-09-15.
