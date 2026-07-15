"""Live fleet dashboard — a read-only view that can, opt-in, act on the fleet.

``snapshot --html`` renders the fleet once; ``serve`` keeps it live. A stdlib
:class:`~http.server.ThreadingHTTPServer` (no new dependency) serves a
self-contained page that polls a JSON endpoint and re-renders the fleet table
in place, with a per-project drill-in. The read surface is offline-first, exactly
like the CLI: the server only ever *reads* the same snapshots the status table
shows, and — like the rest of the engine — never lets an error escape to crash
the loop (a failed request degrades to a 500 line).

With ``--enable-actions`` the drawer also gains two **mutating** buttons —
re-run a project's checks, or heal its red lint/test gate — as ``POST``
endpoints. Those are gated two ways (#52 follow-up):

* **Loopback only.** ``serve`` refuses to enable actions unless it is bound to a
  loopback address, so the mutating surface is never exposed on a network.
* **A per-session CSRF token.** Each ``serve`` process mints a random token,
  embeds it in the page, and requires it back in an ``X-PO-Token`` header on
  every ``POST``. A cross-site page can neither read the token (same-origin
  policy) nor set a custom header on a form-style cross-origin ``POST`` (it
  would trigger a preflight this server never grants), so a drive-by request
  from another tab is refused with a 403.

Actions run on a background thread and record their state in an
:class:`ActionTracker` the snapshot payload exposes, so a click returns at once
(``202``) and its outcome shows up on the next poll — a heal can take minutes and
must not block the request.

The request handlers are thin; the payload builders, the page renderer, and the
action bodies are ordinary functions (``snapshot_payload``, ``project_payload``,
``render_page``, ``run_recheck``, ``run_heal``) so they can be tested without
binding a socket.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import secrets
import socket
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from projects_orchestrator import cache
from projects_orchestrator.checks import collect_checks
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.detail import build_detail
from projects_orchestrator.fleet import COLUMNS, cell_status, fleet_rows, fleet_snapshots
from projects_orchestrator.heal import (
    HEALABLE_TASKS,
    heal_project,
    pending_failures,
    render_heal_result,
)
from projects_orchestrator.registry import FleetConfig, discover

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

_PROJECT_PREFIX = "/api/project/"
_PROJECT_SUFFIX = ".json"

#: Header the page echoes the per-session token back in on every mutating POST.
TOKEN_HEADER = "X-PO-Token"  # noqa: S105 — a header NAME, not a secret value

# Action kinds, keyed by the URL suffix that selects them. ``/api/project/<name>``
# + one of these is a mutating endpoint (POST only).
_ACTION_SUFFIXES = {"/recheck": "recheck", "/heal": "heal"}

RUNNING = "running"
DONE = "done"
ERROR = "error"


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ActionStatus:
    """The latest mutating action tracked for one project.

    Attributes:
        kind: ``recheck`` or ``heal``.
        state: ``running`` while in flight, then ``done`` or ``error``.
        message: A short human-readable outcome, once finished.
        started_at: When the action began (ISO-8601).
        finished_at: When it settled (ISO-8601); empty while running.
    """

    kind: str
    state: str
    message: str = ""
    started_at: str = ""
    finished_at: str = ""


class ActionTracker:
    """Thread-safe record of the most recent mutating action per project.

    One action may run per project at a time: :meth:`begin` refuses a second
    concurrent start (the handler turns that into a 409), which both stops a
    double-click spending twice and keeps the per-project state unambiguous.
    """

    def __init__(self) -> None:
        """Start with no tracked actions."""
        self._lock = threading.Lock()
        self._by_project: dict[str, ActionStatus] = {}

    def begin(self, project: str, kind: str) -> bool:
        """Mark an action running; return ``False`` if one already is for it."""
        with self._lock:
            current = self._by_project.get(project)
            if current is not None and current.state == RUNNING:
                return False
            self._by_project[project] = ActionStatus(kind=kind, state=RUNNING, started_at=_now())
            return True

    def complete(self, project: str, message: str, *, ok: bool = True) -> None:
        """Settle a running action as ``done`` (or ``error``) with a message."""
        with self._lock:
            current = self._by_project.get(project)
            self._by_project[project] = ActionStatus(
                kind=current.kind if current else "",
                state=DONE if ok else ERROR,
                message=message,
                started_at=current.started_at if current else "",
                finished_at=_now(),
            )

    def as_dict(self) -> dict[str, dict[str, str]]:
        """Snapshot the tracked actions as plain dicts for the JSON payload."""
        with self._lock:
            return {project: asdict(status) for project, status in self._by_project.items()}


def is_loopback(host: str) -> bool:
    """Whether binding to ``host`` keeps the server loopback-only (pure).

    Mutating actions are only allowed on a loopback bind, so this is the gate
    ``serve``/``_cmd_serve`` check before enabling them. ``localhost`` is treated
    as loopback; anything that does not parse as a loopback IP (``0.0.0.0``, a LAN
    address, an empty all-interfaces bind) is not.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run_recheck(descriptor: ProjectDescriptor, cache_file: Path | None) -> str:
    """Re-run one project's declared gates and refresh the cache; never raises.

    The browser equivalent of ``checks <project>``: it runs the same
    locally-declared lint/test commands and writes the results back so the table
    reflects them on the next poll.
    """
    fresh = collect_checks(descriptor)
    cache.save_results(fresh, cache_file)
    failed = [result.task for result in fresh if result.status == "fail"]
    if failed:
        return f"checks refreshed — failing: {', '.join(failed)}"
    return "checks refreshed — all gates pass"


