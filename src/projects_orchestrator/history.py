"""Append-only check history — trends the last-known cache can't show.

The checks cache holds only the *latest* result per ``(project, task)``; it
answers "is it green now?" but not "has it been flaky?". This records every
fresh check outcome to a bounded, append-only log under ``$XDG_STATE_HOME`` so
the ``history`` command can show a per-task trend (a compact sparkline) and the
pass/fail transitions over time.

Like the rest of the engine it never raises: recording degrades silently, and a
missing or corrupt log reads as empty history.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.checks import CheckResult

_STATE_DIRNAME = "projects-orchestrator"
_HISTORY_FILENAME = "history.jsonl"

# Hard cap so the log can't grow without bound; the oldest entries roll off.
MAX_ENTRIES = 5000

DEFAULT_TREND_WIDTH = 10

# Only pass/fail outcomes are trend-worthy; a skip means "no gate declared".
_RECORDED = ("pass", "fail")

_SPARK = {"pass": "+", "fail": "x"}


@dataclass(frozen=True)
class HistoryEntry:
    """One recorded check outcome at a point in time.

    Attributes:
        project: Project name.
        task: Gate name (lint, test, …).
        status: ``pass`` | ``fail``.
        checked_at: UTC ISO timestamp of the run.
    """

    project: str
    task: str
    status: str
    checked_at: str


def history_path() -> Path:
    """Return the history log path, honoring ``$XDG_STATE_HOME``."""
    base = os.environ.get("XDG_STATE_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / _STATE_DIRNAME / _HISTORY_FILENAME


def record(results: list[CheckResult], path: Path | None = None) -> None:
    """Append trend-worthy results to the bounded history log; never raises."""
    fresh = [r for r in results if r.status in _RECORDED]
    if not fresh:
        return
    path = path or history_path()
    kept = load_history(path)
    kept.extend(HistoryEntry(r.project, r.task, r.status, r.checked_at) for r in fresh)
    kept = kept[-MAX_ENTRIES:]
    body = "\n".join(
        json.dumps(
            {"project": e.project, "task": e.task, "status": e.status, "checked_at": e.checked_at}
        )
        for e in kept
    )
    _atomic_write(path, body + "\n")


def load_history(path: Path | None = None) -> list[HistoryEntry]:
    """Read the history log (chronological); ``[]`` on any problem, bad lines skipped."""
    path = path or history_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries: list[HistoryEntry] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        entry = _parse_line(line)
        if entry is not None:
            entries.append(entry)
    return entries


def _parse_line(line: str) -> HistoryEntry | None:
    """Parse one JSONL history line; ``None`` when malformed."""
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    fields = {key: raw.get(key) for key in ("project", "task", "status", "checked_at")}
    if not all(isinstance(value, str) for value in fields.values()):
        return None
    return HistoryEntry(**fields)  # type: ignore[arg-type]


def project_history(entries: list[HistoryEntry], project: str) -> dict[str, list[HistoryEntry]]:
    """Group one project's entries by task, preserving chronological order (pure)."""
    grouped: dict[str, list[HistoryEntry]] = {}
    for entry in entries:
        if entry.project == project:
            grouped.setdefault(entry.task, []).append(entry)
    return grouped


def sparkline(entries: list[HistoryEntry], width: int = DEFAULT_TREND_WIDTH) -> str:
    """Render the last ``width`` outcomes as a compact trend, newest on the right."""
    recent = entries[-width:]
    return "".join(_SPARK.get(entry.status, "?") for entry in recent)


# The gate whose trend represents the project in the fleet table, most-preferred
# first; falls back to any recorded task.
_PRIMARY_TASKS = ("test", "lint")


def primary_trend(
    entries: list[HistoryEntry], project: str, width: int = DEFAULT_TREND_WIDTH
) -> str:
    """The sparkline for a project's primary gate; ``""`` when it has no history.

    Prefers ``test`` then ``lint``, else the first recorded task — one compact
    trend to stand in for the project in the fleet overview.
    """
    grouped = project_history(entries, project)
    for task in (*_PRIMARY_TASKS, *sorted(grouped)):
        if task in grouped:
            return sparkline(grouped[task], width)
    return ""


def transitions(entries: list[HistoryEntry]) -> list[HistoryEntry]:
    """Return the entries where status changed from the previous run (pure)."""
    changes: list[HistoryEntry] = []
    previous: str | None = None
    for entry in entries:
        if entry.status != previous:
            changes.append(entry)
            previous = entry.status
    return changes


def _atomic_write(path: Path, text: str) -> None:
    """Write via temp file + replace so an interrupt can't truncate the log."""
    with contextlib.suppress(OSError, ValueError):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        tmp_path = Path(tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            tmp_path.replace(path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise
