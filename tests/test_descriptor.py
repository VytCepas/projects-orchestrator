"""Tests for parsing a project's descriptor contract from .claude/config.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest

from projects_orchestrator.descriptor import (
    DescriptorError,
    ProjectDescriptor,
    load_descriptor,
)


def _config(root: Path) -> Path:
    return root / ".claude" / "config.yaml"


def test_load_reads_name(make_project):
    root = make_project("alpha")
    assert load_descriptor(_config(root)).name == "alpha"


def test_load_reads_description(make_project):
    root = make_project("alpha", description="Orchestrates things")
    assert load_descriptor(_config(root)).description == "Orchestrates things"


def test_load_reads_language(make_project):
    root = make_project("alpha", language="go")
    assert load_descriptor(_config(root)).language == "go"


def test_load_reads_delivery(make_project):
    root = make_project("alpha", delivery="service")
    assert load_descriptor(_config(root)).delivery == "service"


def test_load_reads_project_init_version(make_project):
    root = make_project("alpha", project_init_version="0.5.2")
    assert load_descriptor(_config(root)).project_init_version == "0.5.2"


def test_load_reads_contract_version(make_project):
    root = make_project("alpha", contract_version=1)
    assert load_descriptor(_config(root)).contract_version == 1


def test_load_defaults_contract_version_to_zero_when_absent(make_project):
    root = make_project("alpha", contract_version=None)
    assert load_descriptor(_config(root)).contract_version == 0


def test_load_reads_memory_tier(make_project):
    root = make_project("alpha", memory_tier=2)
    assert load_descriptor(_config(root)).memory.tier == 2


def test_load_reads_memory_stack(make_project):
    root = make_project("alpha", memory_stack="obsidian")
    assert load_descriptor(_config(root)).memory.stack == "obsidian"


def test_load_resolves_memory_path_absolute(make_project):
    root = make_project("alpha")
    expected = (root / ".claude" / "memory").resolve()
    assert load_descriptor(_config(root)).memory.path == expected


def test_load_detects_memory_index_present(make_project):
    root = make_project("alpha", with_memory_index=True)
    assert load_descriptor(_config(root)).memory.has_index is True


def test_load_detects_memory_index_absent(make_project):
    root = make_project("alpha", with_memory_index=False)
    assert load_descriptor(_config(root)).memory.has_index is False


def test_load_reads_installed_mcps(make_project):
    root = make_project("alpha", mcps=["context7", "playwright"])
    assert load_descriptor(_config(root)).mcps == ("context7", "playwright")


def test_load_reports_empty_mcps_as_empty_tuple(make_project):
    root = make_project("alpha", mcps=[])
    assert load_descriptor(_config(root)).mcps == ()


def test_load_resolves_project_root(make_project):
    root = make_project("alpha")
    assert load_descriptor(_config(root)).root == root.resolve()


def test_load_raises_when_config_missing(tmp_path):
    with pytest.raises(DescriptorError):
        load_descriptor(tmp_path / ".claude" / "config.yaml")


def test_load_raises_on_malformed_yaml(tmp_path):
    config = tmp_path / ".claude" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("project: [unbalanced\n", encoding="utf-8")
    with pytest.raises(DescriptorError):
        load_descriptor(config)


def test_load_raises_when_name_missing(tmp_path):
    config = tmp_path / ".claude" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("language: python\n", encoding="utf-8")
    with pytest.raises(DescriptorError):
        load_descriptor(config)


def test_descriptor_is_frozen(make_project):
    descriptor = load_descriptor(_config(make_project("alpha")))
    with pytest.raises(AttributeError):
        descriptor.name = "beta"  # type: ignore[misc]


def test_descriptor_exposes_raw_config(make_project):
    descriptor = load_descriptor(_config(make_project("alpha")))
    assert descriptor.raw["project"]["name"] == "alpha"


def test_descriptor_summary_line_includes_name(make_project):
    descriptor: ProjectDescriptor = load_descriptor(_config(make_project("alpha")))
    assert "alpha" in descriptor.summary()
