"""A heal pass tells the operator what it did (#165), through the webhook sink.

The draft PR stays a draft; the notification is what replaces promoting it. A
clean pass must reach nobody.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import git_init, make_project

from projects_orchestrator.__main__ import main
from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.heal import (
    FIXED,
    MODE_NOTIFY,
    NOTIFIED,
    VERIFY_FAILED,
    AgentOutcome,
    FleetHealReport,
    PrOutcome,
    heal_fleet,
    heal_project,
)
from projects_orchestrator.notify import heal_payload, heal_webhook_sink


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    def _explode(*_args: object, **_kwargs: object) -> AgentOutcome:
        message = "a test reached the REAL coding agent — inject agent_run instead"
        raise AssertionError(message)

    monkeypatch.setattr("projects_orchestrator.heal._default_agent_run", _explode)


def _fail(task: str) -> dict[str, CheckResult]:
    return {task: CheckResult(project="alpha", task=task, status="fail", detail="boom")}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _healable(fleet_dir: Path, tmp_path: Path) -> Path:
    """A project whose lint goes green once `fixed.txt` exists, with a pushable origin."""
    project = make_project(fleet_dir, "alpha", tooling={"lint": "test -f fixed.txt"})
    git_init(project)
    remote = tmp_path / "origin.git"
    _git("init", "--bare", "-q", str(remote), cwd=fleet_dir)
    _git("remote", "add", "origin", str(remote), cwd=project)
    return project


def _fixing_agent(descriptor: object, _prompt: str) -> AgentOutcome:
    (descriptor.path / "fixed.txt").write_text("ok", encoding="utf-8")  # type: ignore[attr-defined]
    return AgentOutcome(ok=True, summary="created fixed.txt")


def _opened_pr(_descriptor: object, branch: str, _tasks: tuple[str, ...]) -> PrOutcome:
    return PrOutcome(ok=True, url=f"https://example.test/pr/{branch}")


def _useless_agent(_descriptor: object, _prompt: str) -> AgentOutcome:
    return AgentOutcome(ok=True, summary="looked, changed nothing")


class _Recorder:
    """What a fake sender was handed; stands in for the network."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.bodies: list[dict[str, object]] = []

    def __call__(self, _url: str, body: bytes) -> int:
        self.bodies.append(json.loads(body))
        return self.status


# --- the payload -----------------------------------------------------------------------


def test_a_successful_heal_names_the_project_the_repaired_gate_and_the_pr(
    fleet_dir: Path, tmp_path: Path
) -> None:
    descriptor = load_descriptor(_healable(fleet_dir, tmp_path))
    result = heal_project(descriptor, _fail("lint"), agent_run=_fixing_agent, open_pr=_opened_pr)
    assert result.status == FIXED
    payload = heal_payload(FleetHealReport(results=(result,), limit=1))
    assert payload is not None
    [heal] = payload["heals"]  # type: ignore[misc]
    assert (heal["project"], heal["status"], heal["tasks"], heal["pr_url"]) == (
        "alpha",
        FIXED,
        ["lint"],
        result.pr_url,
    )
    assert result.pr_url in str(payload["text"])


def test_a_failed_heal_carries_its_diagnosis(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"lint": "false"})
    git_init(project)
    result = heal_project(load_descriptor(project), _fail("lint"), agent_run=_useless_agent)
    assert result.status == VERIFY_FAILED
    payload = heal_payload(FleetHealReport(results=(result,), limit=1))
    assert payload is not None
    [heal] = payload["heals"]  # type: ignore[misc]
    assert heal["status"] == VERIFY_FAILED
    assert heal["detail"] == "still failing after the agent's fix: lint"
    assert heal["tasks"] == ["lint"]


