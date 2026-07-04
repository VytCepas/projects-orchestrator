"""Contract-v1 conformance diagnosis: manageability findings, never raise."""

from __future__ import annotations

from pathlib import Path

from conftest import make_project

from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.doctor import Finding, diagnose


def _report(fleet_dir: Path, name: str = "alpha"):
    return diagnose(load_descriptor(fleet_dir / name))


def _finding(report, check: str) -> Finding:
    return next(f for f in report.findings if f.check == check)


def test_diagnose_config_ok_for_clean_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _finding(_report(fleet_dir), "config").status == "ok"


def test_diagnose_config_fail_on_empty_config(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="")
    assert _finding(_report(fleet_dir), "config").status == "fail"


def test_diagnose_contract_ok_for_clean_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _finding(_report(fleet_dir), "contract").status == "ok"


def test_diagnose_contract_fail_without_version(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="project:\n  name: alpha\n")
    assert _finding(_report(fleet_dir), "contract").status == "fail"


def test_diagnose_contract_ok_for_v2_project(fleet_dir: Path) -> None:
    make_project(
        fleet_dir,
        "alpha",
        config_text="project:\n  name: alpha\n  project_init_contract_version: 2\n",
    )
    assert _finding(_report(fleet_dir), "contract").status == "ok"


def test_diagnose_contract_warns_on_newer_than_understood(fleet_dir: Path) -> None:
    make_project(
        fleet_dir,
        "alpha",
        config_text="project:\n  name: alpha\n  project_init_contract_version: 9\n",
    )
    finding = _finding(_report(fleet_dir), "contract")
    assert finding.status == "warn"
    assert "newer than understood" in finding.detail


def test_diagnose_scaffold_ok_for_clean_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _finding(_report(fleet_dir), "scaffold").status == "ok"


def test_diagnose_scaffold_warn_when_version_unknown(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="project:\n  name: alpha\n")
    assert _finding(_report(fleet_dir), "scaffold").status == "warn"


def test_diagnose_manifest_warn_without_manifest(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _finding(_report(fleet_dir), "manifest").status == "warn"


def test_diagnose_hooks_ok_when_none_shipped(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _finding(_report(fleet_dir), "hooks").status == "ok"


def test_diagnose_tooling_ok_with_declared_command(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    assert _finding(_report(fleet_dir), "tooling").status == "ok"


def test_diagnose_tooling_warn_without_commands(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={})
    assert _finding(_report(fleet_dir), "tooling").status == "warn"


def test_report_status_is_fail_when_any_finding_fails(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="project:\n  name: alpha\n")
    assert _report(fleet_dir).status == "fail"


def test_report_status_is_warn_when_only_warnings(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _report(fleet_dir).status == "warn"
