"""Contract-v1 conformance diagnosis: manageability findings, never raise."""

from __future__ import annotations

from pathlib import Path

from conftest import make_project, make_project_v2

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


def test_diagnose_upgrade_warn_without_workflow(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _finding(_report(fleet_dir), "upgrade").status == "warn"


def test_diagnose_upgrade_ok_with_workflow(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    workflow = project / ".github/workflows/project-init-upgrade.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: upgrade\n", encoding="utf-8")
    assert _finding(_report(fleet_dir), "upgrade").status == "ok"


def test_diagnose_cloud_ok_for_library_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "ok"
    assert "no runtime probe expected" in finding.detail


def test_diagnose_cloud_ok_for_prototype_project(fleet_dir: Path) -> None:
    config = (
        "project:\n  name: alpha\n  project_init_contract_version: 1\n"
        "language: python\n"
        "delivery: prototype\n"
    )
    make_project(fleet_dir, "alpha", config_text=config)
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "ok"
    assert "no runtime probe expected" in finding.detail


def test_diagnose_cloud_warns_for_v1_service_without_deploy_metadata(
    fleet_dir: Path,
) -> None:
    config = (
        "project:\n  name: alpha\n  project_init_contract_version: 1\n"
        "language: python\n"
        "delivery: service\n"
    )
    make_project(fleet_dir, "alpha", config_text=config)
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "warn"
    assert "no deploy metadata" in finding.detail


def test_diagnose_cloud_warns_for_v2_service_without_deploy_block(
    fleet_dir: Path,
) -> None:
    config = (
        "project:\n  name: alpha\n  project_init_contract_version: 2\n"
        "language: python\n"
        "delivery: service\n"
    )
    make_project(fleet_dir, "alpha", config_text=config)
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "warn"
    assert "contract-v2 deploy block" in finding.detail


def test_diagnose_cloud_warns_for_v2_service_with_none_target(fleet_dir: Path) -> None:
    make_project_v2(fleet_dir, "alpha", deploy_target="none")
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "warn"
    assert "cloud-status cannot probe" in finding.detail


def test_diagnose_cloud_warns_for_cloud_run_missing_region(fleet_dir: Path) -> None:
    config = (
        "project:\n  name: alpha\n  project_init_contract_version: 2\n"
        "language: python\n"
        "delivery: service\n"
        "deploy:\n"
        "  target: cloud-run\n"
        "  app: alpha-svc\n"
    )
    make_project(fleet_dir, "alpha", config_text=config)
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "warn"
    assert "app and region" in finding.detail


def test_diagnose_cloud_ok_for_v2_service_with_deploy_metadata(fleet_dir: Path) -> None:
    make_project_v2(fleet_dir, "alpha", deploy_target="cloud-run")
    finding = _finding(_report(fleet_dir), "cloud")
    assert finding.status == "ok"
    assert "cloud-run deploy metadata present" in finding.detail


def test_report_status_is_fail_when_any_finding_fails(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="project:\n  name: alpha\n")
    assert _report(fleet_dir).status == "fail"


def test_report_status_is_warn_when_only_warnings(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _report(fleet_dir).status == "warn"
