# ADR-003: Cockpit architecture — one engine, many surfaces

- Status: superseded by [ADR-004](adr-004-agent-control-surface.md)
- Date: 2026-07-05

> **Superseded 2026-07-05.** The "one engine" (discovery/runtime/runcommands/
> `snapshot()`) is kept, but the human-facing cockpit framing was the wrong
> target: a Textual TUI + live process supervisor fanned out enough subprocesses
> and long-lived dev servers to OOM WSL, and none of it served the actual goal —
> letting an *agent* control the fleet with fewer tokens. ADR-004 refocuses on a
> CLI-first agent control surface (consistent with ADR-002's CLI-over-MCP token
> economy), a read-only cached dashboard, and a bounded supervisor.

## Context and Problem Statement

projects-orchestrator is growing from a read-only status board into a *cockpit*:
from one place the operator wants to see every project-init project, know
whether it is running right now, start/stop and develop it, drive its git/PR
lifecycle, and reach the same capabilities the coding agents reach (MCP servers,
CLIs). Two consumption surfaces are wanted: a live HTML dashboard opened in
VS Code, and an interactive terminal cockpit. How do we structure this so the
surfaces stay thin and the control logic is written once?

## Considered Options

- **One engine, many surfaces** — a renderer-agnostic core (discovery, runtime
  detection, process supervisor, view-model snapshot) with the HTML server,
  the Textual TUI, and JSON/CLI as thin adapters over it.
- **TUI-first, dashboard bolted on** — build the Textual app, scrape state out
  of it for HTML. Couples control logic to one widget tree.
- **Re-implement integrations natively** — bespoke API clients (Google
  Calendar OAuth, a GitHub client) inside the app.

## Decision Outcome

Chosen option: **one engine, many surfaces**, because the control logic (is it
running? start it, stop it, tail its logs, run its lifecycle) is identical
regardless of surface, and we already have three consumers (`status`, `html`,
`json`) plus two more coming (`serve`, `tui`).

Concretely:

- **`core`** — `discovery` (markers + git), `runtime` (port/`ss` + docker
  detection of externally-started servers), `supervisor` (launch/stop/tail
  processes the cockpit itself starts), `runcommands` (infer each project's
  run/test command from its `justfile` / `package.json` / compose file), and
  `cockpit.snapshot()` (a JSON-able view-model combining all of the above).
- **Surfaces** — `web` (stdlib `http.server`, client polls `/api/status`;
  actions POST to `/api/action`), `tui` (**Textual**), and the existing
  `status`/`html`/`json` renderers. Each is a thin adapter; none owns control
  logic.
- **Integrations follow ADR-002: prefer the CLI/agent, don't re-implement.**
  "Access everything the agents can" means the cockpit *launches and
  orchestrates* the same tools — `gh` for GitHub, the `.claude/scripts/*`
  lifecycle scripts, a Claude Code / codex session in the project dir, and
  MCP-configured servers via those agents — rather than shipping its own OAuth
  clients. Calendar and similar land as launched capabilities, not native code.
- **Textual replaces the stdlib-curses TUI.** Curses cannot reasonably carry
  multi-panel layout, live-scrolling logs, and mouse/focus. Textual is added as
  a runtime dependency; it is headless-testable via `App.run_test()`, so the
  cockpit stays covered by CI.

### Consequences

- Good: control logic written once; a new surface (e.g. an MCP server exposing
  the fleet to Claude) is another thin adapter over `cockpit.snapshot()` +
  `supervisor`.
- Good: `serve` and `html` need no TTY, so they are fully CI-verifiable; the
  Textual TUI is verified headless.
- Good: integration surface stays small and agent-agnostic (consistent with
  ADR-002) — no secret-bearing API clients inside the app.
- Bad: adds the `textual` dependency (first non-stdlib runtime dep); the
  supervisor holds process state in memory for the life of one `serve`/`tui`
  session (no cross-session persistence yet).
- Bad: runtime detection is Linux/WSL-specific (`/proc`, `ss`); other platforms
  degrade to supervised-only state.
