"""The watch-timer liveness backstop (#186).

Every other check answers "is the fleet healthy?". This one answers "is anything
still asking?" — and the two are indistinguishable from outside, which is the
whole defect: if the timer stops firing the fleet reads green for ever, because
the last known state is the last state anyone measured and nothing ages it.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from projects_orchestrator import watchdog

_NOW = _dt.datetime(2026, 9, 16, 12, 0, 0, tzinfo=_dt.UTC)
_HOUR = 3600


def _at(path: Path, *, ago: int, interval: int) -> None:
    watchdog.record_pass(interval, path, now=_NOW - _dt.timedelta(seconds=ago))


def test_a_box_where_watch_never_ran_is_not_fresh(tmp_path: Path) -> None:
    """THE CASE THAT MATTERS MOST. A timer that was never installed must not
    read as healthy — that is the whole failure this backstop exists for."""
    assert watchdog.read_state(tmp_path / "absent.json", now=_NOW).status == watchdog.NEVER


def test_a_box_where_watch_never_ran_needs_attention(tmp_path: Path) -> None:
    assert watchdog.read_state(tmp_path / "absent.json", now=_NOW).needs_attention is True


def test_a_recent_pass_is_fresh(tmp_path: Path) -> None:
    path = tmp_path / "watch.json"
    _at(path, ago=60, interval=_HOUR)
    assert watchdog.read_state(path, now=_NOW).status == watchdog.FRESH


def test_a_pass_within_the_grace_window_is_still_fresh(tmp_path: Path) -> None:
    """Two intervals, not one: a pass that merely overlaps its own schedule is
    LATE, not dead, and an alarm that fires on ordinary lateness gets switched
    off — taking the real signal with it."""
    path = tmp_path / "watch.json"
    _at(path, ago=_HOUR * 2, interval=_HOUR)
    assert watchdog.read_state(path, now=_NOW).status == watchdog.FRESH


def test_a_pass_beyond_the_grace_window_is_stale(tmp_path: Path) -> None:
    path = tmp_path / "watch.json"
    _at(path, ago=_HOUR * 3, interval=_HOUR)
    assert watchdog.read_state(path, now=_NOW).status == watchdog.STALE


def test_a_stale_pass_needs_attention(tmp_path: Path) -> None:
    path = tmp_path / "watch.json"
    _at(path, ago=_HOUR * 9, interval=_HOUR)
    assert watchdog.read_state(path, now=_NOW).needs_attention is True


def test_an_undeclared_interval_is_unknown_not_fresh(tmp_path: Path) -> None:
    """Reporting `fresh` without an interval would be a guess wearing a
    verdict's clothes."""
    path = tmp_path / "watch.json"
    _at(path, ago=_HOUR * 100, interval=0)
    assert watchdog.read_state(path, now=_NOW).status == watchdog.UNKNOWN


def test_an_undeclared_interval_is_not_a_fault(tmp_path: Path) -> None:
    """A configuration gap, not a dead monitor. Conflating them would make the
    signal noisy on exactly the boxes that are working."""
    path = tmp_path / "watch.json"
    _at(path, ago=_HOUR * 100, interval=0)
    assert watchdog.read_state(path, now=_NOW).needs_attention is False


def test_a_corrupt_marker_reads_as_never(tmp_path: Path) -> None:
    path = tmp_path / "watch.json"
    path.write_text("{not json", encoding="utf-8")
    assert watchdog.read_state(path, now=_NOW).status == watchdog.NEVER


def test_a_marker_with_no_timestamp_reads_as_never(tmp_path: Path) -> None:
    path = tmp_path / "watch.json"
    path.write_text(json.dumps({"interval_seconds": 60}), encoding="utf-8")
    assert watchdog.read_state(path, now=_NOW).status == watchdog.NEVER


def test_a_future_timestamp_is_not_fresh(tmp_path: Path) -> None:
    """A clock correction or a bad RTC stamps the heartbeat ahead of now.

    Clamping the age to zero fixes the ARITHMETIC and not the verdict — zero
    age still compares as fresh, so a dead timer stays hidden until wall time
    catches up and then advances two more intervals. Raised in review on #244,
    and the first version of this test asserted the clamp rather than the
    conclusion, which is how it passed on the broken behaviour.
    """
    path = tmp_path / "watch.json"
    _at(path, ago=-_HOUR * 50, interval=_HOUR)
    assert watchdog.read_state(path, now=_NOW).status == watchdog.SKEWED


def test_a_future_timestamp_needs_attention(tmp_path: Path) -> None:
    path = tmp_path / "watch.json"
    _at(path, ago=-_HOUR * 50, interval=_HOUR)
    assert watchdog.read_state(path, now=_NOW).needs_attention is True


def test_the_skew_message_blames_the_clock_not_the_timer(tmp_path: Path) -> None:
    """The remedy differs: the clock is wrong, not the monitor."""
    path = tmp_path / "watch.json"
    _at(path, ago=-_HOUR * 50, interval=_HOUR)
    assert "clock is wrong" in watchdog.describe(watchdog.read_state(path, now=_NOW))


def test_the_shipped_timer_declares_its_schedule() -> None:
    """THE DEPLOYMENT, pinned (raised in review on #244).

    The supported installation never passed `--interval`, so the first
    scheduled pass recorded no schedule, `read_state` answered `unknown`, and
    `needs_attention` stayed False for ever — the watchdog was inert in the one
    place it exists to work. A unit that stops declaring it again fails here.
    """
    unit = (
        Path(__file__).resolve().parents[1]
        / "contrib"
        / "systemd"
        / "projects-orchestrator-watch.service"
    )
    assert "--interval" in unit.read_text(encoding="utf-8")


def test_the_stale_message_names_the_consequence(tmp_path: Path) -> None:
    path = tmp_path / "watch.json"
    _at(path, ago=_HOUR * 9, interval=_HOUR)
    assert "not being refreshed" in watchdog.describe(watchdog.read_state(path, now=_NOW))


def test_the_never_message_says_it_may_not_be_installed(tmp_path: Path) -> None:
    state = watchdog.read_state(tmp_path / "absent.json", now=_NOW)
    assert "never run" in watchdog.describe(state)
