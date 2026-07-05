"""A read-only web dashboard served from the local machine.

The browser polls ``/api/status`` for the fleet view-model. To review status is
the whole job — starting and stopping projects is the agent/CLI's concern
(ADR-004) — so there is no action endpoint here. The snapshot is recomputed at
most once per TTL, so a browser poll (or several) can never trigger a
per-project ``ss``/``docker`` subprocess storm. Bound to loopback only.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from projects_orchestrator.cockpit import snapshot
from projects_orchestrator.web_page import PAGE

# A browser polls every few seconds; recomputing the fleet (which shells out to
# ss/docker per project) on every poll is what exhausted memory before ADR-004.
# One scan per TTL, shared by all concurrent pollers, bounds that cost.
_TTL_SECONDS = 3.0


class CockpitServer(ThreadingHTTPServer):
    """HTTP server carrying the scan root and a TTL-cached fleet snapshot."""

    def __init__(self, address: tuple[str, int], root: Path, *, ttl: float = _TTL_SECONDS) -> None:
        """Store the scan root and bind the socket."""
        self.root = root
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache: list[dict[str, object]] | None = None
        self._cache_at = 0.0
        super().__init__(address, CockpitHandler)
        host, port = self.server_address[:2]
        self.origin = f"http://{host}:{port}"

    def status(self) -> list[dict[str, object]]:
        """Return the fleet snapshot, recomputed at most once per TTL."""
        with self._lock:
            now = time.monotonic()
            if self._cache is None or now - self._cache_at >= self._ttl:
                self._cache = snapshot(self.root)
                self._cache_at = now
            return self._cache


class CockpitHandler(BaseHTTPRequestHandler):
    """Serve the dashboard page and the read-only status API."""

    server: CockpitServer

    def log_message(self, *args: object) -> None:  # noqa: D102 - silence access log
        pass

    def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
        """Write a response with the given body and content type."""
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: object, code: int = 200) -> None:
        """Write a JSON response."""
        self._send(json.dumps(obj).encode(), "application/json", code)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        """Route GET requests to the page or the read-only status API."""
        route = urlparse(self.path)
        if route.path in ("/", "/index.html"):
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        elif route.path == "/api/status":
            self._json(self.server.status())
        else:
            self._json({"error": "not found"}, 404)


def serve(root: Path, host: str, port: int) -> None:
    """Run the read-only web dashboard until interrupted.

    Args:
        root: Directory to scan for project-init projects.
        host: Interface to bind (loopback by default).
        port: TCP port to listen on.
    """
    server = CockpitServer((host, port), root.expanduser().resolve())
    url = f"http://{host}:{port}"
    print(f"dashboard on {url}  (open it in VS Code's Simple Browser · Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        server.server_close()
