"""Property-based tests — the nightly fuzz gate, made real (#188).

`just fuzz` and the nightly CI job existed and ran the ordinary suite with
Hypothesis merely *available*: no `@given` anywhere, so the gate explored
nothing and passed for that reason. A scheduled job that cannot fail is the
same defect as a test that cannot fail, with a cron entry attached.

These target PURE functions with invariants a human would not think to probe —
version tuples of odd shapes, durations at unit boundaries, text that survives
a round trip. Hypothesis draws fresh inputs per run, so the nightly job explores
where a fixed corpus would only repeat.

Run locally with `just fuzz`.
"""

from __future__ import annotations

import json
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from projects_orchestrator import persist, watchdog
from projects_orchestrator.descriptor import parse_scaffold_version
from projects_orchestrator.upgrade import OK, OUTDATED, UNKNOWN, plan_status

_VERSION_PART = st.integers(min_value=0, max_value=9999)


@given(_VERSION_PART, _VERSION_PART, _VERSION_PART)
def test_a_dotted_version_round_trips(major: int, minor: int, patch: int) -> None:
    """Rendering a parsed version and re-parsing it must be the identity."""
    assert parse_scaffold_version(f"{major}.{minor}.{patch}") == (major, minor, patch)


@given(st.text(max_size=40))
def test_parsing_a_version_never_raises(value: str) -> None:
    """It reads a value out of a child project's descriptor — arbitrary text
    from a file this process does not own. ADR-003 says it degrades."""
    parse_scaffold_version(value)


_VERSION = st.tuples(_VERSION_PART, _VERSION_PART, _VERSION_PART)


@given(_VERSION, _VERSION)
def test_plan_status_is_exactly_one_verdict(current: tuple, latest: tuple) -> None:
    assert plan_status(current, latest) in {OK, OUTDATED, UNKNOWN}


@given(_VERSION, _VERSION)
def test_plan_status_is_outdated_exactly_when_behind(current: tuple, latest: tuple) -> None:
    """The whole point of the verb, stated as a law rather than an example:
    `outdated` must agree with tuple ordering for every pair, not just the
    hand-picked ones.
    """
    assert (plan_status(current, latest) == OUTDATED) == (current < latest)


@given(st.one_of(st.none(), _VERSION), st.one_of(st.none(), _VERSION))
def test_an_incomparable_version_is_unknown_not_ok(
    current: tuple | None, latest: tuple | None
) -> None:
    """`unknown` must never collapse into `ok`. Reporting a fleet as current
    because its version could not be read is the silence-as-health failure this
    codebase keeps finding."""
    if current is None or latest is None:
        assert plan_status(current, latest) == UNKNOWN


@given(st.text(min_size=0, max_size=2000))
def test_an_atomic_write_round_trips_any_text(text: str) -> None:
    """Every persisted document goes through this: cache JSON, history lines,
    supervisor state, the fleet registry.

    READ BACK WITH ``newline=""``, and that is a finding rather than a
    formality. Hypothesis produced ``"\r"`` on the first run of this file and
    the naive assertion failed — `atomic_write` writes the byte faithfully
    (``b"\r"`` lands on disk), but a plain ``read_text()`` applies universal
    newline translation and hands back ``"\n"``.

    Nothing here is affected today: every payload this helper stores is JSON,
    where a carriage return is escaped and never reaches the file as a bare
    byte. It is pinned because a future caller storing raw text would be
    surprised, and because the asymmetry is in the READER, not the writer —
    which is the opposite of where one would look for it.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "payload"
        persist.atomic_write(path, text)
        assert path.read_text(encoding="utf-8", newline="") == text


@given(st.integers(min_value=0, max_value=10_000_000))
def test_a_duration_always_renders_non_empty(seconds: int) -> None:
    """It lands in an operator-facing warning, so an empty string would make the
    sentence unreadable at exactly the moment it matters."""
    rendered = watchdog._age(seconds)
    assert rendered and rendered[-1] in {"s", "m", "h", "d"}


@given(st.integers(min_value=1, max_value=86_400), st.integers(min_value=0, max_value=10_000_000))
def test_watch_freshness_agrees_with_the_grace_window(interval: int, age: int) -> None:
    """The staleness rule as a law: fresh exactly when within the window.

    Encoded against `read_state`'s real arithmetic rather than a reimplementation
    of it, via a heartbeat written at a known offset.
    """
    import datetime as _dt
    import tempfile

    now = _dt.datetime(2026, 9, 16, 12, 0, 0, tzinfo=_dt.UTC)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "watch.json"
        watchdog.record_pass(interval, path, now=now - _dt.timedelta(seconds=age))
        state = watchdog.read_state(path, now=now)
    expected = watchdog.FRESH if age <= interval * watchdog.STALE_INTERVALS else watchdog.STALE
    assert state.status == expected


@given(st.dictionaries(st.text(min_size=1, max_size=12), st.integers(), max_size=8))
def test_a_cache_document_never_reads_as_a_newer_build(document: dict) -> None:
    """Arbitrary junk must read as `unreadable` or `legacy` — never `future`.

    `future` makes `save_results` REFUSE to write, so a file that wrongly earned
    it would silently stop the cache updating for ever.
    """
    import tempfile

    from projects_orchestrator import cache

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "checks.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        assert cache.read_cache(path).status != cache.FUTURE
