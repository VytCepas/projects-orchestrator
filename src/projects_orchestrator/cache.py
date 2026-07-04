"""Persistent memory of the last known check results.

The fleet view must answer "did this project pass, and how fresh is that
answer?" without re-running every gate. Results are stored per
``(project, task)`` in a JSON file under the user cache directory
(``$XDG_CACHE_HOME`` aware). Loading and saving never raise; a corrupt or
missing cache is simply empty.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from projects_orchestrator.checks import CheckResult

_CACHE_DIRNAME = "projects-orchestrator"
_CACHE_FILENAME = "checks.json"


def cache_path() -> Path:
    """Return the checks-cache file path, honoring ``$XDG_CACHE_HOME``."""
    base = os.environ.get("XDG_CACHE_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / _CACHE_DIRNAME / _CACHE_FILENAME


def load_results(path: Path | None = None) -> dict[str, dict[str, CheckResult]]:
    """Load cached check results; never raises.

    Args:
        path: Cache file override (defaults to :func:`cache_path`).

    Returns:
        ``{project: {task: CheckResult}}``; empty on any problem.
    """
    path = path or cache_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}

    results: dict[str, dict[str, CheckResult]] = {}
    for project, tasks in raw.items():
        if not isinstance(tasks, dict):
            continue
        for task, entry in tasks.items():
            if not isinstance(entry, dict):
                continue
            try:
                result = CheckResult(**{k: entry[k] for k in _RESULT_FIELDS if k in entry})
            except TypeError:
                continue
            results.setdefault(str(project), {})[str(task)] = result
    return results


_RESULT_FIELDS = ("project", "task", "status", "detail", "duration", "checked_at", "head")


def save_results(
    new_results: list[CheckResult], path: Path | None = None
) -> dict[str, dict[str, CheckResult]]:
    """Merge new results into the cache and write it back; never raises.

    Args:
        new_results: Fresh check results to record.
        path: Cache file override (defaults to :func:`cache_path`).

    Returns:
        The merged ``{project: {task: CheckResult}}`` map (even when the
        write itself failed — the caller still gets a coherent view).
    """
    path = path or cache_path()
    merged = load_results(path)
    for result in new_results:
        merged.setdefault(result.project, {})[result.task] = result

    serializable = {
        project: {task: asdict(result) for task, result in tasks.items()}
        for project, tasks in merged.items()
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass
    return merged
