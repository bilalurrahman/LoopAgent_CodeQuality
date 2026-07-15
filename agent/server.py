"""Tiny stdlib HTTP server that exposes the loop's live state to the browser.

Routes:
  GET /              -> the dashboard (ui/index.html)
  GET /api/state     -> current snapshot (JSON) so a fresh page can rebuild
  GET /events        -> Server-Sent Events stream of every event as it happens

Runs in a background thread alongside the loop; both share one EventBus.
"""

from __future__ import annotations

import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from events import EventBus

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


def make_handler(bus: EventBus, controller=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence default stderr spam
            pass

        def _send(self, code, body: bytes, ctype="text/plain; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _read_json(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n) if n else b"{}"
                return json.loads(raw.decode() or "{}")
            except (ValueError, json.JSONDecodeError):
                return {}

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._serve_file(os.path.join(UI_DIR, "index.html"), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(200, json.dumps(bus.snapshot()).encode(), "application/json")
            elif path == "/api/config":
                if controller is None:
                    self._json(200, {"repos": [], "schedules": [], "running": False, "control": False})
                else:
                    snap = controller.config_snapshot()
                    snap["control"] = True
                    self._json(200, snap)
            elif path == "/events":
                self._serve_sse()
            else:
                self._send(404, b"not found")

        def do_DELETE(self):
            path = self.path.split("?", 1)[0]
            if controller is None:
                self._json(403, {"ok": False, "error": "control disabled"})
                return
            if path.startswith("/api/schedule/"):
                sid = path.rsplit("/", 1)[-1]
                ok = controller.delete_schedule(sid)
                self._json(200 if ok else 404, {"ok": ok})
            else:
                self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if controller is None:
                self._json(403, {"ok": False, "error": "control disabled (start with --daemon)"})
                return
            body = self._read_json()
            if path == "/api/run":
                ok, msg = controller.start_run(body)
                self._json(200 if ok else 409, {"ok": ok, "run_id" if ok else "error": msg})
            elif path == "/api/schedule":
                s = controller.add_schedule(body)
                self._json(200, {"ok": True, "schedule": s})
            elif path == "/api/schedule/toggle":
                ok = controller.toggle_schedule(body.get("id"), bool(body.get("enabled")))
                self._json(200 if ok else 404, {"ok": ok})
            else:
                self._json(404, {"ok": False, "error": "not found"})

        def _serve_file(self, fpath, ctype):
            if not os.path.exists(fpath):
                self._send(404, b"ui not found")
                return
            with open(fpath, "rb") as fh:
                self._send(200, fh.read(), ctype)

        def _serve_sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = bus.subscribe()
            try:
                # Prime with a snapshot so the client is immediately consistent.
                self._sse_write({"type": "snapshot", "state": bus.snapshot()})
                while True:
                    try:
                        evt = q.get(timeout=15)
                        self._sse_write(evt)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ValueError):
                pass
            finally:
                bus.unsubscribe(q)

        def _sse_write(self, obj):
            data = json.dumps(obj)
            self.wfile.write(f"data: {data}\n\n".encode())
            self.wfile.flush()

    return Handler


def start_server(bus: EventBus, host: str = "127.0.0.1", port: int = 8787, controller=None) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(bus, controller))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd
