"""Deterministic command controller — the single control point.

Text in, actions out, no LLM in the loop: :func:`parse_command` maps input
to a typed :class:`Intent` (pure, table-testable) and :func:`dispatch` runs
it against the engine, yielding one line at a time so the REPL and the TUI
can stream identical output. ``/ask`` is a reserved seam for an optional
natural-language mode; it stays disabled here so the controller remains
deterministic.
"""

from __future__ import annotations

import datetime as _dt
import shlex
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from projects_orchestrator import cache
from projects_orchestrator.adapters.cloud import as_check_results as cloud_check_results
from projects_orchestrator.adapters.cloud import collect_cloud, trigger_deploy
from projects_orchestrator.adapters.github import as_check_results, collect_github
from projects_orchestrator.adapters.project_init import latest_upstream_version
from projects_orchestrator.audit import audit_project
from projects_orchestrator.checks import collect_checks
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.detail import build_detail, render_detail
from projects_orchestrator.doctor import diagnose
from projects_orchestrator.drift import compute_drift
from projects_orchestrator.fleet import fleet_rows, fleet_snapshots, render_table
from projects_orchestrator.heal import heal_project, render_heal_result
from projects_orchestrator.memory import load_project_memory, search_memory
from projects_orchestrator.observability import filter_since, load_events
from projects_orchestrator.pool import map_ordered
from projects_orchestrator.registry import Fleet, FleetConfig, discover
from projects_orchestrator.status import clean_worktree_head, collect_status
from projects_orchestrator.supervisor import logs as run_logs
from projects_orchestrator.supervisor import start as run_start
from projects_orchestrator.supervisor import stop as run_stop
from projects_orchestrator.upgrade import upgrade_plan

HELP_TEXT = """\
commands:
  status [project]        fleet table, or one project's git health
  lint [project|all]      run declared lint gate(s)
  test [project|all]      run declared test gate(s)
  checks [project|all]    run lint + test gates
  run <task> [project|all]  run any declared tooling task
  start <project>         launch the project's run_command (detached)
  stop <project>          terminate the supervised process
  logs <project>          tail the captured run output
  deploy <project> [action]  plan a cloud action (deploy|rollback|restart); dispatch via CLI --apply
  heal <project>          spawn a scoped agent to fix a cached lint/test failure, open a PR
  memory <query>          search every project's memory files
  drift [project|all]     scaffold drift vs the recorded manifest
  doctor [project|all]    diagnose contract-v1 conformance
  audit [project|all]     governance report (conformance+drift+memory+freshness)
  ci [project|all]        latest CI conclusion + open-PR count (via gh)
  cloud [project|all]     deploy/runtime status (descriptor deploy block)
  events [project|all]    recent guard/usage events from observability logs
  detail <project>        per-project drill-in (descriptor+checks+commits+memory)
  upgrade [project|all]   scaffold version vs upstream project-init
  projects                list discovered projects
  refresh                 re-discover the fleet
  help                    this text
  quit                    leave the controller
  /ask <question>         natural-language mode (opt-in via ORCHESTRATOR_ASK_MODEL)"""

_TASK_VERBS = {"lint": ("lint",), "test": ("test",), "checks": ("lint", "test")}

# Verbs that take an optional project target (default "all").
_TARGET_VERBS = {"drift", "doctor", "audit", "ci", "upgrade", "cloud", "events", "detail"}

# Verbs that require exactly one project target (plus an optional trailing
# argument, e.g. the deploy action — supervise verbs ignore it).
_PROJECT_VERBS = {"start", "stop", "logs", "deploy", "heal"}


