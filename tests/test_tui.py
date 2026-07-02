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


async def test_app_has_both_tabs(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    app = _app(fleet_dir)
    async with app.run_test():
        assert [p.id for p in app.query("TabPane")] == ["overview", "controller"]


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
