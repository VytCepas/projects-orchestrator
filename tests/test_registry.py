"""Fleet discovery: roots, explicit paths, exclusions, degradation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import make_project

from projects_orchestrator.registry import (
    _HINT_BUDGET,
    FleetConfig,
    _git_dirs,
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


def test_one_unreadable_project_does_not_empty_the_fleet(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blast radius is the point (#210). ``discover`` calls
    ``resolve_config`` inside its loop, so an unguarded stat on ONE project
    aborted the loop and took every healthy repo with it — ``doctor`` printed
    nothing at all and the fleet read like a clean one.

    The error is injected rather than chmod-ed because CPython 3.14 swallows
    EACCES in pathlib: a chmod-based version of this test passes on this
    repo's own venv whether or not the bug is present.
    """
    make_project(fleet_dir, "healthy")
    broken = make_project(fleet_dir, "broken")
    real_is_symlink = Path.is_symlink

    def only_broken_is_unreadable(self: Path) -> bool:
        if broken.name in self.parts:
            raise PermissionError(13, "Permission denied")
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", only_broken_is_unreadable)
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    assert fleet.names == ("healthy",)


def test_a_scanned_project_that_stops_resolving_is_named(fleet_dir: Path) -> None:
    """#211: the warning existed but sat in the `config.projects` arm, so it
    could only fire for a path someone had listed by hand — the one case where
    the operator already knows the path exists."""
    make_project(fleet_dir, "healthy")
    broken = make_project(fleet_dir, "broken", layout=".agents")
    (broken / ".agents" / "config.yaml").unlink()
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    assert any(str(broken) in w and "no readable config.yaml" in w for w in fleet.warnings)


def test_a_healthy_sibling_is_still_listed_when_one_drops_out(fleet_dir: Path) -> None:
    """Silence was invisible precisely because the fleet stayed non-empty: the
    'no projects discovered' hint cannot fire, so every verb returned success
    with the project simply absent."""
    make_project(fleet_dir, "healthy")
    broken = make_project(fleet_dir, "broken", layout=".agents")
    (broken / ".agents" / "config.yaml").unlink()
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    assert fleet.names == ("healthy",)


def test_an_ordinary_directory_is_not_warned_about(fleet_dir: Path) -> None:
    """The false-positive guard (§2.11). `_scan_root` yields EVERY directory one
    level under a root, so warning whenever there is no descriptor would fire on
    every folder beside the fleet — and a warning that fires on a correctly
    configured fleet is the one that gets switched off."""
    make_project(fleet_dir, "healthy")
    (fleet_dir / "just-a-folder").mkdir()
    (fleet_dir / "another-folder").mkdir()
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    # Asserts on the DIRECTORY NAMES, not on the message text. Matching the
    # text only proves this particular sentence is absent, which a differently
    # worded flood would satisfy — and the flood is what this guards.
    assert not any("just-a-folder" in w or "another-folder" in w for w in fleet.warnings)


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


def test_a_deep_linked_worktree_is_reported(fleet_dir: Path) -> None:
    # A LINKED WORKTREE STORES `.git` AS A FILE, not a directory. The hint read
    # `.is_dir()` while discovery read `.exists()`, so this one repo was
    # admitted by discovery and skipped by the accounting — left exactly as
    # silent as before #215, which is the defect, not an edge of it.
    make_project(fleet_dir, "s-core")
    worktree = fleet_dir / "s-core" / "wt"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n", encoding="utf-8")
    fleet = discover(FleetConfig(roots=(fleet_dir,), include_plain_repos=True))
    assert "wt" in _nested_warning(fleet)


def test_a_nested_project_already_listed_is_not_called_undiscovered(
    fleet_dir: Path,
) -> None:
    # The warning fired from inside the scan, before anything was reconciled,
    # so a correctly configured fleet was told to go and list a project that
    # was already in it. A warning that fires on a correct config is the one
    # that gets the whole hint switched off.
    make_project(fleet_dir, "s-core")
    nested = make_project(fleet_dir / "s-core", "nested-child")
    fleet = discover(FleetConfig(roots=(fleet_dir,), projects=(nested,)))
    assert "nested-child" in fleet.names
    assert _nested_warning(fleet) == ""


def test_a_nested_project_reached_by_another_root_is_not_called_undiscovered(
    fleet_dir: Path,
) -> None:
    # Same false positive by the other route: overlapping roots, where the
    # second root finds at depth one what the first can only see at depth two.
    make_project(fleet_dir, "s-core")
    make_project(fleet_dir / "s-core", "nested-child")
    fleet = discover(FleetConfig(roots=(fleet_dir, fleet_dir / "s-core")))
    assert "nested-child" in fleet.names
    assert _nested_warning(fleet) == ""


