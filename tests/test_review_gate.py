"""The review gate's Codex-marker predicate, exercised as behaviour.

`review-status.yml` decides whether a required status goes green, and until now
the only thing asserting its logic was the logic itself. A typo or a later
refactor could leave requested reviews permanently pending — or quietly relax
the gate — while CI stayed green, which is the failure the gate was written to
eliminate (raised in review on #231).

THE FILTER IS READ OUT OF THE SHIPPED WORKFLOW, not copied here. A copy would
pass while the workflow was broken, which is the same class of defect as the one
under test.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "review-status.yml"
CONNECTOR = "chatgpt-codex-connector"
HEAD = "0123456789abcdef0123456789abcdef01234567"
PR_AUTHOR = "someone"
# The upstream render counts a comment-review only for the head it names (PI-1003).
REVIEWED = f"\n\nReviewed commit: `{HEAD[:10]}`"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")


def _codex_marker_filter() -> str:
    """Extract the jq program the workflow counts Codex comment-reviews with."""
    text = WORKFLOW.read_text(encoding="utf-8")
    anchor = text.index("| jq -s --arg sha")
    start = text.index("'", anchor) + 1
    end = text.index("'", start)
    body = text[start:end]
    assert "comments.nodes" in body, "extracted the wrong jq program from the workflow"
    return body


def _run(pages: list[dict], sha: str = HEAD, author: str = PR_AUTHOR) -> int:
    """Feed one JSON document per page to the workflow's own filter."""
    stdin = "\n".join(json.dumps(p) for p in pages)
    out = subprocess.run(
        ["jq", "-s", "--arg", "sha", sha, "--arg", "author", author, _codex_marker_filter()],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def _page(*comments: tuple[str, str]) -> dict:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "comments": {
                        "nodes": [{"author": {"login": a}, "body": b} for a, b in comments]
                    }
                }
            }
        }
    }


def test_a_marker_on_a_single_page_is_counted() -> None:
    assert _run([_page((CONNECTOR, "Codex Review: no issues found." + REVIEWED))]) == 1


def test_a_marker_on_a_later_page_is_counted() -> None:
    """The #233 finding: a review older than the last 100 comments still happened."""
    pages = [
        _page((CONNECTOR, "Codex Review: no issues found." + REVIEWED)),
        _page(("someone", "a later comment"), ("someone", "and another")),
    ]
    assert _run(pages) == 1


def test_no_review_counts_zero() -> None:
    assert _run([_page(("someone", "please review this"))]) == 0


def test_an_author_cannot_satisfy_the_gate_by_quoting_the_marker() -> None:
    """The author check is what carries the security: only the connector can
    write that login, so a human pasting the preamble must not count."""
    assert _run([_page(("someone", "Codex Review: looks good to me"))]) == 0


def test_a_connector_comment_that_is_not_a_review_does_not_count() -> None:
    """The onboarding notice the connector posts on an unconfigured repo is a
    comment from the right author and is not a review."""
    assert _run([_page((CONNECTOR, "To use Codex here, create an environment for this repo"))]) == 0


def test_leading_whitespace_does_not_lose_a_review() -> None:
    assert _run([_page((CONNECTOR, "\n  Codex Review: no issues found." + REVIEWED))]) == 1


def test_the_marker_match_is_case_insensitive() -> None:
    assert _run([_page((CONNECTOR, "CODEX REVIEW: no issues found." + REVIEWED))]) == 1


def test_a_review_of_an_older_commit_does_not_count() -> None:
    """After a push the gate waits for a review of the new head (PI-1003)."""
    stale = "\n\nReviewed commit: `fedcba9876`"
    assert _run([_page((CONNECTOR, "Codex Review: no issues found." + stale))]) == 0


def test_a_review_without_a_reviewed_commit_line_does_not_count() -> None:
    assert _run([_page((CONNECTOR, "Codex Review: no issues found."))]) == 0


def test_the_connector_cannot_review_its_own_pr() -> None:
    """REST spells the bot author with `[bot]`; the filter strips it (PI-1003)."""
    body = "Codex Review: no issues found." + REVIEWED
    assert _run([_page((CONNECTOR, body))], author=CONNECTOR + "[bot]") == 0


def test_an_unreadable_author_counts_no_review() -> None:
    body = "Codex Review: no issues found." + REVIEWED
    assert _run([_page((CONNECTOR, body))], author="") == 0
