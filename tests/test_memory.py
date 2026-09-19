"""Fleet memory reading and search."""

from __future__ import annotations

from pathlib import Path

from conftest import add_graph, add_memory, make_memory_project, make_project

from projects_orchestrator.descriptor import load_descriptor
from projects_orchestrator.memory import (
    MODE_GRAPH,
    MODE_GREP,
    MODE_RAG,
    MemoryHit,
    ProjectMemory,
    load_graph_facts,
    load_memory,
    load_project_memory,
    retrieval_mode,
    search_memory,
)


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


# --- Degrade-by-tier retrieval (project-init ADR-025 §4) ---


def _descriptor(project: Path):
    return load_descriptor(project)


def test_retrieval_mode_tier0_greps(fleet_dir: Path) -> None:
    assert retrieval_mode(_descriptor(make_project(fleet_dir, "alpha"))) == MODE_GREP


def test_retrieval_mode_tier2_with_graph_reads_graph(fleet_dir: Path) -> None:
    project = make_memory_project(fleet_dir, "alpha", tier=2, graph_path="graphify-out/graph.json")
    assert retrieval_mode(_descriptor(project)) == MODE_GRAPH


def test_retrieval_mode_tier2_without_graph_degrades_to_grep(fleet_dir: Path) -> None:
    # A tier that offers a surface the child has not declared degrades down.
    project = make_memory_project(fleet_dir, "alpha", tier=2)
    assert retrieval_mode(_descriptor(project)) == MODE_GREP


def test_retrieval_mode_tier3_with_endpoint_queries_rag(fleet_dir: Path) -> None:
    project = make_memory_project(
        fleet_dir,
        "alpha",
        tier=3,
        graph_path="graphify-out/graph.json",
        rag_endpoint="http://127.0.0.1:8099",
    )
    assert retrieval_mode(_descriptor(project)) == MODE_RAG


def test_retrieval_mode_tier3_without_endpoint_degrades_to_graph(fleet_dir: Path) -> None:
    project = make_memory_project(fleet_dir, "alpha", tier=3, graph_path="graphify-out/graph.json")
    assert retrieval_mode(_descriptor(project)) == MODE_GRAPH


def test_load_graph_facts_reads_node_names(fleet_dir: Path) -> None:
    project = make_memory_project(fleet_dir, "alpha", tier=2, graph_path="graphify-out/graph.json")
    add_graph(project, [{"name": "AuthService", "description": "handles login"}])
    assert load_graph_facts(_descriptor(project))[0].name == "AuthService"


def test_load_graph_facts_tolerates_top_level_list(fleet_dir: Path) -> None:
    project = make_memory_project(fleet_dir, "alpha", tier=2, graph_path="graphify-out/graph.json")
    (project / "graphify-out").mkdir(parents=True)
    (project / "graphify-out/graph.json").write_text(
        '[{"label": "Node A", "summary": "prose"}]', encoding="utf-8"
    )
    assert load_graph_facts(_descriptor(project))[0].name == "Node A"


def test_load_graph_facts_missing_graph_is_empty(fleet_dir: Path) -> None:
    project = make_memory_project(fleet_dir, "alpha", tier=2, graph_path="graphify-out/graph.json")
    assert load_graph_facts(_descriptor(project)) == ()


def test_load_graph_facts_malformed_json_is_empty(fleet_dir: Path) -> None:
    project = make_memory_project(fleet_dir, "alpha", tier=2, graph_path="graphify-out/graph.json")
    (project / "graphify-out").mkdir(parents=True)
    (project / "graphify-out/graph.json").write_text("{not json", encoding="utf-8")
    assert load_graph_facts(_descriptor(project)) == ()


def test_load_memory_adds_graph_facts_to_grep_baseline(fleet_dir: Path) -> None:
    project = make_memory_project(fleet_dir, "alpha", tier=2, graph_path="graphify-out/graph.json")
    add_memory(project, "project_context.md", body="grep-only fact")
    add_graph(project, [{"name": "GraphOnly", "description": "graph-only fact"}])
    names = {f.name for f in load_memory(_descriptor(project)).files}
    assert {"Fact", "GraphOnly"} <= names


def test_load_memory_search_finds_graph_only_fact(fleet_dir: Path) -> None:
    project = make_memory_project(fleet_dir, "alpha", tier=2, graph_path="graphify-out/graph.json")
    add_graph(project, [{"name": "AuthService", "description": "handles oauth login"}])
    hits = search_memory([load_memory(_descriptor(project))], "oauth")
    assert hits[0].file.project == "alpha"


def test_load_memory_tier0_is_grep_only(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "project_context.md")
    # A tier-0 read never gains a graph surface even if a stray graph exists.
    assert load_memory(_descriptor(project)).files[0].type != "graph"


# --- Both frontmatter shapes carry the same fields (#217) ---


def _raw_memory(project: Path, filename: str, text: str) -> Path:
    """Write a memory file verbatim — add_memory only emits the flat shape."""
    memory_dir = project / ".claude" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = memory_dir / filename
    path.write_text(text, encoding="utf-8")
    return path


