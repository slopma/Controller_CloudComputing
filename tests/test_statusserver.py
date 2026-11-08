"""Pruebas unitarias para el servidor HTTP de estado."""

import json
import socket
import time
import urllib.request
from datetime import datetime, timezone

from controllerasg.model import Capacity, Decision, DecisionRecord
from controllerasg.statusserver import StatusServer


class DummyProvider:
    def __init__(self, rec: DecisionRecord) -> None:
        self.last_record = rec


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_health_and_status_endpoints():
    port = get_free_port()
    rec = DecisionRecord(
        cycle_id=42,
        time=datetime.now(timezone.utc),
        decision=Decision.MAINTAIN,
        reason="test status ok",
        window_seconds=300,
        metrics=[],
        capacity_before=Capacity(),
        capacity=Capacity(),
    )
    provider = DummyProvider(rec)
    server = StatusServer(port, provider)
    server.start()

    try:
        # Esperar a que el servidor esté activo
        url_health = f"http://127.0.0.1:{port}/health"
        url_status = f"http://127.0.0.1:{port}/status"

        resp = None
        for _ in range(20):
            try:
                with urllib.request.urlopen(url_health, timeout=1.0) as r:
                    resp = r.read()
                    break
            except Exception:
                time.sleep(0.05)

        assert resp is not None
        health_data = json.loads(resp.decode("utf-8"))
        assert health_data["status"] == "ok"

        # Verificar /status
        with urllib.request.urlopen(url_status, timeout=1.0) as r:
            status_bytes = r.read()

        status_data = json.loads(status_bytes.decode("utf-8"))
        assert status_data["CycleID"] == 42
        assert status_data["Reason"] == "test status ok"
        assert status_data["Decision"] == "MAINTAIN_CAPACITY"

    finally:
        server.stop()


def test_start_with_zero_port_does_nothing():
    server = StatusServer(0, DummyProvider(None))
    server.start()
    assert server.server is None
    server.stop()
