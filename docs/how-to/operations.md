# Operate the orchestrator's own state

Everything the orchestrator remembers between runs lives in two directories, and
this page is about keeping them healthy: what grows, what is safe to delete, what
to back up, and what happens when a file moves between versions.

None of it is precious in the sense a database is — every file here is a *cache
of something re-derivable* — but "re-derivable" is not the same as "free", and
one of these files is load-bearing in a way the others are not.

## What lives where

Both roots honour the XDG variables, so a per-machine override moves everything
at once.

| File | Directory | What it is | Safe to delete? |
|---|---|---|---|
| `checks.json` | `$XDG_CACHE_HOME/projects-orchestrator/` | Last known gate results per project/task | Yes — the next `checks` run refills it |
| `history.jsonl` | `$XDG_STATE_HOME/projects-orchestrator/` | Bounded append log behind the trend column | Yes — trends restart from empty |
| `audit-digest.json` | `$XDG_STATE_HOME/projects-orchestrator/` | Baseline for `audit --digest` deltas | **Careful** — see below |
| `watch-heartbeat.json` | `$XDG_STATE_HOME/projects-orchestrator/` | When the scheduled pass last ran | Yes — reads as `never` until the next pass |

Defaults when the variables are unset: `~/.cache/…` and `~/.local/state/…`.

**`audit-digest.json` is the one to think about before deleting.** It is the
*previous* set of findings, and `audit --digest` reports the delta against it.
Delete it and the next digest treats every existing finding as new — which, if a
webhook is configured, means pushing a wall of alerts for problems nobody just
introduced. Deleting it is a reasonable way to force a full re-report; doing it
by accident is a bad afternoon.

## Growth and rotation

`history.jsonl` is the only file that grows with time rather than with fleet
size, and it is **self-bounding**: `record` truncates to the newest
`MAX_ENTRIES` on every write, so it cannot grow without limit and needs no
`logrotate` entry. Nothing here appends unboundedly.

The two caches grow with the number of projects and tasks, not with the number
of runs — a fleet of twenty projects produces a file measured in kilobytes.

Supervised-process logs are the exception: `start` writes each run's stdout to
its own file under the state directory, and those are as large as the process
makes them. They are per-run and never appended to after the run ends, so rotate
them by age if a long-running service is noisy:

```bash
find "${XDG_STATE_HOME:-$HOME/.local/state}/projects-orchestrator/run" \
  -name '*.log' -mtime +30 -delete
```

## Back up

The short answer: **back up nothing, except possibly the digest baseline.**

Everything else is re-derived by running the gates again, and a restored
`checks.json` is *worse* than an empty one — it asserts results that were true on
another machine at another commit, and the status table would present them as
this machine's. An empty cache reads honestly as "never probed".

The one file worth keeping, if you push audit deltas to a webhook:

```bash
cp "${XDG_STATE_HOME:-$HOME/.local/state}/projects-orchestrator/audit-digest.json" \
   ~/backup/
```

**The fleet definition is the thing that actually matters, and it is not here.**
`fleet.yaml` lives beside the orchestrator, not in the state directory, and it is
the only file whose loss changes what the fleet *is* rather than what is known
about it. Keep that under version control or in your dotfiles; the rest of this
page is about disposable state.

## Migrating between versions

The cache carries a `__schema_version__` key and the reader reports four
verdicts, which is what makes an upgrade — or a rollback — safe:

- **Same version** — read normally.
- **Older, pre-envelope** — read, and re-stamped on the next write. No results
  are lost on upgrade.
- **Newer** — refused, and **not overwritten**. A build that does not understand
  the file leaves it for the build that does, rather than replacing it with its
  own narrower view. This is what makes running two installations against one
  `$XDG_CACHE_HOME` safe.
- **Unreadable** — treated as empty, and reported as corrupt rather than as
  version skew, because the remedies differ.

So the migration procedure is: **there isn't one.** Upgrade, downgrade, or run
two versions side by side; no file needs converting and nothing is silently
discarded.

The one thing to know: after a rollback, the older build drops the version key
when it writes. The newer build reads that back as pre-envelope and re-stamps it.
Neither direction loses a result.

## Starting clean

To reset everything the orchestrator knows without touching what it governs:

```bash
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/projects-orchestrator" \
       "${XDG_STATE_HOME:-$HOME/.local/state}/projects-orchestrator"
```

This removes no project data — the orchestrator never writes into the repos it
reads (ADR-003) — and the next `checks` run rebuilds the cache. Expect the first
`audit --digest` afterwards to report every finding as new, for the reason above.
