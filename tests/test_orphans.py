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


def _resource(
    display_name: str,
    asset_type: str = "run.googleapis.com/Service",
    location: str = "",
) -> GcpResource:
    return GcpResource(
        name=f"//run/{display_name}",
        asset_type=asset_type,
        display_name=display_name,
        location=location,
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


def test_a_resource_matching_a_deploy_app_and_region_is_accounted_for(fleet_dir: Path) -> None:
    # A service project ships a deploy.app (the v2 template renders `<name>-svc`)
    # in a deploy.region (`fra`); a resource by that name IN THAT REGION is governed.
    make_project_v2(fleet_dir, "web", deploy_target="cloud-run")
    report = orphans.find_orphans(_fleet(fleet_dir), [_resource("web-svc", location="fra")])
    assert report.orphans == ()


def test_a_same_named_resource_in_another_region_is_an_orphan(fleet_dir: Path) -> None:
    # THE P2 guard: Cloud Run names are not unique across regions. The fleet owns
    # `web-svc` in `fra`; a stray `web-svc` in `us-central1` is a DIFFERENT service
    # and must not be hidden by the name alone.
    make_project_v2(fleet_dir, "web", deploy_target="cloud-run")
    report = orphans.find_orphans(_fleet(fleet_dir), [_resource("web-svc", location="us-central1")])
    assert [o.display_name for o in report.orphans] == ["web-svc"]


def test_accounted_keys_includes_project_and_app_region(fleet_dir: Path) -> None:
    make_project_v2(fleet_dir, "web", deploy_target="cloud-run")
    keys = orphans.accounted_keys(_fleet(fleet_dir))
    assert ("web", "") in keys  # repo name, any region
    assert ("web-svc", "fra") in keys  # app, its declared region


# --- The CLI surface -----------------------------------------------------------


_SCOPE = ("--scope", "projects/p")


def test_cli_orphans_without_a_scope_refuses_and_does_not_claim_zero(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # THE P1 guard: an unscoped scan covers only the configured project. It must
    # NOT run and declare a clean estate — it refuses (exit 2), and never calls the
    # scan at all.
    from projects_orchestrator.__main__ import gcp, main

    make_project(fleet_dir, "alpha")
    scanned: list[int] = []
    monkeypatch.setattr(gcp, "search_resources", lambda *_a, **_k: scanned.append(1) or [])
    assert main(["orphans", "--root", str(fleet_dir)]) == 2
    assert scanned == []  # never scanned
    err = capsys.readouterr()
    assert "scope is required" in err.err
    assert "no orphans" not in err.out


def test_cli_orphans_unknown_exits_nonzero_and_does_not_claim_zero(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # THE guard, end to end: a scan that could not run must NOT print "no orphans".
    from projects_orchestrator.__main__ import gcp, main

    make_project(fleet_dir, "alpha")
    monkeypatch.setattr(gcp, "search_resources", lambda *_a, **_k: None)
    assert main(["orphans", "--root", str(fleet_dir), *_SCOPE]) == 1
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
    assert main(["orphans", "--root", str(fleet_dir), *_SCOPE]) == 0
    out = capsys.readouterr().out
    assert "stray" in out
    assert "1 orphan" in out


def test_cli_orphans_clean_estate_says_so(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from projects_orchestrator.__main__ import gcp, main

    make_project(fleet_dir, "alpha")
    monkeypatch.setattr(gcp, "search_resources", lambda *_a, **_k: [_resource("alpha")])
    assert main(["orphans", "--root", str(fleet_dir), *_SCOPE]) == 0
    assert "no orphans" in capsys.readouterr().out


def test_cli_orphans_json(fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import json

    from projects_orchestrator.__main__ import gcp, main

    make_project(fleet_dir, "alpha")
    monkeypatch.setattr(gcp, "search_resources", lambda *_a, **_k: [_resource("stray")])
    assert main(["orphans", "--root", str(fleet_dir), "--json", *_SCOPE]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["display_name"] == "stray"
