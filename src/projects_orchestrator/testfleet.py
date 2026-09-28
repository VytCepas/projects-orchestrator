"""Run every fleet repo's ``just test`` and report one row per repo (#316).

The cross-repo test contract gives every governed repo one entry point, ``just
test``, whose output ends with ``<suite>: N passed, M failed`` and whose exit code
is the runner's own. This runs it in each repo the fleet names and reads the LAST
such line, so a repo that runs several suites and then totals them is counted by
its total, never twice.

A repo passes when its ``just test`` exits 0 and counts no failure. A non-zero
exit, a counted failure under a zero exit, a timeout, or a runner that could not
start is a failure. A repo with no ``test`` recipe is skipped, never failed: an
ungoverned repo declares no gate, and inventing a red for it is the false
positive that gets a check switched off. A missing summary line leaves the
counts unknown (``?``) and lets the exit code decide.

Registered as the check ``fleet-test``; every line it prints carries
``[check:fleet-test]``, so an alert resolves to its registry row.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.pool import map_ordered
from projects_orchestrator.runner import RunResult, run_command

CHECK_ID = "fleet-test"
STAMP = f"[check:{CHECK_ID}]"
#: One hour per repo: a full suite is minutes, and a hung one must still end.
DEFAULT_TIMEOUT = 3600.0
_RECIPE_TIMEOUT = 60.0
_SUMMARY = re.compile(r"^([A-Za-z0-9._-]+): (\d+) passed, (\d+) failed$", re.MULTILINE)

PASS = "pass"  # noqa: S105 — a row status, not a credential
FAIL = "fail"
SKIP = "skip"

Runner = Callable[..., RunResult]


@dataclass(frozen=True)
class FleetTestRow:
    """One repo's ``just test``.

    Attributes:
        repo: Project name.
        status: ``pass`` | ``fail`` | ``skip``.
        passed: Tests passed, from the summary line; ``None`` when unknown.
        failed: Tests failed, from the summary line; ``None`` when unknown.
        exit: The run's exit code; ``None`` when it never finished.
        detail: Why a row is not a plain pass (or which suite line was read).
        duration: Wall-clock seconds.
    """

    repo: str
    status: str
    passed: int | None
    failed: int | None
    exit: int | None
    detail: str = ""
    duration: float = 0.0


def parse_summary(output: str) -> tuple[str, int, int] | None:
    """Return the last contract summary line in *output* as ``(suite, passed, failed)``.

    Args:
        output: A run's captured output.

    Returns:
        The last line matching ``<suite>: N passed, M failed``, or ``None``.
    """
    found = _SUMMARY.findall(output)
    if not found:
        return None
    suite, passed, failed = found[-1]
    return suite, int(passed), int(failed)


def _has_test_recipe(descriptor: ProjectDescriptor, run: Runner) -> tuple[bool, str]:
    """Whether the repo's justfile defines ``test``; the reason when it cannot say."""
    listed = run("just --summary", cwd=descriptor.path, timeout=_RECIPE_TIMEOUT)
    if listed.error:
        return False, listed.error
    if listed.returncode != 0:
        return False, "no justfile"
    return "test" in listed.stdout.split(), "no `test` recipe"


def _verdict(
    result: RunResult, summary: tuple[str, int, int] | None, timeout: float
) -> tuple[str, str]:
    """The row's status and detail from one finished (or failed-to-finish) run."""
    if result.timed_out:
        return FAIL, f"timed out after {timeout:.0f}s"
    if result.error:
        return FAIL, result.error
    counted = summary[2] if summary else 0
    note = "" if summary else "no summary line — counts unknown"
    if result.returncode != 0:
        return FAIL, note or f"exited {result.returncode}"
    if counted > 0:
        return FAIL, "exited 0 but counted a failure"
    return PASS, note


def run_repo(
    descriptor: ProjectDescriptor, timeout: float = DEFAULT_TIMEOUT, run: Runner = run_command
) -> FleetTestRow:
    """Run one repo's ``just test`` and read its summary line; never raises.

    Args:
        descriptor: The repo.
        timeout: Kill the run after this many seconds (a failure).
        run: The command runner (injectable for tests of the reading alone).

    Returns:
        The repo's row.
    """
    present, why = _has_test_recipe(descriptor, run)
    if not present:
        return FleetTestRow(descriptor.name, SKIP, None, None, None, why)
    result = run("just test", cwd=descriptor.path, timeout=timeout)
    summary = parse_summary(result.stdout) or parse_summary(result.stderr)
    status, detail = _verdict(result, summary, timeout)
    return FleetTestRow(
        repo=descriptor.name,
        status=status,
        passed=summary[1] if summary else None,
        failed=summary[2] if summary else None,
        exit=None if result.timed_out or result.error else result.returncode,
        detail=detail,
        duration=result.duration,
    )


def run_fleet(
    descriptors: Sequence[ProjectDescriptor], timeout: float = DEFAULT_TIMEOUT, jobs: int = 1
) -> list[FleetTestRow]:
    """Run ``just test`` in every repo, in fleet order.

    Args:
        descriptors: The repos.
        timeout: Per-repo timeout in seconds.
        jobs: Repos run at once. Serial by default: a suite already fans out
            across the machine's cores, and two at once only slows both.

    Returns:
        One row per repo, in the given order.
    """
    return map_ordered(lambda d: run_repo(d, timeout), list(descriptors), jobs=jobs)


def exit_code(rows: Sequence[FleetTestRow]) -> int:
    """0 when no repo failed, else 1."""
    return 1 if any(row.status == FAIL for row in rows) else 0


def _cell(value: int | None, unknown: str) -> str:
    return unknown if value is None else str(value)


def render(rows: Sequence[FleetTestRow]) -> list[str]:
    """The table: a header, one row per repo, and the fleet total; every line stamped.

    Args:
        rows: The fleet's rows.

    Returns:
        The lines to print.
    """
    width = max([len("repo"), *(len(row.repo) for row in rows)])
    lines = [f"{STAMP} {'repo':<{width}}  passed  failed  exit  status"]
    for row in rows:
        unknown = "-" if row.status == SKIP else "?"
        exit_cell = "-" if row.exit is None else str(row.exit)
        line = (
            f"{STAMP} {row.repo:<{width}}  {_cell(row.passed, unknown):>6}  "
            f"{_cell(row.failed, unknown):>6}  {exit_cell:>4}  {row.status}"
        )
        lines.append(f"{line}  {row.detail}" if row.detail else line)
    counts = {status: sum(row.status == status for row in rows) for status in (PASS, FAIL, SKIP)}
    passed = sum(row.passed or 0 for row in rows)
    failed = sum(row.failed or 0 for row in rows)
    lines.append(
        f"{STAMP} {CHECK_ID}: {len(rows)} repos, {counts[PASS]} pass, {counts[FAIL]} fail, "
        f"{counts[SKIP]} skip; {passed} passed, {failed} failed"
    )
    return lines
