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
from dataclasses import asdict, dataclass
from pathlib import Path

try:  # POSIX advisory locking; absent on non-POSIX, where we degrade to no lock.
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None  # type: ignore[assignment]

from projects_orchestrator.checks import CheckResult

_CACHE_DIRNAME = "projects-orchestrator"
_CACHE_FILENAME = "checks.json"

#: Envelope format this build writes and understands.
SCHEMA_VERSION = 1

#: Version recorded for a cache written before the envelope existed. Such a file
#: is a bare ``{project: {task: entry}}`` map with no version key at all.
LEGACY_SCHEMA_VERSION = 0

#: The version lives BESIDE the projects, not wrapping them, and that is a
#: compatibility decision rather than a style one. A wrapper (`{"schema_version":
#: 1, "results": {...}}`) is invisible to every build released before it: the old
#: reader walks the top level looking for project maps, finds only `results`,
#: coerces nothing, and reads the cache as EMPTY — then its next save writes a
#: bare map over the file, which is precisely the skew-into-data-loss this change
#: exists to prevent, merely relocated to the rollback path (raised in review on
#: #242).
#:
#: As a sibling the old reader skips the key harmlessly (its value is an int, not
#: a dict) and reads every project correctly, so a pre-envelope build MERGES
#: rather than clobbers. It drops the version key when it writes, which this
#: build reads back as `legacy` and re-stamps on the next save. Neither direction
#: loses a result.
#:
#: The name is dunder-wrapped because the top level is otherwise a project
#: namespace: a project would have to be named `__schema_version__` to collide.
_VERSION_KEY = "__schema_version__"

#: Envelope verdicts. ``future`` is the one that matters: it means the file was
#: written by a NEWER build, which is version skew, not corruption — and the two
#: were previously indistinguishable because both read as an empty cache.
OK = "ok"
LEGACY = "legacy"
FUTURE = "future"
UNREADABLE = "unreadable"


@dataclass(frozen=True)
class CacheState:
    """A cache read, with the envelope verdict that produced it.

    Attributes:
        results: ``{project: {task: CheckResult}}``; empty when unusable.
        schema_version: The version found on disk (``0`` = pre-envelope).
        status: One of ``ok`` · ``legacy`` · ``future`` · ``unreadable``.
    """

    results: dict[str, dict[str, CheckResult]]
    schema_version: int
    status: str

    @property
    def is_skew(self) -> bool:
        """Whether the cache is unusable because it is NEWER, not broken."""
        return self.status == FUTURE


def cache_path() -> Path:
    """Return the checks-cache file path, honoring ``$XDG_CACHE_HOME``."""
    base = os.environ.get("XDG_CACHE_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / _CACHE_DIRNAME / _CACHE_FILENAME


def read_cache(path: Path | None = None) -> CacheState:
    """Load the cache WITH its envelope verdict; never raises.

    The cache carried no format version, so a renamed or removed
    :class:`CheckResult` field made every entry fail coercion and the whole file
    read as empty — a silent full-cache wipe indistinguishable from "never
    probed" and from genuine corruption. Worse, ``save_results`` then merged
    into that empty view and wrote it back, DESTROYING a cache written by a
    newer build (#183).

    Args:
        path: Cache file override (defaults to :func:`cache_path`).

    Returns:
        A :class:`CacheState`. ``results`` is empty for ``future`` and
        ``unreadable``; the ``status`` is what tells the two apart.
    """
    path = path or cache_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CacheState({}, LEGACY_SCHEMA_VERSION, UNREADABLE)
    if not isinstance(raw, dict):
        return CacheState({}, LEGACY_SCHEMA_VERSION, UNREADABLE)

    version = raw.get(_VERSION_KEY)
    payload = {k: v for k, v in raw.items() if k != _VERSION_KEY}
    if isinstance(version, int) and not isinstance(version, bool):
        if version > SCHEMA_VERSION:
            # Refuse to read it AND refuse to call it corrupt. A newer build
            # wrote this; the right move is to leave it alone, not to silently
            # replace it with whatever this build happens to know.
            return CacheState({}, version, FUTURE)
        status = OK
    else:
        # No version key: written by a pre-envelope build. Read it and re-stamp
        # on the next save.
        version, status = LEGACY_SCHEMA_VERSION, LEGACY
    return CacheState(_coerce_results(payload), version, status)


def _coerce_results(payload: dict[str, object]) -> dict[str, dict[str, CheckResult]]:
    """Build the result map from an envelope payload, skipping malformed entries."""
    results: dict[str, dict[str, CheckResult]] = {}
    for project, tasks in payload.items():
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


def load_results(path: Path | None = None) -> dict[str, dict[str, CheckResult]]:
    """Load cached check results; never raises.

    Args:
        path: Cache file override (defaults to :func:`cache_path`).

    Returns:
        ``{project: {task: CheckResult}}``; empty on any problem.
    """
    return read_cache(path).results


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
        state = read_cache(path)
        merged = state.results
        for result in new_results:
            merged.setdefault(result.project, {})[result.task] = result

        if state.is_skew:
            # A NEWER BUILD WROTE THIS FILE, and it read as empty here. Merging
            # into that empty view and writing it back is how version skew
            # became data loss: the newer cache would be replaced by whatever
            # this build happened to have in hand. The caller still gets a
            # coherent in-memory view; the file is left for the build that owns
            # it (#183).
            return merged

        with contextlib.suppress(OSError, ValueError):
            _atomic_write(path, _serialize(merged))
    return merged


def _serialize(results: dict[str, dict[str, CheckResult]]) -> str:
    """Render the cache document — ONE writer, so the envelope cannot drift.

    `drop_result` previously built its own payload and omitted the version key,
    which silently demoted the file back to the pre-envelope shape between a
    drop and the next save (raised in review on #242).
    """
    document: dict[str, object] = {
        project: {task: asdict(result) for task, result in tasks.items()}
        for project, tasks in results.items()
    }
    document[_VERSION_KEY] = SCHEMA_VERSION
    return json.dumps(document, indent=2)


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
        state = read_cache(path)
        if state.is_skew:
            # Same refusal as `save_results`: a newer build owns this file, and
            # retiring one entry is not a reason to rewrite it with this build's
            # understanding (raised in review on #242).
            return
        merged = state.results
        if merged.get(project, {}).pop(task, None) is None:
            return
        if not merged[project]:
            del merged[project]
        with contextlib.suppress(OSError, ValueError):
            _atomic_write(path, _serialize(merged))


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
