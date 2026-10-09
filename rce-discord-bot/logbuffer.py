"""Hält die letzten Log-Zeilen im Speicher – das Webinterface zeigt sie unter „Bot-Log“.

Eigenes Modul, weil bot.py als __main__ läuft und ein Import von „bot“ eine zweite,
leere Kopie erzeugen würde.
"""
from __future__ import annotations

import logging
from collections import deque


class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.records: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:  # Logging darf den Bot nie stoppen
            pass


memory_log = MemoryLogHandler()
