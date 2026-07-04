"""Live dashboard server: pure payloads plus a real-socket smoke test."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import make_project

from projects_orchestrator.registry import FleetConfig
from projects_orchestrator.server import (
    make_server,
    project_payload,
    render_page,
    snapshot_payload,
)


def _config(fleet_dir: Path) -> FleetConfig:
    return FleetConfig(roots=(fleet_dir,))


def test_snapshot_payload_has_columns_and_rows(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta")
    payload = snapshot_payload(_config(fleet_dir), None, "2026-07-04T00:00:00+00:00")
    assert payload["generated_at"] == "2026-07-04T00:00:00+00:00"
    assert [row["Project"] for row in payload["rows"]] == ["alpha", "beta"]  # type: ignore[index,union-attr]


def test_snapshot_payload_reflects_projects_added_after_start(fleet_dir: Path) -> None:
    config = _config(fleet_dir)
    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta")
    # Re-discovery per call means a newly added project appears on the next poll.
    payload = snapshot_payload(config, None, "now")
    assert len(payload["rows"]) == 2  # type: ignore[arg-type]


def test_snapshot_payload_statuses_come_from_shared_classifier(fleet_dir: Path) -> None:
    # The page styles cells from server-supplied statuses, not a client-side
    # copy of the good/bad/warn vocabulary. A declared run_command → Runnable
    # cell "yes" → status "good".
    make_project(fleet_dir, "alpha", tooling={"run": "true"})
    payload = snapshot_payload(_config(fleet_dir), None, "now")
    statuses = payload["statuses"]
    assert statuses[0]["Runnable"] == "good"  # type: ignore[index]
    assert set(statuses[0]) == set(payload["columns"])  # type: ignore[index,arg-type]


def test_project_payload_returns_detail_sections(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    payload = project_payload(_config(fleet_dir), None, "alpha")
    assert payload is not None
    assert set(payload) == {"project", "summary", "checks", "commits", "memory"}


def test_project_payload_unknown_is_none(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert project_payload(_config(fleet_dir), None, "ghost") is None


def test_render_page_is_self_contained() -> None:
    page = render_page()
    assert page.lstrip().startswith("<!DOCTYPE")
    assert "/api/snapshot.json" in page
    # No external assets: no http(s) references to other hosts.
    assert "http://" not in page and "https://" not in page


@pytest.fixture
def _server(fleet_dir: Path) -> Iterator[str]:
    make_project(fleet_dir, "alpha")
    server = make_server(_config(fleet_dir), "127.0.0.1", 0, cache_file=fleet_dir / "cache.json")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_server_serves_snapshot_json(_server: str) -> None:
    status, body = _get(f"{_server}/api/snapshot.json")
    assert status == 200
    assert json.loads(body)["rows"][0]["Project"] == "alpha"


def test_server_serves_project_detail(_server: str) -> None:
    status, body = _get(f"{_server}/api/project/alpha.json")
    assert status == 200
    assert json.loads(body)["project"] == "alpha"


def test_server_unknown_project_is_404(_server: str) -> None:
    status, _body = _get(f"{_server}/api/project/ghost.json")
    assert status == 404


def test_server_unknown_route_is_404(_server: str) -> None:
    status, _body = _get(f"{_server}/nonsense")
    assert status == 404