def run_heal(descriptor: ProjectDescriptor, cache_file: Path | None) -> str:
    """Heal one project's red lint/test gate end to end; never raises.

    Re-runs the healable gates first so heal acts on current state, then hands
    off to :func:`~projects_orchestrator.heal.heal_project` — which cuts an
    isolated worktree, runs a scoped agent, and opens a draft PR on a verified
    fix, never touching a default branch (ADR-006). A project that is already
    green is left alone.
    """
    fresh = collect_checks(descriptor, HEALABLE_TASKS)
    cache.save_results(fresh, cache_file)
    cached = {result.task: result for result in fresh}
    if not pending_failures(cached):
        return "nothing to heal — lint and test already pass"
    return render_heal_result(heal_project(descriptor, cached))


def _run_in_background(
    tracker: ActionTracker, project: str, kind: str, work: Callable[[], str]
) -> bool:
    """Start ``work`` on a daemon thread, tracking it; ``False`` if one is running.

    The action body runs off the request thread so the ``POST`` returns at once
    (a heal can take minutes). ``work`` never raising is the norm — ``run_heal``/
    ``run_recheck`` degrade to a message — but a defensive catch keeps even an
    unforeseen error from silently killing the worker with no recorded outcome.
    """
    if not tracker.begin(project, kind):
        return False

    def _worker() -> None:
        try:
            tracker.complete(project, work())
        except Exception as exc:  # noqa: BLE001 — a background action must not vanish silently
            tracker.complete(project, str(exc), ok=False)

    threading.Thread(target=_worker, name=f"po-action-{kind}-{project}", daemon=True).start()
    return True


def snapshot_payload(
    config: FleetConfig,
    cache_file: Path | None,
    generated_at: str,
    actions: dict[str, dict[str, str]] | None = None,
) -> dict[str, object]:
    """Build the fleet-overview JSON payload (pure w.r.t. ``generated_at``).

    Re-discovers the fleet so projects added while the server runs appear on
    the next poll. Returns the same rows the status table and HTML snapshot
    show, plus the column order the client renders them in. ``actions`` — the
    tracked per-project action state — is included only when actions are enabled,
    so the read-only server's payload is unchanged.
    """
    fleet = discover(config)
    snapshots = fleet_snapshots(fleet, cache_file)
    rows = fleet_rows(snapshots)
    payload: dict[str, object] = {
        "generated_at": generated_at,
        "columns": list(COLUMNS),
        "rows": rows,
        # Per-cell status from the shared classifier, so the page styles cells
        # without re-encoding the good/bad/warn vocabulary client-side.
        "statuses": [{column: cell_status(row[column]) for column in COLUMNS} for row in rows],
        "warnings": list(fleet.warnings),
    }
    if actions is not None:
        payload["actions"] = actions
    return payload


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


def render_page(token: str = "", actions_enabled: bool = False) -> str:
    """Render the self-contained dashboard shell (pure).

    The page polls ``/api/snapshot.json`` on an interval and rebuilds the
    table client-side; clicking a row fetches ``/api/project/<name>.json``
    into a drawer. When ``actions_enabled``, the drawer also shows re-check and
    heal buttons that ``POST`` with the CSRF ``token`` embedded here. No external
    assets — inline CSS/JS only, same discipline as :mod:`~projects_orchestrator.html`.
    """
    return (
        _PAGE_TEMPLATE.replace("__COLUMNS__", json.dumps(list(COLUMNS)))
        .replace("__TOKEN__", json.dumps(token))
        .replace("__ACTIONS__", "true" if actions_enabled else "false")
    )


def _action_for_path(path: str) -> tuple[str, str] | None:
    """Resolve a mutating POST path to ``(project, kind)`` (pure); ``None`` if not one."""
    if not path.startswith(_PROJECT_PREFIX):
        return None
    for suffix, kind in _ACTION_SUFFIXES.items():
        if path.endswith(suffix):
            return unquote(path[len(_PROJECT_PREFIX) : -len(suffix)]), kind
    return None


