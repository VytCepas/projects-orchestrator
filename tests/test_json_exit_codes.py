"""``--json`` exits exactly as the text mode does (#278).

``_emit_json`` returns 0, and ten commands used to return it: ``checks --json``
on a failing gate exited 0 while ``checks`` exited 1, so a monitor reading the
JSON was told the fleet was healthy. A monitor can ignore an exit code; it
cannot recover one it was never given.

Each case below builds one state and runs the same command twice, as text and
as ``--json``, each with its own cache and state directories so neither run can
change what the other sees. Failing states must exit 1 in both modes; the
passing controls must exit 0 in both, so a fix that made ``--json`` always exit
1 would fail here too.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import make_project

import projects_orchestrator.__main__ as cli
from projects_orchestrator.__main__ import main
from projects_orchestrator.adapters.cloud import CloudStatus
from projects_orchestrator.registry import RegisterOutcome

Setup = Callable[[Path, Path, pytest.MonkeyPatch], list[str]]

_MANIFEST_CONFIG = """\
project:
  name: "alpha"
  project_init_version: 0.5.2
  project_init_contract_version: 1

scaffold:
  manifest: {{"README.md": "{digest}"}}
"""


def _root(fleet: Path) -> list[str]:
    return ["--root", str(fleet)]


def _checks(lint: str) -> Setup:
    def setup(fleet: Path, _tmp: Path, _mp: pytest.MonkeyPatch) -> list[str]:
        make_project(fleet, "alpha", tooling={"lint": lint})
        return ["checks", *_root(fleet), "--task", "lint"]

    return setup


def _drift(tamper: bool) -> Setup:
    def setup(fleet: Path, _tmp: Path, _mp: pytest.MonkeyPatch) -> list[str]:
        digest = hashlib.sha256(b"hello").hexdigest()
        project = make_project(fleet, "alpha", config_text=_MANIFEST_CONFIG.format(digest=digest))
        readme = "hello" + ("tampered" if tamper else "")
        (project / "README.md").write_text(readme, encoding="utf-8")
        return ["drift", *_root(fleet)]

    return setup


def _doctor(config_text: str | None) -> Setup:
    def setup(fleet: Path, _tmp: Path, _mp: pytest.MonkeyPatch) -> list[str]:
        make_project(fleet, "alpha", config_text=config_text)
        return ["doctor", *_root(fleet)]

    return setup


def _plain(*command: str) -> Setup:
    def setup(fleet: Path, _tmp: Path, _mp: pytest.MonkeyPatch) -> list[str]:
        make_project(fleet, "alpha")
        return [*command, *_root(fleet)]

    return setup


def _ci(failed: bool) -> Setup:
    def setup(fleet: Path, _tmp: Path, mp: pytest.MonkeyPatch) -> list[str]:
        make_project(fleet, "alpha")
        ci = "fail" if failed else "pass"
        payload = {"project": "alpha", "ci": ci, "count": 0, "unit": "PR"}
        mp.setattr(cli, "probe_ci", lambda _d: (payload, [], failed))
        return ["ci", *_root(fleet)]

    return setup


def _cloud(state: str) -> Setup:
    def setup(fleet: Path, _tmp: Path, mp: pytest.MonkeyPatch) -> list[str]:
        make_project(fleet, "alpha")
        status = CloudStatus(project="alpha", target="cloud-run", state=state)
        mp.setattr(cli, "collect_cloud", lambda _d: status)
        return ["cloud-status", *_root(fleet)]

    return setup


def _upgrade(latest: tuple[int, int, int] | None) -> Setup:
    def setup(fleet: Path, _tmp: Path, mp: pytest.MonkeyPatch) -> list[str]:
        make_project(fleet, "alpha")
        mp.setattr(cli, "latest_upstream_version", lambda _cwd: latest)
        return ["upgrade-plan", *_root(fleet)]

    return setup


def _register(write_fails: bool) -> Setup:
    def setup(fleet: Path, tmp: Path, mp: pytest.MonkeyPatch) -> list[str]:
        project = make_project(fleet, "alpha")
        result = tmp / "scaffold.json"
        result.write_text(json.dumps({"target": str(project)}), encoding="utf-8")
        fleet_file = tmp / "fleet.yaml"
        if write_fails:
            outcome = RegisterOutcome(
                fleet_file=fleet_file,
                project=project,
                added=False,
                warnings=("cannot write the fleet file",),
            )
            mp.setattr(cli, "register_project", lambda _f, _t: outcome)
        return ["register", str(result), "--fleet", str(fleet_file)]

    return setup


_CASES: list[tuple[str, Setup, int]] = [
    # a failing state: both modes exit 1
    ("checks, a failing gate", _checks("false"), 1),
    ("drift, a tampered file", _drift(tamper=True), 1),
    ("doctor, no contract version", _doctor("project:\n  name: alpha\n"), 1),
    ("audit, findings", _plain("audit"), 1),
    ("audit --digest, new findings", _plain("audit", "--digest"), 1),
    ("hardening, gaps", _plain("hardening"), 1),
    ("ci, a failed run", _ci(failed=True), 1),
    ("cloud-status, stopped", _cloud("stopped"), 1),
    ("upgrade-plan, outdated", _upgrade((99, 0, 0)), 1),
    ("register, a failed write", _register(write_fails=True), 1),
    # passing controls: both modes exit 0
    ("checks, a passing gate", _checks("true"), 0),
    ("drift, clean", _drift(tamper=False), 0),
    ("doctor, conformant", _doctor(None), 0),
    ("ci, a passing run", _ci(failed=False), 0),
    ("cloud-status, running", _cloud("running"), 0),
    ("upgrade-plan, upstream unknown", _upgrade(None), 0),
    ("register, added", _register(write_fails=False), 0),
]


def _run(argv: list[str], tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch) -> int:
    """Run once with its own cache and state, so the two modes cannot interfere."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / mode / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / mode / "state"))
    return main(argv)


@pytest.mark.parametrize(("setup", "expected"), [c[1:] for c in _CASES], ids=[c[0] for c in _CASES])
def test_json_exits_as_the_text_mode_does(
    setup: Setup,
    expected: int,
    fleet_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = setup(fleet_dir, tmp_path, monkeypatch)
    text_rc = _run(argv, tmp_path, "text", monkeypatch)
    capsys.readouterr()
    json_rc = _run([*argv, "--json"], tmp_path, "json", monkeypatch)
    json.loads(capsys.readouterr().out)  # still exactly one JSON document on stdout
    assert (text_rc, json_rc) == (expected, expected)
