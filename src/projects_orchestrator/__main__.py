"""CLI entry point for `projects-orchestrator`."""

from __future__ import annotations

import argparse
from pathlib import Path

from projects_orchestrator import __version__
from projects_orchestrator.registry import Registry


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser and its subcommands.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="projects-orchestrator",
        description="Cross-project orchestration layer for agentic development.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    discover = sub.add_parser("discover", help="list project-init projects under a root")
    discover.add_argument("root", type=Path, help="directory to scan recursively")
    discover.set_defaults(func=_cmd_discover)

    show = sub.add_parser("show", help="show one project's descriptor in detail")
    show.add_argument("name", help="project name to show")
    show.add_argument("root", type=Path, help="directory to scan recursively")
    show.set_defaults(func=_cmd_show)

    return parser


def _cmd_discover(args: argparse.Namespace) -> int:
    """Print a one-line-per-project table of discovered projects.

    Args:
        args: Parsed arguments carrying ``root``.

    Returns:
        Process exit code (0 always; an empty fleet is not an error).
    """
    registry = Registry.discover(args.root)
    if not registry.projects:
        print(f"No project-init projects found under {args.root}")
        return 0
    width = max(len(d.name) for d in registry)
    for d in registry:
        index = "index" if d.memory.has_index else "-"
        print(
            f"{d.name:<{width}}  {d.language:<8}  {d.delivery:<9}  "
            f"t{d.memory.tier}  v{d.contract_version}  {index}"
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    """Print the full descriptor for a single named project.

    Args:
        args: Parsed arguments carrying ``name`` and ``root``.

    Returns:
        Process exit code (0 when found, 1 when the name is unknown).
    """
    descriptor = Registry.discover(args.root).get(args.name)
    if descriptor is None:
        print(f"No project named {args.name!r} under {args.root}")
        return 1
    print(descriptor.summary())
    print(f"  root:         {descriptor.root}")
    print(f"  description:  {descriptor.description}")
    print(f"  project-init: {descriptor.project_init_version}")
    print(f"  memory:       tier {descriptor.memory.tier} ({descriptor.memory.stack})")
    print(f"  memory path:  {descriptor.memory.path}")
    print(f"  memory index: {'present' if descriptor.memory.has_index else 'absent'}")
    print(f"  mcps:         {', '.join(descriptor.mcps) or 'none'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the projects-orchestrator CLI.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    args = _build_parser().parse_args(argv)
    if getattr(args, "command", None) is None:
        print("projects-orchestrator — orchestration layer.")
        print("Run `projects-orchestrator discover <root>` to index projects.")
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
