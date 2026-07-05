"""Run a project's inferred command to completion from the CLI.

This is the agent-facing control path (ADR-004): an agent invokes
``projects-orchestrator run|test <project>`` through the Bash tool it already
has, and the orchestrator resolves *where* the project lives (discovery) and
*what* its run/test command is (runcommands) so the agent doesn't have to.

The one-shot CLI keeps no long-lived supervisor, so the command runs in the
**foreground**: it inherits the terminal, streams its own output, and its exit
code becomes the CLI's — exactly what an agent reads back over Bash. A
``MemAvailable`` pre-flight check fails fast before spawning so a fleet command
can never be the launch that triggers the OOM killer.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from projects_orchestrator.discovery import discover
from projects_orchestrator.guard import admit
from projects_orchestrator.runcommands import plan_for

# Exit codes distinct from a command's own, so an agent can tell "the gate
# failed" (the command's code) from "we could not run it" (these).
NOT_FOUND = 2
REFUSED = 3


def execute(root: Path, name: str, op: str, *, out=sys.stderr) -> int:
    """Run project ``name``'s ``run`` or ``test`` command to completion.

    Args:
        root: Directory to scan for project-init projects.
        name: The project's discovered name.
        op: ``"run"`` or ``"test"`` — which inferred command to execute.
        out: Stream for orchestrator-level diagnostics (not the command's own
            output, which streams to the inherited stdio).

    Returns:
        The command's exit code, or :data:`NOT_FOUND` / :data:`REFUSED` when the
        project/command is missing or admission control declines to launch.
    """
    project = next((p for p in discover(root) if p.name == name), None)
    if project is None:
        print(f"unknown project: {name}", file=out)
        return NOT_FOUND

    plan = plan_for(project.path)
    command = plan.run if op == "run" else plan.test
    if not command:
        print(f"no {op} command detected for {name}", file=out)
        return NOT_FOUND

    verdict = admit(0)
    if not verdict.ok:
        print(f"refusing to launch: {verdict.reason}", file=out)
        return REFUSED

    print(f"→ {name}: {command}", file=out)
    completed = subprocess.run(shlex.split(command), cwd=project.path, check=False)
    return completed.returncode
