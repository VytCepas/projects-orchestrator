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


# --- Poll-until-settled: deploy --wait (#152, ADR-005 deferred loop) -----------

from projects_orchestrator.adapters.cloud import (  # noqa: E402
    SETTLE_FAILED,
    SETTLE_SUCCEEDED,
    SETTLE_TIMED_OUT,
    SETTLE_UNCONFIRMED,
    SETTLE_UNKNOWN,
    SETTLE_UNSUPPORTED,
    WaitPolicy,
    classify_run,
    newest_run_id,
    resolved_workflow,
    wait_for_deploy,
)


def _runs_json(*runs: dict) -> str:
    return json.dumps(list(runs))


def _run(db_id: int, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {
        "databaseId": db_id,
        "status": status,
        "conclusion": conclusion,
        "url": f"https://gh/run/{db_id}",
        "createdAt": "2026-07-15T00:00:00Z",
    }


# newest_run_id / classify_run — the pure core


def test_newest_run_id_picks_the_highest() -> None:
    assert newest_run_id(_runs_json(_run(7), _run(42), _run(13))) == 42


def test_newest_run_id_of_no_runs_is_none() -> None:
    assert newest_run_id("[]") is None


def test_newest_run_id_of_garbage_is_none() -> None:
    assert newest_run_id("not json") is None


def test_classify_run_success() -> None:
    assert classify_run(_run(1, "completed", "success")) == SETTLE_SUCCEEDED


def test_classify_run_failure() -> None:
    assert classify_run(_run(1, "completed", "failure")) == SETTLE_FAILED


def test_classify_run_in_flight_is_empty() -> None:
    # Still running: the caller keeps polling, so the state is deliberately blank.
    assert classify_run(_run(1, "in_progress", None)) == ""


def test_classify_run_unknown_conclusion_is_failure() -> None:
    # A completed verdict we don't recognise must not read as success.
    assert classify_run(_run(1, "completed", "neutral")) == SETTLE_FAILED


# The loop, with injected clock/sleep and a scripted run_command


class _ScriptedRuns:
    """A run_command stand-in that returns queued stdout payloads in order."""

    def __init__(self, payloads: list[str], ok: bool = True) -> None:
        self._payloads = payloads
        self._ok = ok
        self.calls = 0

    def __call__(self, command: str, cwd: Path, timeout: float = 0.0) -> RunResult:  # noqa: ARG002
        idx = min(self.calls, len(self._payloads) - 1)
        self.calls += 1
        return RunResult(
            command=command,
            returncode=0 if self._ok else 1,
            stdout=self._payloads[idx] if self._payloads else "",
            duration=0.0,
        )


def _fast_policy() -> WaitPolicy:
    # A clock that advances 1s per read and a no-op sleep, so the loop never
    # touches real time. timeout=5 gives a handful of polls before the deadline.
    ticks = iter(range(0, 10_000))
    return WaitPolicy(
        timeout=5.0, poll_interval=1.0, now=lambda: next(ticks), sleep=lambda _s: None
    )


def _service(fleet_dir: Path) -> object:
    return load_descriptor(
        _with_deploy_workflow(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    )


def test_wait_returns_succeeded_when_our_run_completes(fleet_dir: Path, monkeypatch) -> None:
    # Watermark was 41; our run is 42, completed successfully.
    scripted = _ScriptedRuns([_runs_json(_run(42, "completed", "success"))])
    monkeypatch.setattr(cloud, "run_command", scripted)
    result = wait_for_deploy(_service(fleet_dir), "deploy.yml", 41, _fast_policy())
    assert result.state == SETTLE_SUCCEEDED


def test_wait_returns_failed_when_our_run_fails(fleet_dir: Path, monkeypatch) -> None:
    scripted = _ScriptedRuns([_runs_json(_run(42, "completed", "failure"))])
    monkeypatch.setattr(cloud, "run_command", scripted)
    result = wait_for_deploy(_service(fleet_dir), "deploy.yml", 41, _fast_policy())
    assert result.state == SETTLE_FAILED


def test_wait_polls_until_the_run_completes(fleet_dir: Path, monkeypatch) -> None:
    # First poll: still running. Second: done. The loop must keep going.
    scripted = _ScriptedRuns(
        [
            _runs_json(_run(42, "in_progress", None)),
            _runs_json(_run(42, "completed", "success")),
        ]
    )
    monkeypatch.setattr(cloud, "run_command", scripted)
    result = wait_for_deploy(_service(fleet_dir), "deploy.yml", 41, _fast_policy())
    assert result.state == SETTLE_SUCCEEDED
    assert scripted.calls == 2


def test_wait_ignores_a_pre_existing_run_below_the_watermark(fleet_dir: Path, monkeypatch) -> None:
    # A PREVIOUS deploy's run (id 41, failed) is in the list. Our dispatch's run
    # never appears. Following 41 would report the old failure as ours — instead
    # this must be unconfirmed, not failed.
    scripted = _ScriptedRuns([_runs_json(_run(41, "completed", "failure"))])
    monkeypatch.setattr(cloud, "run_command", scripted)
    result = wait_for_deploy(_service(fleet_dir), "deploy.yml", 41, _fast_policy())
    assert result.state == SETTLE_UNCONFIRMED


def test_wait_times_out_when_our_run_never_finishes(fleet_dir: Path, monkeypatch) -> None:
    scripted = _ScriptedRuns([_runs_json(_run(42, "in_progress", None))])
    monkeypatch.setattr(cloud, "run_command", scripted)
    result = wait_for_deploy(_service(fleet_dir), "deploy.yml", 41, _fast_policy())
    assert result.state == SETTLE_TIMED_OUT


def test_wait_is_unknown_when_gh_is_unreachable(fleet_dir: Path, monkeypatch) -> None:
    scripted = _ScriptedRuns([""], ok=False)  # every poll fails
    monkeypatch.setattr(cloud, "run_command", scripted)
    result = wait_for_deploy(_service(fleet_dir), "deploy.yml", 41, _fast_policy())
    assert result.state == SETTLE_UNKNOWN


def test_wait_is_unconfirmed_not_success_when_no_run_appears(fleet_dir: Path, monkeypatch) -> None:
    # THE honesty guard: gh answers, but our run never shows. Never call it success.
    scripted = _ScriptedRuns([_runs_json()])  # empty list, reachable
    monkeypatch.setattr(cloud, "run_command", scripted)
    result = wait_for_deploy(_service(fleet_dir), "deploy.yml", 41, _fast_policy())
    assert result.state == SETTLE_UNCONFIRMED
    assert not result.succeeded


def test_wait_with_no_watermark_follows_the_first_run(fleet_dir: Path, monkeypatch) -> None:
    # No prior runs (watermark None): the first run we see is ours.
    scripted = _ScriptedRuns([_runs_json(_run(1, "completed", "success"))])
    monkeypatch.setattr(cloud, "run_command", scripted)
    result = wait_for_deploy(_service(fleet_dir), "deploy.yml", None, _fast_policy())
    assert result.state == SETTLE_SUCCEEDED


def test_wait_on_a_gitlab_project_is_unsupported(fleet_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(cloud, "run_command", _explode)  # must not even poll
    descriptor = load_descriptor(_gitlab_project(fleet_dir))
    result = wait_for_deploy(descriptor, "deploy.yml", 41, _fast_policy())
    assert result.state == SETTLE_UNSUPPORTED


def test_resolved_workflow_defaults(fleet_dir: Path) -> None:
    assert resolved_workflow(_service(fleet_dir)) == "deploy.yml"