@dataclass(frozen=True)
class Intent:
    """A parsed controller command.

    Attributes:
        verb: Canonical action (status, check, run, memory, drift, doctor,
            audit, ci, upgrade, projects, refresh, help, quit, ask, unknown).
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


def _parse_arg_verb(verb: str, rest: list[str]) -> Intent | None:
    """Parse the verbs that carry arguments beyond a bare target; else ``None``.

    ``run`` and ``memory`` have their own arg shapes; ``work`` takes a project and
    the whole remaining tail as a (multi-word) task — unlike the ``_PROJECT_VERBS``
    below, which keep only a single trailing token.
    """
    if verb == "run" and rest:
        return Intent(verb="run", target=rest[1] if len(rest) > 1 else "all", args=(rest[0],))
    if verb == "memory" and rest:
        return Intent(verb="memory", args=(" ".join(rest),))
    if verb == "work":
        return Intent(verb="work", target=rest[0] if rest else None, args=tuple(rest[1:]))
    return None


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
    if stripped == "/ask" or stripped.startswith("/ask "):
        return Intent(verb="ask", args=(stripped[len("/ask") :].strip(),))

    word, *rest = stripped.split()
    verb = word.lower()
    if verb in _TASK_VERBS:
        return _parse_task_verb(verb, rest)
    if verb == "status":
        return Intent(verb="status", target=rest[0] if rest else None)
    arg_verb = _parse_arg_verb(verb, rest)
    if arg_verb is not None:
        return arg_verb
    if verb in _TARGET_VERBS:
        return Intent(verb=verb, target=rest[0] if rest else "all")
    if verb in _PROJECT_VERBS:
        return Intent(
            verb=verb,
            target=rest[0] if rest else None,
            args=(rest[1],) if len(rest) > 1 else (),
        )
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
        ask_complete: Completer override for /ask (None = the real API);
            tests inject a fake so no live call is ever made.
    """

    config: FleetConfig
    fleet: Fleet = field(init=False)
    cache_file: Path | None = None
    ask_complete: Callable[[str, str], str] | None = None

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
    # Stamp the clean-worktree HEAD so a REPL/TUI run's cached passes can later
    # satisfy `checks --changed-only` (an unstamped result never matches).
    per_project = map_ordered(
        lambda d: collect_checks(d, intent.args, head=clean_worktree_head(d)), selected
    )
    results = [result for project_results in per_project for result in project_results]
    for result in results:
        suffix = f" — {result.detail}" if result.detail else ""
        yield f"{result.project} {result.task}: {result.status.upper()}{suffix}"
    cache.save_results(results, ctx.cache_file)


