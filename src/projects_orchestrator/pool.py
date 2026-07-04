"""Bounded thread-pool fan-out for per-project engine work.

Everything the engine does per project is subprocess- or filesystem-bound
(git probes, gate runs, `gh`/platform CLIs), so a modest thread pool turns
fleet-wide wall-clock from *sum of projects* into *slowest project*. One
helper keeps the semantics uniform everywhere: input order is preserved,
a single item (or ``jobs=1``) short-circuits to a plain loop, and worker
count is bounded so a large fleet cannot fork-bomb the machine.

Engine callables passed here follow the never-raise rule (ADR-003), so no
exception-translation layer is added — a raise would be an engine bug and
should surface, exactly as it would have in the serial loop.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

_MAX_DEFAULT_JOBS = 8

T = TypeVar("T")
R = TypeVar("R")


def default_jobs() -> int:
    """Return the default worker count: ``min(8, cpu count)``, at least 1."""
    return max(1, min(_MAX_DEFAULT_JOBS, os.cpu_count() or 1))


def map_ordered(fn: Callable[[T], R], items: Sequence[T], jobs: int | None = None) -> list[R]:
    """Apply ``fn`` to every item concurrently, preserving input order.

    Args:
        fn: Per-item worker (engine callables never raise).
        items: Items to process.
        jobs: Worker-count override; ``None`` uses :func:`default_jobs`,
            values below 2 (or a single item) run serially.

    Returns:
        One result per item, in the items' order.
    """
    workers = default_jobs() if jobs is None else max(1, jobs)
    if len(items) <= 1 or workers == 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as executor:
        return list(executor.map(fn, items))
