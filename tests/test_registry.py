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


def test_po_fleet_root_is_read_when_no_fleet_file(tmp_path: Path) -> None:
    """#204: the variable `watch` told people to check was read by nothing.

    Its failure message named `PO_FLEET_ROOT`, and `grep -rn PO_FLEET_ROOT src/`
    matched only that message — the variable lived solely in the docs' cron
    recipes, where the shell expands it into `--root`. So the advice given at
    the moment of confusion was a dead end.
    """
    cwd = tmp_path / "somewhere-with-no-fleet-file"
    cwd.mkdir()
    declared = tmp_path / "the-fleet"
    declared.mkdir()
    config = default_fleet_config(cwd, env={"PO_FLEET_ROOT": str(declared)})
    assert config.roots == (declared.resolve(),)


def test_a_local_fleet_file_still_beats_po_fleet_root(tmp_path: Path) -> None:
    """A file in the directory is a more specific statement than an env default."""
    (tmp_path / "fleet.yaml").write_text('roots: ["kids"]\n', encoding="utf-8")
    other = tmp_path / "ignored"
    other.mkdir()
    config = default_fleet_config(tmp_path, env={"PO_FLEET_ROOT": str(other)})
    assert config.roots == (tmp_path / "kids",)


def test_an_unusable_po_fleet_root_warns_instead_of_silently_scanning(
    tmp_path: Path,
) -> None:
    """Set but not a directory is a typo, and a typo must not read as an answer."""
    cwd = tmp_path / "orchestrator"
    cwd.mkdir()
    config = default_fleet_config(cwd, env={"PO_FLEET_ROOT": str(tmp_path / "nope")})
    assert config.roots == (tmp_path,), "must fall back to the parent scan"
    assert any("is not a directory" in w for w in config.warnings)


def test_po_fleet_root_empty_or_blank_is_ignored(tmp_path: Path) -> None:
    """An exported-but-empty variable is not a configuration."""
    cwd = tmp_path / "orchestrator"
    cwd.mkdir()
    for value in ("", "   "):
        config = default_fleet_config(cwd, env={"PO_FLEET_ROOT": value})
        assert config.roots == (tmp_path,)
        assert config.warnings == ()


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


def test_register_project_preserves_exclude(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text('projects: []\nexclude: ["archive-*"]\n', encoding="utf-8")
    register_project(fleet_file, make_project(tmp_path, "alpha"))
    assert load_fleet_config(fleet_file).exclude == ("archive-*",)


def test_register_project_preserves_include_plain_repos(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text("projects: []\ninclude_plain_repos: true\n", encoding="utf-8")
    register_project(fleet_file, make_project(tmp_path, "alpha"))
    assert load_fleet_config(fleet_file).include_plain_repos is True
