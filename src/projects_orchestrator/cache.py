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
import tempfile
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

try:  # POSIX advisory locking; absent on non-POSIX, where we degrade to no lock.
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None  # type: ignore[assignment]

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


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock across the load-merge-write; best-effort.

    Two overlapping writers (a cron ``ci`` while a ``checks`` run finishes, the
    TUI open while the CLI runs) otherwise both read, both rewrite the whole
    file, and the last writer silently discards the other's fresh results. The
    lock serializes them. Acquisition never raises — if it fails, the save
    proceeds unlocked rather than being lost.
    """
    handle = None
    with contextlib.suppress(OSError, ValueError):
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = (path.parent / f"{path.name}.lock").open("w", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()  # closing the descriptor releases the flock


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + ``os.replace`` so an interrupt can't truncate.

    A plain ``write_text`` interrupted mid-flush leaves partial JSON, and the
    next load reads it as empty — silently wiping the whole check history.
    ``os.replace`` swaps the file in atomically once it is fully written.
    """
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
