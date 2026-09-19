"""The host-health tile (#247): one line about the machine, never blank, never a shell."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_project

from projects_orchestrator.__main__ import main
from projects_orchestrator.host import HOST_UNKNOWN, host_health
from projects_orchestrator.html import render_html
from projects_orchestrator.registry import FleetConfig, load_fleet_config
from projects_orchestrator.server import snapshot_payload


def _reporter(tmp_path: Path, script: str) -> str:
    path = tmp_path / "reporter.sh"
    path.write_text(script, encoding="utf-8")
    return f"sh {path}"


def test_the_first_non_empty_line_is_the_tile(tmp_path: Path) -> None:
    command = _reporter(tmp_path, "echo\necho '  ok, disk 40%  '\necho second line\n")
    assert host_health(command) == "host: ok, disk 40%"


def test_no_declared_command_reads_unknown() -> None:
    assert host_health("") == HOST_UNKNOWN
    assert host_health("   ") == HOST_UNKNOWN


def test_a_failing_command_reads_unknown_even_when_it_printed(tmp_path: Path) -> None:
    assert host_health(_reporter(tmp_path, "echo ok\nexit 1\n")) == HOST_UNKNOWN


def test_a_command_that_times_out_reads_unknown() -> None:
    assert host_health("sleep 5", timeout=0.3) == HOST_UNKNOWN


def test_a_command_that_prints_nothing_reads_unknown() -> None:
    assert host_health("true") == HOST_UNKNOWN


def test_a_missing_or_unparseable_command_reads_unknown(tmp_path: Path) -> None:
    assert host_health(str(tmp_path / "no-such-reporter")) == HOST_UNKNOWN
    assert host_health('echo "unclosed') == HOST_UNKNOWN


def test_the_command_is_an_argv_not_a_shell() -> None:
    # Under a shell, `;` would start a second command. As an argv it is text.
    assert host_health("echo ok; echo injected") == "host: ok; echo injected"


def test_a_tilde_in_the_program_path_is_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    reporter = tmp_path / "reporter"
    reporter.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    reporter.chmod(0o755)
    assert host_health("~/reporter") == "host: ok"


def test_a_long_line_is_cut_to_one_tile() -> None:
    assert host_health("echo " + "x" * 500) == "host: " + "x" * 200


def test_the_fleet_file_declares_the_command(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text("host_health_command: echo ok\n", encoding="utf-8")
    assert load_fleet_config(fleet_file).host_health_command == "echo ok"


def test_a_non_string_command_is_ignored_with_a_warning(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text("host_health_command: [echo, ok]\n", encoding="utf-8")
    config = load_fleet_config(fleet_file)
    assert config.host_health_command == ""
    assert any("host_health_command" in w for w in config.warnings)


def _fleet_file(tmp_path: Path, fleet_dir: Path, extra: str = "") -> Path:
    make_project(fleet_dir, "alpha")
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(f"roots:\n  - {fleet_dir}\n{extra}", encoding="utf-8")
    return fleet_file


def test_the_status_table_carries_the_tile(
    tmp_path: Path, fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet_file = _fleet_file(tmp_path, fleet_dir, "host_health_command: echo ok, load 1.2\n")
    main(["status", "--fleet", str(fleet_file)])
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == "host: ok, load 1.2"
    assert "Project" in lines[0], "the table header stays the first line"


def test_the_status_table_says_unknown_without_a_command(
    tmp_path: Path, fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["status", "--fleet", str(_fleet_file(tmp_path, fleet_dir))])
    assert capsys.readouterr().out.splitlines()[-1] == HOST_UNKNOWN


def test_status_json_is_unchanged_by_the_tile(
    tmp_path: Path, fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The status and snapshot --json documents are frozen arrays; the tile
    # must not appear in them.
    fleet_file = _fleet_file(tmp_path, fleet_dir, "host_health_command: echo ok\n")
    main(["status", "--fleet", str(fleet_file), "--json"])
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_the_html_dashboard_carries_the_tile_escaped() -> None:
    page = render_html([], "now", "host: <b>ok</b>")
    assert '<p id="host">host: &lt;b&gt;ok&lt;/b&gt;</p>' in page


def test_the_live_dashboard_payload_carries_the_tile(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    config = FleetConfig(roots=(fleet_dir,), host_health_command="echo ok")
    assert snapshot_payload(config, None, "now")["host"] == "host: ok"
    assert snapshot_payload(FleetConfig(roots=(fleet_dir,)), None, "now")["host"] == HOST_UNKNOWN
