# Heal the fleet on a schedule

`heal` fixes a failing lint/test gate the way the engine detects one: it runs the
gate, hands a scoped coding agent the failing command and its error, re-runs the
gate to verify the fix, and — only on a verified pass — opens a **draft PR**. It
never touches a default branch and never merges anything (ADR-006). This guide
runs it across the whole fleet, unattended, on a timer.

Read this first, because unlike the digest, **an unattended heal spends money and
opens PRs**. Everything below is about keeping that bounded.

## What one pass does

```bash
just heal-all                  # or: projects-orchestrator heal --all --limit 3
```

`heal --all`:

1. runs each project's `lint` and `test` gates (fresh, unless `--cached`),
2. picks the projects with a red gate, **up to `--limit`**,
3. heals them one at a time, opening a draft PR for each verified fix.

Projects past the limit are reported as `deferred` — named, never silently
dropped — so the cap is always visible. The closing line tallies what it healed
and what the agents cost:

```
alpha: fixed — PR opened at https://github.com/…/pull/42 (branch heal/lint-alpha-9f3c)
beta: verify_failed — still failing after the agent's fix: test (branch heal/test-beta-1a2b)
deferred 1 more (limit 3): gamma
healed 1/2 attempted — spend $1.34 across 2 runs
```

Exit codes suit a scheduler: **1** when the pass was *eventful* (it opened a PR, a
heal failed to verify, or a project was deferred), **0** when the fleet had
nothing to heal, **2** on a usage error.

## The safety model — why this is safe to leave running

- **Nothing merges.** A verified fix lands as a *draft* PR. The worst an
  autonomous pass can do is open a PR nobody wants.
- **Each agent is budget-capped.** A single heal's coding agent runs under a
  small per-run USD cap and a scoped tool allowlist (it can edit files and re-run
  *this project's own* declared lint/test commands, nothing else).
- **The pass is project-capped.** `--limit` bounds how many projects one firing
  spends an agent on, so a fleet that goes red overnight cannot fan out unbounded
  spend. Start low.
- **Only lint/test are eligible.** `ci`/`cloud` failures depend on remote state a
  local run can't reproduce, so they are never healed automatically.

Start with `--limit 1` or `2`, watch the PRs it opens for a few days, and raise it
only once you trust them.

## Install the timer

The unit heals `~/projects` and attempts at most 3 projects per firing. Override
either in a private env file:

```bash
mkdir -p ~/.config/projects-orchestrator
printf 'PO_FLEET_ROOT=%s\nPO_HEAL_LIMIT=%s\n' "$HOME/projects" 2 \
  > ~/.config/projects-orchestrator/heal.env
```

Skip that step to accept the defaults (`~/projects`, limit 3).

```bash
mkdir -p ~/.config/systemd/user
cp contrib/systemd/projects-orchestrator-heal.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now projects-orchestrator-heal.timer
```

Check it:

```bash
systemctl --user list-timers projects-orchestrator-heal.timer   # next firing
systemctl --user start projects-orchestrator-heal.service       # run it now
journalctl --user -u projects-orchestrator-heal.service -n 30   # what it did
```

The unit expects the console script at `~/.local/bin/projects-orchestrator` (a
`uv tool install` / `pip install --user` puts it there). Adjust `ExecStart` if
yours lives elsewhere.

### cron instead

```cron
PO_FLEET_ROOT=/home/you/projects

# Daily at 03:00. `heal --all` exits 1 on an eventful pass — that is a working
# run with something to review, so don't let cron treat it as an error.
0 3 * * * "$HOME/.local/bin/projects-orchestrator" heal --all --root "$PO_FLEET_ROOT" --limit 2 || true
```

cron runs with a near-empty environment, so set the variables at the top of the
crontab and use absolute paths. The agent still needs your `claude` and `gh`
credentials on `PATH` — see below.

## Why this runs on your machine, not in GitHub Actions

The same two reasons the audit digest can't:

1. **The fleet is local.** Discovery scans sibling directories on disk for
   `.agents/config.yaml`. The descriptor records no git remote, so a hosted
   runner has no way to find — or clone — the fleet it is meant to heal.
2. **It needs your credentials.** Heal spawns the `claude` CLI and opens PRs with
   `gh`; both authenticate as *you*, from your user session. A hosted runner has
   neither, and wiring them in would mean handing a cloud runner standing write
   access to every repo in the fleet — the opposite of the PR-gated,
   nothing-merges-itself boundary this feature is built on.

Both problems disappear when the heal runs where the fleet already lives and your
credentials already are. A self-hosted runner on that machine would work for the
same reason — the timer is just the smaller way to get there.
