"""Tests for the fleet TUI: pure row building plus an app smoke test."""

from __future__ import annotations

import asyncio

from textual.widgets import DataTable, RichLog

from projects_orchestrator.tui import OrchestratorApp, fleet_rows


def test_fleet_rows_one_per_project(make_project):
    make_project("alpha")
    root = make_project("beta").parent
    assert len(fleet_rows(root)) == 2


def test_fleet_rows_sorted_by_name(make_project):
    make_project("gamma")
    make_project("alpha")
    root = make_project("beta").parent
    assert [r.name for r in fleet_rows(root)] == ["alpha", "beta", "gamma"]


def test_fleet_row_reports_no_git_health(make_project):
    root = make_project("alpha").parent
    assert fleet_rows(root)[0].health == "no-git"


def test_fleet_row_reports_clean_health(make_project, git_init):
    root = make_project("alpha")
    git_init(root)
    assert fleet_rows(root.parent)[0].health == "clean"


def test_fleet_row_reports_language(make_project):
    root = make_project("alpha", language="go").parent
    assert fleet_rows(root)[0].language == "go"


def test_fleet_row_reports_memory_tier(make_project):
    root = make_project("alpha", memory_tier=2).parent
    assert fleet_rows(root)[0].memory == "t2"


def test_app_table_has_row_per_project(make_project):
    make_project("alpha")
    root = make_project("beta").parent

    async def _run() -> int:
        app = OrchestratorApp(root)
        async with app.run_test() as pilot:
            await pilot.pause()
            return app.query_one(DataTable).row_count

    assert asyncio.run(_run()) == 2


def test_app_refresh_action_repopulates(make_project):
    root = make_project("alpha").parent

    async def _run() -> int:
        app = OrchestratorApp(root)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_refresh()
            await pilot.pause()
            return app.query_one(DataTable).row_count

    assert asyncio.run(_run()) == 1


def test_app_mounts_log_widget(make_project):
    root = make_project("alpha").parent

    async def _run() -> bool:
        app = OrchestratorApp(root)
        async with app.run_test() as pilot:
            await pilot.pause()
            return app.query_one(RichLog) is not None

    assert asyncio.run(_run()) is True
