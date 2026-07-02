"""Textual TUI: a fleet overview that monitors and runs projects.

The heavy lifting lives in :mod:`registry`, :mod:`status`, and :mod:`runner`;
this module is a thin presentation layer. :func:`fleet_rows` is a pure function
(no Textual) so the data shown in the table is unit-tested independently of the
UI, and :class:`OrchestratorApp` just renders those rows and dispatches runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, RichLog

from projects_orchestrator.registry import Registry
from projects_orchestrator.runner import run_command_line
from projects_orchestrator.status import collect_status

_HEALTH_STYLE = {"clean": "green", "dirty": "yellow", "no-git": "dim"}
_COLUMNS = ("Project", "Health", "Branch", "Ahead/Behind", "Lang", "Memory")


@dataclass(frozen=True)
class FleetRow:
    """One project's row in the fleet table.

    Attributes:
        name: Project name.
        health: Health verdict (``clean`` | ``dirty`` | ``no-git``).
        branch: Current branch, or ``-``.
        drift: Ahead/behind display, e.g. ``+1/-0``, or ``-`` for non-repos.
        language: Primary language.
        memory: Memory tier display, e.g. ``t0``.
    """

    name: str
    health: str
    branch: str
    drift: str
    language: str
    memory: str


def fleet_rows(root: Path) -> list[FleetRow]:
    """Build the fleet table rows for every project under ``root``.

    Args:
        root: Directory to scan recursively.

    Returns:
        One :class:`FleetRow` per discovered project, in registry order.
    """
    rows: list[FleetRow] = []
    for descriptor in Registry.discover(root):
        status = collect_status(descriptor)
        drift = f"+{status.git.ahead}/-{status.git.behind}" if status.git.is_repo else "-"
        rows.append(
            FleetRow(
                name=descriptor.name,
                health=status.health,
                branch=status.git.branch or "-",
                drift=drift,
                language=descriptor.language,
                memory=f"t{descriptor.memory.tier}",
            )
        )
    return rows


class OrchestratorApp(App[None]):
    """A terminal overview of the project fleet with a lint-all action."""

    TITLE = "projects-orchestrator"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("l", "run_lint", "Lint all"),
    ]

    def __init__(self, root: Path) -> None:
        """Initialize the app for a fleet root.

        Args:
            root: Directory to scan recursively for projects.
        """
        super().__init__()
        self._root = root

    def compose(self) -> ComposeResult:
        """Lay out the header, fleet table, log, and footer.

        Yields:
            The app's child widgets.
        """
        yield Header()
        yield DataTable(id="fleet", cursor_type="row")
        yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        """Set up the table columns and load the fleet."""
        table = self.query_one(DataTable)
        table.add_columns(*_COLUMNS)
        self.action_refresh()

    def action_refresh(self) -> None:
        """Reload the fleet and repopulate the table."""
        table = self.query_one(DataTable)
        table.clear()
        for row in fleet_rows(self._root):
            health = Text(row.health, style=_HEALTH_STYLE.get(row.health, ""))
            table.add_row(row.name, health, row.branch, row.drift, row.language, row.memory)

    @work(thread=True)
    def action_run_lint(self) -> None:
        """Run each project's declared lint command in a background worker."""
        log = self.query_one(RichLog)
        for descriptor in Registry.discover(self._root):
            command = descriptor.tooling.lint
            if not command:
                continue
            result = run_command_line(descriptor, command)
            mark = "[green]OK[/]" if result.ok else "[red]FAIL[/]"
            self.call_from_thread(log.write, f"{mark} {descriptor.name}: {command}")


def run_tui(root: Path) -> None:
    """Launch the orchestrator TUI for a fleet root.

    Args:
        root: Directory to scan recursively for projects.
    """
    OrchestratorApp(root).run()