def test_only_the_unreachable_half_is_reported(fleet_dir: Path) -> None:
    # The control that keeps the two above from degenerating into "never warn":
    # the subtraction has to be a set difference, not an off switch.
    make_project(fleet_dir, "s-core")
    listed = make_project(fleet_dir / "s-core", "governed")
    make_project(fleet_dir / "s-core", "orphan")
    warning = _nested_warning(discover(FleetConfig(roots=(fleet_dir,), projects=(listed,))))
    assert "orphan" in warning
    assert "governed" not in warning


def test_an_ordinary_nested_clone_is_still_reported(fleet_dir: Path) -> None:
    # The control for the worktree fix: `exists()` must still admit the
    # directory form, or the fix trades one blind spot for the other.
    make_project(fleet_dir, "s-core")
    (fleet_dir / "s-core" / "clone" / ".git").mkdir(parents=True)
    fleet = discover(FleetConfig(roots=(fleet_dir,), include_plain_repos=True))
    assert "clone" in _nested_warning(fleet)


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


def test_two_concurrent_registrations_both_land(tmp_path: Path) -> None:
    """#182: `register` is a read-modify-write of the file that DEFINES the
    fleet. Unlocked, two concurrent calls both loaded the old list, both rewrote
    it whole, and the second silently discarded the first project — a lost
    update that un-manages a repo with no signal anywhere.
    """
    import threading

    fleet_file = tmp_path / "fleet.yaml"
    projects = [make_project(tmp_path / f"root{i}", f"proj{i}") for i in range(8)]

    def register(project: Path) -> None:
        register_project(fleet_file, project)

    threads = [threading.Thread(target=register, args=(p,)) for p in projects]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    listed = load_fleet_config(fleet_file).projects
    assert len(listed) == len(projects)


def test_registering_writes_through_a_symlinked_fleet_file(tmp_path: Path) -> None:
    """Raised in review on #240. `atomic_write` replaces a directory entry, so a
    symlinked --fleet would have had the LINK replaced by a regular file:
    registration reports success, the link is gone, and the canonical file it
    pointed at still holds the old list. `write_text` followed the link, so this
    was a regression the hardening introduced."""
    canonical = tmp_path / "canonical.yaml"
    canonical.write_text("projects: []\n", encoding="utf-8")
    link = tmp_path / "fleet.yaml"
    link.symlink_to(canonical)

    project = make_project(tmp_path / "elsewhere", "gamma")
    register_project(link, project)

    assert link.is_symlink(), "the symlink must survive the write"
    assert str(project.resolve()) in canonical.read_text(encoding="utf-8")


# --- #260: a scanned worktree of a repo the fleet holds is not another project ---------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _repo_with_worktree(repo: Path, worktree: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("x", encoding="utf-8")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "worktree", "add", "-q", "-b", "task", str(worktree))


def _names(config: FleetConfig) -> list[str]:
    return sorted(d.path.name for d in discover(config).descriptors)


def test_a_worktree_beside_its_repo_is_not_a_second_project(tmp_path: Path) -> None:
    _repo_with_worktree(tmp_path / "repo", tmp_path / "repo-wt-task")
    config = FleetConfig(roots=(tmp_path,), include_plain_repos=True)
    assert _names(config) == ["repo"]


def test_a_worktree_whose_main_checkout_is_outside_the_fleet_is_kept(tmp_path: Path) -> None:
    # It is the only checkout the fleet can see, so dropping it would lose the project.
    _repo_with_worktree(tmp_path / "elsewhere" / "repo", tmp_path / "root" / "repo-wt-task")
    config = FleetConfig(roots=(tmp_path / "root",), include_plain_repos=True)
    assert _names(config) == ["repo-wt-task"]


def test_a_worktree_listed_explicitly_is_kept(tmp_path: Path) -> None:
    _repo_with_worktree(tmp_path / "repo", tmp_path / "repo-wt-task")
    config = FleetConfig(
        roots=(tmp_path,), projects=(tmp_path / "repo-wt-task",), include_plain_repos=True
    )
    assert _names(config) == ["repo", "repo-wt-task"]


