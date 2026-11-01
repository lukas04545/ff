"""Slash-command flood guard: one member spamming commands can't tie up the bot (RCON, database, Discord rate limits).

Per member: at most LIMIT commands within WINDOW seconds; whoever keeps going over is paused for BLOCK_MINUTES.
Admins are never limited. The member gets a short private note instead of the command.
"""
from __future__ import annotations

import time

from web.ratelimit import RateLimiter

LIMIT, WINDOW = 12, 30
STRIKES, STRIKE_WINDOW = 3, 300
BLOCK_MINUTES = 5


class CommandGuard:
    def __init__(self, limit: int = LIMIT) -> None:
        self._limiter = RateLimiter(max(3, limit), WINDOW)
        self._strikes = RateLimiter(STRIKES - 1, STRIKE_WINDOW)   # the STRIKES-th time: paused
        self.blocked: dict[int, float] = {}
        self.denied = 0

    def check(self, user_id: int, now: float | None = None) -> str | None:
        """None = allowed, else the note for the member."""
        wall = time.time() if now is None else now
        until = self.blocked.get(user_id)
        if until is not None:
            if until > wall:
                self.denied += 1
                return f"You're sending commands too fast – paused until <t:{int(until)}:T>."
            self.blocked.pop(user_id, None)
        mono = time.monotonic() if now is None else now
        wait = self._limiter.hit(str(user_id), mono)
        if wait is None:
            return None
        self.denied += 1
        if self._strikes.hit(str(user_id), mono) is not None:
            self.blocked[user_id] = wall + BLOCK_MINUTES * 60
            return f"Too many commands – you're paused for {BLOCK_MINUTES} minutes."
        return f"Slow down – try again in {int(wait) + 1} s."
