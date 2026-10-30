"""The shared hardened write path (#181).

The atomic-write pattern existed as four divergent copies plus two writers that
did not use it at all, and the divergence was the point: only ``cache`` locked,
and nothing fsynced. These tests pin the two properties the copies disagreed
about, with a control for each so neither can pass on a broken helper.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path

from projects_orchestrator import persist

_WRITERS = 16


def _contend(path: Path, *, locked: bool) -> None:
    """Run `_WRITERS` overlapping load-modify-writes against one file."""

    def add(index: int) -> None:
        def body() -> None:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Widen the read-modify-write window so the race is reliable rather
            # than occasional — an unlocked run must fail every time, or this
            # control proves nothing.
            time.sleep(0.005)
            data[f"key-{index}"] = index
            persist.atomic_write(path, json.dumps(data))

        if locked:
            with persist.locked(path):
                body()
        else:
            body()

    threads = [threading.Thread(target=add, args=(i,)) for i in range(_WRITERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def test_a_concurrent_load_modify_write_loses_no_entries(tmp_path: Path) -> None:
    """The lost update #182 is about, at the helper level."""
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    _contend(path, locked=True)
    assert len(json.loads(path.read_text(encoding="utf-8"))) == _WRITERS


def test_without_the_lock_the_same_writers_lose_entries(tmp_path: Path) -> None:
    """THE CONTROL. Without it the test above could pass on a no-op lock —
    which is exactly the state four of the five call sites were in."""
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    _contend(path, locked=False)
    assert len(json.loads(path.read_text(encoding="utf-8"))) < _WRITERS


def test_atomic_write_creates_a_missing_parent(tmp_path: Path) -> None:
    """Every caller depends on the first write creating its own directory."""
    path = tmp_path / "deep" / "nested" / "state.json"
    persist.atomic_write(path, "hello")
    assert path.read_text(encoding="utf-8") == "hello"


def test_atomic_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    persist.atomic_write(path, "hello")
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_atomic_write_leaves_no_temp_file_behind_on_failure(tmp_path: Path) -> None:
    """A failed write must not litter: the temp file is unlinked before the
    OSError propagates, or a crashing writer fills the state dir."""
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("", encoding="utf-8")
    with contextlib.suppress(OSError):
        persist.atomic_write(blocked / "state.json", "hello")
    assert [p.name for p in tmp_path.iterdir()] == ["not-a-dir"]


def test_a_failed_write_raises_so_a_caller_can_ask(tmp_path: Path) -> None:
    """The helper does not suppress. ``supervisor`` kills a process it could not
    record, which it can only do if the failure reaches it."""
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("", encoding="utf-8")
    raised = False
    try:
        persist.atomic_write(blocked / "state.json", "hello")
    except OSError:
        raised = True
    assert raised


def test_the_lock_never_raises_when_it_cannot_be_taken(tmp_path: Path) -> None:
    """Best-effort: a racy write beats a lost one, and the engine never raises."""
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("", encoding="utf-8")
    with persist.locked(blocked / "state.json"):
        pass  # reaching here without an exception is the assertion
