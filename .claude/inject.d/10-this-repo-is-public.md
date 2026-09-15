---
id: projects-orchestrator-is-a-public-repo
event: SessionStart
when: always
once: never
---

**This repository is PUBLIC on GitHub.** Measured 2026-09-15 with
`gh repo view --json visibility`. Almost every other tree on this box is private,
so this is one of only two repositories where a commit publishes.

**It loads at session start rather than on the command that publishes**, because
a rule keyed to one publishing path does not cover the others. This fired on
`cmd-contains git` AND `cmd-contains commit` first — `when` repeats are ANDed —
so it was silent for `gh issue create` and `gh pr create`, and the documented
workflow here opens an issue *before* the first commit. The warning arrived after
the publish it existed to prevent. There is no OR in the predicate vocabulary, so
enumerating the paths means one pack per path and a body that drifts between
them; being resident from the first turn covers every path including the ones
nobody enumerated. Raised in review on #232.

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
