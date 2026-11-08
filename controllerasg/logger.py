"""Registro estructurado de decisiones en formato JSON Lines."""

import json
import sys
import threading
from typing import Any, TextIO

from controllerasg.model import DecisionRecord


class JSONLogger:
    """Escribe cada decisión tomada por el controlador en una línea JSON (JSON Lines)."""

    def __init__(self, *writers: TextIO) -> None:
        self.writers = list(writers) if writers else [sys.stdout]
        self._lock = threading.Lock()

    def log(self, rec: DecisionRecord) -> None:
        """Serializa y escribe de forma atómica el registro en todos los flujos de salida configurados."""
        data = json.dumps(rec.to_dict(), ensure_ascii=False) + "\n"
        with self._lock:
            for w in self.writers:
                try:
                    w.write(data)
                    w.flush()
                except Exception:
                    pass
