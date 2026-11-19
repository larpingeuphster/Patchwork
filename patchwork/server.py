"""Serve the web interface and its API on 127.0.0.1 only.

The window (app.py) loads ``http://127.0.0.1:<port>/#<token>``. The page sends
the token back in an ``X-Patchwork-Token`` header on every API call, so other
web pages and programs can't drive Patchwork even though it listens on a
local port. The Host header is checked too, which blocks DNS-rebinding tricks.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .controller import Controller, UserError

log = logging.getLogger("patchwork")

WEBUI_DIR = Path(__file__).with_name("webui")

API_METHODS = {
    "snapshot", "activity_log", "detect",
    "select_profile", "set_profile_enabled", "new_profile", "duplicate_profile", "rename_profile",
    "delete_profile",
    "save_swap", "set_swaps_enabled", "remove_swaps", "undo",
    "pick_file", "import_preset", "export_preset",
    "start", "stop", "clear_cache", "clear_downloads", "remove_certificate",
    "set_setting", "accept_notice", "open_folder",
}

_TYPES = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml",
          ".png": "image/png", ".woff2": "font/woff2"}


class UiServer:
    def __init__(self, controller: Controller, port: int = 0, webui: Path = WEBUI_DIR):
        self.controller = controller
        self.token = secrets.token_urlsafe(24)
        self.webui = webui.resolve()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.on_quit = None  # set by the window
        self.on_show = None  # set by the window
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#{self.token}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="patchwork-ui", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep the console quiet
                pass

            def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, data: object) -> None:
                self._send(status, json.dumps(data).encode(), "application/json")

            def _host_ok(self) -> bool:
                return self.headers.get("Host", "") in (f"127.0.0.1:{server.port}", f"localhost:{server.port}")

            def _authorized(self) -> bool:
                token = self.headers.get("X-Patchwork-Token", "")
                return self._host_ok() and secrets.compare_digest(token, server.token)

            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, b"Forbidden", "text/plain")
                url = urlparse(self.path)
                if url.path == "/api/events":
                    if not self._authorized():
                        return self._json(403, {"ok": False, "error": "forbidden"})
                    after = int((parse_qs(url.query).get("after") or ["0"])[0] or 0)
                    events = server.controller.events_after(after, timeout=20)
                    return self._json(200, {"ok": True, "events": events})
                rel = url.path.lstrip("/") or "index.html"
                target = (server.webui / rel).resolve()
                if server.webui not in target.parents or not target.is_file():
                    return self._send(404, b"Not found", "text/plain")
                ctype = _TYPES.get(target.suffix) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                csp = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                       "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                       "frame-ancestors 'none'")
                self._send(200, target.read_bytes(), ctype, {"Content-Security-Policy": csp})

            def do_POST(self):
                if not self._authorized():
                    return self._json(403, {"ok": False, "error": "forbidden"})
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._json(400, {"ok": False, "error": "bad request"})
                path = urlparse(self.path).path
                if path == "/api/show":
                    # Another copy of Patchwork was opened: bring this window forward instead.
                    self._json(200, {"ok": True})
                    if server.on_show:
                        threading.Thread(target=server.on_show, daemon=True).start()
                    return
                if path == "/api/quit":
                    self._json(200, {"ok": True})
                    if server.on_quit:
                        threading.Thread(target=server.on_quit, daemon=True).start()
                    return
                method = path.removeprefix("/api/")
                if method not in API_METHODS:
                    return self._json(404, {"ok": False, "error": f"unknown method {method}"})
                args = payload.get("args") or {}
                if not isinstance(args, dict):
                    return self._json(400, {"ok": False, "error": "args must be an object"})
                try:
                    result = getattr(server.controller, method)(**args)
                except UserError as exc:
                    return self._json(200, {"ok": False, "error": str(exc)})
                except TypeError as exc:
                    return self._json(400, {"ok": False, "error": f"bad arguments: {exc}"})
                except Exception as exc:
                    log.exception("API call %s failed", method)
                    return self._json(200, {"ok": False, "error": f"Something went wrong: {exc}"})
                self._json(200, {"ok": True, "result": result})

        return Handler
