"""Docs that can go stale silently, pinned (#190).

The README's command reference is the kind of list that is correct on the day it
is written and wrong three commands later, because nothing connects it to the
parser. Eighteen of the thirty registered subcommands were missing from it when
this was written — not through carelessness, but because adding a command and
updating a table are two separate acts and only one of them is enforced.

So the README says the table is checked. This is the check. A claim about a
mechanism, with no mechanism, is how documentation starts lying.
"""

from __future__ import annotations

import re
from pathlib import Path

from projects_orchestrator.__main__ import _build_parser

_ROOT = Path(__file__).resolve().parents[1]

#: Internal plumbing, not an operator-facing command.
_PRIVATE = {"_run-agent"}


def _registered() -> set[str]:
    """Every subcommand the parser actually registers."""
    parser = _build_parser()
    for action in parser._subparsers._group_actions:
        if hasattr(action, "choices") and action.choices:
            return set(action.choices) - _PRIVATE
    raise AssertionError("no subparsers found on the parser")


def _readme_rows() -> set[str]:
    """Commands that have a ROW in the reference table.

    Deliberately not "appears anywhere in the README": the first version of this
    matched any backticked word, so a command mentioned once in prose counted as
    documented and dropping its table row changed nothing. The claim in the
    README is about the table, so the test has to be about the table.
    """
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\| `([a-z][a-z-]*)`", readme, re.M)) | set(
        # `start` / `stop` / `logs` share one row.
        word
        for row in re.findall(r"^\| ((?:`[a-z-]+` / )+`[a-z-]+`) \|", readme, re.M)
        for word in re.findall(r"`([a-z-]+)`", row)
    )


def test_the_readme_documents_every_registered_command() -> None:
    missing = sorted(_registered() - _readme_rows())
    assert not missing, f"commands registered but absent from the README table: {missing}"


def test_the_readme_documents_no_command_that_does_not_exist() -> None:
    """The other direction, which matters more after a rename: a table row for a
    command that was removed sends the reader to an error message."""
    unknown = sorted(_readme_rows() - _registered() - _PRIVATE)
    assert not unknown, f"README.md documents commands that do not exist: {unknown}"


def test_no_doc_cites_an_adr_without_saying_where_it_is() -> None:
    """A dead ADR citation reads exactly like a live one.

    Where the decision has not been written up (tracked in #189), the citation
    must say so — the reader can then stop looking, instead of concluding the
    docs are wrong about everything.
    """
    present = {
        match.group(1)
        for path in (_ROOT / ".agents" / "docs" / "adr").glob("adr-*.md")
        if (match := re.match(r"adr-(\d+)", path.name))
    }
    dangling: list[str] = []
    for path in _ROOT.joinpath("docs").rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            for number in re.findall(r"ADR-(\d{3})", line):
                if number in present or "#189" in line:
                    continue
                dangling.append(f"{path.relative_to(_ROOT)}: ADR-{number}")
    assert not dangling, f"docs cite ADRs that do not exist and are not marked: {dangling}"
