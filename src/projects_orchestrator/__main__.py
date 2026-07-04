"""CLI entry point for `projects-orchestrator`.

One command surface over the fleet engine: discover projects, show git
health, run their declared gates, search their memories, or drive it all
interactively (``controller`` REPL / ``tui``). Every data command takes
``--json`` so external monitors can consume the same truth the tables show.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from dataclasses import asdict
from pathlib import Path

from projects_orchestrator import __version__, cache
from projects_orchestrator.adapters.cloud import as_check_results as cloud_check_results
from projects_orchestrator.adapters.cloud import collect_cloud
from projects_orchestrator.adapters.github import as_check_results, collect_github
from projects_orchestrator.adapters.project_init import latest_upstream_version, trigger_upgrade
from projects_orchestrator.audit import audit_project, render_markdown
from projects_orchestrator.checks import DEFAULT_TASKS, CheckResult, collect_checks
from projects_orchestrator.controller import ControllerContext, dispatch, parse_command
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.doctor import diagnose
from projects_orchestrator.drift import compute_drift
from projects_orchestrator.fleet import fleet_rows, fleet_snapshots, render_table
from projects_orchestrator.memory import load_project_memory, search_memory
from projects_orchestrator.observability import filter_since, load_events
from projects_orchestrator.pool import map_ordered
from projects_orchestrator.registry import (
    Fleet,
    FleetConfig,
    default_fleet_config,
    discover,
    load_fleet_config,
)
from projects_orchestrator.status import clean_worktree_head, collect_status
from projects_orchestrator.upgrade import upgrade_plan


def _fleet_config(args: argparse.Namespace) -> FleetConfig:
    """Resolve discovery config from --fleet / --root / defaults."""
    if args.fleet:
        return load_fleet_config(Path(args.fleet))
    if args.root:
        return FleetConfig(roots=(Path(args.root).expanduser().resolve(),))
    return default_fleet_config()


def _discover(args: argparse.Namespace) -> Fleet:
    """Discover the fleet, surfacing warnings on stderr."""
    fleet = discover(_fleet_config(args))
    for warning in fleet.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return fleet


def _emit_json(payload: object) -> int:
    """Print a JSON document (paths become strings)."""
    print(json.dumps(payload, indent=2, default=str))
    return 0


def _cmd_projects(args: argparse.Namespace) -> int:
    """List discovered projects."""
    fleet = _discover(args)
    if args.json:
        return _emit_json([asdict(d) for d in fleet.descriptors])
    for descriptor in fleet.descriptors:
        print(f"{descriptor.name}  ({descriptor.language}, {descriptor.path})")
    if not fleet.descriptors:
        print("no projects discovered")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Show the fleet table, or one project's git health."""
    fleet = _discover(args)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        status = collect_status(descriptor)
        if args.json:
            return _emit_json(asdict(status))
        print(f"{status.project}: {status.health} on {status.branch or '?'}")
        return 0
    snapshots = fleet_snapshots(fleet)
    if args.json:
        return _emit_json([asdict(s.status) for s in snapshots])
    print(render_table(fleet_rows(snapshots)))
    return 0


def _reusable_pass(
    cached: dict[str, CheckResult] | None, task: str, head: str
) -> CheckResult | None:
    """Return the cached result that can stand in for a fresh run, if any.

    A cached result is trusted only when it is a ``pass`` recorded at the
    same clean-worktree HEAD the project is at now. Fails, skips, dirty
    trees, and unknown identities never match — they always re-run.
    """
    if not head or cached is None:
        return None
    result = cached.get(task)
    if result is not None and result.status == "pass" and result.head == head:
        return result
    return None


