"""Extra memory sources from the fleet file are searched and ranked with the projects' (#247)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import add_memory, make_project

from projects_orchestrator.__main__ import main
from projects_orchestrator.controller import ControllerContext, Intent, dispatch
from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.memory import (
    load_memory_sources,
    load_project_memory,
    memory_source_label,
    search_memory,
)
from projects_orchestrator.registry import FleetConfig, load_fleet_config

_NOTE = """\
---
name: {name}
description: {description}
type: reference
---

{body}
"""


def _source(tmp_path: Path, notes: dict[str, tuple[str, str]]) -> Path:
    """A memory directory outside every project, one file per note."""
    source = tmp_path / "box-memory"
    source.mkdir()
    for filename, (name, body) in notes.items():
        text = _NOTE.format(name=name, description=name, body=body)
        (source / filename).write_text(text, encoding="utf-8")
    (source / "MEMORY.md").write_text("- index, never a fact\n", encoding="utf-8")
    return source


def test_a_source_is_read_by_the_memory_loader(tmp_path: Path) -> None:
    source = _source(tmp_path, {"a.md": ("Deploy", "the deploy runs nightly")})
    [memory] = load_memory_sources((source,))
    assert [f.name for f in memory.files] == ["Deploy"]
    assert memory.warnings == ()


def test_a_hit_from_a_source_says_where_it_came_from(tmp_path: Path) -> None:
    source = _source(tmp_path, {"a.md": ("Deploy", "the deploy runs nightly")})
    [hit] = search_memory(load_memory_sources((source,)), "nightly")
    assert hit.file.project == memory_source_label(source)
    assert "/" in hit.file.project, "a label with a slash can never be a project's name"


def test_a_source_under_home_is_labelled_with_a_tilde() -> None:
    assert memory_source_label(Path.home() / "notes" / "memory") == "~/notes/memory"
    assert memory_source_label(Path("/srv/memory")) == "/srv/memory"


def test_a_missing_source_warns_and_names_it(tmp_path: Path) -> None:
    missing = tmp_path / "gone"
    [memory] = load_memory_sources((missing,))
    assert memory.files == ()
    assert memory.warnings == (
        f"memory source {memory_source_label(missing)} is not a readable directory — skipped",
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root can list any directory")
def test_an_unreadable_source_warns_rather_than_reading_as_empty(tmp_path: Path) -> None:
    source = _source(tmp_path, {"a.md": ("Deploy", "nightly")})
    source.chmod(0o000)
    try:
        [memory] = load_memory_sources((source,))
    finally:
        source.chmod(0o755)
    assert memory.files == ()
    assert "not a readable directory" in memory.warnings[0]


def test_ranking_applies_across_projects_and_sources(fleet_dir: Path, tmp_path: Path) -> None:
    # "common" is in three project notes, "rare" only in the source's one. Each
    # note matches one term, so BM25's IDF decides: the rare term ranks first,
    # which it can only do if the source is part of the same corpus.
    project = make_project(fleet_dir, "alpha")
    for n in range(3):
        add_memory(project, f"n{n}.md", name=f"Note {n}", body="a common word")
    source = _source(tmp_path, {"r.md": ("Rare", "a rare word")})
    memories = [load_project_memory(load_descriptor(project)), *load_memory_sources((source,))]
    hits = search_memory(memories, "common rare")
    assert hits[0].file.project == memory_source_label(source)
    assert hits[0].score > hits[-1].score


def test_a_source_that_is_a_projects_own_memory_is_read_once(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "a.md", name="Deploy", body="nightly")
    descriptor = load_descriptor(project)
    assert load_memory_sources((descriptor.memory_path,), (descriptor.memory_path,)) == []


def test_the_fleet_file_expands_a_tilde(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text("memory_sources:\n  - ~/notes\n  - rel\n", encoding="utf-8")
    config = load_fleet_config(fleet_file)
    assert config.memory_sources == (Path.home() / "notes", (tmp_path / "rel").resolve())


def test_the_cli_searches_a_source_and_warns_on_a_missing_one(
    fleet_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_project(fleet_dir, "alpha")
    source = _source(tmp_path, {"a.md": ("Deploy", "the deploy runs nightly")})
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(
        f"roots:\n  - {fleet_dir}\nmemory_sources:\n  - {source}\n  - {tmp_path / 'gone'}\n",
        encoding="utf-8",
    )
    assert main(["memory", "nightly", "--fleet", str(fleet_file)]) == 0
    out = capsys.readouterr()
    assert f"{memory_source_label(source)}/a.md:" in out.out
    assert "memory source" in out.err and "gone" in out.err


def test_the_repl_searches_a_source(fleet_dir: Path, tmp_path: Path) -> None:
    make_project(fleet_dir, "alpha")
    source = _source(tmp_path, {"a.md": ("Deploy", "the deploy runs nightly")})
    ctx = ControllerContext(config=FleetConfig(roots=(fleet_dir,), memory_sources=(source,)))
    lines = list(dispatch(Intent(verb="memory", args=("nightly",)), ctx))
    assert any(line.startswith(f"{memory_source_label(source)}/a.md:") for line in lines)
