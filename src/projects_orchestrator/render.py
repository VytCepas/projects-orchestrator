"""Render discovered projects as a terminal table, HTML dashboard, or JSON."""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from dataclasses import asdict

from projects_orchestrator.discovery import Project, status_of

_HEADERS = ("PROJECT", "LANG", "BRANCH", "STATE", "LAST COMMIT")


def _state(project: Project) -> str:
    """Return a short working-tree state label."""
    return status_of(project)


def _row(project: Project) -> tuple[str, ...]:
    """Flatten a project into the display columns."""
    return (
        project.name,
        project.language,
        project.branch,
        _state(project),
        project.last_commit,
    )


def render_tui(projects: Sequence[Project]) -> str:
    """Render an aligned plain-text table (safe for any terminal).

    Args:
        projects: Projects to display.

    Returns:
        A multi-line string with a header row and one row per project.
    """
    rows = [_HEADERS, *(_row(p) for p in projects)]
    widths = [max(len(row[i]) for row in rows) for i in range(len(_HEADERS))]

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    lines = [fmt(_HEADERS), fmt(tuple("-" * w for w in widths))]
    lines.extend(fmt(_row(p)) for p in projects)
    summary = f"\n{len(projects)} project(s) · {sum(p.dirty for p in projects)} dirty"
    return "\n".join(lines) + summary


def render_json(projects: Sequence[Project]) -> str:
    """Render projects as pretty-printed JSON (paths as strings)."""

    def encode(project: Project) -> dict[str, object]:
        data = asdict(project)
        data["path"] = str(project.path)
        return data

    return json.dumps([encode(p) for p in projects], indent=2)


def _card(project: Project) -> str:
    """Render a single project card for the HTML dashboard."""
    state = _state(project)
    return f"""    <article class="card {state}">
      <header><h2>{html.escape(project.name)}</h2><span class="pill {state}">{state}</span></header>
      <p class="desc">{html.escape(project.description) or "&mdash;"}</p>
      <dl>
        <dt>Language</dt><dd>{html.escape(project.language)}</dd>
        <dt>Branch</dt><dd><code>{html.escape(project.branch)}</code></dd>
        <dt>Memory</dt><dd>{html.escape(project.memory_stack)}</dd>
        <dt>MCPs</dt><dd>{html.escape(project.mcps)}</dd>
      </dl>
      <footer><code>{html.escape(project.last_commit)}</code></footer>
      <small>{html.escape(str(project.path))}</small>
    </article>"""


def render_html(projects: Sequence[Project]) -> str:
    """Render a self-contained HTML dashboard.

    Args:
        projects: Projects to display.

    Returns:
        A complete HTML document with inline CSS and no external assets.
    """
    dirty = sum(p.dirty for p in projects)
    cards = "\n".join(_card(p) for p in projects) or "    <p>No projects found.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>projects-orchestrator</title>
<style>
  :root {{ color-scheme: light dark; --bg:#0d1117; --card:#161b22; --edge:#30363d;
           --fg:#e6edf3; --muted:#8b949e; --ok:#3fb950; --warn:#d29922; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:2rem; font:15px/1.5 ui-sans-serif,system-ui,sans-serif;
          background:var(--bg); color:var(--fg); }}
  h1 {{ margin:0 0 .25rem; font-size:1.4rem; }}
  .sub {{ color:var(--muted); margin:0 0 1.5rem; }}
  .grid {{ display:grid; gap:1rem; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); }}
  .card {{ background:var(--card); border:1px solid var(--edge); border-radius:10px;
           padding:1rem; overflow:hidden; }}
  .card header {{ display:flex; align-items:center; justify-content:space-between; gap:.5rem; }}
  .card h2 {{ margin:0; font-size:1.05rem; }}
  .desc {{ color:var(--muted); margin:.4rem 0 .8rem; min-height:1.4em; }}
  dl {{ display:grid; grid-template-columns:auto 1fr; gap:.15rem .75rem; margin:0; }}
  dt {{ color:var(--muted); }}  dd {{ margin:0; text-align:right; }}
  footer {{ margin-top:.8rem; padding-top:.6rem; border-top:1px solid var(--edge); }}
  code {{ font:12px ui-monospace,monospace; }}
  small {{ display:block; margin-top:.5rem; color:var(--muted); font-size:11px;
           overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .pill {{ font-size:11px; padding:.1rem .5rem; border-radius:99px; text-transform:uppercase; }}
  .pill.clean {{ background:color-mix(in srgb,var(--ok) 22%,transparent); color:var(--ok); }}
  .pill.dirty {{ background:color-mix(in srgb,var(--warn) 22%,transparent); color:var(--warn); }}
  .pill.unversioned {{ background:color-mix(in srgb,var(--muted) 20%,transparent); color:var(--muted); }}
  .card.dirty {{ border-left:3px solid var(--warn); }}
  .card.unversioned {{ border-left:3px solid var(--muted); }}
</style>
</head>
<body>
  <h1>projects-orchestrator</h1>
  <p class="sub">{len(projects)} project(s) &middot; {dirty} with uncommitted changes</p>
  <div class="grid">
{cards}
  </div>
</body>
</html>
"""
