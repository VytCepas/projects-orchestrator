"""Persistent memory of the last known check results.

The fleet view must answer "did this project pass, and how fresh is that
answer?" without re-running every gate. Results are stored per
``(project, task)`` in a JSON file under the user cache directory
(``$XDG_CACHE_HOME`` aware). Loading and saving never raise; a corrupt or
missing cache is simply empty.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict
from pathlib import Path

try:  # POSIX advisory locking; absent on non-POSIX, where we degrade to no lock.
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None  # type: ignore[assignment]

from projects_orchestrator import persist
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
            result = _coerce_result(entry)
            if result is None:
                continue
            results.setdefault(str(project), {})[str(task)] = result
    return results


_STR_FIELDS = ("project", "task", "status", "detail", "checked_at", "head")
_FLOAT_FIELDS = ("duration",)


def _coerce_result(entry: dict[str, object]) -> CheckResult | None:
    """Build a :class:`CheckResult` from a cache entry; ``None`` if malformed.

    Field *types* are validated, not just presence: a valid-JSON but
    type-corrupt entry (e.g. ``status`` as an int from a hand edit or bit
    flip) is dropped rather than loaded, so it cannot crash the renderers
    downstream — a corrupt cache reads as empty (ADR-003).
    """
    values: dict[str, object] = {}
    for key in _STR_FIELDS:
        if key in entry:
            if not isinstance(entry[key], str):
                return None
            values[key] = entry[key]
    for key in _FLOAT_FIELDS:
        if key in entry:
            if not isinstance(entry[key], (int, float)) or isinstance(entry[key], bool):
                return None
            values[key] = float(entry[key])  # type: ignore[arg-type]
    try:
        return CheckResult(**values)  # type: ignore[arg-type]
    except TypeError:
        return None


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
    with _locked(path):
        merged = load_results(path)
        for result in new_results:
            merged.setdefault(result.project, {})[result.task] = result

        serializable = {
            project: {task: asdict(result) for task, result in tasks.items()}
            for project, tasks in merged.items()
        }
        with contextlib.suppress(OSError, ValueError):
            _atomic_write(path, json.dumps(serializable, indent=2))
    return merged


def drop_result(project: str, task: str, path: Path | None = None) -> None:
    """Remove one ``(project, task)`` entry from the cache; never raises.

    The merge-only :func:`save_results` can update an entry but not retire
    one, and some truths are *absences*: a project whose supervised process
    is gone should carry no ``process`` result at all, not a stale last-known
    one. A missing entry is a no-op.

    Args:
        project: The project whose entry to remove.
        task: The task to remove.
        path: Cache file override (defaults to :func:`cache_path`).
    """
    path = path or cache_path()
    with _locked(path):
        merged = load_results(path)
        if merged.get(project, {}).pop(task, None) is None:
            return
        if not merged[project]:
            del merged[project]
        serializable = {
            proj: {t: asdict(result) for t, result in tasks.items()}
            for proj, tasks in merged.items()
        }
        with contextlib.suppress(OSError, ValueError):
            _atomic_write(path, json.dumps(serializable, indent=2))


#: Both helpers now delegate to :mod:`persist` (#181). They were the ONLY copy
#: that locked and they still did not fsync, so the shared version is strictly
#: stronger; the private names are kept so callers here read unchanged.
_locked = persist.locked
_atomic_write = persist.atomic_write
