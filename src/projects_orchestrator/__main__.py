"""CLI entry point for `projects-orchestrator`.

The command surface is split by audience (ADR-004): ``run``/``test`` are the
agent control path — an agent drives the fleet through these over Bash — while
``status``/``json`` are the read surface and ``serve``/``html`` render the
read-only dashboard.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from projects_orchestrator import __version__
from projects_orchestrator.actions import execute
from projects_orchestrator.discovery import discover
from projects_orchestrator.render import render_html, render_json, render_tui
from projects_orchestrator.web import serve


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser with the control and read subcommands."""
    parser = argparse.ArgumentParser(
        prog="projects-orchestrator",
        description="Cross-project orchestration layer for agentic development.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.set_defaults(command=None)

    sub = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("run", "run a project's inferred run command to completion"),
        ("test", "run a project's inferred test/lint command to completion"),
        ("status", "print a terminal overview of project-init projects"),
        ("json", "print discovered projects as JSON"),
        ("serve", "run the read-only web dashboard (open it in VS Code)"),
        ("html", "write a self-contained HTML dashboard"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument(
            "--root",
            type=Path,
            default=Path.home() / "projects",
            help="directory to scan for project-init projects (default: ~/projects)",
        )
        if name in ("run", "test"):
            cmd.add_argument("project", help="name of the discovered project to act on")
        if name == "html":
            cmd.add_argument(
                "-o",
                "--output",
                type=Path,
                default=Path("orchestrator.html"),
                help="where to write the dashboard (default: ./orchestrator.html)",
            )
        if name == "serve":
            cmd.add_argument(
                "--host", default="127.0.0.1", help="interface to bind (default: loopback)"
            )
            cmd.add_argument(
                "--port", type=int, default=8765, help="port to listen on (default: 8765)"
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the projects-orchestrator CLI.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code (a project command's own code for ``run``/``test``).
    """
    args = _build_parser().parse_args(argv)

    if args.command is None:
        print("projects-orchestrator — agent control layer for your projects.")
        print("Try: projects-orchestrator status   |   projects-orchestrator run <project>")
        return 0

    if args.command in ("run", "test"):
        return execute(args.root, args.project, args.command)

    if args.command == "serve":
        serve(args.root, args.host, args.port)
        return 0

    projects = discover(args.root)
    if args.command == "status":
        print(render_tui(projects))
    elif args.command == "json":
        print(render_json(projects))
    elif args.command == "html":
        args.output.write_text(render_html(projects), encoding="utf-8")
        print(f"Wrote {len(projects)} project(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
