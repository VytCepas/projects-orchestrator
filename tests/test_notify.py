"""Threshold alerts and the webhook sink: pure detection, never-raising send."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_project

from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.fleet import collect_snapshot
from projects_orchestrator.notify import (
    CRITICAL,
    WARNING,
    Alert,
    alerts_payload,
    fleet_alerts,
    post_webhook,
    render_alerts,
    snapshot_alerts,
)


def _snapshot(fleet_dir: Path, cached: dict | None = None, name: str = "alpha"):
    return collect_snapshot(load_descriptor(fleet_dir / name), cached)


def _cached(task: str, status: str) -> dict[str, CheckResult]:
    return {task: CheckResult(project="alpha", task=task, status=status)}


def test_snapshot_alerts_flags_failing_tests_as_critical(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    alerts = snapshot_alerts(_snapshot(fleet_dir, _cached("test", "fail")))
    assert Alert("alpha", CRITICAL, "tests", "tests are failing") in alerts


def test_snapshot_alerts_flags_red_ci_as_critical(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert any(
        a.category == "ci" and a.level == CRITICAL
        for a in snapshot_alerts(_snapshot(fleet_dir, _cached("ci", "fail")))
    )


def test_snapshot_alerts_flags_failing_lint_as_warning(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert any(
        a.category == "lint" and a.level == WARNING
        for a in snapshot_alerts(_snapshot(fleet_dir, _cached("lint", "fail")))
    )


def test_snapshot_alerts_clean_project_has_no_alerts(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"test": "true"})
    assert snapshot_alerts(_snapshot(fleet_dir, _cached("test", "pass"))) == []


def test_snapshot_alerts_flags_uninstalled_hooks(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    source = project / ".github" / "hooks"
    source.mkdir(parents=True)
    (source / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    assert any(a.category == "hooks" for a in snapshot_alerts(_snapshot(fleet_dir)))


def test_fleet_alerts_orders_critical_before_warning(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    snap = _snapshot(fleet_dir, {**_cached("test", "fail"), **_cached("lint", "fail")})
    levels = [a.level for a in fleet_alerts([snap])]
    assert levels == sorted(levels, key=lambda level: 0 if level == CRITICAL else 1)


def test_render_alerts_all_clear_when_empty() -> None:
    assert "no alerts" in render_alerts([])


def test_alerts_payload_has_slack_text_field() -> None:
    payload = alerts_payload([Alert("alpha", CRITICAL, "ci", "CI is red")])
    assert "text" in payload
    assert payload["alerts"][0]["category"] == "ci"  # type: ignore[index]


def test_post_webhook_delivers_and_reports_success() -> None:
    captured: dict[str, object] = {}

    def send(url: str, body: bytes) -> int:
        captured["url"] = url
        captured["body"] = json.loads(body)
        return 200

    assert post_webhook("http://hook", [Alert("a", CRITICAL, "ci", "red")], send=send) is True
    assert captured["url"] == "http://hook"
    assert captured["body"]["alerts"][0]["project"] == "a"  # type: ignore[index,call-overload]


def test_post_webhook_non_2xx_is_failure() -> None:
    assert (
        post_webhook("http://hook", [Alert("a", CRITICAL, "ci", "red")], send=lambda _u, _b: 500)
        is False
    )


def test_post_webhook_never_raises_on_send_error() -> None:
    def boom(_url: str, _body: bytes) -> int:
        raise OSError("network down")

    assert post_webhook("http://hook", [Alert("a", CRITICAL, "ci", "red")], send=boom) is False
