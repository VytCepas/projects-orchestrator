"""Tests for the read-only web dashboard: it serves status and starts nothing."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from projects_orchestrator.web import CockpitServer


@pytest.fixture
def server(tmp_path):
    """Start a dashboard server on an ephemeral port and tear it down."""
    proj = tmp_path / "demo" / ".claude"
    proj.mkdir(parents=True)
    (proj / "project-init.md").write_text("# Project: demo\n", encoding="utf-8")
    srv = CockpitServer(("127.0.0.1", 0), tmp_path)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_page_is_served(server):
    with urllib.request.urlopen(f"{server.origin}/") as resp:
        body = resp.read().decode()
    assert "projects-orchestrator" in body


def test_status_returns_a_list(server):
    with urllib.request.urlopen(f"{server.origin}/api/status") as resp:
        data = json.loads(resp.read())
    assert isinstance(data, list)


def test_snapshot_is_cached_within_ttl(server):
    assert server.status() is server.status()


def test_action_endpoint_is_gone(server):
    req = urllib.request.Request(
        f"{server.origin}/api/action",
        data=json.dumps({"name": "demo", "op": "start"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 501
