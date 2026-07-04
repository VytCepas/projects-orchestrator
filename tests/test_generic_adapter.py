"""Generic adapter: plain git repos inferred conservatively, opt-in only."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import git_init, make_project

from projects_orchestrator.adapters.generic import infer_descriptor
from projects_orchestrator.fleet import fleet_rows, fleet_snapshots
from projects_orchestrator.registry import FleetConfig, discover, load_fleet_config


def _plain_repo(base: Path, name: str, files: dict[str, str] | None = None) -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    for relpath, text in (files or {}).items():
        (repo / relpath).write_text(text, encoding="utf-8")
    git_init(repo, commit=False)
    return repo


def test_infer_descriptor_non_git_is_none(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    assert infer_descriptor(tmp_path / "plain") is None


def test_infer_descriptor_names_from_directory(fleet_dir: Path) -> None:
    repo = _plain_repo(fleet_dir, "legacy")
    assert infer_descriptor(repo).name == "legacy"


def test_infer_descriptor_carries_warning(fleet_dir: Path) -> None:
    repo = _plain_repo(fleet_dir, "legacy")
    assert "inferred" in infer_descriptor(repo).warnings[0]


def test_infer_python_language(fleet_dir: Path) -> None:
    repo = _plain_repo(fleet_dir, "py", {"pyproject.toml": "[project]\nname='py'\n"})
    assert infer_descriptor(repo).language == "python"


def test_infer_python_test_command(fleet_dir: Path) -> None:
    repo = _plain_repo(fleet_dir, "py", {"pyproject.toml": "[project]\nname='py'\n"})
    assert infer_descriptor(repo).tooling["test"] == "pytest"


def test_infer_rust_commands(fleet_dir: Path) -> None:
    repo = _plain_repo(fleet_dir, "rs", {"Cargo.toml": "[package]\nname='rs'\n"})
    assert infer_descriptor(repo).tooling == {"lint": "cargo clippy", "test": "cargo test"}


def test_infer_go_commands(fleet_dir: Path) -> None:
    repo = _plain_repo(fleet_dir, "gomod", {"go.mod": "module example.com/gomod\n"})
    assert infer_descriptor(repo).tooling["test"] == "go test ./..."


def test_infer_npm_scripts_only_when_declared(fleet_dir: Path) -> None:
    manifest = json.dumps({"scripts": {"test": "vitest", "start": "node ."}})
    repo = _plain_repo(fleet_dir, "js", {"package.json": manifest})
    assert infer_descriptor(repo).tooling == {"test": "npm run test"}


def test_infer_npm_bad_json_yields_no_commands(fleet_dir: Path) -> None:
    repo = _plain_repo(fleet_dir, "js", {"package.json": "{broken"})
    assert infer_descriptor(repo).tooling == {}


def test_infer_justfile_wins_over_ecosystem(fleet_dir: Path) -> None:
    repo = _plain_repo(
        fleet_dir,
        "py",
        {"pyproject.toml": "[project]\nname='py'\n", "justfile": "test:\n    pytest -q\n"},
    )
    assert infer_descriptor(repo).tooling["test"] == "just test"


def test_infer_justfile_ignores_assignments(fleet_dir: Path) -> None:
    repo = _plain_repo(fleet_dir, "j", {"justfile": "version := '1'\nlint:\n    ruff check\n"})
    assert infer_descriptor(repo).tooling == {"lint": "just lint"}


def test_infer_no_manifest_means_no_commands(fleet_dir: Path) -> None:
    repo = _plain_repo(fleet_dir, "bare")
    assert infer_descriptor(repo).tooling == {}


def test_discover_ignores_plain_repos_by_default(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    _plain_repo(fleet_dir, "legacy")
    fleet = discover(FleetConfig(roots=(fleet_dir,)))
    assert fleet.names == ("alpha",)


def test_discover_includes_plain_repos_when_enabled(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    _plain_repo(fleet_dir, "legacy")
    fleet = discover(FleetConfig(roots=(fleet_dir,), include_plain_repos=True))
    assert fleet.names == ("alpha", "legacy")


def test_discover_still_ignores_non_git_dirs_when_enabled(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    (fleet_dir / "downloads").mkdir()
    fleet = discover(FleetConfig(roots=(fleet_dir,), include_plain_repos=True))
    assert fleet.names == ("alpha",)


def test_fleet_yaml_parses_include_plain_repos(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text("roots: ['.']\ninclude_plain_repos: true\n", encoding="utf-8")
    assert load_fleet_config(fleet_file).include_plain_repos is True


def test_fleet_yaml_include_plain_repos_defaults_false(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text("roots: ['.']\n", encoding="utf-8")
    assert load_fleet_config(fleet_file).include_plain_repos is False


def test_inferred_project_renders_in_status_table(fleet_dir: Path, tmp_path: Path) -> None:
    _plain_repo(fleet_dir, "legacy", {"pyproject.toml": "[project]\nname='x'\n"})
    fleet = discover(FleetConfig(roots=(fleet_dir,), include_plain_repos=True))
    rows = fleet_rows(fleet_snapshots(fleet, tmp_path / "checks.json"))
    assert rows[0]["Contract"] == "none"
