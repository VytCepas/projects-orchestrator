# Watch the fleet on a schedule

`watch` is one pass of everything a scheduled observer needs: it runs the
declared gates fleet-wide, probes each project's CI (via the forge its host
names) and cloud state, merges everything into the checks cache, appends it
to history (the trend column), then computes threshold alerts from the
refreshed view and — when a webhook is configured — pushes them. It chains
what `checks`, `ci`, `cloud-status` and `notify` do separately, so a timer
needs a single invocation.

```bash
projects-orchestrator watch --root ~/projects --webhook "$SLACK_WEBHOOK"
```

Exit codes are scheduler-shaped: **1** means eventful (an alert fired — look
at it), **0** means quiet (within thresholds), **2** means no fleet was found
(a mispointed root is a misconfiguration, not a permanently quiet fleet).

## Install the timer

```bash
mkdir -p ~/.config/systemd/user
cp contrib/systemd/projects-orchestrator-watch.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now projects-orchestrator-watch.timer
```

Configuration is optional, via a private env file:

```bash
mkdir -p ~/.config/projects-orchestrator
printf 'PO_FLEET_ROOT=%s\nPO_WATCH_WEBHOOK=%s\n' \
  "$HOME/projects" "https://hooks.slack.com/services/…" \
  > ~/.config/projects-orchestrator/watch.env
chmod 600 ~/.config/projects-orchestrator/watch.env
```

With no env file the unit watches `~/projects` and prints alerts only to the
journal. As with the other timers, `loginctl enable-linger "$USER"` keeps the
schedule alive without an open session, and on WSL systemd must be enabled in
`/etc/wsl.conf` (`[boot] systemd=true`) first.

## Why hourly is affordable

The unit passes `--changed-only`: a gate whose last cached **pass** is at the
project's current clean HEAD is reused, so an untouched healthy project costs
nothing per firing. Fails are never reused — a red gate reruns every hour, so
recovery is noticed within one firing. Drop the flag from your copy of the
service file to force every gate to run from scratch each time.

The CI and cloud probes always run regardless of `--changed-only` — an
unchanged local HEAD says nothing about a forge or a deployment, whose state
moves without any local edit. They stay cheap: a couple of forge calls per
project, and `deploy: none` projects cost nothing.

## Check it

```bash
systemctl --user list-timers projects-orchestrator-watch.timer
journalctl --user -u projects-orchestrator-watch.service -n 30
```

An eventful pass logs the alerts and exits 1 — the unit counts that as
success (`SuccessExitStatus=1`); a *failed* unit means the pass itself broke,
usually a mispointed `PO_FLEET_ROOT` (exit 2).

## How it relates to the other timers

The three ship as a set: the hourly **watch** notices (this guide), the daily
[digest](scheduled-audit-digest.md) summarizes what changed, and the daily
[heal](scheduled-heal.md) opens PRs that fix what watch noticed. Each is
independent — install any subset.
