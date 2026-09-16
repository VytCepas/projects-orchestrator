"""Whether the scheduled watch pass is still firing (#186).

Every other check here answers "is the fleet healthy?". This one answers "is
anything still asking?" — and it exists because the two are indistinguishable
from the outside. The timers install fine, the fleet reads green, and if the
timer stops firing it goes on reading green for ever: the last known state is
the last state anyone measured, and nothing ages it.

That is the same failure as an unresolved fleet exiting 0 (#204) and a cache
from a newer build reading as empty (#183) — silence presented as health. The
answer is the same too: make the absence of a signal say something.

TWO ABSENCES, KEPT APART, because only one of them is a fault:

- ``never`` — no pass has ever been recorded. On a box where the timer was
  never installed this is the honest answer, and it must not read as fresh.
- ``unknown`` — a pass was recorded but the interval it runs on was not
  declared, so staleness cannot be judged. Reporting "fresh" here would be a
  guess wearing a verdict's clothes.

Reading and writing never raise; a missing or corrupt marker degrades to
``never`` and the pass still runs.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator import persist

_STATE_DIRNAME = "projects-orchestrator"
_WATCH_FILENAME = "watch-heartbeat.json"

#: How many intervals may elapse before a pass counts as stale. Two, not one:
#: a pass that merely overlaps its own schedule — a slow fleet, a machine that
#: slept — is late, not dead, and a staleness alarm that fires on ordinary
#: lateness is the §2.11 false positive that gets the alarm switched off.
STALE_INTERVALS = 2

NEVER = "never"
FRESH = "fresh"
STALE = "stale"
UNKNOWN = "unknown"


def watch_path() -> Path:
    """Return the watch-heartbeat path, honoring ``$XDG_STATE_HOME``."""
    base = os.environ.get("XDG_STATE_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / _STATE_DIRNAME / _WATCH_FILENAME


@dataclass(frozen=True)
class WatchState:
    """When the scheduled pass last ran, and whether that is recent enough.

    Attributes:
        last_pass: ISO-8601 timestamp of the last recorded pass (``""`` = never).
        interval_seconds: The declared schedule (``0`` = not declared).
        age_seconds: Seconds since the last pass (``0`` when never).
        status: ``never`` | ``fresh`` | ``stale`` | ``unknown``.
    """

    last_pass: str = ""
    interval_seconds: int = 0
    age_seconds: int = 0
    status: str = NEVER

    @property
    def needs_attention(self) -> bool:
        """Whether an operator should be told. ``unknown`` is NOT a fault.

        A pass that ran without declaring its schedule is a configuration gap,
        not a dead monitor, and conflating the two would make the signal noisy
        on exactly the boxes that are working.
        """
        return self.status in {NEVER, STALE}


def record_pass(
    interval_seconds: int = 0, path: Path | None = None, now: _dt.datetime | None = None
) -> None:
    """Record that a scheduled pass just completed; never raises.

    Args:
        interval_seconds: The schedule this pass runs on. ``0`` leaves
            staleness unjudgeable, which :func:`read_state` reports as
            ``unknown`` rather than inventing a default.
        path: Heartbeat file override.
        now: Clock override for tests.
    """
    moment = now or _dt.datetime.now(tz=_dt.UTC)
    document = {
        "last_pass": moment.isoformat(timespec="seconds"),
        "interval_seconds": max(0, int(interval_seconds)),
    }
    with contextlib.suppress(OSError, ValueError):
        persist.locked_write(path or watch_path(), json.dumps(document, indent=2))


def read_state(path: Path | None = None, now: _dt.datetime | None = None) -> WatchState:
    """Read the heartbeat and judge it; never raises.

    Args:
        path: Heartbeat file override.
        now: Clock override for tests.

    Returns:
        A :class:`WatchState`. A missing or unreadable marker is ``never`` —
        the honest answer on a box where the timer was never installed, and the
        one that must not read as healthy.
    """
    target = path or watch_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return WatchState()
    if not isinstance(raw, dict):
        return WatchState()

    stamp = raw.get("last_pass")
    if not isinstance(stamp, str) or not stamp:
        return WatchState()
    try:
        last = _dt.datetime.fromisoformat(stamp)
    except ValueError:
        return WatchState()
    if last.tzinfo is None:
        last = last.replace(tzinfo=_dt.UTC)

    interval = raw.get("interval_seconds")
    interval = interval if isinstance(interval, int) and not isinstance(interval, bool) else 0
    interval = max(0, interval)

    moment = now or _dt.datetime.now(tz=_dt.UTC)
    # Clamped at zero: a heartbeat stamped in the future (a clock correction, a
    # machine that woke with a bad RTC) is not negative age, and letting it go
    # negative would read as freshly-run for as long as the skew lasts.
    age = max(0, int((moment - last).total_seconds()))

    if interval <= 0:
        return WatchState(stamp, 0, age, UNKNOWN)
    status = STALE if age > interval * STALE_INTERVALS else FRESH
    return WatchState(stamp, interval, age, status)


def describe(state: WatchState) -> str:
    """Render a one-line verdict for an operator."""
    if state.status == NEVER:
        return "watch has never run — the scheduled pass is not installed or has never completed"
    if state.status == UNKNOWN:
        return f"watch last ran {_age(state.age_seconds)} ago; no interval declared, so staleness cannot be judged"
    if state.status == STALE:
        return (
            f"watch last ran {_age(state.age_seconds)} ago, "
            f"more than {STALE_INTERVALS}x its {_age(state.interval_seconds)} interval — "
            "the fleet view is not being refreshed"
        )
    return f"watch last ran {_age(state.age_seconds)} ago"


def _age(seconds: int) -> str:
    """Render a duration the way an operator reads one."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"
