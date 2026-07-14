"""Name sanitisation: a child repo's `name` must never become a path we obey.

`descriptor.name` is read from a **child repo's own** config.yaml. Two modules now
turn it into a filesystem path (`worktree`, `runs`), and the failure mode is
silent rather than loud — an absolute component does not raise, it just wins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from projects_orchestrator.naming import FALLBACK, safe_component


def test_an_ordinary_name_survives_intact() -> None:
    # Sanitising must not make normal names unreadable — a guard people route
    # around because it mangles their project names is a guard that gets removed.
    assert safe_component("my-project_1.0") == "my-project_1.0"


@pytest.mark.parametrize(
    "hostile",
    [
        "/tmp/owned",  # absolute: `root / this` IS `/tmp/owned`
        "../../../../tmp/owned",  # traversal
        "..",  # the parent itself
        ".",  # the current dir
        "a/b",  # a nested path
        "a\\b",  # a Windows-flavoured separator
    ],
)
def test_a_hostile_name_reduces_to_one_inert_component(hostile: str) -> None:
    cleaned = safe_component(hostile)
    assert "/" not in cleaned
    assert "\\" not in cleaned
    assert cleaned not in {"", ".", ".."}


def test_a_sanitised_name_cannot_escape_a_root_it_is_joined_to() -> None:
    # The property that actually matters, stated as the operation we perform.
    root = Path("/state/worktrees")
    joined = root / safe_component("/tmp/owned")
    assert root in joined.parents


def test_a_traversing_name_cannot_climb_out_when_joined() -> None:
    root = Path("/state/worktrees")
    joined = (root / safe_component("../../../../tmp/owned")).resolve()
    assert root in joined.parents


def test_a_name_that_reduces_to_nothing_still_yields_a_usable_component() -> None:
    # "" would join to the parent directory; "." and ".." traverse. None of
    # those are acceptable outputs, so it must fall back to something inert.
    assert safe_component("...") == FALLBACK
    assert safe_component("///") == FALLBACK
    assert safe_component("") == FALLBACK


def test_a_name_never_starts_with_a_dash() -> None:
    # A leading dash reads as a flag the moment it reaches any CLI.
    assert not safe_component("--force").startswith("-")


def test_the_guard_is_pure() -> None:
    assert safe_component("alpha") == safe_component("alpha")
