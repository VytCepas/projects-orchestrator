#!/usr/bin/env python3
"""Turn a mutmut run into a pass/fail signal — and fail when it tested nothing.

`mutmut run` exits 0 whatever the result, so the score has to be gated
separately. This is that gate, and it lives in a file rather than inside a
heredoc in ci.yml for one reason: **a gate that cannot be tested is a gate that
cannot be trusted.** The bug this replaces (#132) is proof — the old inline
version computed

    score = (killed / total * 100) if total else 100.0

so a run that mutated *nothing* scored 100% and passed. There was no
`[tool.mutmut]` section at the time, so that is precisely what it did, every
night, silently. The check that exists to catch tests which cannot fail was
itself a check that could not fail, and nobody could tell, because there was
nothing to run it against.

Its behaviour is now pinned by `tests/test_mutation_gate.py`.

Exit codes: 0 = at or above the floor. 1 = below it, or nothing was tested.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATS = Path("mutants/mutmut-cicd-stats.json")

#: A ratchet, not an aspiration. Raise it when the score rises; never lower it to
#: turn a red build green. 100% is not the target and never will be — some
#: mutants are *equivalent*, changing the code without changing the behaviour
#: under test, and demanding they die just teaches people to pin implementation
#: details in tests, which is worse than the disease.
DEFAULT_FLOOR = 60.0


@dataclass(frozen=True)
class Verdict:
    """The gate's decision.

    Attributes:
        ok: Whether the run passes.
        message: What to print — the score, or why there is no score.
    """

    ok: bool
    message: str


def evaluate(stats: object, floor: float = DEFAULT_FLOOR) -> Verdict:
    """Judge a mutmut stats blob (pure; no filesystem, no exit).

    Args:
        stats: The decoded ``mutmut-cicd-stats.json``.
        floor: Minimum percentage of mutants that must be killed.

    Returns:
        A :class:`Verdict`. **Zero mutants is a failure, not a perfect score** —
        an empty run means nothing was tested, and reporting that as 100% is the
        single most misleading thing this script could do.
    """
    if not isinstance(stats, dict):
        return Verdict(False, "The mutation stats are not an object — the run is broken.")

    total = stats.get("total")
    killed = stats.get("killed")
    if not isinstance(total, int) or not isinstance(killed, int):
        return Verdict(False, "The mutation stats have no total/killed — the run is broken.")

    if total <= 0:
        return Verdict(
            False,
            "No mutants were generated — nothing was tested. Check the [tool.mutmut] "
            "section in pyproject.toml (source_paths / only_mutate). An empty run is a "
            "broken gate, not a passing one.",
        )

    score = killed / total * 100
    summary = f"Mutation score: {score:.1f}% ({killed}/{total} killed, floor {floor:.0f}%)"
    if score < floor:
        return Verdict(False, f"{summary}\nBelow the {floor:.0f}% floor.")
    return Verdict(True, summary)


def load_stats(path: Path) -> object | None:
    """Read the stats file; ``None`` when it is missing or unreadable.

    A missing file means the run did not happen, not that it went well.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    """Gate a mutmut run; return the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stats", type=Path, default=DEFAULT_STATS)
    parser.add_argument("--floor", type=float, default=DEFAULT_FLOOR)
    args = parser.parse_args(argv)

    stats = load_stats(args.stats)
    if stats is None:
        print(f"Could not read the mutation stats at {args.stats} — the run did not happen.")
        return 1

    verdict = evaluate(stats, args.floor)
    print(verdict.message)
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
