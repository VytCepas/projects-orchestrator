"""Cloud adapter: descriptor-driven, read-only, degrades to unknown offline."""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest
from conftest import make_project, make_project_v2

from projects_orchestrator.adapters import cloud
from projects_orchestrator.adapters.cloud import (
    CloudStatus,
    as_check_results,
    collect_cloud,
    parse_cloud_run_status,
    parse_fly_status,
    probe_health,
    trigger_deploy,
)
from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.runner import RunResult


def _with_deploy_workflow(project: Path, relpath: str = ".github/workflows/deploy.yml") -> Path:
    """Give a project the workflow a dispatch targets — otherwise it is `no-workflow`."""
    workflow = project / relpath
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text("on: workflow_dispatch\n", encoding="utf-8")
    return project


def _spy(calls: list[str], ok: bool = True, stderr: str = ""):
    """A run_command stand-in that records the command instead of running it."""

    def fake(command: str, cwd: Path, timeout: float = 0.0) -> RunResult:  # noqa: ARG001
        calls.append(command)
        return RunResult(
            command=command,
            returncode=0 if ok else 1,
            stdout="",
            stderr=stderr,
            duration=0.0,
        )

    return fake


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


# --- cloud control plane (ADR-005): dispatch-only deploy actions ---


def test_trigger_deploy_no_target_is_skipped(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    assert trigger_deploy(descriptor, "deploy", apply=True).status == "skipped"


def test_trigger_deploy_unknown_action_is_skipped(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    assert trigger_deploy(descriptor, "nuke", apply=True).status == "skipped"


def test_trigger_deploy_dry_run_is_planned(fleet_dir: Path) -> None:
    descriptor = load_descriptor(
        _with_deploy_workflow(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    )
    assert trigger_deploy(descriptor, "deploy").status == "planned"


def test_trigger_deploy_dry_run_carries_default_workflow(fleet_dir: Path) -> None:
    descriptor = load_descriptor(
        _with_deploy_workflow(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    )
    assert trigger_deploy(descriptor, "rollback").workflow == "deploy.yml"


def test_trigger_deploy_uses_declared_workflow(fleet_dir: Path) -> None:
    config = (
        "project:\n  name: alpha\n  project_init_contract_version: 2\n"
        "language: python\ndelivery: service\n"
        "deploy:\n  target: fly\n  app: alpha-svc\n  workflow: ship.yml\n"
    )
    project = _with_deploy_workflow(
        make_project(fleet_dir, "alpha", config_text=config), ".github/workflows/ship.yml"
    )
    descriptor = load_descriptor(project)
    assert trigger_deploy(descriptor, "deploy").workflow == "ship.yml"


# --- The guardrail: a dry run MUST NOT shell out ---------------------------
# This is the load-bearing safety property of the whole control plane, so it is
# asserted on the *behaviour* (was a subprocess launched?), not on the returned
# status. Asserting `status == "planned"` alone is vacuous: hoisting run_command
# above the `if not apply` branch keeps the status planned while every dry run
# fires a real production workflow_dispatch. That mutation must fail a test.


def _explode(*_args: object, **_kwargs: object) -> RunResult:
    raise AssertionError("dry run shelled out — the plan-only guardrail is broken")


def test_dry_run_never_shells_out(fleet_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(cloud, "run_command", _explode)
    descriptor = load_descriptor(
        _with_deploy_workflow(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    )
    assert trigger_deploy(descriptor, "deploy").status == "planned"


def test_dry_run_never_shells_out_for_any_action(fleet_dir: Path, monkeypatch) -> None:
    # rollback/restart are the destructive ones — pin them individually.
    monkeypatch.setattr(cloud, "run_command", _explode)
    descriptor = load_descriptor(
        _with_deploy_workflow(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    )
    assert [trigger_deploy(descriptor, a).status for a in ("rollback", "restart")] == [
        "planned",
        "planned",
    ]


def test_apply_does_shell_out(fleet_dir: Path, monkeypatch) -> None:
    # The other half of the guardrail: if apply=True silently stopped dispatching,
    # the tests above would still pass and the control plane would be a no-op.
    calls: list[str] = []
    monkeypatch.setattr(cloud, "run_command", _spy(calls, ok=True))
    descriptor = load_descriptor(
        _with_deploy_workflow(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    )
    assert trigger_deploy(descriptor, "deploy", apply=True).status == "dispatched"
    assert calls and calls[0].startswith("gh workflow run")


def test_trigger_deploy_degrades_to_failed_when_dispatch_fails(
    fleet_dir: Path, monkeypatch
) -> None:
    # Previously this test really ran `gh workflow run` against the developer's
    # ambient auth, and only "passed" because the fixture repo has no remote.
    monkeypatch.setattr(cloud, "run_command", _spy([], ok=False, stderr="gh: not authenticated"))
    descriptor = load_descriptor(
        _with_deploy_workflow(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    )
    assert trigger_deploy(descriptor, "deploy", apply=True).status == "failed"


# --- Pre-flight: a plan that cannot execute must say so on the DRY RUN -------


def test_missing_deploy_workflow_is_reported_on_the_dry_run(fleet_dir: Path) -> None:
    # No deploy.yml in the child. Previously this reported a clean `planned` —
    # a plan that can never execute — and only failed at --apply time.
    descriptor = load_descriptor(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    result = trigger_deploy(descriptor, "deploy")
    assert result.status == "no-workflow"


def test_missing_deploy_workflow_says_which_file_is_missing(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    assert ".github/workflows/deploy.yml" in trigger_deploy(descriptor, "deploy").detail


def test_missing_deploy_workflow_never_shells_out_even_with_apply(
    fleet_dir: Path, monkeypatch
) -> None:
    monkeypatch.setattr(cloud, "run_command", _explode)
    descriptor = load_descriptor(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    assert trigger_deploy(descriptor, "deploy", apply=True).status == "no-workflow"


# --- A failed dispatch must say WHY ----------------------------------------


def test_failed_dispatch_carries_the_stderr_reason(fleet_dir: Path, monkeypatch) -> None:
    # "failed" with an empty detail is indistinguishable from "retry in 5s".
    monkeypatch.setattr(cloud, "run_command", _spy([], ok=False, stderr="gh: not authenticated"))
    descriptor = load_descriptor(
        _with_deploy_workflow(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    )
    assert "not authenticated" in trigger_deploy(descriptor, "deploy", apply=True).detail


def test_failed_dispatch_reports_a_timeout_as_a_timeout(fleet_dir: Path, monkeypatch) -> None:
    def timed_out(command: str, cwd: Path, timeout: float = 0.0) -> RunResult:  # noqa: ARG001
        return RunResult(
            command=command, returncode=None, stdout="", stderr="", duration=20.0, timed_out=True
        )

    monkeypatch.setattr(cloud, "run_command", timed_out)
    descriptor = load_descriptor(
        _with_deploy_workflow(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    )
    assert "timed out" in trigger_deploy(descriptor, "deploy", apply=True).detail


# --- GitLab children dispatch via glab, not gh ------------------------------


def _gitlab_project(fleet_dir: Path) -> Path:
    config = (
        "project:\n  name: alpha\n  project_init_contract_version: 2\n"
        "  project_init_host: gitlab.com\n"
        "language: python\ndelivery: service\n"
        "deploy:\n  target: fly\n  app: alpha-svc\n"
    )
    return _with_deploy_workflow(
        make_project(fleet_dir, "alpha", config_text=config), ".gitlab/deploy.yml"
    )


def test_gitlab_child_dispatches_via_glab(fleet_dir: Path, monkeypatch) -> None:
    # `gh workflow run` in a GitLab repo can only ever fail — the project would
    # be structurally undeployable while the dry run cheerfully said `planned`.
    calls: list[str] = []
    monkeypatch.setattr(cloud, "run_command", _spy(calls, ok=True))
    descriptor = load_descriptor(_gitlab_project(fleet_dir))
    assert trigger_deploy(descriptor, "deploy", apply=True).status == "dispatched"
    assert calls[0].startswith("glab ")


def test_gitlab_child_looks_for_its_workflow_under_gitlab(fleet_dir: Path) -> None:
    descriptor = load_descriptor(_gitlab_project(fleet_dir))
    assert trigger_deploy(descriptor, "deploy").status == "planned"
