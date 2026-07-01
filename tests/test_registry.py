"""Tests for discovering and indexing project-init projects under a root."""

from __future__ import annotations

from projects_orchestrator.registry import Registry, discover_projects


def test_discover_finds_single_project(make_project):
    make_project("alpha")
    root = make_project("alpha").parent
    assert len(discover_projects(root)) == 1


def test_discover_finds_multiple_projects(make_project):
    make_project("alpha")
    make_project("beta")
    root = make_project("gamma").parent
    assert len(discover_projects(root)) == 3


def test_discover_returns_sorted_by_name(make_project):
    make_project("gamma")
    make_project("alpha")
    root = make_project("beta").parent
    names = [d.name for d in discover_projects(root)]
    assert names == ["alpha", "beta", "gamma"]


def test_discover_skips_malformed_project(make_project, tmp_path):
    make_project("good")
    broken = tmp_path / "broken" / ".claude"
    broken.mkdir(parents=True)
    (broken / "config.yaml").write_text("project: [bad\n", encoding="utf-8")
    assert [d.name for d in discover_projects(tmp_path)] == ["good"]


def test_discover_returns_empty_for_barren_root(tmp_path):
    assert discover_projects(tmp_path) == []


def test_discover_finds_project_at_root_itself(make_project, tmp_path):
    make_project("alpha", under=tmp_path)
    assert len(discover_projects(tmp_path / "alpha")) == 1


def test_registry_discover_builds_from_root(make_project):
    make_project("alpha")
    root = make_project("beta").parent
    assert len(Registry.discover(root).projects) == 2


def test_registry_names_lists_projects(make_project):
    make_project("alpha")
    root = make_project("beta").parent
    assert Registry.discover(root).names() == ["alpha", "beta"]


def test_registry_get_returns_named_project(make_project):
    make_project("alpha")
    root = make_project("beta").parent
    assert Registry.discover(root).get("alpha").name == "alpha"


def test_registry_get_returns_none_for_unknown(make_project):
    root = make_project("alpha").parent
    assert Registry.discover(root).get("missing") is None


def test_registry_by_language_filters(make_project):
    make_project("py-one", language="python")
    make_project("go-one", language="go")
    root = make_project("py-two", language="python").parent
    assert {d.name for d in Registry.discover(root).by_language("python")} == {
        "py-one",
        "py-two",
    }


def test_registry_len_reports_project_count(make_project):
    make_project("alpha")
    root = make_project("beta").parent
    assert len(Registry.discover(root)) == 2


def test_registry_iterates_projects(make_project):
    root = make_project("alpha").parent
    assert [d.name for d in Registry.discover(root)] == ["alpha"]
