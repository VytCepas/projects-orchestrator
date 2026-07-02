"""CLI entry point for `projects-orchestrator`.

One command surface over the fleet engine: discover projects, show git
health, run their declared gates, search their memories, or drive it all
interactively (``controller`` REPL / ``tui``). Every data command takes
``--json`` so external monitors can consume the same truth the tables show.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from projects_orchestrator import __version__, cache
from projects_orchestrator.checks import DEFAULT_TASKS, run_check
from projects_orchestrator.controller import ControllerContext, dispatch, parse_command
from projects_orchestrator.drift import compute_drift
from projects_orchestrator.fleet import fleet_rows, fleet_snapshots, render_table
from projects_orchestrator.memory import load_project_memory, search_memory
from projects_orchestrator.registry import (
    Fleet,
    FleetConfig,
    default_fleet_config,
    discover,
    load_fleet_config,
)
from projects_orchestrator.status import collect_status


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
    results = [run_check(d, task) for d in selected for task in tasks]
    cache.save_results(results)
    if args.json:
        return _emit_json([asdict(r) for r in results])
    for result in results:
        suffix = f" — {result.detail}" if result.detail else ""
        print(f"{result.project} {result.task}: {result.status}{suffix}")
    return 1 if any(r.status == "fail" for r in results) else 0


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
    sub.choices["checks"].add_argument(
        "--task", action="append", help="gate to run (repeatable; default: lint, test)"
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
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
