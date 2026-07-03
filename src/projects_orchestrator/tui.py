"""Textual TUI: fleet table, per-project detail, and the command controller.

A thin shell only — every overview cell comes from :func:`fleet_rows`, the
Detail pane from :func:`build_detail`/:func:`render_detail`, and every
controller reply from :func:`dispatch`, so the TUI shows exactly what the
CLI shows. Requires the ``tui`` extra (``uv sync --extra tui``).
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Header, Input, RichLog, TabbedContent, TabPane

from projects_orchestrator import cache
from projects_orchestrator.controller import ControllerContext, Intent, dispatch, parse_command
from projects_orchestrator.detail import build_detail, render_detail
from projects_orchestrator.fleet import COLUMNS, fleet_rows, fleet_snapshots
from projects_orchestrator.registry import FleetConfig

_STATUS_STYLE = {
    "pass": "green",
    "clean": "green",
    "ok": "green",
    "none": "green",
    "yes": "green",
    "fail": "red",
    "missing": "red",
    "unhealthy": "red",
    "dirty": "yellow",
    "diverged": "yellow",
    "behind": "yellow",
    "partial": "yellow",
    "outdated": "yellow",
}


class OrchestratorApp(App[None]):
    """Fleet overview table + per-project detail + deterministic controller."""

    TITLE = "projects-orchestrator"
    BINDINGS: ClassVar[list[BindingType]] = [
        ("r", "refresh", "Refresh"),
        ("l", "run_task('lint')", "Lint selected"),
        ("t", "run_task('test')", "Test selected"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, config: FleetConfig) -> None:
        """Create the app around a fleet discovery configuration.

        Args:
            config: Where to look for projects.
        """
        super().__init__()
        self.ctx = ControllerContext(config=config)
        self.selected_project: str | None = None

    def compose(self) -> ComposeResult:
        """Lay out the Overview, Detail, and Controller tabs."""
        yield Header()
        with TabbedContent():
            with TabPane("Overview", id="overview"):
                yield DataTable(id="fleet-table")
            with TabPane("Detail", id="detail"):
                yield RichLog(id="detail-log", wrap=True, markup=False)
            with TabPane("Controller", id="controller"), Vertical():
                yield RichLog(id="transcript", wrap=True, markup=False)
                yield Input(id="command", placeholder="help for commands")
        yield Footer()

    def on_mount(self) -> None:
        """Populate the table on startup."""
        table = self.query_one("#fleet-table", DataTable)
        table.add_columns(*COLUMNS)
        table.cursor_type = "row"
        self._reload_table()

    def _reload_table(self) -> None:
        """Rebuild the overview rows from a fresh fleet snapshot."""
        table = self.query_one("#fleet-table", DataTable)
        table.clear()
        for row in fleet_rows(fleet_snapshots(self.ctx.fleet, self.ctx.cache_file)):
            table.add_row(*(self._styled(row[column]) for column in COLUMNS), key=row["Project"])

    @staticmethod
    def _styled(cell: str) -> Text:
        """Color-code pass/fail-ish cells."""
        return Text(cell, style=_STATUS_STYLE.get(cell, ""))

    def action_refresh(self) -> None:
        """Re-discover the fleet and redraw the table."""
        self.ctx.refresh()
        self._reload_table()

    def action_run_task(self, task: str) -> None:
        """Run one gate for the selected project, streaming into Detail."""
        log = self.query_one("#detail-log", RichLog)
        if self.selected_project is None:
            log.write("select a project in Overview first")
            return
        log.write(f"> {task} {self.selected_project}")
        intent = Intent(verb="check", target=self.selected_project, args=(task,))
        for line in dispatch(intent, self.ctx):
            log.write(line)
        self._show_detail(self.selected_project)

    def _show_detail(self, project: str) -> None:
        """Render one project's drill-in into the Detail pane."""
        descriptor = self.ctx.fleet.get(project)
        if descriptor is None:
            return
        cached = cache.load_results(self.ctx.cache_file)
        log = self.query_one("#detail-log", RichLog)
        for line in render_detail(build_detail(descriptor, cached.get(descriptor.name))):
            log.write(line)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the Detail pane for the selected overview row."""
        key = event.row_key.value
        if key is None:
            return
        self.selected_project = key
        self.query_one("#detail-log", RichLog).clear()
        self._show_detail(key)
        self.query_one(TabbedContent).active = "detail"

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Run one controller command and stream its output."""
        transcript = self.query_one("#transcript", RichLog)
        transcript.write(f"> {event.value}")
        intent = parse_command(event.value)
        if intent.verb == "quit":
            self.exit()
            return
        for line in dispatch(intent, self.ctx):
            transcript.write(line)
        event.input.value = ""
