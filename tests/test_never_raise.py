"""The never-raise invariant, tested systematically instead of ad hoc (#187).

Thirty-odd modules document "never raises" — it is ADR-003, the contract the
whole control plane rests on — and it was pinned by roughly ten scattered files
that each injected one fault at one call site. What was missing is the shape
that catches a NEW collector, or an old one whose guard moves: feed every
``collect_*`` the same corpus of hostile inputs and assert none of them raise.

The gap was not theoretical. #210 was an unguarded stat inside the descriptor
read path: one unreadable directory raised out of a function ADR-003 says never
raises, aborted ``registry.discover``'s loop, and emptied the whole fleet. No
test failed, because no test asked this question of that path.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import make_project

from projects_orchestrator.adapters.cloud import collect_cloud
from projects_orchestrator.adapters.github import collect_github
from projects_orchestrator.adapters.gitlab import collect_gitlab
from projects_orchestrator.checks import collect_checks
from projects_orchestrator.descriptor import ProjectDescriptor, load_descriptor
from projects_orchestrator.fleet import collect_snapshot
from projects_orchestrator.status import collect_status

#: Every collector that takes a descriptor and answers about one project.
#: A new one added without a guard fails here rather than in the field.
COLLECTORS: tuple[Callable[[ProjectDescriptor], Any], ...] = (
    collect_snapshot,
    collect_checks,
    collect_status,
    collect_cloud,
    collect_gitlab,
    collect_github,
)


def _absent(tmp_path: Path) -> ProjectDescriptor:
    """A descriptor whose project directory does not exist at all."""
    return ProjectDescriptor(name="gone", path=tmp_path / "no-such-project")


def _unreadable(tmp_path: Path) -> ProjectDescriptor:
    """A descriptor whose every filesystem probe raises PermissionError.

    Injected rather than chmod-ed: CPython 3.14 swallows EACCES in pathlib and
    returns False, so a chmod-based fault passes on this repo's own venv whether
    or not a guard exists — the trap #210 was hiding behind.
    """
    return ProjectDescriptor(name="blocked", path=tmp_path / "blocked")


def _garbage(tmp_path: Path) -> ProjectDescriptor:
    """A descriptor whose fields are the wrong shape for every consumer."""
    return ProjectDescriptor(
        name="",
        path=tmp_path,
        language="\x00not-a-language",
        tooling={"lint": ""},
        memory_path=tmp_path / "\x00",
    )


FAULTS: tuple[tuple[str, Callable[[Path], ProjectDescriptor]], ...] = (
    ("absent", _absent),
    ("unreadable", _unreadable),
    ("garbage", _garbage),
)


@pytest.mark.parametrize("collector", COLLECTORS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("fault", FAULTS, ids=lambda f: f[0])
def test_no_collector_raises_on_a_hostile_descriptor(
    collector: Callable[[ProjectDescriptor], Any],
    fault: tuple[str, Callable[[Path], ProjectDescriptor]],
    tmp_path: Path,
) -> None:
    collector(fault[1](tmp_path))


@pytest.mark.parametrize("collector", COLLECTORS, ids=lambda c: c.__name__)
def test_no_collector_raises_when_every_stat_denies(
    collector: Callable[[ProjectDescriptor], Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #210 shape, generalised: the filesystem itself refuses."""
    project = make_project(tmp_path, "alpha")
    descriptor = load_descriptor(project)
    assert descriptor is not None

    def deny(*_args: object, **_kwargs: object) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "is_file", deny)
    monkeypatch.setattr(Path, "is_dir", deny)
    monkeypatch.setattr(Path, "exists", deny)
    collector(descriptor)
