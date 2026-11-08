# servidor http basico para health checks y ver el ultimo estado

import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

logger = logging.getLogger(__name__)


def make_status_handler(provider: Any):
    class StatusHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # no imprimimos cada request para no llenar la consola de basura
            pass

        def do_GET(self) -> None:
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                payload = json.dumps(
                    {
                        "status": "ok",
                        "time": datetime.now(timezone.utc).isoformat(),
                    }
                ).encode("utf-8")
                self.wfile.write(payload)

            elif self.path == "/status":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                rec = provider.last_record
                payload_dict = rec.to_dict() if rec is not None else {}
                self.wfile.write(
                    json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
                )

            else:
                self.send_response(404)
                self.end_headers()

    return StatusHandler


class StatusServer:
    def __init__(self, port: int, provider: Any) -> None:
        self.port = port
        self.provider = provider
        self.server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # arranca el server en un hilo aparte
    def start(self) -> None:
        if self.port <= 0:
            return

        handler_cls = make_status_handler(self.provider)
        self.server = HTTPServer(("0.0.0.0", self.port), handler_cls)

        self._thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self._thread.start()
        logger.info(
            f"[statusserver] escuchando en http://0.0.0.0:{self.port} (GET /health, GET /status)"
        )

    # apaga el server de forma limpia
    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
