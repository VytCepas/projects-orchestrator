"""Render the fleet view as one self-contained HTML page.

``snapshot --html`` turns the same rows the status table shows into a
static dashboard you can pin in a browser tab, serve from a cron job, or
glance at from a phone. Pure and stdlib-only: inline CSS, no JavaScript,
no external assets, every cell HTML-escaped. The renderer consumes
:func:`~projects_orchestrator.fleet.fleet_rows` output verbatim, so the
page can never disagree with the terminal.
"""

from __future__ import annotations

import html as _html

from projects_orchestrator.fleet import COLUMNS

_GOOD = frozenset({"pass", "clean", "ok", "none", "yes"})
_BAD = frozenset({"fail", "missing", "unhealthy"})
_WARN = frozenset({"dirty", "diverged", "behind", "partial", "outdated"})

_STYLE = """
body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 2rem;
       background: #f6f8fa; color: #1f2328; }
h1 { font-size: 1.25rem; }
table { border-collapse: collapse; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
th, td { padding: .45rem .8rem; border-bottom: 1px solid #d1d9e0; text-align: left;
         font-size: .85rem; white-space: nowrap; }
th { background: #eaeef2; position: sticky; top: 0; }
tr:hover td { background: #f6f8fa; }
.good { color: #1a7f37; font-weight: 600; }
.bad { color: #cf222e; font-weight: 600; }
.warn { color: #9a6700; font-weight: 600; }
footer { margin-top: 1rem; color: #59636e; font-size: .8rem; }
"""


def _cell_class(text: str) -> str:
    """Map a cell's text to its status CSS class (empty when neutral)."""
    if text in _GOOD or text.startswith("up "):
        return "good"
    if text in _BAD:
        return "bad"
    if text in _WARN:
        return "warn"
    return ""


def _cell(text: str) -> str:
    """Render one escaped table cell."""
    css = _cell_class(text)
    attr = f' class="{css}"' if css else ""
    return f"<td{attr}>{_html.escape(text)}</td>"


def render_html(rows: list[dict[str, str]], generated_at: str) -> str:
    """Render fleet rows as a complete standalone HTML document (pure).

    Args:
        rows: Output of :func:`~projects_orchestrator.fleet.fleet_rows`.
        generated_at: Timestamp text for the footer.

    Returns:
        A full HTML document; an empty fleet renders a friendly line
        instead of an empty table.
    """
    if rows:
        header = "".join(f"<th>{_html.escape(column)}</th>" for column in COLUMNS)
        body = "".join(
            "<tr>" + "".join(_cell(row[column]) for column in COLUMNS) + "</tr>" for row in rows
        )
        table = f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"
    else:
        table = "<p>no projects discovered</p>"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>projects-orchestrator — fleet</title>\n"
        f"<style>{_STYLE}</style></head>\n"
        "<body><h1>projects-orchestrator — fleet</h1>\n"
        f"{table}\n"
        f"<footer>generated {_html.escape(generated_at)}</footer></body></html>\n"
    )
