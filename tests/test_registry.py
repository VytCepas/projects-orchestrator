"""Fleet discovery: roots, explicit paths, exclusions, degradation."""

from __future__ import annotations

from pathlib import Path

from conftest import make_project

from projects_orchestrator.registry import (
    FleetConfig,
    default_fleet_config,
    discover,
    load_fleet_config,
    register_project,
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


def test_load_fleet_config_invalid_yaml_warns(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text("{[", encoding="utf-8")
    assert load_fleet_config(fleet_file).warnings != ()


def test_load_fleet_config_unreadable_path_warns(tmp_path: Path) -> None:
    # A misspelled --fleet path must not look identical to an empty fleet.
    config = load_fleet_config(tmp_path / "does-not-exist.yaml")
    assert config.warnings != ()
    assert "cannot read fleet file" in config.warnings[0]


def test_discover_warns_on_duplicate_project_names(tmp_path: Path) -> None:
    make_project(tmp_path / "root-a", "app")
    make_project(tmp_path / "root-b", "app")
    fleet = discover(FleetConfig(roots=(tmp_path / "root-a", tmp_path / "root-b")))
    assert any("duplicate project name 'app'" in w for w in fleet.warnings)


def test_discover_surfaces_fleet_config_warnings(tmp_path: Path) -> None:
    config = load_fleet_config(tmp_path / "missing.yaml")
    assert any("cannot read fleet file" in w for w in discover(config).warnings)


def test_default_fleet_config_prefers_local_fleet_file(tmp_path: Path) -> None:
    (tmp_path / "fleet.yaml").write_text('roots: ["kids"]\n', encoding="utf-8")
    assert default_fleet_config(tmp_path).roots == (tmp_path / "kids",)


def test_default_fleet_config_falls_back_to_parent_scan(tmp_path: Path) -> None:
    cwd = tmp_path / "orchestrator"
    cwd.mkdir()
    assert default_fleet_config(cwd).roots == (tmp_path,)


def test_register_project_creates_fleet_file(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    project = make_project(tmp_path, "alpha")
    register_project(fleet_file, project)
    assert fleet_file.is_file()


def test_register_project_reports_added(tmp_path: Path) -> None:
    project = make_project(tmp_path, "alpha")
    assert register_project(tmp_path / "fleet.yaml", project).added is True


def test_register_project_makes_project_discoverable(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    project = make_project(tmp_path, "alpha")
    register_project(fleet_file, project)
    assert "alpha" in discover(load_fleet_config(fleet_file)).names


def test_register_project_is_idempotent(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    project = make_project(tmp_path, "alpha")
    register_project(fleet_file, project)
    assert register_project(fleet_file, project).added is False


def test_register_project_preserves_existing_entries(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    register_project(fleet_file, make_project(tmp_path, "alpha"))
    register_project(fleet_file, make_project(tmp_path, "beta"))
    assert set(discover(load_fleet_config(fleet_file)).names) == {"alpha", "beta"}
