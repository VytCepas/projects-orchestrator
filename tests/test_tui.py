"""TUI pilot tests: the shell mounts and mirrors the engine."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_project

from projects_orchestrator.registry import FleetConfig

textual = pytest.importorskip("textual")

from textual.widgets import DataTable, Input, RichLog, TabbedContent  # noqa: E402

from projects_orchestrator.tui import OrchestratorApp  # noqa: E402


def _app(fleet_dir: Path) -> OrchestratorApp:
    return OrchestratorApp(config=FleetConfig(roots=(fleet_dir,)))


async def test_app_mounts_overview_table(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    app = _app(fleet_dir)
    async with app.run_test():
        assert app.query_one("#fleet-table", DataTable).row_count == 1


async def test_app_has_all_tabs(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    app = _app(fleet_dir)
    async with app.run_test():
        assert [p.id for p in app.query("TabPane")] == ["overview", "detail", "controller"]


async def test_refresh_binding_picks_up_new_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    app = _app(fleet_dir)
    async with app.run_test() as pilot:
        make_project(fleet_dir, "beta")
        await pilot.press("r")
        assert app.query_one("#fleet-table", DataTable).row_count == 2


async def test_controller_input_streams_output(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    app = _app(fleet_dir)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "controller"
        await pilot.pause()
        command_input = app.query_one("#command", Input)
        command_input.focus()
        command_input.value = "projects"
        await pilot.press("enter")
        transcript = app.query_one("#transcript", RichLog)
        assert any("alpha" in line.text for line in transcript.lines)


async def test_row_selection_opens_detail(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    app = _app(fleet_dir)
    async with app.run_test() as pilot:
        table = app.query_one("#fleet-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "detail"


async def test_row_selection_renders_detail_lines(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    app = _app(fleet_dir)
    async with app.run_test() as pilot:
        table = app.query_one("#fleet-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        detail_log = app.query_one("#detail-log", RichLog)
        assert any("# alpha" in line.text for line in detail_log.lines)


async def test_run_task_binding_streams_result(fleet_dir: Path, tmp_path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    app = _app(fleet_dir)
    app_cache = tmp_path / "checks.json"
    async with app.run_test() as pilot:
        app.ctx.cache_file = app_cache
        table = app.query_one("#fleet-table", DataTable)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        detail_log = app.query_one("#detail-log", RichLog)
        assert any("alpha lint: PASS" in line.text for line in detail_log.lines)
