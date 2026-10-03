"""control_server.py :: Minimal HTTP control server for the overlay.

Listens on localhost only (127.0.0.1:52800) and exposes simple endpoints
that Fauxmo's SimpleHTTPPlugin (or any HTTP client) can call.

Endpoints
---------
GET /on          → set state to BLACKOUT (Alexa "on" = screen blackout)
GET /off         → set state to OFF (Alexa "off" = restore screen)
GET /dim         → set state to DIM
GET /cycle       → advance to next state
GET /state       → return JSON {"state": "off"|"dim"|"blackout"}

Design notes
------------
* Pure stdlib: ``http.server`` + ``socketserver``.
* Runs in a daemon thread so it exits automatically when the main process ends.
* Binds ONLY to 127.0.0.1 – not exposed to the LAN, only Fauxmo (same machine).
"""

from __future__ import annotations

import json
import logging
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from overlay_engine import OverlayEngine, STATE_DIM, STATE_BLACKOUT, STATE_OFF

logger = logging.getLogger(__name__)

CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 52800


class _Handler(BaseHTTPRequestHandler):
    """Minimal GET-only HTTP handler."""

    engine: OverlayEngine  # set as class attr after engine is created

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: ANN002
        logger.debug(fmt, *args)

    def _respond(self, body: str, status: int = 200) -> None:
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.rstrip("/").lower()
        engine = _Handler.engine

        if path == "/on":
            engine.set_state(STATE_BLACKOUT)
            self._respond(json.dumps({"state": engine.get_state_name()}))
        elif path == "/off":
            engine.set_state(STATE_OFF)
            self._respond(json.dumps({"state": engine.get_state_name()}))
        elif path == "/dim":
            engine.set_state(STATE_DIM)
            self._respond(json.dumps({"state": engine.get_state_name()}))
        elif path == "/cycle":
            engine.cycle()
            self._respond(json.dumps({"state": engine.get_state_name()}))
        elif path == "/state":
            self._respond(json.dumps({"state": engine.get_state_name()}))
        else:
            self._respond(json.dumps({"error": "unknown endpoint"}), 404)


class ControlServer:
    """Thin wrapper that starts the HTTP server in a daemon thread."""

    def __init__(self, engine: OverlayEngine) -> None:
        _Handler.engine = engine
        self._server = HTTPServer((CONTROL_HOST, CONTROL_PORT), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="ControlHTTPServer",
        )

    def start(self) -> None:
        self._thread.start()
        logger.info(
            "Control server listening on http://%s:%d",
            CONTROL_HOST,
            CONTROL_PORT,
        )

    def stop(self) -> None:
        self._server.shutdown()
