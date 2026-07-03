"""Descriptor parsing: contract v1 in, dataclass out, never raise."""

from __future__ import annotations

from pathlib import Path

from conftest import make_project

from projects_orchestrator.descriptor import (
    load_descriptor,
    parse_config,
    parse_scaffold_version,
)


def test_parse_scaffold_version_reads_dotted_integers() -> None:
    assert parse_scaffold_version("0.5.2") == (0, 5, 2)


def test_parse_scaffold_version_unknown_is_none() -> None:
    assert parse_scaffold_version("unknown") is None


def test_parse_scaffold_version_non_numeric_is_none() -> None:
    assert parse_scaffold_version("1.2.beta") is None


def test_parse_scaffold_version_orders_by_component() -> None:
    assert parse_scaffold_version("0.6.0") > parse_scaffold_version("0.5.9")


def test_load_descriptor_reads_name(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    assert load_descriptor(project).name == "alpha"


def test_load_descriptor_reads_tooling_commands(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"lint": "true", "test": "false"})
    assert load_descriptor(project).tooling == {"lint": "true", "test": "false"}


def test_load_descriptor_reads_contract_version(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    assert load_descriptor(project).contract_version == 1


def test_load_descriptor_resolves_memory_path(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    assert load_descriptor(project).memory_path == project.resolve() / ".claude/memory"


def test_load_descriptor_returns_none_for_non_project(tmp_path: Path) -> None:
    assert load_descriptor(tmp_path) is None


def test_parse_config_invalid_yaml_degrades_with_warning(tmp_path: Path) -> None:
    descriptor = parse_config("{unclosed: [", tmp_path)
    assert descriptor.warnings != ()


def test_parse_config_invalid_yaml_falls_back_to_dir_name(tmp_path: Path) -> None:
    descriptor = parse_config("{unclosed: [", tmp_path / "beta")
    assert descriptor.name == "beta"


def test_parse_config_empty_file_warns(tmp_path: Path) -> None:
    assert parse_config("", tmp_path).warnings == ("config.yaml is empty",)


def test_parse_config_ignores_blank_tooling_commands(tmp_path: Path) -> None:
    descriptor = parse_config('tooling:\n  lint_command: "  "\n', tmp_path)
    assert descriptor.has_task("lint") is False


def test_has_task_true_for_declared_command(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"run": "echo hi"})
    assert load_descriptor(project).has_task("run") is True


def test_parse_config_tolerates_non_string_tooling_key(tmp_path: Path) -> None:
    # PyYAML coerces a bare `on:` key to the bool True; must not crash.
    descriptor = parse_config('tooling:\n  on: echo hi\n  lint_command: "ruff check"\n', tmp_path)
    assert descriptor.has_task("lint") is True
