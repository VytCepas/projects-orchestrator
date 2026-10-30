"""HTML export: one escaped, self-contained page from fleet rows."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git_init, make_project

from projects_orchestrator.__main__ import main
from projects_orchestrator.fleet import COLUMNS
from projects_orchestrator.html import render_html


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _row(**overrides: str) -> dict[str, str]:
    row = dict.fromkeys(COLUMNS, "-")
    row["Project"] = "alpha"
    row.update(overrides)
    return row


def test_render_html_contains_every_column() -> None:
    document = render_html([_row()], "2026-07-04T00:00:00+00:00")
    assert all(f"<th>{column}</th>" in document for column in COLUMNS)


def test_render_html_contains_project_row() -> None:
    assert "<td>alpha</td>" in render_html([_row()], "now")


def test_render_html_escapes_hostile_cell_text() -> None:
    document = render_html([_row(Project="<script>alert(1)</script>")], "now")
    assert "<script>" not in document


def test_render_html_escapes_footer_timestamp() -> None:
    document = render_html([], "<img src=x>")
    assert "<img" not in document


def test_render_html_empty_fleet_is_friendly() -> None:
    assert "no projects discovered" in render_html([], "now")


def test_render_html_marks_pass_cells_good() -> None:
    assert '<td class="good">pass</td>' in render_html([_row(Lint="pass")], "now")


def test_render_html_marks_fail_cells_bad() -> None:
    assert '<td class="bad">fail</td>' in render_html([_row(Tests="fail")], "now")


def test_render_html_marks_uptime_good() -> None:
    assert '<td class="good">up 5m</td>' in render_html([_row(Running="up 5m")], "now")


def test_render_html_is_a_complete_document() -> None:
    assert render_html([], "now").startswith("<!DOCTYPE html>")


def test_cli_snapshot_html_prints_document(fleet_dir: Path, capsys) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    main(["snapshot", "--root", str(fleet_dir), "--html"])
    assert capsys.readouterr().out.startswith("<!DOCTYPE html>")


def test_cli_snapshot_html_output_writes_file(fleet_dir: Path, tmp_path: Path) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    target = tmp_path / "fleet.html"
    main(["snapshot", "--root", str(fleet_dir), "--html", "-o", str(target)])
    assert "alpha" in target.read_text(encoding="utf-8")


def test_cli_snapshot_html_unwritable_output_exits_1(fleet_dir: Path, tmp_path: Path) -> None:
    make_project(fleet_dir, "alpha")
    target = tmp_path / "missing-dir" / "fleet.html"
    assert main(["snapshot", "--root", str(fleet_dir), "--html", "-o", str(target)]) == 1


def test_cli_snapshot_plain_table_unchanged(fleet_dir: Path, capsys) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    main(["snapshot", "--root", str(fleet_dir)])
    assert "Project" in capsys.readouterr().out
