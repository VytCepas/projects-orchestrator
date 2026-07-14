"""Orphan diff: what runs in the cloud that no repo governs — and unknown ≠ clean.

The load-bearing property is the pessimistic one: a scan that could not run must
report UNKNOWN, never an empty orphan list. A blank estate and an unauthenticated
scan rendering the same is exactly how a forgotten, billing service stays hidden.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_project, make_project_v2

from projects_orchestrator import orphans
from projects_orchestrator.adapters.gcp import GcpResource
from projects_orchestrator.registry import FleetConfig, discover


def _fleet(fleet_dir: Path):
    return discover(FleetConfig(roots=(fleet_dir,)))


def _resource(display_name: str, asset_type: str = "run.googleapis.com/Service") -> GcpResource:
    return GcpResource(
        name=f"//run/{display_name}", asset_type=asset_type, display_name=display_name
    )


def test_unknown_is_not_clean(fleet_dir: Path) -> None:
    # THE guard: a failed scan (None) is UNKNOWN, never zero orphans.
    make_project(fleet_dir, "alpha")
    report = orphans.find_orphans(_fleet(fleet_dir), None)
    assert report.is_unknown is True
    assert report.scanned is False
    assert report.orphans == ()  # empty, but is_unknown says it means nothing


def test_an_empty_inventory_is_a_clean_estate(fleet_dir: Path) -> None:
    # Distinct from unknown: the scan RAN and found nothing unaccounted.
    make_project(fleet_dir, "alpha")
    report = orphans.find_orphans(_fleet(fleet_dir), [])
    assert report.is_unknown is False
    assert report.orphans == ()


def test_a_resource_matching_no_project_is_an_orphan(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    report = orphans.find_orphans(_fleet(fleet_dir), [_resource("forgotten-service")])
    assert report.scanned is True
    assert [o.display_name for o in report.orphans] == ["forgotten-service"]


def test_a_resource_named_for_a_project_is_accounted_for(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    report = orphans.find_orphans(_fleet(fleet_dir), [_resource("Alpha")])  # case-insensitive
    assert report.orphans == ()


def test_a_resource_matching_a_deploy_app_is_accounted_for(fleet_dir: Path) -> None:
    # A service project ships a deploy.app that is its Cloud Run service name (the
    # v2 template renders it as `<name>-svc`); a resource by that name is governed,
    # even though it differs from the repo name.
    make_project_v2(fleet_dir, "web", deploy_target="cloud-run")
    report = orphans.find_orphans(_fleet(fleet_dir), [_resource("web-svc")])
    assert report.orphans == ()


def test_accounted_names_includes_project_and_app(fleet_dir: Path) -> None:
    make_project_v2(fleet_dir, "web", deploy_target="cloud-run")
    names = orphans.accounted_names(_fleet(fleet_dir))
    assert "web" in names
    assert "web-svc" in names


# --- The CLI surface -----------------------------------------------------------


def test_cli_orphans_unknown_exits_nonzero_and_does_not_claim_zero(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # THE guard, end to end: a scan that could not run must NOT print "no orphans".
    from projects_orchestrator.__main__ import gcp, main

    make_project(fleet_dir, "alpha")
    monkeypatch.setattr(gcp, "search_resources", lambda *_a, **_k: None)
    assert main(["orphans", "--root", str(fleet_dir)]) == 1
    err = capsys.readouterr()
    assert "unknown" in err.err.lower()
    assert "no orphans" not in err.out


def test_cli_orphans_lists_unaccounted_resources(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from projects_orchestrator.__main__ import gcp, main

    make_project(fleet_dir, "alpha")
    monkeypatch.setattr(
        gcp, "search_resources", lambda *_a, **_k: [_resource("stray"), _resource("alpha")]
    )
    assert main(["orphans", "--root", str(fleet_dir)]) == 0
    out = capsys.readouterr().out
    assert "stray" in out
    assert "1 orphan" in out


def test_cli_orphans_clean_estate_says_so(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from projects_orchestrator.__main__ import gcp, main

    make_project(fleet_dir, "alpha")
    monkeypatch.setattr(gcp, "search_resources", lambda *_a, **_k: [_resource("alpha")])
    assert main(["orphans", "--root", str(fleet_dir)]) == 0
    assert "no orphans" in capsys.readouterr().out


def test_cli_orphans_json(fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import json

    from projects_orchestrator.__main__ import gcp, main

    make_project(fleet_dir, "alpha")
    monkeypatch.setattr(gcp, "search_resources", lambda *_a, **_k: [_resource("stray")])
    assert main(["orphans", "--root", str(fleet_dir), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["display_name"] == "stray"
