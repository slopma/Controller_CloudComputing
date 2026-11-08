# logger para guardar las decisiones en archivo jsonl

import json
import sys
import threading
from typing import TextIO

from controllerasg.model import DecisionRecord


class JSONLogger:
    def __init__(self, *writers: TextIO) -> None:
        self.writers = list(writers) if writers else [sys.stdout]
        self._lock = threading.Lock()

    # escribe la linea de log en todos los streams configurados
    def log(self, rec: DecisionRecord) -> None:
        data = json.dumps(rec.to_dict(), ensure_ascii=False) + "\n"
        with self._lock:
            for w in self.writers:
                try:
                    w.write(data)
                    w.flush()
                except Exception:
                    pass