class _Handler(BaseHTTPRequestHandler):
    """Route read-only GETs (and, opt-in, mutating POSTs); never raises out."""

    config: FleetConfig
    cache_file: Path | None
    actions_enabled: bool = False
    token: str = ""
    tracker: ActionTracker

    def do_GET(self) -> None:
        """Dispatch a GET to the page or a JSON endpoint."""
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                page = render_page(self.token, self.actions_enabled)
                self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))
            elif path == "/api/snapshot.json":
                actions = self.tracker.as_dict() if self.actions_enabled else None
                self._send_json(snapshot_payload(self.config, self.cache_file, _now(), actions))
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

    def do_POST(self) -> None:
        """Dispatch a mutating action, guarded by the actions flag and CSRF token."""
        try:
            if not self.actions_enabled:
                self._send_json(
                    {"error": "mutating actions are disabled (start serve with --enable-actions)"},
                    status=403,
                )
                return
            if not secrets.compare_digest(self.headers.get(TOKEN_HEADER, ""), self.token):
                self._send_json({"error": "missing or invalid CSRF token"}, status=403)
                return
            action = _action_for_path(self.path.split("?", 1)[0])
            if action is None:
                self._send_json({"error": "not found"}, status=404)
                return
            self._dispatch_action(*action)
        except Exception as exc:  # noqa: BLE001 — a bad request must not kill the server
            self._send_json({"error": str(exc)}, status=500)

    def _dispatch_action(self, name: str, kind: str) -> None:
        """Start the named action in the background, or explain why it can't."""
        descriptor = discover(self.config).get(name)
        if descriptor is None:
            self._send_json({"error": f"unknown project: {name}"}, status=404)
            return
        work: Callable[[], str] = (
            (lambda: run_recheck(descriptor, self.cache_file))
            if kind == "recheck"
            else (lambda: run_heal(descriptor, self.cache_file))
        )
        if not _run_in_background(self.tracker, descriptor.name, kind, work):
            self._send_json(
                {"error": f"an action is already running for {descriptor.name}"}, status=409
            )
            return
        self._send_json({"status": "started", "project": descriptor.name, "kind": kind}, status=202)

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


class _ThreadingHTTPServerV6(ThreadingHTTPServer):
    """The dashboard server over IPv6 — ``ThreadingHTTPServer`` is IPv4-only.

    ``ThreadingHTTPServer`` hard-codes ``address_family = AF_INET``, so binding an
    IPv6 host like ``::1`` (which ``is_loopback`` accepts and the CLI advertises)
    would otherwise raise ``gaierror``. This subclass binds ``AF_INET6`` instead.
    """

    address_family = socket.AF_INET6


def _is_ipv6(host: str) -> bool:
    """Whether ``host`` is an IPv6 literal (pure); needs the IPv6 server family."""
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def make_server(
    config: FleetConfig,
    host: str,
    port: int,
    cache_file: Path | None = None,
    *,
    token: str = "",
) -> ThreadingHTTPServer:
    """Build (but do not serve) the dashboard HTTP server bound to host:port.

    Mutating actions are enabled iff ``token`` is non-empty: a real per-session
    token both switches the POST endpoints on and is the value they check. An
    empty token leaves the server read-only, so there is no state in which
    actions are live without a secret to guard them. Each server gets its own
    :class:`ActionTracker`, so action state never leaks between two ``serve``
    processes on one host. An IPv6 ``host`` (e.g. ``::1``) is bound with an
    IPv6-capable server; everything else uses the stdlib IPv4 server.
    """
    handler = type(
        "BoundHandler",
        (_Handler,),
        {
            "config": config,
            "cache_file": cache_file,
            "actions_enabled": bool(token),
            "token": token,
            "tracker": ActionTracker(),
        },
    )
    server_cls = _ThreadingHTTPServerV6 if _is_ipv6(host) else ThreadingHTTPServer
    return server_cls((host, port), handler)


def serve(
    config: FleetConfig,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    enable_actions: bool = False,
) -> None:
    """Serve the live dashboard until interrupted (blocks).

    With ``enable_actions`` the server mints a per-session CSRF token and accepts
    mutating POSTs. It refuses to do so on a non-loopback bind — exposing
    re-check/heal on a network is never implicit — raising :class:`ValueError` so
    a caller cannot start an unsafe server by mistake (the CLI checks first and
    exits cleanly).
    """
    if enable_actions and not is_loopback(host):
        message = f"mutating actions require a loopback bind; refusing to enable them on {host}"
        raise ValueError(message)
    token = secrets.token_urlsafe(32) if enable_actions else ""
    server = make_server(config, host, port, token=token)
    bound_port = server.server_address[1]
    print(f"serving fleet dashboard at http://{host}:{bound_port} (Ctrl-C to stop)")
    if enable_actions:
        print(
            "mutating actions ENABLED — re-check and heal, guarded by a loopback bind + CSRF token"
        )
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
#actions { margin: 1rem 0; display: flex; gap: .5rem; flex-wrap: wrap; }
#actions button { cursor: pointer; border: 1px solid #d1d9e0; background: #f6f8fa;
                  border-radius: 6px; padding: .35rem .8rem; font-size: .82rem; }