def _dispatch_status(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Show the fleet table, or one project's git health."""
    if intent.target in (None, "all"):
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
    query = intent.args[0] if intent.args else ""
    if not query.strip():
        yield "usage: memory <query>"
        return
    memories = [load_project_memory(d) for d in ctx.fleet.descriptors]
    hits = search_memory(memories, query)
    if not hits:
        yield f"no memory matches for: {query}"
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


def _dispatch_doctor(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Report contract-v1 conformance per project."""
    selected = _select_projects(ctx, intent.target)
    if isinstance(selected, str):
        yield selected
        return
    for descriptor in selected:
        report = diagnose(descriptor)
        yield f"{report.project}: {report.status}"
        for finding in report.findings:
            yield f"  [{finding.status}] {finding.check}: {finding.detail}"


def _dispatch_audit(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Report the composed governance audit per project."""
    selected = _select_projects(ctx, intent.target)
    if isinstance(selected, str):
        yield selected
        return
    cached = cache.load_results(ctx.cache_file)
    for descriptor in selected:
        report = audit_project(descriptor, cached.get(descriptor.name))
        yield f"{report.project}: {report.status}"
        for finding in report.findings:
            yield f"  [{finding.severity}] {finding.category}: {finding.message}"


def _dispatch_ci(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Probe CI conclusion + open-PR count per project (via gh); cache them."""
    selected = _select_projects(ctx, intent.target)
    if isinstance(selected, str):
        yield selected
        return
    checked_at = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
    results = []
    for descriptor in selected:
        status = collect_github(descriptor)
        results.extend(as_check_results(status, checked_at))
        prs = "?" if status.open_prs is None else str(status.open_prs)
        yield f"{status.project}: CI {status.ci}, {prs} open PR(s)"
    cache.save_results(results, ctx.cache_file)


def _dispatch_cloud(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Probe deploy/runtime status per project (descriptor-driven); cache it."""
    selected = _select_projects(ctx, intent.target)
    if isinstance(selected, str):
        yield selected
        return
    checked_at = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
    results = []
    for descriptor in selected:
        status = collect_cloud(descriptor)
        results.extend(cloud_check_results(status, checked_at))
        parts = [status.state]
        if status.revision:
            parts.append(status.revision)
        if status.health:
            parts.append(status.health)
        yield f"{status.project}: {status.target} — {' '.join(parts)}"
    cache.save_results(results, ctx.cache_file)


def _dispatch_events(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Show recent guard/usage events per project."""
    selected = _select_projects(ctx, intent.target)
    if isinstance(selected, str):
        yield selected
        return
    since = intent.args[0] if intent.args else ""
    empty = True
    for descriptor in selected:
        project_events = load_events(descriptor)
        for event in filter_since(project_events.events, since):
            empty = False
            command = f" — {event.command}" if event.command else ""
            yield f"{event.project} {event.timestamp} [{event.hook}] {event.action}{command}"
    if empty:
        yield "no events recorded"


def _dispatch_detail(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Render the per-project drill-in."""
    if intent.target is None or intent.target == "all":
        yield "usage: detail <project>"
        return
    selected = _select_projects(ctx, intent.target)
    if isinstance(selected, str):
        yield selected
        return
    cached = cache.load_results(ctx.cache_file)
    yield from render_detail(build_detail(selected[0], cached.get(selected[0].name)))


def _dispatch_supervise(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Start/stop/tail one project's supervised process."""
    if intent.target is None or intent.target == "all":
        yield f"usage: {intent.verb} <project>"
        return
    selected = _select_projects(ctx, intent.target)
    if isinstance(selected, str):
        yield selected
        return
    descriptor = selected[0]
    if intent.verb == "start":
        yield run_start(descriptor)
    elif intent.verb == "stop":
        yield run_stop(descriptor)
    else:
        yield from run_logs(descriptor)


def _dispatch_deploy(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Plan a cloud action from the cockpit — never dispatch (ADR-005).

    The REPL/TUI is plan-only: it reports what the CLI ``deploy --apply`` would
    dispatch, so an agent driving the controller cannot fire a production
    deploy by typing a line.
    """
    if intent.target is None or intent.target == "all":
        yield "usage: deploy <project> [deploy|rollback|restart]"
        return
    selected = _select_projects(ctx, intent.target)
    if isinstance(selected, str):
        yield selected
        return
    action = intent.args[0] if intent.args else "deploy"
    result = trigger_deploy(selected[0], action, apply=False)
    if result.status == "planned":
        # The hint MUST carry the action that was just planned. `--action`
        # defaults to `deploy`, so a bare `deploy alpha --apply` copied out of a
        # *rollback* plan would dispatch a DEPLOY — the cockpit would have handed
        # the operator the wrong production mutation, in their own words.
        yield (
            f"{result.project}: {result.action} planned via {result.workflow} "
            f"— run `deploy {result.project} --action {result.action} --apply` "
            f"on the CLI to dispatch"
        )
        return
    yield f"{result.project}: {result.action} {result.status} — {result.detail}"


def _dispatch_work(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Propose an agent run — and never, ever launch one (#124).

    Launching an agent is a write action with a token budget and a real blast
    radius (a branch and a draft PR per project). The controller — reachable by
    the ``/ask`` model and by a mistyped line — must be unable to start one, for
    the same reason it is plan-only for ``deploy`` (ADR-005). So this prints the
    exact CLI command that WOULD launch it and dispatches nothing: the shortest
    path from a typo to twelve running agents ends here, at a printed string.
    """
    if intent.target is None or intent.target == "all":
        yield "usage: work <project> <task>"
        return
    selected = _select_projects(ctx, intent.target)
    if isinstance(selected, str):
        yield selected
        return
    task = " ".join(intent.args).strip()
    if not task:
        yield "usage: work <project> <task>"
        return
    project = selected[0].name
    # shlex.quote both fields: the task comes from the /ask model or a typed line
    # and may carry shell metacharacters. A naive `"..."` would NOT stop `$(...)`
    # substitution, so an operator pasting the hint could execute an injected
    # command — the exact harm this plan-only surface exists to prevent.
    command = f"work {shlex.quote(project)} {shlex.quote(task)}"
    yield (
        f"{project}: would launch an agent — run `{command}` on the CLI to start it. "
        "Nothing was dispatched here."
    )


def _dispatch_heal(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Attempt an autonomous fix for one project's cached lint/test failure."""
    if intent.target is None or intent.target == "all":
        yield "usage: heal <project>"
        return
    selected = _select_projects(ctx, intent.target)
    if isinstance(selected, str):
        yield selected
        return
    descriptor = selected[0]
    cached = cache.load_results(ctx.cache_file).get(descriptor.name, {})
    yield render_heal_result(heal_project(descriptor, cached))


def _dispatch_ask(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Resolve /ask to an existing intent (opt-in), then dispatch it."""
    import os

    from projects_orchestrator.ask import resolve_ask

    resolved = resolve_ask(
        intent.args[0] if intent.args else "",
        ctx.fleet.names,
        os.environ,
        complete=ctx.ask_complete,
    )
    if isinstance(resolved, str):
        yield resolved
        return
    yield f"→ {resolved.verb}" + (f" {resolved.target}" if resolved.target else "")
    yield from dispatch(resolved, ctx)


def _dispatch_upgrade(ctx: ControllerContext, intent: Intent) -> Iterator[str]:
    """Report each project's scaffold version vs upstream project-init."""
    selected = _select_projects(ctx, intent.target)
    if isinstance(selected, str):
        yield selected
        return
    latest = latest_upstream_version(Path.cwd())
    rows = upgrade_plan(selected, latest, cache.load_results(ctx.cache_file))
    for row in rows:
        yield f"{row.project}: {row.status} (scaffold {row.scaffold_version}, drift {row.drift})"


# Engine verbs: each handler reads the fleet and streams result lines.
_ENGINE = {
    "check": _dispatch_check,
    "run": _dispatch_check,
    "status": _dispatch_status,
    "memory": _dispatch_memory,
    "drift": _dispatch_drift,
    "doctor": _dispatch_doctor,
    "audit": _dispatch_audit,
    "ci": _dispatch_ci,
    "cloud": _dispatch_cloud,
    "events": _dispatch_events,
    "detail": _dispatch_detail,
    "start": _dispatch_supervise,
    "stop": _dispatch_supervise,
    "logs": _dispatch_supervise,
    "deploy": _dispatch_deploy,
    "work": _dispatch_work,
    "heal": _dispatch_heal,
    "ask": _dispatch_ask,
    "upgrade": _dispatch_upgrade,
}

# Constant replies that need neither fleet state nor arguments.
_STATIC_REPLIES = {
    "help": tuple(HELP_TEXT.splitlines()),
}


def dispatch(intent: Intent, ctx: ControllerContext) -> Iterator[str]:
    """Execute one intent against the engine, yielding output lines.

    Args:
        intent: The parsed command.
        ctx: Fleet state to act on.

    Yields:
        Human-readable output lines (colorless; surfaces add styling).
        Terminal verbs (e.g. ``quit``) match nothing and yield nothing.
    """
    handler = _ENGINE.get(intent.verb)
    if handler is not None:
        yield from handler(ctx, intent)
    elif intent.verb in _STATIC_REPLIES:
        yield from _STATIC_REPLIES[intent.verb]
    elif intent.verb == "projects":
        yield from (ctx.fleet.names or ("no projects discovered",))
    elif intent.verb == "refresh":
        ctx.refresh()
        yield f"fleet refreshed: {len(ctx.fleet.descriptors)} project(s)"
    elif intent.verb == "unknown":
        yield f"unknown command: {intent.args[0]} (try: help)"
