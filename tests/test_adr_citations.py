"""Every ADR a file cites must be findable (#189).

Four numbers — ADR-012, ADR-017, ADR-024, ADR-025 — were cited across ``src/``
and ``docs/`` as if they were this repo's decisions, and no file for them exists
in ``.agents/docs/adr/``. They are project-init's ADRs: the descriptor contract,
the tier model and the credential boundary are decided upstream, and this repo
consumes them. A bare ``ADR-025 §4`` read as a local section of a local document
that had no first section, and an audit spent effort looking for it.

So a citation takes one of two forms, and this test enforces it:

- ``ADR-NNN`` — resolves to ``.agents/docs/adr/adr-NNN-*.md`` in this repo;
- ``project-init ADR-NNN`` — names the repo whose decision it is.

Files project-init renders (the scaffold-managed set in ``.upgrade-base.json``,
and the descriptor) cite project-init's numbering in project-init's own words and
are overwritten on upgrade, so they are fixed upstream, not here. That set is
READ from the upgrade base rather than listed, because a hand-written list of
managed files is a second copy of a fact the scaffold already records.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_ADR_DIR = _ROOT / ".agents" / "docs" / "adr"

_CITATION = re.compile(r"ADR-(\d{3})\b")
_UPSTREAM = "project-init "

#: Tracked paths not scanned, each with the reason. Scaffold-managed files are
#: derived from the upgrade base and never listed here.
_EXEMPT: dict[str, str] = {
    ".agents/config.yaml": "the descriptor, rendered by project-init; its comments are project-init's",
    ".agents/.upgrade-base.json": "verbatim bodies of project-init's managed files",
    ".claude/.upgrade-base.json": "verbatim bodies of project-init's managed files",
    "tests/fixtures/project_init/config.v1.yaml": "golden project-init output, not authored here",
    "tests/fixtures/project_init/config.v2.yaml": "golden project-init output, not authored here",
    "tests/fixtures/project_init/capabilities.v2.md": "golden project-init output, not authored here",
    "tests/fixtures/project_init/schemas/descriptor.schema.json": "vendored project-init schema",
}

_UPGRADE_BASES = (".agents/.upgrade-base.json", ".claude/.upgrade-base.json")


def phantoms(text: str, local: set[str]) -> list[tuple[int, str]]:
    """``(line, number)`` for each citation that is neither local nor qualified."""
    found = []
    for match in _CITATION.finditer(text):
        number = match.group(1)
        if number in local or text[: match.start()].endswith(_UPSTREAM):
            continue
        found.append((text.count("\n", 0, match.start()) + 1, number))
    return found


def _tracked() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_ROOT, capture_output=True, text=True, check=False
    )
    return [p for p in result.stdout.split("\0") if p] if result.returncode == 0 else []


def _local_adrs() -> set[str]:
    return {
        m.group(1) for p in _ADR_DIR.glob("adr-*.md") if (m := re.match(r"adr-(\d{3})-", p.name))
    }


def _managed() -> set[str]:
    managed: set[str] = set()
    for base in _UPGRADE_BASES:
        managed |= set(json.loads((_ROOT / base).read_text(encoding="utf-8")))
    return managed


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    # mutmut runs the suite from a copy of the tree with no ADR directory and no
    # git index of its own; there is nothing to scan there, so say so.
    files = _tracked()
    if not _ADR_DIR.is_dir() or not files:
        pytest.skip("not a git checkout of this repo (e.g. mutmut's mutants/ copy)")
    return files


def test_every_cited_adr_resolves_here_or_names_its_repo(tracked: list[str]) -> None:
    local = _local_adrs()
    skip = _managed() | set(_EXEMPT)
    bad = []
    scanned = 0
    for path in tracked:
        if path in skip:
            continue
        try:
            text = (_ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        scanned += 1
        bad += [f"{path}:{line}: ADR-{number}" for line, number in phantoms(text, local)]
    assert scanned > 50, f"scanned only {scanned} files — the listing is broken, not clean"
    assert not bad, (
        "ADR cited with no file in .agents/docs/adr/ — write the ADR, or, if the "
        "decision is project-init's, cite it as 'project-init ADR-NNN':\n  " + "\n  ".join(bad)
    )


def test_the_scan_sees_local_adrs_and_they_exist(tracked: list[str]) -> None:
    # Control: a scanner that matched nothing would pass the test above on any tree.
    local = _local_adrs()
    assert {"003", "006", "007"} <= local
    cited = set()
    for path in tracked:
        if path.startswith("src/") and path.endswith(".py"):
            cited |= set(_CITATION.findall((_ROOT / path).read_text(encoding="utf-8")))
    assert {"003", "006", "007"} <= cited


def test_every_exemption_names_a_tracked_path(tracked: list[str]) -> None:
    # An exemption for a path that is gone is a decision nobody is re-reading.
    stale = sorted(set(_EXEMPT) - set(tracked))
    assert not stale, f"exemptions for untracked paths: {stale}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see ADR-003 for the engine", []),
        ("per ADR-025 §4, degrade by tier", [(1, "025")]),
        ("per project-init ADR-025 §4", []),
        ("line one\nADR-003 / ADR-012 boundary", [(2, "012")]),
        ("ADR-003 / project-init ADR-012 boundary", []),
        ("the project-init\nADR-012 wrapped across a line", [(2, "012")]),
    ],
)
def test_phantoms(text: str, expected: list[tuple[int, str]]) -> None:
    assert phantoms(text, {"003"}) == expected
