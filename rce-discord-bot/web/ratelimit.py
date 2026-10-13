"""Rate limits for logins and the public pages (sliding window per key, e.g. per IP or per username).

Memory stays small: keys that haven't been used within their window are dropped when there are many of them, so
someone sending thousands of random usernames or IPs can't fill the memory.
"""
from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    def __init__(self, limit: int, window: float, *, max_keys: int = 20_000) -> None:
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, now: float) -> None:
        stale = [k for k, d in self._hits.items() if not d or now - d[-1] > self.window]
        for k in stale:
            del self._hits[k]
        if len(self._hits) > self.max_keys:         # still too many: forget the oldest half
            oldest = sorted(self._hits, key=lambda k: self._hits[k][-1])[: len(self._hits) // 2]
            for k in oldest:
                del self._hits[k]

    def retry_after(self, key: str, now: float | None = None) -> float | None:
        """Seconds to wait if `key` is over the limit right now (doesn't count a hit)."""
        now = time.monotonic() if now is None else now
        hits = self._hits.get(key)
        if not hits:
            return None
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return max(self.window - (now - hits[0]), 1.0)
        return None

    def hit(self, key: str, now: float | None = None) -> float | None:
        """Counts a hit. Returns the seconds to wait if the key is over the limit (the hit isn't counted then)."""
        now = time.monotonic() if now is None else now
        wait = self.retry_after(key, now)
        if wait is not None:
            return wait
        if key not in self._hits and len(self._hits) >= self.max_keys:
            self._prune(now)
        self._hits.setdefault(key, deque()).append(now)
        return None

    def clear(self, key: str) -> None:
        self._hits.pop(key, None)

    def __len__(self) -> int:
        return len(self._hits)
