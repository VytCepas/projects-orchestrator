"""Fleet memory reading and search."""

from __future__ import annotations

from pathlib import Path

from conftest import add_memory, make_project

from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.memory import load_project_memory, search_memory


def _memory(fleet_dir: Path, name: str = "alpha"):
    return load_project_memory(load_descriptor(fleet_dir / name))


def test_load_project_memory_parses_frontmatter_name(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "project_context.md", name="Deploy target")
    assert _memory(fleet_dir).files[0].name == "Deploy target"


def test_load_project_memory_parses_type(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "feedback_style.md", type_="feedback")
    assert _memory(fleet_dir).files[0].type == "feedback"


def test_load_project_memory_excludes_index_files(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "project_context.md")
    assert [f.path.name for f in _memory(fleet_dir).files] == ["project_context.md"]


def test_load_project_memory_reports_index_present(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "project_context.md")
    assert _memory(fleet_dir).index_present is True


def test_load_project_memory_missing_dir_warns(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _memory(fleet_dir).warnings == ("no memory directory",)


def test_load_project_memory_file_without_frontmatter_is_unknown_type(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    memory_dir = project / ".claude" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "loose.md").write_text("just text", encoding="utf-8")
    assert _memory(fleet_dir).files[0].type == "unknown"


def test_search_memory_finds_body_line(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "project_context.md", body="**Why:** we deploy to fly.io.")
    hits = search_memory([_memory(fleet_dir)], "fly.io")
    assert "fly.io" in hits[0].line


def test_search_memory_ranks_name_hits_first(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "project_context.md", name="Database choice", body="database is postgres")
    hits = search_memory([_memory(fleet_dir)], "database")
    assert hits[0].line_number == 0


def test_search_memory_is_case_insensitive(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "project_context.md", body="Uses PostgreSQL 16.")
    assert len(search_memory([_memory(fleet_dir)], "postgresql")) == 1


def test_search_memory_empty_query_returns_nothing(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "project_context.md")
    assert search_memory([_memory(fleet_dir)], "  ") == []


def test_search_memory_spans_projects(fleet_dir: Path) -> None:
    add_memory(make_project(fleet_dir, "alpha"), "project_context.md", body="shared-token here")
    add_memory(make_project(fleet_dir, "beta"), "project_context.md", body="shared-token too")
    memories = [_memory(fleet_dir, "alpha"), _memory(fleet_dir, "beta")]
    assert {h.file.project for h in search_memory(memories, "shared-token")} == {"alpha", "beta"}
