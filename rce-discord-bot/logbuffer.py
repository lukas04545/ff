"""Keeps the latest log lines in memory – the web interface shows them under "Bot log".

Separate module because bot.py runs as __main__ and importing "bot" would create a second,
empty copy.
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
        except Exception:  # logging must never stop the bot
            pass


memory_log = MemoryLogHandler()
