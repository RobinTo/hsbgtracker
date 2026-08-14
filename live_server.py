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


def start(state_fn, html_fn, cards_fn=None, art_fn=None, port: int = PORT):
    """state_fn() -> dict, html_fn() -> str, cards_fn() -> str (card pool
    browser), art_fn(kind, card_id) -> bytes|None (locally cached card art);
    the latter two are optional. Returns the server or None."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                cache = "no-store"
                if self.path.startswith("/state"):
                    body = json.dumps(state_fn()).encode("utf-8")
                    ctype = "application/json"
                elif self.path in ("/", "/index.html", "/stats"):
                    body = html_fn().encode("utf-8")
                    ctype = "text/html; charset=utf-8"
                elif self.path == "/cards" and cards_fn is not None:
                    body = cards_fn().encode("utf-8")
                    ctype = "text/html; charset=utf-8"
                elif self.path.startswith("/art/") and art_fn is not None:
                    # /art/<kind>/<card_id>.png, served from the disk cache
                    # (downloaded from the CDN at most once per card).
                    parts = self.path[len("/art/"):].split("/")
                    body = None
                    if len(parts) == 2 and parts[1].endswith(".png"):
                        body = art_fn(parts[0], parts[1][:-4])
                    if body is None:
                        self.send_response(404)
                        # Spare the browser re-asking for known-missing art.
                        self.send_header("Cache-Control", "max-age=86400")
                        self.end_headers()
                        return
                    ctype = "image/png"
                    cache = "max-age=604800"
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
            self.send_header("Cache-Control", cache)
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
