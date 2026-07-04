"""Live fleet dashboard — a read-only HTTP view that refreshes itself.

``snapshot --html`` renders the fleet once; ``serve`` keeps it live. A stdlib
:class:`~http.server.ThreadingHTTPServer` (no new dependency) serves a
self-contained page that polls a JSON endpoint and re-renders the fleet table
in place, with a per-project drill-in. Everything is read-only and offline-
first, exactly like the CLI: the server only ever *reads* the same snapshots
the status table shows, and — like the rest of the engine — never lets an
error escape to crash the loop (a failed request degrades to a 500 line).

The request handlers are thin; the payload builders and the page renderer are
pure functions (``snapshot_payload``, ``project_payload``, ``render_page``) so
they can be tested without binding a socket.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from projects_orchestrator import cache
from projects_orchestrator.detail import build_detail
from projects_orchestrator.fleet import COLUMNS, fleet_rows, fleet_snapshots
from projects_orchestrator.registry import FleetConfig, discover

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

_PROJECT_PREFIX = "/api/project/"
_PROJECT_SUFFIX = ".json"


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")


def snapshot_payload(
    config: FleetConfig, cache_file: Path | None, generated_at: str
) -> dict[str, object]:
    """Build the fleet-overview JSON payload (pure w.r.t. ``generated_at``).

    Re-discovers the fleet so projects added while the server runs appear on
    the next poll. Returns the same rows the status table and HTML snapshot
    show, plus the column order the client renders them in.
    """
    fleet = discover(config)
    snapshots = fleet_snapshots(fleet, cache_file)
    return {
        "generated_at": generated_at,
        "columns": list(COLUMNS),
        "rows": fleet_rows(snapshots),
        "warnings": list(fleet.warnings),
    }


def project_payload(
    config: FleetConfig, cache_file: Path | None, name: str
) -> dict[str, object] | None:
    """Build one project's drill-in payload; ``None`` when it is unknown."""
    fleet = discover(config)
    descriptor = fleet.get(name)
    if descriptor is None:
        return None
    cached = cache.load_results(cache_file).get(descriptor.name)
    return asdict(build_detail(descriptor, cached))


def render_page() -> str:
    """Render the self-contained dashboard shell (pure).

    The page polls ``/api/snapshot.json`` on an interval and rebuilds the
    table client-side; clicking a row fetches ``/api/project/<name>.json``
    into a drawer. No external assets — inline CSS/JS only, same discipline
    as :mod:`~projects_orchestrator.html`.
    """
    columns = json.dumps(list(COLUMNS))
    return _PAGE_TEMPLATE.replace("__COLUMNS__", columns)


