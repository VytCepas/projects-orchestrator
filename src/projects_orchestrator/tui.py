"""Textual TUI: the fleet table and the command controller in one screen.

A thin shell only — every cell comes from :func:`fleet_rows` and every
controller reply from :func:`dispatch`, so the TUI shows exactly what the
CLI shows. Requires the ``tui`` extra (``uv sync --extra tui``).
"""

from __future__ import annotations

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Header, Input, RichLog, TabbedContent, TabPane

from projects_orchestrator.controller import ControllerContext, dispatch, parse_command
from projects_orchestrator.fleet import COLUMNS, fleet_rows, fleet_snapshots
from projects_orchestrator.registry import FleetConfig

_STATUS_STYLE = {
    "pass": "green",
    "clean": "green",
    "ok": "green",
    "none": "green",
    "fail": "red",
    "missing": "red",
    "dirty": "yellow",
    "diverged": "yellow",
    "behind": "yellow",
    "partial": "yellow",
}


class OrchestratorApp(App):
    """Fleet overview table + deterministic command controller."""

    TITLE = "projects-orchestrator"
    BINDINGS = [("r", "refresh", "Refresh"), ("q", "quit", "Quit")]

    def __init__(self, config: FleetConfig) -> None:
        """Create the app around a fleet discovery configuration.

        Args:
            config: Where to look for projects.
        """
        super().__init__()
        self.ctx = ControllerContext(config=config)

    def compose(self) -> ComposeResult:
        """Lay out the Overview and Controller tabs."""
        yield Header()
        with TabbedContent():
            with TabPane("Overview", id="overview"):
                yield DataTable(id="fleet-table")
            with TabPane("Controller", id="controller"), Vertical():
                yield RichLog(id="transcript", wrap=True, markup=False)
                yield Input(id="command", placeholder="help for commands")
        yield Footer()

    def on_mount(self) -> None:
        """Populate the table on startup."""
        table = self.query_one("#fleet-table", DataTable)
        table.add_columns(*COLUMNS)
        self._reload_table()

    def _reload_table(self) -> None:
        """Rebuild the overview rows from a fresh fleet snapshot."""
        table = self.query_one("#fleet-table", DataTable)
        table.clear()
        for row in fleet_rows(fleet_snapshots(self.ctx.fleet)):
            table.add_row(*(self._styled(row[column]) for column in COLUMNS))

    @staticmethod
    def _styled(cell: str) -> Text:
        """Color-code pass/fail-ish cells."""
        return Text(cell, style=_STATUS_STYLE.get(cell, ""))

    def action_refresh(self) -> None:
        """Re-discover the fleet and redraw the table."""
        self.ctx.refresh()
        self._reload_table()

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
