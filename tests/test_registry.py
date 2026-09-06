"""Fleet discovery: roots, explicit paths, exclusions, degradation."""

from __future__ import annotations

from pathlib import Path

from conftest import make_project

from projects_orchestrator.registry import (
    _HINT_BUDGET,
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


# --- Nested projects are accounted for, not discovered (#215) ---


def _nested_warning(fleet) -> str:
    """The one warning about deeper-than-one-level projects, or ``""``."""
    return next((w for w in fleet.warnings if "nested deeper" in w), "")


def test_a_project_nested_two_levels_is_reported_as_skipped(fleet_dir: Path) -> None:
    make_project(fleet_dir, "s-core")
    make_project(fleet_dir / "s-core", "nested-child")
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    assert "nested-child" in _nested_warning(fleet)


def test_a_project_nested_three_levels_is_reported_as_skipped(fleet_dir: Path) -> None:
    # The depth the issue reports actually having been bitten by.
    make_project(fleet_dir, "s-core")
    make_project(fleet_dir / "s-core" / "deep", "deeper")
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    assert "deeper" in _nested_warning(fleet)


def test_a_nested_project_is_still_not_discovered(fleet_dir: Path) -> None:
    # THE CONTRACT IS UNCHANGED, deliberately. Widening discovery would alter
    # what the fleet *is* on every box with repos under a root; the defect was
    # the silence, not the depth. If this ever flips, _HINT_DEPTH's note is stale.
    make_project(fleet_dir, "s-core")
    make_project(fleet_dir / "s-core", "nested-child")
    assert discover(FleetConfig(roots=(fleet_dir,))).names == ("s-core",)


def test_a_nested_project_listed_explicitly_is_still_discovered(fleet_dir: Path) -> None:
    # The control that proves the project is well-formed and merely unreachable,
    # so the warning is pointing at real work rather than at a broken directory.
    make_project(fleet_dir, "s-core")
    nested = make_project(fleet_dir / "s-core", "nested-child")
    assert discover(FleetConfig(projects=(nested,))).names == ("nested-child",)


def test_no_nested_warning_when_nothing_is_nested(fleet_dir: Path) -> None:
    # A warning that fires on a clean fleet is the one that gets ignored.
    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta")
    assert _nested_warning(discover(FleetConfig(roots=(fleet_dir,)))) == ""


def test_an_excluded_nested_directory_is_not_reported(fleet_dir: Path) -> None:
    make_project(fleet_dir, "s-core")
    make_project(fleet_dir / "s-core", "vendored")
    fleet = discover(FleetConfig(roots=(fleet_dir,), exclude=("vendored",)))
    assert _nested_warning(fleet) == ""


def test_a_vendored_tree_is_not_walked(fleet_dir: Path) -> None:
    # node_modules holds no governed project and plenty of directories; walking
    # it would make the accounting cost more than the scan it annotates.
    make_project(fleet_dir, "s-core")
    make_project(fleet_dir / "s-core" / "node_modules", "pkg")
    assert _nested_warning(discover(FleetConfig(roots=(fleet_dir,)))) == ""


def test_a_project_under_a_dotted_directory_is_not_reported(fleet_dir: Path) -> None:
    # A stated limitation, pinned so it is a known gap and not a surprise:
    # dotted names are skipped wholesale so `.git` and `.venv` need no entry.
    make_project(fleet_dir, "s-core")
    make_project(fleet_dir / "s-core" / ".hidden", "buried")
    assert _nested_warning(discover(FleetConfig(roots=(fleet_dir,)))) == ""


def test_a_deep_plain_repo_is_ignored_by_default(fleet_dir: Path) -> None:
    make_project(fleet_dir, "s-core")
    (fleet_dir / "s-core" / "plain" / ".git").mkdir(parents=True)
    assert _nested_warning(discover(FleetConfig(roots=(fleet_dir,)))) == ""


def test_a_deep_plain_repo_is_reported_when_plain_repos_are_included(fleet_dir: Path) -> None:
    # The hint uses discovery's own admission test, so it must track this flag.
    make_project(fleet_dir, "s-core")
    (fleet_dir / "s-core" / "plain" / ".git").mkdir(parents=True)
    fleet = discover(FleetConfig(roots=(fleet_dir,), include_plain_repos=True))
    assert "plain" in _nested_warning(fleet)


def test_the_count_is_a_lower_bound_when_the_visit_budget_runs_out(fleet_dir: Path) -> None:
    # An undercount that says it is one is honest; one that does not is worse
    # than no count at all. Filler is named `zz*` so the nested project sorts
    # BEFORE it and is therefore found before the budget is spent.
    make_project(fleet_dir, "s-core")
    make_project(fleet_dir / "s-core", "nested-child")
    for i in range(_HINT_BUDGET + 10):
        (fleet_dir / "s-core" / f"zz{i}").mkdir()
    assert "at least" in _nested_warning(discover(FleetConfig(roots=(fleet_dir,))))


def test_an_incomplete_search_is_reported_even_when_it_found_nothing(
    fleet_dir: Path,
) -> None:
    """The hole the test above uncovered in the first version of this fix.

    Filler named `d*` sorts before the nested project, so the budget is spent
    before it is reached: nothing found, search incomplete. Reporting nothing
    would put the operator back in #215's exact state — silence that cannot be
    told apart from absence. Having stopped looking is its own fact.
    """
    make_project(fleet_dir, "s-core")
    make_project(fleet_dir / "s-core", "nested-child")
    for i in range(_HINT_BUDGET + 10):
        (fleet_dir / "s-core" / f"d{i}").mkdir()
    assert "stopped looking" in " ".join(discover(FleetConfig(roots=(fleet_dir,))).warnings)
