"""One hardened write path for every file the orchestrator persists.

The atomic-write pattern existed as four divergent copies — ``cache``,
``digest``, ``history``, ``runs`` — plus two writers in ``supervisor`` that did
not use it at all, and the copies had already drifted in the way duplication
always drifts: **only ``cache`` took a lock, and not one of them fsynced.**

What that cost, concretely:

- Two concurrent writers to an unlocked load-modify-write lose entries. Both
  read, both rewrite the whole file, and the last one silently discards the
  other's work. ``cache`` had solved this; nothing else had.
- ``os.replace`` is atomic with respect to *readers*, not with respect to
  *power loss*. Without an fsync the rename can reach disk before the bytes do,
  so a crash leaves a zero-length file where a complete one is expected — and
  every loader here treats an unreadable file as empty. A wiped history is
  indistinguishable from a fresh one.
- ``supervisor``'s plain ``write_text`` could be interrupted mid-flush, leaving
  partial JSON in the file that says whether a process is alive.

Everything here is best-effort and **never raises**: this module sits under the
engine's never-raise contract (ADR-003), so a failure to lock degrades to an
unlocked write rather than losing the write entirely, and callers keep their own
``contextlib.suppress`` around the call.

``fcntl`` is imported defensively because it is POSIX-only; on a platform
without it the lock degrades to a no-op and the atomic replace still holds.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

try:  # pragma: no cover - POSIX-only, present on every supported platform
    import fcntl
except ImportError:  # pragma: no cover
    # expected: no fcntl off POSIX, so writes degrade to unlocked
    fcntl = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

#: Suffix for the sidecar lock file. A SEPARATE file, never the payload itself:
#: locking the payload and then replacing it would drop the lock with the inode
#: it was taken on, so the next writer locks a file nobody is reading.
LOCK_SUFFIX = ".lock"


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock across a load-modify-write; best-effort.

    Two overlapping writers otherwise both read, both rewrite the whole file,
    and the last writer silently discards the other's work. The lock serializes
    them.

    Acquisition never raises. If it fails the body still runs, unlocked — a
    racy write beats a lost one, and the alternative is an engine that raises.

    Args:
        path: The payload path being guarded (the lock is a sidecar beside it).
    """
    handle = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = (path.parent / f"{path.name}{LOCK_SUFFIX}").open("w", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except (OSError, ValueError) as exc:
        _log.debug("cannot lock %s, so this write runs unlocked: %r", path, exc)
    try:
        yield
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):  # expected: the flock is released either way
                handle.close()  # closing the descriptor releases the flock


def atomic_write(path: Path, text: str) -> None:
    """Write via temp file + fsync + ``os.replace``; raises only on OSError.

    THE FSYNC IS THE HALF THAT WAS MISSING EVERYWHERE. ``os.replace`` makes the
    swap atomic for a concurrent *reader*, so no one ever sees a half-written
    file — but it says nothing about what reaches the platter. Without the
    fsync the rename can be durable while the bytes are not, and a crash leaves
    a zero-length file. Every loader in this package reads an unreadable file as
    empty, so that is a silent wipe rather than a visible error.

    The directory fsync is the second half of the same argument: the *rename*
    also needs to be durable, or a crash can leave the old name pointing at
    nothing. It is suppressed separately because some filesystems refuse to
    fsync a directory handle, and failing the whole write over that would be a
    regression.

    Args:
        path: Destination path.
        text: Full file contents.

    Raises:
        OSError: If the write or replace fails. Callers suppress it; this
            function does not, so a caller that wants to know can ask.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    except OSError:
        with contextlib.suppress(OSError):  # expected: the original error is re-raised below
            tmp_path.unlink()
        raise
    # expected: a directory fsync is best-effort, and some filesystems refuse it
    with contextlib.suppress(OSError):
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def locked_write(path: Path, text: str) -> None:
    """Take the lock and write atomically — the whole-file case.

    For a read-modify-write, hold :func:`locked` across the *read* as well and
    call :func:`atomic_write` inside it; taking the lock only around the write
    leaves exactly the race it is meant to close.

    Args:
        path: Destination path.
        text: Full file contents.
    """
    with locked(path):
        atomic_write(path, text)
