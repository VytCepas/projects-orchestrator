"""Fleet discovery: roots, explicit paths, exclusions, degradation."""

from __future__ import annotations

from pathlib import Path

from conftest import make_project

from projects_orchestrator.registry import (
    FleetConfig,
    default_fleet_config,
    discover,
    load_fleet_config,
)


def test_discover_finds_projects_under_root(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta")
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    assert fleet.names == ("alpha", "beta")


def test_discover_skips_non_project_directories(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    (fleet_dir / "random-dir").mkdir()
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    assert fleet.names == ("alpha",)


def test_discover_honors_exclude_patterns(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "archive-old")
    fleet = discover(FleetConfig(roots=(fleet_dir,), exclude=("archive-*",)))
    assert fleet.names == ("alpha",)


def test_discover_includes_explicit_projects(tmp_path: Path) -> None:
    elsewhere = make_project(tmp_path / "elsewhere", "gamma")
    fleet = discover(FleetConfig(projects=(elsewhere,)))
    assert fleet.names == ("gamma",)


def test_discover_warns_on_bad_explicit_project(tmp_path: Path) -> None:
    fleet = discover(FleetConfig(projects=(tmp_path / "missing",)))
    assert "not a project-init project" in fleet.warnings[0]


def test_discover_dedupes_by_resolved_path(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    fleet = discover(FleetConfig(roots=(fleet_dir,), projects=(project,)))
    assert fleet.names == ("alpha",)


def test_discover_missing_root_warns(tmp_path: Path) -> None:
    fleet = discover(FleetConfig(roots=(tmp_path / "nope",)))
    assert "cannot scan root" in fleet.warnings[0]


def test_fleet_get_is_case_insensitive(fleet_dir: Path) -> None:
    make_project(fleet_dir, "Alpha")
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    assert fleet.get("alpha").name == "Alpha"


def test_load_fleet_config_resolves_relative_roots(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text('roots: ["projects"]\n', encoding="utf-8")
    assert load_fleet_config(fleet_file).roots == (tmp_path / "projects",)


def test_load_fleet_config_invalid_yaml_yields_empty(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text("{[", encoding="utf-8")
    assert load_fleet_config(fleet_file).roots == ()


def test_default_fleet_config_prefers_local_fleet_file(tmp_path: Path) -> None:
    (tmp_path / "fleet.yaml").write_text('roots: ["kids"]\n', encoding="utf-8")
    assert default_fleet_config(tmp_path).roots == (tmp_path / "kids",)


def test_default_fleet_config_falls_back_to_parent_scan(tmp_path: Path) -> None:
    cwd = tmp_path / "orchestrator"
    cwd.mkdir()
    assert default_fleet_config(cwd).roots == (tmp_path,)