_NESTED = """\
---
name: nested-form
description: a memory written to the documented format
metadata:
  type: reference
  tags: [a, b]
---
ZORBLAX body text
"""


def test_a_type_nested_under_metadata_is_read(fleet_dir: Path) -> None:
    # Was `unknown`: values were stringified one level deep, so the nested block
    # arrived as the repr of a dict. The flat test above is the control.
    project = make_project(fleet_dir, "alpha")
    _raw_memory(project, "nested.md", _NESTED)
    assert _memory(fleet_dir).files[0].type == "reference"


def test_a_top_level_type_wins_over_a_nested_one(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    _raw_memory(
        project,
        "both.md",
        "---\nname: n\ntype: outer\nmetadata:\n  type: inner\n---\nbody\n",
    )
    assert _memory(fleet_dir).files[0].type == "outer"


def test_a_name_nested_under_metadata_is_read(fleet_dir: Path) -> None:
    # Stronger than the type case: `name` feeds _score_metadata, so a nested
    # name meant the file ranked on body text only.
    project = make_project(fleet_dir, "alpha")
    _raw_memory(project, "nn.md", "---\nmetadata:\n  name: Deploy target\n---\nbody\n")
    assert _memory(fleet_dir).files[0].name == "Deploy target"


def test_a_description_nested_under_metadata_ranks_above_a_body_hit(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    _raw_memory(
        project,
        "nd.md",
        "---\nname: n\nmetadata:\n  description: zorblax matters here\n---\nplain body\n",
    )
    _raw_memory(project, "body.md", "---\nname: m\n---\nzorblax body\n")
    hits = search_memory([_memory(fleet_dir)], "zorblax")
    assert (hits[0].file.path.name, hits[0].line_number) == ("nd.md", 0)
    assert hits[0].score > hits[-1].score


def test_a_nested_block_of_nothing_useful_is_still_unknown_type(fleet_dir: Path) -> None:
    # The fold must not invent a type where the file declares none.
    project = make_project(fleet_dir, "alpha")
    _raw_memory(project, "empty.md", "---\nname: n\nmetadata:\n  tags: [a]\n---\nbody\n")
    assert _memory(fleet_dir).files[0].type == "unknown"


# --- Relevance ranking (#247) ---


def _corpus(fleet_dir: Path, bodies: dict[str, str]) -> list[ProjectMemory]:
    project = make_project(fleet_dir, "alpha")
    for filename, body in bodies.items():
        add_memory(project, filename, body=body)
    return [_memory(fleet_dir)]


def _order(hits: list[MemoryHit]) -> list[str]:
    return list(dict.fromkeys(h.file.path.name for h in hits))


def test_a_rare_term_outranks_a_common_one(fleet_dir: Path) -> None:
    # Same length, one occurrence each: only the corpus can tell them apart.
    bodies = {f"c{i}.md": "common filler words here" for i in range(4)}
    bodies["r.md"] = "rare filler words here"
    hits = search_memory(_corpus(fleet_dir, bodies), "common rare")
    assert _order(hits)[0] == "r.md"
    assert len(_order(hits)) == 5


def test_a_multi_word_query_matches_terms_used_apart(fleet_dir: Path) -> None:
    memories = _corpus(
        fleet_dir,
        {
            "both.md": "the descriptor changed\nand then drift followed",
            "one.md": "drift only, nothing else",
            "none.md": "unrelated note",
        },
    )
    hits = search_memory(memories, "descriptor drift")
    assert _order(hits) == ["both.md", "one.md"]


def test_a_note_with_every_term_outranks_a_short_one_with_some(fleet_dir: Path) -> None:
    # BM25 alone fails this: length normalisation scores a short one-term note
    # above a long note carrying the whole query (review on #264).
    bodies = {f"u{i}.md": "unrelated" for i in range(20)}
    bodies["long.md"] = "descriptor drift " + "padding " * 2000
    bodies["short.md"] = "drift"
    hits = search_memory(_corpus(fleet_dir, bodies), "descriptor drift")
    assert _order(hits) == ["long.md", "short.md"]
    assert [h.matched for h in hits][:1] == [2]


def test_a_short_note_outranks_a_long_one_with_the_same_count(fleet_dir: Path) -> None:
    # b > 0: one mention in a short note is stronger evidence than in a long one.
    memories = _corpus(
        fleet_dir,
        {"long.md": "zorblax " + "padding " * 40, "short.md": "zorblax here"},
    )
    assert _order(search_memory(memories, "zorblax")) == ["short.md", "long.md"]


def test_no_body_hit_carries_a_constant_score(fleet_dir: Path) -> None:
    memories = _corpus(fleet_dir, {"a.md": "zorblax zorblax zorblax", "b.md": "zorblax once"})
    scores = {h.score for h in search_memory(memories, "zorblax")}
    assert len(scores) == 2
    assert 1 not in scores


def test_substring_recall_is_kept(fleet_dir: Path) -> None:
    # Ranking changed; what is found did not. The old scan matched substrings.
    memories = _corpus(fleet_dir, {"a.md": "Uses PostgreSQL 16."})
    assert len(search_memory(memories, "postgres")) == 1
