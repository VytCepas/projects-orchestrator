"""Deterministic command controller — the single control point.

Text in, actions out, no LLM in the loop: :func:`parse_command` maps input
to a typed :class:`Intent` (pure, table-testable) and :func:`dispatch` runs
it against the engine, yielding one line at a time so the REPL and the TUI
can stream identical output. ``/ask`` is a reserved seam for an optional
natural-language mode; it stays disabled here so the controller remains
deterministic.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from projects_orchestrator import cache
from projects_orchestrator.checks import run_check
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.drift import compute_drift
from projects_orchestrator.fleet import fleet_rows, fleet_snapshots, render_table
from projects_orchestrator.memory import load_project_memory, search_memory
from projects_orchestrator.registry import Fleet, FleetConfig, discover
from projects_orchestrator.status import collect_status

HELP_TEXT = """\
commands:
  status [project]        fleet table, or one project's git health
  lint [project|all]      run declared lint gate(s)
  test [project|all]      run declared test gate(s)
  checks [project|all]    run lint + test gates
  run <task> [project|all]  run any declared tooling task
  memory <query>          search every project's memory files
  drift [project|all]     scaffold drift vs the recorded manifest
  projects                list discovered projects
  refresh                 re-discover the fleet
  help                    this text
  quit                    leave the controller
  /ask <question>         natural-language mode (not enabled)"""

_TASK_VERBS = {"lint": ("lint",), "test": ("test",), "checks": ("lint", "test")}


@dataclass(frozen=True)
class Intent:
    """A parsed controller command.

    Attributes:
        verb: Canonical action (status, check, run, memory, projects,
            refresh, help, quit, ask, unknown).
        target: Project name, ``all``, or ``None`` (verb-dependent).
        args: Extra arguments (task names for ``run``, query words for
            ``memory``).
    """

    verb: str
    target: str | None = None
    args: tuple[str, ...] = ()


def _parse_task_verb(verb: str, rest: list[str]) -> Intent:
    """Build a check intent for lint/test/checks."""
    target = rest[0] if rest else "all"
    return Intent(verb="check", target=target, args=_TASK_VERBS[verb])


def parse_command(text: str) -> Intent:
    """Map one input line to an intent (pure; never raises).

    Args:
        text: Raw controller input.

    Returns:
        The parsed intent; unrecognized input yields ``verb="unknown"``.
    """
    stripped = text.strip()
    if not stripped:
        return Intent(verb="help")
    if stripped.startswith("/ask"):
        return Intent(verb="ask", args=(stripped[len("/ask") :].strip(),))

    word, *rest = stripped.split()
    verb = word.lower()
    if verb in _TASK_VERBS:
        return _parse_task_verb(verb, rest)
    if verb == "status":
        return Intent(verb="status", target=rest[0] if rest else None)
    if verb == "run" and rest:
        return Intent(verb="run", target=rest[1] if len(rest) > 1 else "all", args=(rest[0],))
    if verb == "memory" and rest:
        return Intent(verb="memory", args=(" ".join(rest),))
    if verb == "drift":
        return Intent(verb="drift", target=rest[0] if rest else "all")
    if verb in {"projects", "refresh", "help", "quit", "exit"}:
        return Intent(verb="quit" if verb == "exit" else verb)
    return Intent(verb="unknown", args=(stripped,))


@dataclass
class ControllerContext:
    """Mutable state the dispatcher works against.

    Attributes:
        config: Fleet discovery configuration.
        fleet: The currently discovered fleet.
        cache_file: Checks-cache override (None = default location).
    """

    config: FleetConfig
    fleet: Fleet = field(init=False)
    cache_file: Path | None = None

    def __post_init__(self) -> None:
        """Discover the fleet immediately so every command has one."""
        self.fleet = discover(self.config)

    def refresh(self) -> None:
        """Re-discover the fleet from the same configuration."""
        self.fleet = discover(self.config)


def _select_projects(ctx: ControllerContext, target: str | None) -> list[ProjectDescriptor] | str:
    """Resolve a target to descriptors, or an error line."""
    if target is None or target == "all":
        return list(ctx.fleet.descriptors)
    descriptor = ctx.fleet.get(target)
    if descriptor is None:
        known = ", ".join(ctx.fleet.names) or "none discovered"
        return f"unknown project: {target} (known: {known})"
    return [descriptor]


def _dispatch_check(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Run declared gates and stream pass/fail lines."""
    selected = _select_projects(ctx, intent.target)
    if isinstance(selected, str):
        yield selected
        return
    results = []
    for descriptor in selected:
        for task in intent.args:
            result = run_check(descriptor, task)
            results.append(result)
            suffix = f" — {result.detail}" if result.detail else ""
            yield f"{result.project} {result.task}: {result.status.upper()}{suffix}"
    cache.save_results(results, ctx.cache_file)


def _dispatch_status(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Show the fleet table, or one project's git health."""
    if intent.target is None:
        snapshots = fleet_snapshots(ctx.fleet, ctx.cache_file)
        yield from render_table(fleet_rows(snapshots)).splitlines()
        return
    selected = _select_projects(ctx, intent.target)
    if isinstance(selected, str):
        yield selected
        return
    status = collect_status(selected[0])
    detail = f" ({status.detail})" if status.detail else ""
    yield f"{status.project}: {status.health} on {status.branch or '?'}{detail}"


def _dispatch_memory(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Search the whole fleet's memory."""
    memories = [load_project_memory(d) for d in ctx.fleet.descriptors]
    hits = search_memory(memories, intent.args[0])
    if not hits:
        yield f"no memory matches for: {intent.args[0]}"
        return
    for hit in hits[:50]:
        location = f"{hit.file.project}/{hit.file.path.name}"
        yield f"{location}:{hit.line_number} [{hit.file.type}] {hit.file.name} — {hit.line}"


def _dispatch_drift(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Report scaffold drift per project."""
    selected = _select_projects(ctx, intent.target)
    if isinstance(selected, str):
        yield selected
        return
    for descriptor in selected:
        report = compute_drift(descriptor)
        yield f"{report.project}: {report.summary}"
        for relpath in report.modified:
            yield f"  modified: {relpath}"
        for relpath in report.missing:
            yield f"  missing:  {relpath}"


def dispatch(intent: Intent, ctx: ControllerContext) -> Iterator[str]:
    """Execute one intent against the engine, yielding output lines.

    Args:
        intent: The parsed command.
        ctx: Fleet state to act on.

    Yields:
        Human-readable output lines (colorless; surfaces add styling).
    """
    if intent.verb == "check" or intent.verb == "run":
        yield from _dispatch_check(ctx, intent)
    elif intent.verb == "status":
        yield from _dispatch_status(ctx, intent)
    elif intent.verb == "memory":
        yield from _dispatch_memory(ctx, intent)
    elif intent.verb == "drift":
        yield from _dispatch_drift(ctx, intent)
    elif intent.verb == "projects":
        yield from (ctx.fleet.names or ("no projects discovered",))
    elif intent.verb == "refresh":
        ctx.refresh()
        yield f"fleet refreshed: {len(ctx.fleet.descriptors)} project(s)"
    elif intent.verb == "ask":
        yield "natural-language mode is not enabled — this controller is deterministic"
    elif intent.verb == "help":
        yield from HELP_TEXT.splitlines()
    elif intent.verb == "unknown":
        yield f"unknown command: {intent.args[0]} (try: help)"
