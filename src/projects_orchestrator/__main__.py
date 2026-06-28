"""CLI entry point for `projects-orchestrator`."""

from __future__ import annotations

import argparse

from projects_orchestrator import __version__


def main(argv: list[str] | None = None) -> int:
    """Run the projects-orchestrator CLI.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="projects-orchestrator",
        description="Cross-project orchestration layer for agentic development.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args(argv)
    print("projects-orchestrator — orchestration layer (scaffold).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
