"""Live dashboard server: pure payloads plus a real-socket smoke test."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import git_init, make_project

from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.registry import FleetConfig
from projects_orchestrator.server import (
    ActionTracker,
    _action_for_path,
    is_loopback,
    make_server,
    project_payload,
    render_page,
    run_heal,
    run_recheck,
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


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch) -> None:
    # run_recheck/run_heal write to the default checks cache when cache_file is
    # None; isolate it (and heal's worktree state) so no server test pollutes the
    # real ~/.cache or another test's fleet under xdist.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _no_live_heal_agent(monkeypatch) -> None:
    """No server test may spawn a real ``claude`` via a heal action — fuse it shut.

    A test that means to exercise a heal injects its own fake by overriding this.
    """

    def _explode(*_args: object, **_kwargs: object) -> object:
        message = "a server test reached the REAL heal agent — inject a fake"
        raise AssertionError(message)

    monkeypatch.setattr("projects_orchestrator.heal._default_agent_run", _explode)


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("192.168.1.10", False),
        ("", False),
    ],
)
def test_is_loopback_classifies_bind_hosts(host: str, loopback: bool) -> None:
    assert is_loopback(host) is loopback


def test_render_page_disables_actions_by_default() -> None:
    page = render_page()
    # The button-building JS is static; the runtime flag is what gates it.
    assert "const ACTIONS_ENABLED = false;" in page
    assert 'const TOKEN = "";' in page
    assert "__ACTIONS__" not in page and "__TOKEN__" not in page  # placeholders substituted


def test_render_page_embeds_token_and_enables_actions() -> None:
    page = render_page(token="s3cr3t", actions_enabled=True)
    assert "const ACTIONS_ENABLED = true;" in page
    assert 'const TOKEN = "s3cr3t";' in page
    # Still self-contained: no external assets even with actions on.
    assert "http://" not in page and "https://" not in page


def test_snapshot_payload_omits_actions_when_none(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    payload = snapshot_payload(_config(fleet_dir), None, "now")
    assert "actions" not in payload


def test_snapshot_payload_includes_actions_when_given(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    tracker = ActionTracker()
    tracker.begin("alpha", "recheck")
    payload = snapshot_payload(_config(fleet_dir), None, "now", tracker.as_dict())
    assert payload["actions"]["alpha"]["state"] == "running"  # type: ignore[index,call-overload]


def test_action_tracker_serialises_one_action_per_project() -> None:
    tracker = ActionTracker()
    assert tracker.begin("alpha", "heal") is True
    # A second start while one is running is refused (a 409 to the client).
    assert tracker.begin("alpha", "recheck") is False
    tracker.complete("alpha", "fixed")
    assert tracker.as_dict()["alpha"]["state"] == "done"
    # Once settled, a fresh action may begin again.
    assert tracker.begin("alpha", "recheck") is True


def test_action_for_path_parses_kind_and_project() -> None:
    assert _action_for_path("/api/project/alpha/recheck") == ("alpha", "recheck")
    assert _action_for_path("/api/project/alpha/heal") == ("alpha", "heal")
    assert _action_for_path("/api/project/alpha.json") is None
    assert _action_for_path("/nope") is None


def test_run_recheck_refreshes_cache_and_reports(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"lint": "false"})
    descriptor = load_descriptor(project)
    message = run_recheck(descriptor, fleet_dir / "cache.json")
    assert "failing: lint" in message


def test_run_heal_is_a_noop_on_a_green_project(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    descriptor = load_descriptor(project)
    assert "nothing to heal" in run_heal(descriptor, None)


def test_run_heal_dispatches_agent_on_a_red_gate(fleet_dir: Path, monkeypatch) -> None:
    from projects_orchestrator.heal import AgentOutcome

    project = make_project(fleet_dir, "alpha", tooling={"lint": "test -f fixed.txt"})
    git_init(project)
    descriptor = load_descriptor(project)

    def useless_agent(_descriptor: object, _prompt: str) -> AgentOutcome:
        return AgentOutcome(ok=True, summary="changed nothing")

    monkeypatch.setattr("projects_orchestrator.heal._default_agent_run", useless_agent)
    # lint is red; the fake agent fixes nothing -> heal reports verify_failed.
    assert "verify_failed" in run_heal(descriptor, None)


@pytest.fixture
def _action_server(fleet_dir: Path, tmp_path: Path, monkeypatch) -> Iterator[tuple[str, str]]:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    token = "test-token-abc123"
    server = make_server(
        _config(fleet_dir), "127.0.0.1", 0, cache_file=fleet_dir / "cache.json", token=token
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", token
    finally:
        server.shutdown()
        server.server_close()


def _post(url: str, token: str | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url, method="POST")
    if token is not None:
        request.add_header("X-PO-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_action_without_token_is_refused(_action_server: tuple[str, str]) -> None:
    base, _token = _action_server
    status, _body = _post(f"{base}/api/project/alpha/recheck")
    assert status == 403


def test_action_with_wrong_token_is_refused(_action_server: tuple[str, str]) -> None:
    base, _token = _action_server
    status, _body = _post(f"{base}/api/project/alpha/recheck", token="wrong")
    assert status == 403


def test_action_on_unknown_project_is_404(_action_server: tuple[str, str]) -> None:
    base, token = _action_server
    status, _body = _post(f"{base}/api/project/ghost/recheck", token=token)
    assert status == 404


def test_recheck_with_token_starts_and_completes(_action_server: tuple[str, str]) -> None:
    base, token = _action_server
    status, body = _post(f"{base}/api/project/alpha/recheck", token=token)
    assert status == 202
    assert json.loads(body)["kind"] == "recheck"
    # The action runs in the background; poll the snapshot until it settles.
    actions: dict[str, dict[str, str]] = {}
    deadline = time.time() + 5
    while time.time() < deadline:
        _status, snap = _get(f"{base}/api/snapshot.json")
        actions = json.loads(snap).get("actions", {})
        if actions.get("alpha", {}).get("state") == "done":
            break
        time.sleep(0.05)
    assert actions["alpha"]["state"] == "done"


def test_read_only_server_refuses_actions(_server: str) -> None:
    # The default server has no token -> actions disabled -> POST is 403.
    status, _body = _post(f"{_server}/api/project/alpha/recheck")
    assert status == 403
