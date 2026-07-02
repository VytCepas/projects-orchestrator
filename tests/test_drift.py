"""Scaffold drift and hook health against real trees."""

from __future__ import annotations

import hashlib
from pathlib import Path

from conftest import make_project

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
