# ADR-004: Fleet monitor, task runner, and TUI over the registry

- Status: accepted
- Date: 2026-07-01

## Context and Problem Statement

With the descriptor registry (ADR-003) the orchestrator can see the fleet. The
next need is operational: *monitor* each project's health at a glance and *run*
tasks across projects without `cd`-ing into each one — surfaced through a single
overview so the orchestrator behaves like a control panel for logically-connected
projects. How should status collection, task execution, and the UI be layered?

## Considered Options

- **Thin, testable engine + thin TUI**: pure/subprocess modules (`status`,
  `runner`) that the CLI and a Textual TUI both render.
- **Logic inside the TUI**: collect status and run tasks from within Textual
  widgets/workers.
- **Shell script wrappers** driving `git`/`just` per project.

## Decision Outcome

Chosen option: **thin engine + thin TUI**.

- `status.collect_status()` derives health from `git` (repo? branch? dirty?
  ahead/behind) and never raises — a non-repo is reported as `no-git`, so one
  bad project never aborts a fleet sweep.
- `runner.run_in_project()` / `run_across()` execute a task in a project root and
  report ordinary failures (non-zero, timeout, missing binary) as a non-`ok`
  `RunResult` rather than raising, so a sweep continues past a failure.
- The descriptor now parses the `tooling` block (lint/test/format commands) so
  the runner and TUI can offer a project's own declared tasks.
- `tui.fleet_rows()` is a **pure function** (no Textual) — the data shown is
  unit-tested independently; `OrchestratorApp` only renders rows and dispatches
  runs on a background thread worker.
- CLI mirrors the engine: `status <root>` and `run <cmd> <root> [--project|--all]`;
  `tui <root>` launches the overview.

Rationale: keeping all logic in tested, UI-free modules means the TUI (hard to
test, needs a terminal) stays a thin shell, and the same engine powers both the
scriptable CLI and the interactive view.

## Consequences

- Good: health + task-running are fully unit-tested without a terminal.
- Good: CLI and TUI share one engine — no logic duplicated in the UI.
- Good: failure-tolerant by design; a broken project degrades, never aborts.
- Bad: adds `textual` (and its `rich` dep) to runtime dependencies.
- Bad: status is git-only for now; CI/gate health (needs `gh`/network) and
  richer governance rollups are deferred to a follow-up.