#actions button.heal { border-color: #cf222e; color: #cf222e; }
#action-status { font-size: .8rem; color: #59636e; margin-top: .3rem; min-height: 1rem; }
</style></head>
<body>
<h1>projects-orchestrator — fleet</h1>
<div id="warnings"></div>
<table><thead><tr id="head"></tr></thead><tbody id="rows"></tbody></table>
<footer id="footer">connecting…</footer>
<aside id="drawer"><button id="close">&times;</button><div id="detail"></div></aside>
<script>
const COLUMNS = __COLUMNS__;
const TOKEN = __TOKEN__;
const ACTIONS_ENABLED = __ACTIONS__;
let latestActions = {};
let openProject = null;
function text(t){ const d=document.createElement("div"); d.textContent=t==null?"":t; return d.innerHTML; }
const head=document.getElementById("head");
head.innerHTML = COLUMNS.map(c=>"<th>"+text(c)+"</th>").join("");
function renderActionStatus(name){
  const el = document.getElementById("action-status");
  if(!el) return;
  const a = latestActions[name];
  el.textContent = a ? (a.kind+": "+a.state+(a.message?" — "+a.message:"")) : "";
}
async function refresh(){
  try{
    const r = await fetch("/api/snapshot.json", {cache:"no-store"});
    const data = await r.json();
    latestActions = data.actions || {};
    document.getElementById("rows").innerHTML = data.rows.map((row, i) =>
      "<tr data-name='"+text(row.Project)+"'>" + COLUMNS.map(c => {
        const v = row[c]||""; const k = (data.statuses[i]||{})[c] || "";
        return "<td"+(k?" class='"+k+"'":"")+">"+text(v)+"</td>";
      }).join("") + "</tr>").join("");
    document.getElementById("warnings").textContent = (data.warnings||[]).join("\\n");
    document.getElementById("footer").textContent = "updated " + data.generated_at;
    for(const tr of document.querySelectorAll("#rows tr"))
      tr.onclick = () => openDetail(tr.getAttribute("data-name"));
    if(openProject) renderActionStatus(openProject);
  }catch(e){ document.getElementById("footer").textContent = "disconnected — retrying"; }
}
async function postAction(name, kind){
  if(kind==="heal" && !confirm("Heal "+name+"? This spawns a paid coding agent and opens a draft PR.")) return;
  const el = document.getElementById("action-status");
  try{
    const r = await fetch("/api/project/"+encodeURIComponent(name)+"/"+kind,
      {method:"POST", headers:{"X-PO-Token": TOKEN}, cache:"no-store"});
    const d = await r.json().catch(()=>({}));
    if(el) el.textContent = r.status===202 ? (kind+": started") : ("refused — "+(d.error||r.status));
  }catch(e){ if(el) el.textContent = "request failed"; }
}
function actionBar(name){
  if(!ACTIONS_ENABLED) return "";
  return "<div id='actions'>"+
    "<button id='act-recheck'>Re-run checks</button>"+
    "<button id='act-heal' class='heal'>Heal</button></div>"+
    "<div id='action-status'></div>";
}
async function openDetail(name){
  openProject = name;
  const r = await fetch("/api/project/"+encodeURIComponent(name)+".json", {cache:"no-store"});
  const d = await r.json();
  const sect = (title, lines) => "<h3>"+text(title)+"</h3><pre>"+(lines||[]).map(text).join("\\n")+"</pre>";
  document.getElementById("detail").innerHTML = "<h2>"+text(d.project||name)+"</h2>" +
    actionBar(name) +
    sect("descriptor", d.summary) + sect("checks", d.checks) +
    sect("recent commits", d.commits) + sect("memory", d.memory);
  if(ACTIONS_ENABLED){
    document.getElementById("act-recheck").onclick = () => postAction(name, "recheck");
    document.getElementById("act-heal").onclick = () => postAction(name, "heal");
    renderActionStatus(name);
  }
  document.getElementById("drawer").classList.add("open");
}
document.getElementById("close").onclick = () => {
  openProject = null;
  document.getElementById("drawer").classList.remove("open");
};
refresh();
setInterval(refresh, 5000);
</script>
</body></html>
"""