def test_a_bare_repositorys_worktree_is_kept(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _repo_with_worktree(src, tmp_path / "src-wt")
    _git(tmp_path, "clone", "-q", "--bare", str(src), str(tmp_path / "root" / "proj.git"))
    _git(tmp_path / "root" / "proj.git", "worktree", "add", "-q", str(tmp_path / "root" / "proj"))
    config = FleetConfig(roots=(tmp_path / "root",), include_plain_repos=True)
    assert "proj" in _names(config)


def test_a_separate_git_dir_repos_worktree_is_not_a_second_project(tmp_path: Path) -> None:
    # Codex on #261: `git init --separate-git-dir` puts the worktrees under
    # <git-dir>/worktrees/, with no `.git` component in the pointer.
    repo = tmp_path / "root" / "repo"
    repo.parent.mkdir()
    _git(
        tmp_path, "init", "-q", "-b", "main", "--separate-git-dir", str(tmp_path / "gd"), str(repo)
    )
    (repo / "f").write_text("x", encoding="utf-8")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "worktree", "add", "-q", "-b", "task", str(tmp_path / "root" / "repo-wt"))
    config = FleetConfig(roots=(tmp_path / "root",), include_plain_repos=True)
    assert _names(config) == ["repo"]


def test_git_dirs_tells_a_linked_worktree_from_a_main_checkout(tmp_path: Path) -> None:
    _repo_with_worktree(tmp_path / "repo", tmp_path / "wt")
    main = _git_dirs(tmp_path / "repo")
    linked = _git_dirs(tmp_path / "wt")
    assert main is not None and linked is not None
    assert main[0] == main[1]  # a main checkout: one directory for both
    assert linked[0] != linked[1] and linked[1] == main[1]  # same repository
    sub = tmp_path / "sub"
    (tmp_path / "modgit").mkdir()
    sub.mkdir()
    (sub / ".git").write_text(f"gitdir: {tmp_path / 'modgit'}\n", encoding="utf-8")
    got = _git_dirs(sub)
    assert got is not None and got[0] == got[1]  # a submodule reads as a main checkout


def test_a_worktree_is_kept_when_its_main_checkout_is_not_admitted(tmp_path: Path) -> None:
    # Codex on #261: without include_plain_repos, a main checkout with no descriptor is
    # rejected; if only the worktree branch carries one, suppressing the worktree too
    # dropped the repository from the fleet entirely.
    _repo_with_worktree(tmp_path / "repo", tmp_path / "repo-wt-task")
    agents = tmp_path / "repo-wt-task" / ".agents"
    agents.mkdir()
    (agents / "config.yaml").write_text("project:\n  name: repo\n", encoding="utf-8")
    config = FleetConfig(roots=(tmp_path,), include_plain_repos=False)
    assert _names(config) == ["repo-wt-task"]


def test_a_nested_worktree_of_a_governed_repo_is_not_warned_about(tmp_path: Path) -> None:
    # Codex on #261: the nested-project hint must apply the same dedupe, or it tells
    # the operator to list a second checkout of a repository already in the fleet.
    _repo_with_worktree(tmp_path / "repo", tmp_path / "wts" / "repo-wt")
    fleet = discover(FleetConfig(roots=(tmp_path,), include_plain_repos=True))
    assert [d.path.name for d in fleet.descriptors] == ["repo"]
    assert not [w for w in fleet.warnings if "NOT discovered" in w], fleet.warnings


def test_an_explicit_worktree_suppresses_its_scanned_sibling(tmp_path: Path) -> None:
    # Codex on #261: two worktrees of one repository, one listed explicitly and one
    # scanned, with no main checkout in the fleet: the scanned one is a duplicate.
    _repo_with_worktree(tmp_path / "elsewhere" / "repo", tmp_path / "root" / "wt-a")
    _git(
        tmp_path / "elsewhere" / "repo",
        "worktree",
        "add",
        "-q",
        "-b",
        "b",
        str(tmp_path / "root" / "wt-b"),
    )
    config = FleetConfig(
        roots=(tmp_path / "root",), projects=(tmp_path / "root" / "wt-a",), include_plain_repos=True
    )
    assert _names(config) == ["wt-a"]


def test_a_gitdir_through_a_symlink_loop_does_not_abort_discovery(tmp_path: Path) -> None:
    # Codex on #261: on Python 3.11 resolve() raises RuntimeError on a loop, which the
    # OSError handler did not catch, and one bad pointer emptied the whole fleet.
    import pathlib
    from unittest import mock

    _repo_with_worktree(tmp_path / "repo", tmp_path / "repo-wt")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / ".git").write_text("gitdir: loop/worktrees/x\n", encoding="utf-8")
    real = pathlib.Path.resolve

    def resolve(self, *a, **k):  # the 3.11 behaviour, on this one path only
        if "loop" in str(self):
            raise RuntimeError("Symlink loop from 'loop'")
        return real(self, *a, **k)

    with mock.patch.object(pathlib.Path, "resolve", resolve):
        assert _git_dirs(bad) is None
        assert "repo" in _names(FleetConfig(roots=(tmp_path,), include_plain_repos=True))
