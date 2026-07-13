"""Cloud adapter: descriptor-driven, read-only, degrades to unknown offline."""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest
from conftest import make_project, make_project_v2

from projects_orchestrator.adapters.cloud import (
    CloudStatus,
    as_check_results,
    collect_cloud,
    parse_cloud_run_status,
    parse_fly_status,
    probe_health,
)
from projects_orchestrator.descriptor import load_descriptor

FLY_DEPLOYED = json.dumps({"Name": "svc", "Deployed": True, "Status": "deployed", "Version": "42"})
FLY_SUSPENDED = json.dumps({"Name": "svc", "Deployed": False, "Status": "suspended"})

CLOUD_RUN_READY = json.dumps(
    {
        "status": {
            "latestReadyRevisionName": "svc-00042-abc",
            "conditions": [{"type": "Ready", "status": "True"}],
        }
    }
)
CLOUD_RUN_NOT_READY = json.dumps({"status": {"conditions": [{"type": "Ready", "status": "False"}]}})


def test_parse_fly_status_deployed() -> None:
    assert parse_fly_status(FLY_DEPLOYED) == ("deployed", "42")


def test_parse_fly_status_suspended_is_stopped() -> None:
    assert parse_fly_status(FLY_SUSPENDED)[0] == "stopped"


def test_parse_fly_status_garbage_is_unknown() -> None:
    assert parse_fly_status("not json") == ("unknown", "")


def test_parse_cloud_run_ready_is_deployed() -> None:
    assert parse_cloud_run_status(CLOUD_RUN_READY) == ("deployed", "svc-00042-abc")


def test_parse_cloud_run_not_ready_is_stopped() -> None:
    assert parse_cloud_run_status(CLOUD_RUN_NOT_READY)[0] == "stopped"


def test_parse_cloud_run_garbage_is_unknown() -> None:
    assert parse_cloud_run_status("[]") == ("unknown", "")


def test_collect_cloud_no_deploy_block_is_none(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    assert collect_cloud(descriptor).state == "none"


def test_collect_cloud_deploy_none_is_none(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project_v2(fleet_dir, "alpha", deploy_target="none"))
    assert collect_cloud(descriptor).state == "none"


def test_collect_cloud_missing_cli_is_unknown(fleet_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")
    descriptor = load_descriptor(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    assert collect_cloud(descriptor).state == "unknown"


def test_collect_cloud_unknown_target_is_unknown(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project_v2(fleet_dir, "alpha", deploy_target="mainframe"))
    assert collect_cloud(descriptor).state == "unknown"


def test_collect_cloud_does_not_execute_injected_app_name(fleet_dir: Path) -> None:
    # A hostile deploy.app must not inject shell into the read-only probe.
    marker = fleet_dir / "INJECTED"
    config = (
        "project:\n  name: alpha\n  project_init_contract_version: 2\n"
        "language: python\ndelivery: service\n"
        f'deploy:\n  target: cloud-run\n  app: "x; touch {marker}"\n  region: us\n'
    )
    descriptor = load_descriptor(make_project(fleet_dir, "alpha", config_text=config))
    collect_cloud(descriptor)
    assert not marker.exists()


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def health_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/health"
    server.shutdown()


def test_probe_health_2xx_is_healthy(health_server: str) -> None:
    _HealthHandler.status = 200
    assert probe_health(health_server) == "healthy"


def test_probe_health_5xx_is_unhealthy(health_server: str) -> None:
    _HealthHandler.status = 503
    assert probe_health(health_server) == "unhealthy"


def test_probe_health_unreachable_is_unknown() -> None:
    assert probe_health("http://127.0.0.1:1/health", timeout=0.5) == "unknown"


def test_probe_health_non_http_scheme_is_unknown() -> None:
    assert probe_health("file:///etc/passwd") == "unknown"


def test_as_check_results_none_target_renders_none() -> None:
    results = as_check_results(CloudStatus(project="alpha"), "2026-07-03T00:00:00+00:00")
    assert results[0].status == "none"


def test_as_check_results_deployed_healthy_is_pass() -> None:
    status = CloudStatus(project="alpha", target="fly", state="deployed", health="healthy")
    assert as_check_results(status, "")[0].status == "pass"


def test_as_check_results_unhealthy_is_fail() -> None:
    status = CloudStatus(project="alpha", target="fly", state="deployed", health="unhealthy")
    assert as_check_results(status, "")[0].status == "fail"


def test_as_check_results_detail_carries_revision() -> None:
    status = CloudStatus(project="alpha", target="fly", state="deployed", revision="42")
    assert as_check_results(status, "")[0].detail == "42"
