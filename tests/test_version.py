"""The version has one source, and the changelog agrees with it (#191).

``pyproject.toml`` and ``__init__.py`` each held the version as a literal. They
agreed, but nothing bound them, and a second copy of one fact drifts silently
(#212 measured it in the descriptor). Now hatch reads ``__version__`` at build
time. These tests pin that, plus the one copy that has to stay by hand: the
newest released heading in ``CHANGELOG.md``.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from projects_orchestrator import __version__

_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _ROOT / "pyproject.toml"
_CHANGELOG = _ROOT / "CHANGELOG.md"


def _pyproject() -> dict:
    if not _PYPROJECT.is_file():
        pytest.skip("no pyproject.toml beside the tests (e.g. mutmut's mutants/ copy)")
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def test_pyproject_carries_no_version_literal() -> None:
    project = _pyproject()["project"]
    assert "version" not in project
    assert "version" in project.get("dynamic", [])


def test_hatch_reads_the_version_from_the_package() -> None:
    path = _pyproject()["tool"]["hatch"]["version"]["path"]
    assert (_ROOT / path).resolve() == (_ROOT / "src/projects_orchestrator/__init__.py").resolve()


def _released(changelog: str) -> list[str]:
    """Version headings in file order, ``Unreleased`` excluded."""
    return re.findall(r"^## \[(\d+\.\d+\.\d+[^\]]*)\]", changelog, flags=re.MULTILINE)


def test_the_newest_changelog_release_is_the_package_version() -> None:
    if not _CHANGELOG.is_file():
        pytest.skip("no CHANGELOG.md beside the tests (e.g. mutmut's mutants/ copy)")
    released = _released(_CHANGELOG.read_text(encoding="utf-8"))
    assert released, "CHANGELOG.md has no released version heading"
    assert released[0] == __version__, (
        f"__version__ is {__version__} but the newest CHANGELOG release is {released[0]} — "
        "a release moves Unreleased under a heading equal to __version__"
    )


def test_released_ignores_unreleased() -> None:
    text = "## [Unreleased]\n\n## [1.2.0] - 2026-01-01\n\n## [1.1.0] - 2025-01-01\n"
    assert _released(text) == ["1.2.0", "1.1.0"]