class _Handler(BaseHTTPRequestHandler):
    """Route read-only GETs to the payload builders; never raises out."""

    config: FleetConfig
    cache_file: Path | None

    def do_GET(self) -> None:
        """Dispatch a GET to the page or a JSON endpoint."""
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", render_page().encode("utf-8"))
            elif path == "/api/snapshot.json":
                self._send_json(snapshot_payload(self.config, self.cache_file, _now()))
            elif path.startswith(_PROJECT_PREFIX) and path.endswith(_PROJECT_SUFFIX):
                name = path[len(_PROJECT_PREFIX) : -len(_PROJECT_SUFFIX)]
                detail = project_payload(self.config, self.cache_file, unquote(name))
                if detail is None:
                    self._send_json({"error": f"unknown project: {name}"}, status=404)
                else:
                    self._send_json(detail)
            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as exc:  # noqa: BLE001 — a bad request must not kill the server
            self._send_json({"error": str(exc)}, status=500)

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence the default per-request stderr logging."""


def make_server(
    config: FleetConfig, host: str, port: int, cache_file: Path | None = None
) -> ThreadingHTTPServer:
    """Build (but do not serve) the dashboard HTTP server bound to host:port."""
    handler = type("BoundHandler", (_Handler,), {"config": config, "cache_file": cache_file})
    return ThreadingHTTPServer((host, port), handler)


def serve(config: FleetConfig, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Serve the live dashboard until interrupted (blocks)."""
    server = make_server(config, host, port)
    bound_port = server.server_address[1]
    print(f"serving fleet dashboard at http://{host}:{bound_port} (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>projects-orchestrator — fleet</title>
<style>
body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 2rem;
       background: #f6f8fa; color: #1f2328; }
h1 { font-size: 1.25rem; }
table { border-collapse: collapse; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
th, td { padding: .45rem .8rem; border-bottom: 1px solid #d1d9e0; text-align: left;
         font-size: .85rem; white-space: nowrap; }
th { background: #eaeef2; position: sticky; top: 0; }
tbody tr { cursor: pointer; }
tbody tr:hover td { background: #f6f8fa; }
.good { color: #1a7f37; font-weight: 600; }
.bad { color: #cf222e; font-weight: 600; }
.warn { color: #9a6700; font-weight: 600; }
footer { margin-top: 1rem; color: #59636e; font-size: .8rem; }
#warnings { color: #9a6700; font-size: .8rem; margin: .5rem 0; white-space: pre-line; }
#drawer { position: fixed; top: 0; right: 0; height: 100%; width: min(90vw, 32rem);
          background: #fff; box-shadow: -2px 0 8px rgba(0,0,0,.15); padding: 1.5rem;
          overflow: auto; transform: translateX(100%); transition: transform .2s; }
#drawer.open { transform: translateX(0); }
#drawer h2 { font-size: 1.1rem; margin-top: 0; }
#drawer h3 { font-size: .85rem; color: #59636e; margin: 1rem 0 .25rem; text-transform: uppercase; }
#drawer pre { font: .8rem/1.4 ui-monospace, monospace; white-space: pre-wrap; margin: 0; }
#close { float: right; cursor: pointer; border: 0; background: none; font-size: 1.2rem; }
</style></head>
<body>
<h1>projects-orchestrator — fleet</h1>
<div id="warnings"></div>
<table><thead><tr id="head"></tr></thead><tbody id="rows"></tbody></table>
<footer id="footer">connecting…</footer>
<aside id="drawer"><button id="close">&times;</button><div id="detail"></div></aside>
<script>
const COLUMNS = __COLUMNS__;
const GOOD = new Set(["pass","clean","ok","none","yes"]);
const BAD = new Set(["fail","missing","unhealthy"]);
const WARN = new Set(["dirty","diverged","behind","partial","outdated"]);
function cls(v){ if(GOOD.has(v)||(v||"").startsWith("up ")) return "good";
  if(BAD.has(v)) return "bad"; if(WARN.has(v)) return "warn"; return ""; }
function text(t){ const d=document.createElement("div"); d.textContent=t==null?"":t; return d.innerHTML; }
const head=document.getElementById("head");
head.innerHTML = COLUMNS.map(c=>"<th>"+text(c)+"</th>").join("");
async function refresh(){
  try{
    const r = await fetch("/api/snapshot.json", {cache:"no-store"});
    const data = await r.json();
    document.getElementById("rows").innerHTML = data.rows.map(row =>
      "<tr data-name='"+text(row.Project)+"'>" + COLUMNS.map(c => {
        const v = row[c]||""; const k = cls(v);
        return "<td"+(k?" class='"+k+"'":"")+">"+text(v)+"</td>";
      }).join("") + "</tr>").join("");
    document.getElementById("warnings").textContent = (data.warnings||[]).join("\\n");
    document.getElementById("footer").textContent = "updated " + data.generated_at;
    for(const tr of document.querySelectorAll("#rows tr"))
      tr.onclick = () => openDetail(tr.getAttribute("data-name"));
  }catch(e){ document.getElementById("footer").textContent = "disconnected — retrying"; }
}
async function openDetail(name){
  const r = await fetch("/api/project/"+encodeURIComponent(name)+".json", {cache:"no-store"});
  const d = await r.json();
  const sect = (title, lines) => "<h3>"+text(title)+"</h3><pre>"+(lines||[]).map(text).join("\\n")+"</pre>";
  document.getElementById("detail").innerHTML = "<h2>"+text(d.project||name)+"</h2>" +
    sect("descriptor", d.summary) + sect("checks", d.checks) +
    sect("recent commits", d.commits) + sect("memory", d.memory);
  document.getElementById("drawer").classList.add("open");
}
document.getElementById("close").onclick = () =>
  document.getElementById("drawer").classList.remove("open");
refresh();
setInterval(refresh, 5000);
</script>
</body></html>
"""
