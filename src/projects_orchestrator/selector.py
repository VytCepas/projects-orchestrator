"""``--where ci=fail`` — filter the fleet on what it already knows.

This is what turns "control forty projects" from forty terminal tabs into one
command. It is also, deliberately, the cheapest thing in the engine: a selector
**probes nothing**. Every field it can filter on is already computed for the
fleet table (:mod:`fleet`), so `--where` is a predicate over a
:class:`~fleet.ProjectSnapshot` and nothing more.

That has a consequence worth stating plainly rather than discovering later:
**a selector filters on what is KNOWN, not on what is TRUE right now.** Gate
results come from the checks cache, so ``--where ci=fail`` means "the last time
anyone looked, CI was failing". Run ``checks`` first if you need it fresh. The
alternative — silently re-probing the whole fleet on every filter — would make a
one-line filter cost minutes, and people would stop using it.

**An unknown field is an error, never an empty match.** `--where cli=fail` (note
the typo) must not quietly select nothing, and must certainly not select
everything: an operator who mistypes a filter and gets a confident-looking answer
has been actively misled. Unknown fields exit non-zero with the list of real ones.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from projects_orchestrator.fleet import ProjectSnapshot

#: Every field a selector can filter on, and how to read it off a snapshot.
#: Each returns a string; numeric comparisons parse it. Adding a field here is
#: the ONLY thing needed to make it filterable — there is no second registry to
#: forget to update.
_FIELDS: dict[str, Callable[[ProjectSnapshot], str]] = {
    "name": lambda s: s.descriptor.name,
    "language": lambda s: s.descriptor.language,
    "health": lambda s: s.status.health,
    "branch": lambda s: s.status.branch or "",
    "dirty": lambda s: "yes" if s.status.dirty else "no",
    # `scaffold` is the one people reach for first: "which projects have no
    # project-init yet?" — the target list for the rollout campaign.
    "scaffold": lambda s: (
        "none"
        if s.descriptor.project_init_version == "unknown"
        else s.descriptor.project_init_version
    ),
    "drift": lambda s: str(len(s.drift.modified) + len(s.drift.missing)),
    "hooks": lambda s: s.hooks,
    "lint": lambda s: _gate(s, "lint"),
    "test": lambda s: _gate(s, "test"),
    "ci": lambda s: _gate(s, "ci"),
    "running": lambda s: "yes" if s.run_state is not None else "no",
    "runnable": lambda s: "yes" if s.descriptor.has_task("run") else "no",
}

FIELDS = tuple(sorted(_FIELDS))

# NOTE the alternation order in the regex: `!=`, `>=`, `<=` MUST precede the
# single-character `=`, `>`, `<`. Otherwise `drift>=1` matches on the bare `>`
# and yields the value `=1`, which parses as no number at all and silently
# matches nothing.
_EXPRESSION = re.compile(r"\A(?P<field>[a-z_]+)\s*(?P<op>!=|>=|<=|=|>|<)\s*(?P<value>.*)\Z")


def _gate(snapshot: ProjectSnapshot, task: str) -> str:
    """A gate's last-known status, or ``unknown`` when nobody has looked.

    ``unknown`` is a real, filterable value — not a synonym for pass. "We have
    never checked this project's CI" and "this project's CI passes" are different
    facts, and a fleet tool that conflates them will quietly skip the projects it
    knows least about.
    """
    result = snapshot.checks.get(task)
    return result.status if result is not None else "unknown"


class SelectorError(ValueError):
    """A selector that cannot be honoured — an unknown field or bad syntax."""


@dataclass(frozen=True)
class Term:
    """One ``field op value`` predicate."""

    field: str
    op: str
    value: str

    def matches(self, snapshot: ProjectSnapshot) -> bool:
        """Whether ``snapshot`` satisfies this term (pure, never raises)."""
        actual = _FIELDS[self.field](snapshot)
        if self.op == "=":
            return actual == self.value
        if self.op == "!=":
            return actual != self.value
        return _compare(actual, self.op, self.value)


def _compare(actual: str, op: str, value: str) -> bool:
    """Numeric comparison; a non-numeric side never matches (never raises).

    ``drift>0`` on a project whose drift is unknown is *not* a match. Treating an
    unparseable value as 0 would silently fold "we don't know" into "it's fine",
    which is the same lie :func:`_gate` refuses to tell.
    """
    try:
        left, right = float(actual), float(value)
    except ValueError:
        return False
    if op == ">":
        return left > right
    if op == "<":
        return left < right
    if op == ">=":
        return left >= right
    return left <= right


def parse_term(expression: str) -> Term:
    """Parse one ``--where`` expression; raise :class:`SelectorError` on nonsense.

    Raising — rather than returning ``None`` and letting the caller shrug — is the
    point. A mistyped filter that silently matches nothing looks exactly like a
    fleet that is healthy, and one that silently matches everything is worse.
    """
    match = _EXPRESSION.match(expression.strip())
    if match is None:
        message = (
            f"cannot parse '{expression}': expected field=value "
            f"(also !=, >, <, >=, <=). Fields: {', '.join(FIELDS)}"
        )
        raise SelectorError(message)
    field = match.group("field")
    if field not in _FIELDS:
        message = f"unknown field '{field}'. Fields: {', '.join(FIELDS)}"
        raise SelectorError(message)
    return Term(field=field, op=match.group("op"), value=match.group("value").strip())


def parse(expressions: list[str] | tuple[str, ...]) -> tuple[Term, ...]:
    """Parse every ``--where`` expression; raises on the first bad one."""
    return tuple(parse_term(expression) for expression in expressions)


def select(
    snapshots: list[ProjectSnapshot], expressions: list[str] | tuple[str, ...]
) -> list[ProjectSnapshot]:
    """Return the snapshots matching every expression (AND); raises on a bad one.

    Terms combine with AND because that is what an operator means by
    ``--where ci=fail --where language=python``: narrow, then narrow again. OR is
    deliberately absent — it is rarely what is wanted, and its absence keeps the
    grammar small enough to hold in your head.
    """
    if not expressions:
        return snapshots
    terms = parse(expressions)
    return [s for s in snapshots if all(term.matches(s) for term in terms)]
