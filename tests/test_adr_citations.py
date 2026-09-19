"""Every ADR a file cites must be findable (#189).

Four numbers — ADR-012, ADR-017, ADR-024, ADR-025 — were cited across ``src/``
and ``docs/`` as if they were this repo's decisions, and no file for them exists
in ``.agents/docs/adr/``. They are project-init's ADRs: the descriptor contract,
the tier model and the credential boundary are decided upstream, and this repo
consumes them. A bare ``ADR-025 §4`` read as a local section of a local document
that had no first section, and an audit spent effort looking for it.

So a citation takes one of two forms, and this test enforces it:

- ``ADR-NNN`` — resolves to ``.agents/docs/adr/adr-NNN-*.md`` in this repo;
- ``project-init ADR-NNN`` — resolves to a row of ``.agents/docs/adr/UPSTREAM.md``,
  which names the upstream file. Accepting any qualified number would let a typo
  point at nothing, so the qualifier alone is not enough.

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
_INDEX = ".agents/docs/adr/UPSTREAM.md"

_CITATION = re.compile(r"ADR-(\d{3})\b")
_UPSTREAM = "project-init "
_INDEX_ROW = re.compile(
    r"^\| project-init ADR-(\d{3}) \| `adr-(\d{3})-[a-z0-9-]+\.md` \|", flags=re.MULTILINE
)

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
    "tests/test_adr_citations.py": "this file: its docstring and cases name bare numbers on purpose",
}

_UPGRADE_BASES = (".agents/.upgrade-base.json", ".claude/.upgrade-base.json")


def citations(text: str) -> list[tuple[int, str, bool]]:
    """``(line, number, qualified)`` for every ADR citation in ``text``."""
    return [
        (
            text.count("\n", 0, match.start()) + 1,
            match.group(1),
            text[: match.start()].endswith(_UPSTREAM),
        )
        for match in _CITATION.finditer(text)
    ]


def phantoms(text: str, local: set[str], upstream: set[str]) -> list[tuple[int, str]]:
    """``(line, citation)`` for each citation that resolves to nothing."""
    return [
        (line, f"{_UPSTREAM if qualified else ''}ADR-{number}")
        for line, number, qualified in citations(text)
        if number not in (upstream if qualified else local)
    ]


def upstream_index(text: str) -> set[str]:
    """The numbers ``UPSTREAM.md`` records, each row naming its own file."""
    rows = _INDEX_ROW.findall(text)
    mismatched = [(cited, filed) for cited, filed in rows if cited != filed]
    assert not mismatched, f"UPSTREAM.md rows naming another ADR's file: {mismatched}"
    return {cited for cited, _ in rows}


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
def scanned() -> dict[str, str]:
    """Path -> text for every tracked, authored-here text file."""
    # mutmut runs the suite from a copy of the tree with no ADR directory and no
    # git index of its own; there is nothing to scan there, so say so.
    files = _tracked()
    if not _ADR_DIR.is_dir() or not files:
        pytest.skip("not a git checkout of this repo (e.g. mutmut's mutants/ copy)")
    stale = sorted(set(_EXEMPT) - set(files))
    assert not stale, f"exemptions for untracked paths: {stale}"
    skip = _managed() | set(_EXEMPT)
    texts = {}
    for path in files:
        if path in skip:
            continue
        try:
            texts[path] = (_ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
    assert len(texts) > 50, f"scanned only {len(texts)} files — the listing is broken, not clean"
    return texts


def test_every_cited_adr_resolves_here_or_upstream(scanned: dict[str, str]) -> None:
    local = _local_adrs()
    upstream = upstream_index(scanned[_INDEX])
    bad = [
        f"{path}:{line}: {citation}"
        for path, text in scanned.items()
        for line, citation in phantoms(text, local, upstream)
    ]
    assert not bad, (
        "ADR cited that resolves to nothing. Write the ADR in .agents/docs/adr/, or, if "
        f"the decision is project-init's, cite it as 'project-init ADR-NNN' with a row in "
        f"{_INDEX}:\n  " + "\n  ".join(bad)
    )


def test_every_upstream_row_is_cited(scanned: dict[str, str]) -> None:
    # A row nothing cites is an index entry nobody re-reads.
    upstream = upstream_index(scanned[_INDEX])
    cited = {
        number
        for path, text in scanned.items()
        if path != _INDEX
        for _, number, qualified in citations(text)
        if qualified
    }
    assert upstream, "UPSTREAM.md has no rows — the row pattern no longer matches the table"
    assert not upstream - cited, f"UPSTREAM.md rows cited nowhere: {sorted(upstream - cited)}"


def test_the_scan_sees_local_citations(scanned: dict[str, str]) -> None:
    # Control: a scanner that matched nothing would pass the tests above on any tree.
    local = _local_adrs()
    assert {"003", "006", "007"} <= local
    cited = {
        number
        for path, text in scanned.items()
        if path.startswith("src/")
        for _, number, _ in citations(text)
    }
    assert {"003", "006", "007"} <= cited


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see ADR-003 for the engine", []),
        ("per ADR-025 §4, degrade by tier", [(1, "ADR-025")]),
        ("per project-init ADR-025 §4", []),
        ("per project-init ADR-251, a typo", [(1, "project-init ADR-251")]),
        ("line one\nADR-003 / ADR-012 boundary", [(2, "ADR-012")]),
        ("ADR-003 / project-init ADR-012 boundary", []),
        ("the project-init\nADR-012 wrapped across a line", [(2, "ADR-012")]),
        ("project-init ADR-003 is not this repo's ADR-003", [(1, "project-init ADR-003")]),
    ],
)
def test_phantoms(text: str, expected: list[tuple[int, str]]) -> None:
    assert phantoms(text, {"003"}, {"012", "025"}) == expected


def test_an_index_row_must_name_its_own_file() -> None:
    row = "| project-init ADR-012 | `adr-017-per-surface-config-generator.md` | x |\n"
    with pytest.raises(AssertionError, match="another ADR's file"):
        upstream_index(row)
