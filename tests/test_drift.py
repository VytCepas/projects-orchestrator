"""Scaffold drift and hook health against real trees."""

from __future__ import annotations

import hashlib
from pathlib import Path

from conftest import make_project, make_project_v2

from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.drift import compute_drift, hook_health

CONFIG_WITH_MANIFEST = """\
project:
  name: "{name}"
  project_init_version: 0.5.2
  project_init_contract_version: 1

scaffold:
  manifest: {{"README.md": "{readme_hash}"}}
"""


def _project_with_manifest(fleet_dir: Path, readme_text: str = "hello", tamper: bool = False):
    digest = hashlib.sha256(readme_text.encode()).hexdigest()
    project = make_project(
        fleet_dir,
        "alpha",
        config_text=CONFIG_WITH_MANIFEST.format(name="alpha", readme_hash=digest),
    )
    (project / "README.md").write_text(
        readme_text + ("tampered" if tamper else ""), encoding="utf-8"
    )
    return load_descriptor(project)


def test_compute_drift_clean_when_hashes_match(fleet_dir: Path) -> None:
    assert compute_drift(_project_with_manifest(fleet_dir)).status == "clean"


def test_compute_drift_detects_modified_file(fleet_dir: Path) -> None:
    report = compute_drift(_project_with_manifest(fleet_dir, tamper=True))
    assert report.modified == ("README.md",)


def test_compute_drift_detects_missing_file(fleet_dir: Path) -> None:
    descriptor = _project_with_manifest(fleet_dir)
    (descriptor.path / "README.md").unlink()
    assert compute_drift(descriptor).missing == ("README.md",)


def test_compute_drift_without_manifest_is_no_manifest(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    assert compute_drift(descriptor).status == "no-manifest"


def test_drift_summary_counts_changes(fleet_dir: Path) -> None:
    report = compute_drift(_project_with_manifest(fleet_dir, tamper=True))
    assert report.summary == "1 file"


def test_drift_summary_clean_is_none(fleet_dir: Path) -> None:
    assert compute_drift(_project_with_manifest(fleet_dir)).summary == "none"


def test_drift_reports_unhashable_tracked_file_as_modified(fleet_dir: Path, monkeypatch) -> None:
    # A tracked file that exists but cannot be hashed (too large / unreadable)
    # must not be silently reported as clean — it cannot match its recorded SHA.
    import projects_orchestrator.drift as drift_mod

    descriptor = _project_with_manifest(fleet_dir)
    monkeypatch.setattr(drift_mod, "_sha256", lambda _path: None)
    report = compute_drift(descriptor)
    assert report.modified == ("README.md",)
    assert report.status == "drift"


def test_hook_health_no_hooks_dir_is_dash(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    assert hook_health(descriptor) == "-"


def _with_hooks(fleet_dir: Path, install: bool):
    project = make_project(fleet_dir, "alpha")
    source = project / ".github" / "hooks"
    source.mkdir(parents=True)
    (source / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    if install:
        installed = project / ".git" / "hooks"
        installed.mkdir(parents=True)
        (installed / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    return load_descriptor(project)


def test_hook_health_installed_is_ok(fleet_dir: Path) -> None:
    assert hook_health(_with_hooks(fleet_dir, install=True)) == "ok"


def test_hook_health_uninstalled_is_missing(fleet_dir: Path) -> None:
    assert hook_health(_with_hooks(fleet_dir, install=False)) == "missing"


def test_hook_health_uses_v2_expected_list(fleet_dir: Path) -> None:
    project = make_project_v2(fleet_dir, "alpha")
    installed = project / ".git" / "hooks"
    installed.mkdir(parents=True)
    (installed / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    assert hook_health(load_descriptor(project)) == "partial"


def test_hook_health_v2_all_expected_installed_is_ok(fleet_dir: Path) -> None:
    project = make_project_v2(fleet_dir, "alpha")
    installed = project / ".git" / "hooks"
    installed.mkdir(parents=True)
    for name in ("pre-commit", "commit-msg"):
        (installed / name).write_text("#!/bin/sh\n", encoding="utf-8")
    assert hook_health(load_descriptor(project)) == "ok"
