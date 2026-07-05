# ADR-004: Agent control surface — a token-lean CLI, a read-only dashboard

- Status: accepted
- Date: 2026-07-05
- Supersedes: [ADR-003](adr-003-cockpit-architecture.md)

## Context and Problem Statement

The goal for projects-orchestrator is: **let a coding agent (Claude, or
another) control every project-init project from one place, using fewer tokens
than driving each project by hand.** ADR-003 mistook this for a human-facing
*cockpit* and built a Textual TUI and a live web dashboard on top of a process
supervisor. Two problems surfaced immediately:

1. **It OOMs the machine.** The TUI polls a full `snapshot()` every second, and
   each snapshot shells out `ss` + `docker ps` per project; the web surface is
   thread-per-request with no cap. The supervisor holds long-lived dev servers
   (`just dev`, `docker compose up`) in memory with no worker cap and no memory
   ceiling. On WSL2 (~16 GB) this exhausts memory, the OOM killer takes
   `dbus.service`, and the systemd user session wedges until `wsl --shutdown`.
2. **It does not serve the goal.** A TUI is driven by a *human*, not an agent,
   and saves no tokens. The one surface that would — an agent-facing control
   API — was never built.

## Decision Outcome

**Keep the engine, change the surface.** Retain the renderer-agnostic core
(`discovery`, `runtime`, `runcommands`, `cockpit.snapshot()`). Replace the
human cockpit framing with an agent control surface:

- **Agent control is a CLI, not an MCP server.** Per [ADR-002](adr-002-mcp-choices.md),
  MCP servers load every tool definition into context on startup — a standing
  token cost paid whether or not the tools are used — which is why this project
  prefers CLIs (`gh` over the GitHub MCP, etc.). The token-optimal way for an
  agent to control the fleet is therefore a **terse CLI it invokes through the
  Bash tool it already has**: zero standing cost, one `json`/`status` call
  returns the whole fleet, and control is expressed as subcommands
  (`run`, `test`, `stop`, `logs`). An MCP server remains a possible *future*
  thin adapter for hosted/Cowork surfaces, opt-in and paying its own token cost.
- **The dashboard survives as read-only.** A human (or an agent) still wants to
  glance at fleet status, so `serve` stays — but read-only, serving a
  **snapshot cached with a short TTL** so a browser poll never triggers a
  per-project subprocess storm. State-changing endpoints move off the web
  surface; actions go through the CLI/agent.
- **The supervisor is bounded.** Any process the orchestrator starts is gated by
  a **concurrent-worker cap** and a **`MemAvailable` ceiling**, and **fails fast
  with a clear error** when free memory is below a threshold — the fixes the
  OOM issue itself proposed. Run-to-completion gates (`test`/`lint`) are
  preferred over long-lived servers.
- **The Textual TUI is retired.** It is the 1-second poller and the heaviest
  liability, it serves a human rather than an agent, and dropping it removes the
  project's only non-stdlib runtime dependency (`textual`).

### Consequences

- Good: directly serves the goal — an agent controls the fleet in one Bash call,
  at no standing token cost, consistent with ADR-002.
- Good: the OOM root causes are removed (bounded supervisor, cached read-only
  dashboard, no 1s poller).
- Good: runtime dependencies drop back to the stdlib (`textual` removed); the
  read surfaces (`json`/`status`/`serve`) need no TTY and stay CI-verifiable.
- Bad: no interactive terminal UI. If a rich human TUI is wanted later it must
  be re-justified against this decision and built as a bounded, non-polling
  client of the same engine.
- Bad: supervised long-lived servers still consume memory; the cap/ceiling
  bounds the blast radius but the operator must still choose what to keep
  running.
- Neutral: runtime detection remains Linux/WSL-specific (`/proc`, `ss`); other
  platforms degrade to supervised-only state (unchanged from ADR-003).