def test_a_notify_mode_project_says_what_to_do_by_hand(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    report = heal_fleet([(descriptor, _fail("test"))], limit=1, mode=MODE_NOTIFY)
    payload = heal_payload(report)
    assert payload is not None
    [heal] = payload["heals"]  # type: ignore[misc]
    assert (heal["status"], heal["tasks"]) == (NOTIFIED, ["test"])
    assert "projects-orchestrator heal alpha" in heal["detail"]


def test_a_deferred_project_is_told_too() -> None:
    report = FleetHealReport(results=(), deferred=("beta",), limit=1)
    payload = heal_payload(report)
    assert payload is not None
    assert payload["deferred"] == ["beta"]


def test_a_clean_pass_has_nothing_to_tell() -> None:
    assert heal_payload(FleetHealReport(results=(), limit=3)) is None


# --- the sink --------------------------------------------------------------------------


def test_the_sink_sends_nothing_on_a_clean_pass() -> None:
    sender = _Recorder()
    delivered = heal_webhook_sink("https://hooks.example.test/x", sender)(
        FleetHealReport(results=(), limit=3)
    )
    assert (delivered, sender.bodies) == (None, [])


def test_the_sink_reports_a_refused_delivery(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    report = heal_fleet([(descriptor, _fail("lint"))], limit=1, mode=MODE_NOTIFY)
    assert heal_webhook_sink("https://hooks.example.test/x", _Recorder(status=500))(report) is False


# --- end to end, through the CLI and a real HTTP endpoint ------------------------------


class _Hook:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []
        self.url = ""


@pytest.fixture()
def hook() -> Iterator[_Hook]:
    """A loopback webhook that records every JSON body POSTed to it."""
    received = _Hook()

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", 0))
            received.posts.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    received.url = f"http://127.0.0.1:{server.server_address[1]}/hook"
    try:
        yield received
    finally:
        server.shutdown()
        server.server_close()


def test_a_healed_project_reaches_the_webhook_with_its_pr_url(
    fleet_dir: Path,
    tmp_path: Path,
    hook: _Hook,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _healable(fleet_dir, tmp_path)
    monkeypatch.setattr("projects_orchestrator.heal._default_agent_run", _fixing_agent)
    monkeypatch.setattr("projects_orchestrator.heal._default_open_pr", _opened_pr)
    assert main(["heal", "alpha", "--root", str(fleet_dir), "--webhook", hook.url]) == 1
    [post] = hook.posts
    [heal] = post["heals"]  # type: ignore[misc]
    assert (heal["status"], heal["tasks"]) == (FIXED, ["lint"])
    assert str(heal["pr_url"]).startswith("https://example.test/pr/heal/lint-alpha-")
    assert "webhook: delivered" in capsys.readouterr().err


def test_a_failed_heal_reaches_the_webhook_with_its_diagnosis(
    fleet_dir: Path, hook: _Hook, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_init(make_project(fleet_dir, "alpha", tooling={"lint": "false"}))
    monkeypatch.setattr("projects_orchestrator.heal._default_agent_run", _useless_agent)
    main(["heal", "alpha", "--root", str(fleet_dir), "--webhook", hook.url])
    [post] = hook.posts
    [heal] = post["heals"]  # type: ignore[misc]
    assert (heal["status"], heal["detail"]) == (
        VERIFY_FAILED,
        "still failing after the agent's fix: lint",
    )


def test_a_green_fleet_posts_nothing(
    fleet_dir: Path, hook: _Hook, capsys: pytest.CaptureFixture[str]
) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    assert main(["heal", "--all", "--root", str(fleet_dir), "--webhook", hook.url]) == 0
    assert hook.posts == []
    assert "webhook" not in capsys.readouterr().err


def test_a_dead_webhook_does_not_change_the_exit_code(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "false"})
    argv = ["heal", "--all", "--mode", "notify", "--root", str(fleet_dir), "--json"]
    # Port 9 (discard) on loopback: nothing listens, so the POST is refused.
    assert main([*argv, "--webhook", "http://127.0.0.1:9/hook"]) == 1
    out = capsys.readouterr()
    assert json.loads(out.out)["webhook"] == "failed"
    assert "webhook: delivery failed" in out.err