def _project_checks(
    descriptor: ProjectDescriptor,
    tasks: tuple[str, ...],
    cached: dict[str, CheckResult] | None,
    changed_only: bool,
) -> list[tuple[CheckResult, bool]]:
    """Run one project's gates, reusing cached passes when allowed.

    Returns:
        ``(result, reused)`` pairs in task order; ``reused`` marks results
        served from the cache instead of a fresh run.
    """
    head = clean_worktree_head(descriptor)
    reusable: dict[str, CheckResult] = {}
    if changed_only:
        reusable = {
            task: result
            for task in tasks
            if (result := _reusable_pass(cached, task, head)) is not None
        }
    to_run = tuple(task for task in tasks if task not in reusable)
    fresh = dict(zip(to_run, collect_checks(descriptor, to_run, head=head), strict=True))
    return [
        (reusable[task], True) if task in reusable else (fresh[task], False) for task in tasks
    ]


def _cmd_checks(args: argparse.Namespace) -> int:
    """Run declared gates; exit 1 when any project fails one."""
    fleet = _discover(args)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    else:
        selected = list(fleet.descriptors)

    tasks = tuple(args.task) if args.task else DEFAULT_TASKS
    cached = cache.load_results() if args.changed_only else {}
    per_project = map_ordered(
        lambda d: _project_checks(d, tasks, cached.get(d.name), args.changed_only),
        selected,
        jobs=args.jobs,
    )
    pairs = [pair for project_pairs in per_project for pair in project_pairs]
    cache.save_results([result for result, reused in pairs if not reused])
    if args.json:
        return _emit_json([{**asdict(r), "cached": reused} for r, reused in pairs])
    for result, reused in pairs:
        suffix = f" — {result.detail}" if result.detail else ""
        cached_mark = " (cached)" if reused else ""
        print(f"{result.project} {result.task}: {result.status}{cached_mark}{suffix}")
    return 1 if any(result.status == "fail" for result, _ in pairs) else 0


def _cmd_memory(args: argparse.Namespace) -> int:
    """Search every project's memory files."""
    fleet = _discover(args)
    memories = [load_project_memory(d) for d in fleet.descriptors]
    hits = search_memory(memories, " ".join(args.query))
    if args.json:
        return _emit_json([asdict(h) for h in hits])
    for hit in hits:
        location = f"{hit.file.project}/{hit.file.path.name}:{hit.line_number}"
        print(f"{location} [{hit.file.type}] {hit.file.name} — {hit.line}")
    if not hits:
        print("no matches")
    return 0


def _cmd_drift(args: argparse.Namespace) -> int:
    """Report scaffold drift; exit 1 when any project drifted."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    reports = [compute_drift(d) for d in selected]
    if args.json:
        return _emit_json([asdict(r) for r in reports])
    for report in reports:
        print(f"{report.project}: {report.summary}")
        for relpath in report.modified:
            print(f"  modified: {relpath}")
        for relpath in report.missing:
            print(f"  missing:  {relpath}")
    return 1 if any(r.status == "drift" for r in reports) else 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose contract-v1 conformance; exit 1 when any project fails."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    reports = [diagnose(d) for d in selected]
    if args.json:
        return _emit_json([asdict(r) for r in reports])
    for report in reports:
        print(f"{report.project}: {report.status}")
        for finding in report.findings:
            print(f"  [{finding.status}] {finding.check}: {finding.detail}")
    return 1 if any(r.status == "fail" for r in reports) else 0


def _cmd_audit(args: argparse.Namespace) -> int:
    """Run the composed governance audit; exit 1 when anything needs attention."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    cached = cache.load_results()
    reports = [audit_project(d, cached.get(d.name)) for d in selected]
    if args.json:
        return _emit_json([asdict(r) for r in reports])
    if args.markdown:
        print(render_markdown(reports))
    else:
        for report in reports:
            print(f"{report.project}: {report.status}")
            for finding in report.findings:
                print(f"  [{finding.severity}] {finding.category}: {finding.message}")
    return 1 if any(r.needs_attention for r in reports) else 0


