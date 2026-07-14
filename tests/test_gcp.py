"""The read-only GCP adapter: it lists, it never mutates, and it never lies.

The two properties under test are the safety ones. First, the adapter is
*incapable of writing*: it issues exactly one command, a pure read, and a test
fails if it ever shells out anything else. Second, a scan that cannot run returns
``None`` (unknown), never an empty list — because "I found nothing" and "I could
not look" must never render the same.
"""

from __future__ import annotations

import json

import pytest

from projects_orchestrator.adapters import gcp
from projects_orchestrator.runner import RunResult

_MUTATING_TOKENS = ("deploy", "delete", "create", "update", "remove", "set-iam", "add-iam")


def test_the_only_command_is_read_only() -> None:
    # The whole cloud surface of this module is one line; it must be a pure read.
    command = gcp.search_command("organizations/123")
    assert "search-all-resources" in command
    assert not any(token in command.lower() for token in _MUTATING_TOKENS)


def test_the_scope_is_shell_quoted() -> None:
    # The scope is operator input; it must be quoted so it cannot inject flags or
    # shell metacharacters into the read-only command.
    command = gcp.search_command("projects/p; rm -rf /")
    assert "'projects/p; rm -rf /'" in command


def test_search_resources_issues_only_the_read_only_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # THE behavioural guard (criterion 4): whatever changes, the adapter must never
    # launch a mutating gcloud subprocess. Spy on the runner and assert it.
    issued: list[str] = []

    def spy(command: str, **_kwargs: object) -> RunResult:
        issued.append(command)
        return RunResult(command=command, returncode=0, stdout="[]")

    monkeypatch.setattr(gcp, "run_command", spy)
    gcp.search_resources("projects/p")
    assert issued == [gcp.search_command("projects/p")]
    assert "search-all-resources" in issued[0]
    assert all(token not in issued[0].lower() for token in _MUTATING_TOKENS)


def test_search_resources_parses_the_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        [
            {
                "name": "//run.googleapis.com/projects/p/services/api",
                "assetType": "run.googleapis.com/Service",
                "displayName": "api",
                "project": "p",
            }
        ]
    )
    monkeypatch.setattr(
        gcp,
        "run_command",
        lambda *_a, **_k: RunResult(command="gcloud read", returncode=0, stdout=payload),
    )
    resources = gcp.search_resources("projects/p")
    assert resources is not None
    assert len(resources) == 1
    assert resources[0].display_name == "api"
    assert resources[0].asset_type == "run.googleapis.com/Service"


def test_search_resources_is_unknown_when_the_scan_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unauthenticated / errored gcloud must be UNKNOWN, never "no resources".
    monkeypatch.setattr(
        gcp,
        "run_command",
        lambda *_a, **_k: RunResult(
            command="gcloud read", returncode=1, stderr="ERROR: unauthenticated"
        ),
    )
    assert gcp.search_resources("projects/p") is None


def test_search_resources_is_unknown_when_gcloud_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gcp,
        "run_command",
        lambda *_a, **_k: RunResult(
            command="gcloud read", returncode=None, error="gcloud: not found"
        ),
    )
    assert gcp.search_resources("projects/p") is None


def test_search_resources_is_unknown_on_non_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gcp,
        "run_command",
        lambda *_a, **_k: RunResult(command="gcloud read", returncode=0, stdout="not json at all"),
    )
    assert gcp.search_resources("projects/p") is None


def test_search_resources_is_unknown_when_output_is_not_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A JSON object (e.g. an error envelope) is not an inventory of zero resources.
    monkeypatch.setattr(
        gcp,
        "run_command",
        lambda *_a, **_k: RunResult(command="gcloud read", returncode=0, stdout='{"error": "x"}'),
    )
    assert gcp.search_resources("projects/p") is None
