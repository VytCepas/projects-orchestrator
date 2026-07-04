"""Fleet audit: composed governance probes, never raise, warn+ needs attention."""

from __future__ import annotations

from pathlib import Path

from conftest import add_memory, make_project

from projects_orchestrator.audit import audit_project, render_markdown
from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import load_descriptor


def _report(fleet_dir: Path, cached: dict | None = None, name: str = "alpha"):
    return audit_project(load_descriptor(fleet_dir / name), cached)


def _find(report, category: str, message_contains: str = ""):
    return [f for f in report.findings if f.category == category and message_contains in f.message]


def test_audit_includes_conformance_categories(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    categories = {f.category for f in _report(fleet_dir).findings}
    assert {"config", "contract", "scaffold", "hooks", "tooling"} <= categories


def test_audit_flags_empty_memory_template(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "user_role.md", body="<!-- Fill in: role -->")
    assert _find(_report(fleet_dir), "memory", "empty template")


def test_audit_accepts_filled_memory(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "project_context.md", body="- **Fact:** uses postgres.")
    assert not _find(_report(fleet_dir), "memory", "empty template")


def test_audit_flags_unindexed_memory_file(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    add_memory(project, "project_context.md", body="- **Fact:** indexed one.")
    (project / ".claude" / "memory" / "orphan.md").write_text(
        "---\nname: Orphan\ntype: project\n---\n\n- **Fact:** not indexed.\n", encoding="utf-8"
    )
    assert _find(_report(fleet_dir), "memory", "not indexed")


def test_audit_unindexed_check_uses_filename_boundary(fleet_dir: Path) -> None:
    # "a.md" is a substring of the indexed "data.md"; a plain substring test
    # would wrongly consider a.md indexed. The boundary match must still warn.
    project = make_project(fleet_dir, "alpha")
    memory_dir = project / ".claude" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = "---\nname: N\ntype: project\n---\n\n- **Fact:** x.\n"
    (memory_dir / "data.md").write_text(frontmatter, encoding="utf-8")
    (memory_dir / "a.md").write_text(frontmatter, encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text("- [Data](data.md)\n", encoding="utf-8")
    messages = [f.message for f in _find(_report(fleet_dir), "memory", "not indexed")]
    assert any(m.startswith("a.md:") for m in messages)
    assert not any(m.startswith("data.md:") for m in messages)


def test_audit_freshness_never_checked_warns(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _find(_report(fleet_dir), "freshness", "never checked")


def test_audit_freshness_ok_with_cached_result(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    cached = {"lint": CheckResult(project="alpha", task="lint", status="pass")}
    assert _find(_report(fleet_dir, cached), "freshness")[0].severity == "ok"


def test_audit_report_needs_attention_on_warn(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _report(fleet_dir).needs_attention is True


def test_audit_report_status_fail_when_conformance_fails(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="project:\n  name: alpha\n")
    assert _report(fleet_dir).status == "fail"


def test_render_markdown_has_project_heading(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert "## alpha" in render_markdown([_report(fleet_dir)])


def test_render_markdown_empty_fleet_is_friendly() -> None:
    assert "No projects discovered" in render_markdown([])