def _cmd_ci(args: argparse.Namespace) -> int:
    """Probe each project's CI conclusion + open-PR count via gh; cache them.

    Exits 1 when any project's CI has failed. Results are written to the
    checks cache so the ``status`` table shows last-known CI state offline.
    """
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    checked_at = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
    statuses = map_ordered(collect_github, selected)
    cache.save_results([r for s in statuses for r in as_check_results(s, checked_at)])
    if args.json:
        return _emit_json([asdict(s) for s in statuses])
    for status in statuses:
        prs = "?" if status.open_prs is None else str(status.open_prs)
        print(f"{status.project}: CI {status.ci}, {prs} open PR(s)")
    return 1 if any(s.ci == "fail" for s in statuses) else 0


def _cmd_cloud_status(args: argparse.Namespace) -> int:
    """Probe deploy/runtime status per project (descriptor-driven); cache it.

    Exits 1 when any probed service is stopped or unhealthy. ``deploy: none``
    projects cost nothing (no subprocess, no network).
    """
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    checked_at = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
    statuses = map_ordered(collect_cloud, selected)
    cache.save_results([r for s in statuses for r in cloud_check_results(s, checked_at)])
    if args.json:
        return _emit_json([asdict(s) for s in statuses])
    for status in statuses:
        parts = [status.state]
        if status.revision:
            parts.append(status.revision)
        if status.health:
            parts.append(status.health)
        print(f"{status.project}: {status.target} — {' '.join(parts)}")
    return 1 if any(s.health == "unhealthy" or s.state == "stopped" for s in statuses) else 0


