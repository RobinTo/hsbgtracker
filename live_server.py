"""Tiny local HTTP server inside the tracker.

Serves the stats dashboard (regenerated per request, so it is always fresh)
and a /state endpoint the page polls to follow the live game's round.
Localhost only. If the port is taken (second tracker instance), the caller
falls back to the static file build.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765


def start(state_fn, html_fn, port: int = PORT):
    """state_fn() -> dict, html_fn() -> str. Returns the server or None."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.path.startswith("/state"):
                    body = json.dumps(state_fn()).encode("utf-8")
                    ctype = "application/json"
                elif self.path in ("/", "/index.html", "/stats"):
                    body = html_fn().encode("utf-8")
                    ctype = "text/html; charset=utf-8"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
            except Exception:
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            # The page may also be opened from file:// — allow it to poll.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
