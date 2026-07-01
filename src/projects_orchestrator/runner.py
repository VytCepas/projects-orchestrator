"""Run tasks inside a project's directory and capture the result.

This is the "act" half of the orchestrator: given a project descriptor and a
command, execute it in that project's root and return a structured result. The
runner never raises for an ordinary failure (non-zero exit, missing binary,
timeout) — those are reported as a non-``ok`` :class:`RunResult` so a fleet-wide
sweep can continue past a failing project.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass

from projects_orchestrator.descriptor import ProjectDescriptor

DEFAULT_TIMEOUT = 600.0


@dataclass(frozen=True)
class RunResult:
    """The outcome of running a command in one project.

    Attributes:
        project: Project name the command ran in.
        command: The command that was run (joined for display).
        exit_code: Process exit code (124 on timeout, 127 on missing binary).
        output: Combined stdout+stderr text.
    """

    project: str
    command: str
    exit_code: int
    output: str

    @property
    def ok(self) -> bool:
        """Return whether the command succeeded.

        Returns:
            ``True`` when the exit code is 0.
        """
        return self.exit_code == 0


def run_in_project(
    descriptor: ProjectDescriptor,
    command: list[str],
    *,
    timeout: float | None = DEFAULT_TIMEOUT,
) -> RunResult:
    """Run a command in a single project's root directory.

    Args:
        descriptor: The project to run in.
        command: Command argument vector (not run through a shell).
        timeout: Seconds before the command is killed; ``None`` disables it.

    Returns:
        A :class:`RunResult`; failures (non-zero, timeout, missing binary) are
        reported rather than raised.
    """
    joined = " ".join(command)
    try:
        proc = subprocess.run(
            command,
            cwd=descriptor.root,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return RunResult(descriptor.name, joined, 124, f"timed out after {timeout}s")
    except (OSError, ValueError) as exc:
        return RunResult(descriptor.name, joined, 127, str(exc))
    return RunResult(descriptor.name, joined, proc.returncode, proc.stdout + proc.stderr)


def run_command_line(
    descriptor: ProjectDescriptor,
    command_line: str,
    *,
    timeout: float | None = DEFAULT_TIMEOUT,
) -> RunResult:
    """Run a shell-style command string in a project (split with ``shlex``).

    Args:
        descriptor: The project to run in.
        command_line: A command string, e.g. ``"just lint"``.
        timeout: Seconds before the command is killed; ``None`` disables it.

    Returns:
        A :class:`RunResult`.
    """
    return run_in_project(descriptor, shlex.split(command_line), timeout=timeout)


def run_across(
    descriptors: Iterable[ProjectDescriptor],
    command_line: str,
    *,
    timeout: float | None = DEFAULT_TIMEOUT,
) -> list[RunResult]:
    """Run the same command string across many projects.

    Args:
        descriptors: Projects to run in, in order.
        command_line: A command string, e.g. ``"just lint"``.
        timeout: Per-project timeout in seconds; ``None`` disables it.

    Returns:
        One :class:`RunResult` per project, in the given order.
    """
    return [run_command_line(d, command_line, timeout=timeout) for d in descriptors]