def _cmd_events(args: argparse.Namespace) -> int:
    """Show guard/usage events across the fleet's observability logs."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    reports = [load_events(d) for d in selected]
    since = args.since or ""
    if args.json:
        payload = [
            {
                "project": r.project,
                "events": [asdict(e) for e in filter_since(r.events, since)],
                "warnings": list(r.warnings),
            }
            for r in reports
        ]
        return _emit_json(payload)
    empty = True
    for report in reports:
        for warning in report.warnings:
            print(f"warning: {report.project}: {warning}", file=sys.stderr)
        for event in filter_since(report.events, since):
            empty = False
            command = f" — {event.command}" if event.command else ""
            print(f"{event.project} {event.timestamp} [{event.hook}] {event.action}{command}")
    if empty:
        print("no events recorded")
    return 0


def _cmd_upgrade_plan(args: argparse.Namespace) -> int:
    """Compare each project's scaffold version against upstream project-init.

    Exits 1 when any project is outdated. ``--apply`` dispatches each outdated
    child's own ``project-init-upgrade.yml`` workflow (never edits its tree).
    """
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    latest = latest_upstream_version(Path.cwd())
    rows = upgrade_plan(selected, latest, cache.load_results())
    applied: dict[str, str] = {}
    if args.apply:
        by_name = {d.name: d for d in selected}
        applied = {
            row.project: trigger_upgrade(by_name[row.project])
            for row in rows
            if row.status == "outdated"
        }
    if args.json:
        return _emit_json([{**asdict(r), "applied": applied.get(r.project)} for r in rows])
    for row in rows:
        line = (
            f"{row.project}: {row.status} "
            f"(scaffold {row.scaffold_version}, drift {row.drift}, PRs {row.open_prs})"
        )
        if row.project in applied:
            line += f" — upgrade {applied[row.project]}"
        print(line)
    return 1 if any(r.status == "outdated" for r in rows) else 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    """Dump the full joined fleet view."""
    fleet = _discover(args)
    snapshots = fleet_snapshots(fleet)
    if args.json:
        return _emit_json([asdict(s) for s in snapshots])
    print(render_table(fleet_rows(snapshots)))
    return 0


def _cmd_controller(args: argparse.Namespace) -> int:
    """Run the deterministic command REPL."""
    ctx = ControllerContext(config=_fleet_config(args))
    print(f"fleet: {len(ctx.fleet.descriptors)} project(s) — type 'help' for commands")
    while True:
        try:
            line = input("orchestrator> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        intent = parse_command(line)
        if intent.verb == "quit":
            return 0
        for output in dispatch(intent, ctx):
            print(output)


def _cmd_tui(args: argparse.Namespace) -> int:
    """Launch the Textual TUI (requires the ``tui`` extra)."""
    try:
        from projects_orchestrator.tui import OrchestratorApp
    except ModuleNotFoundError:
        print(
            "the TUI needs the optional dependency: uv sync --extra tui "
            "(or: pip install 'projects-orchestrator[tui]')",
            file=sys.stderr,
        )
        return 2
    OrchestratorApp(config=_fleet_config(args)).run()
    return 0


def _add_common(parser: argparse.ArgumentParser, json_flag: bool = True) -> None:
    """Attach the shared --fleet/--root (and usually --json) options."""
    parser.add_argument("--fleet", help="path to a fleet.yaml describing the fleet")
    parser.add_argument("--root", help="directory scanned one level deep for projects")
    if json_flag:
        parser.add_argument("--json", action="store_true", help="emit JSON instead of text")


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="projects-orchestrator",
        description="Cross-project orchestration layer for agentic development.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    specs: list[tuple[str, str, object, bool]] = [
        ("projects", "list discovered projects", _cmd_projects, True),
        ("status", "fleet git health (table) or one project", _cmd_status, True),
        ("checks", "run each project's declared gates", _cmd_checks, True),
        ("memory", "search all project memories", _cmd_memory, True),
        ("drift", "scaffold drift vs the recorded manifest", _cmd_drift, True),
        ("doctor", "diagnose contract-v1 conformance", _cmd_doctor, True),
        (
            "audit",
            "composed governance report (conformance + drift + memory + freshness)",
            _cmd_audit,
            True,
        ),
        ("ci", "latest CI conclusion + open-PR count per project (via gh)", _cmd_ci, True),
        (
            "cloud-status",
            "deploy/runtime status per project (descriptor deploy block)",
            _cmd_cloud_status,
            True,
        ),
        ("events", "guard/usage events from the fleet's observability logs", _cmd_events, True),
        (
            "upgrade-plan",
            "scaffold version vs upstream project-init (--apply triggers upgrades)",
            _cmd_upgrade_plan,
            True,
        ),
        ("snapshot", "full joined fleet view", _cmd_snapshot, True),
        ("controller", "interactive deterministic command REPL", _cmd_controller, False),
        ("tui", "terminal UI (requires the tui extra)", _cmd_tui, False),
    ]
    for name, help_text, handler, json_flag in specs:
        sp = sub.add_parser(name, help=help_text)
        _add_common(sp, json_flag=json_flag)
        sp.set_defaults(handler=handler)

    sub.choices["status"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["checks"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["drift"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["doctor"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["audit"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["audit"].add_argument(
        "--markdown", action="store_true", help="render the report as Markdown"
    )
    sub.choices["ci"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["cloud-status"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["events"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["events"].add_argument(
        "--since", help="only events at/after this ISO-8601 instant"
    )
    sub.choices["upgrade-plan"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["upgrade-plan"].add_argument(
        "--apply", action="store_true", help="dispatch the upgrade workflow for outdated projects"
    )
    sub.choices["checks"].add_argument(
        "--task", action="append", help="gate to run (repeatable; default: lint, test)"
    )
    sub.choices["checks"].add_argument(
        "--jobs", type=int, help="parallel projects (default: min(8, cpu count))"
    )
    sub.choices["checks"].add_argument(
        "--changed-only",
        action="store_true",
        help="skip gates whose last cached pass is at the current clean HEAD",
    )
    sub.choices["memory"].add_argument("query", nargs="+", help="text to search for")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the projects-orchestrator CLI.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    exit_code: int = args.handler(args)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
