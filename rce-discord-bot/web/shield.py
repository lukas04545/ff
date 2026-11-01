"""Flood (DDoS) shield for the web interface and the player website – the first thing every request meets.

What a program can do against a DDoS: big traffic floods have to be absorbed in front of it – that's what the
Cloudflare tunnel does (visitors only ever reach Cloudflare, your PC's IP stays hidden and nothing listens on the
internet). The shield handles what still gets through, cheaply and before any page is built:
  * a per-IP limit for ALL requests (pages, API, pictures, files, 404 floods)
  * automatic temporary bans for IPs that keep hitting the limit
  * a cap on requests handled at the same time (the bot stays responsive for Discord and RCON)
  * "under attack" mode when the total traffic spikes: anonymous visitors get a much lower limit for a while,
    logged-in panel users are never limited
Your own PC (localhost without a proxy header) is never limited.
"""
from __future__ import annotations

import time
from collections import Counter, deque

from web.ratelimit import RateLimiter

WINDOW = 10                      # seconds
STRIKES_TO_BAN = 5               # limit hits within STRIKE_WINDOW -> ban
STRIKE_WINDOW = 300
ATTACK_MINUTES = 10              # how long "under attack" lasts after the last spike
ATTACK_LIMIT_SHARE = 5           # under attack: anonymous per-IP limit = normal / this
ASSET_FACTOR = 8                 # item pictures and /static files: this many times the page limit


class Shield:
    def __init__(self, *, per_ip: int = 150, total: int = 3000, max_inflight: int = 200,
                 ban_minutes: int = 15) -> None:
        self.per_ip, self.total, self.max_inflight, self.ban_minutes = per_ip, total, max_inflight, ban_minutes
        self._ip = RateLimiter(per_ip, WINDOW)
        self._ip_attack = RateLimiter(max(5, per_ip // ATTACK_LIMIT_SHARE), WINDOW)
        self._assets = RateLimiter(per_ip * ASSET_FACTOR, WINDOW)      # pictures and site files: many per page
        self._total: deque[float] = deque()
        self._strikes = RateLimiter(STRIKES_TO_BAN, STRIKE_WINDOW)
        self.bans: dict[str, float] = {}           # ip -> banned until (time.time)
        self.inflight = 0
        self.attack_until = 0.0
        self.blocked = 0
        self.blocked_by_ip: Counter = Counter()
        self.events: deque = deque(maxlen=50)      # (time, text) for the panel / admin log
        self.on_event = None                        # callback(text) – the admin log

    # ------------------------------------------------------------ State

    @property
    def under_attack(self) -> bool:
        return time.time() < self.attack_until

    def _attacking(self, now: float) -> bool:
        return now < self.attack_until

    def _event(self, text: str) -> None:
        self.events.append((int(time.time()), text))
        if self.on_event is not None:
            try:
                self.on_event(text)
            except Exception:
                pass

    def ban(self, ip: str, minutes: int | None = None, why: str = "") -> None:
        self.bans[ip] = time.time() + (minutes or self.ban_minutes) * 60
        self._event(f"🛡️ {ip} blocked for {minutes or self.ban_minutes} min" + (f" ({why})" if why else ""))

    def unban(self, ip: str) -> bool:
        return self.bans.pop(ip, None) is not None

    def banned(self, ip: str, now: float | None = None) -> float | None:
        until = self.bans.get(ip)
        now = time.time() if now is None else now
        if until is None:
            return None
        if until <= now:
            self.bans.pop(ip, None)
            return None
        return until - now

    # ------------------------------------------------------------ The check (cheap, synchronous)

    def check(self, ip: str, *, trusted: bool = False, asset: bool = False,
              now: float | None = None) -> tuple[int, str, int] | None:
        """None = let it through; else (status, text, retry-after seconds). trusted = logged-in user or local."""
        now_t = time.time() if now is None else now
        mono = time.monotonic() if now is None else now
        self._total.append(mono)
        while self._total and mono - self._total[0] > WINDOW:
            self._total.popleft()
        if len(self._total) > self.total and not self._attacking(now_t):
            self.attack_until = now_t + ATTACK_MINUTES * 60
            self._event(f"🚨 Under attack: {len(self._total)} requests in {WINDOW} s – stricter limits for "
                        f"{ATTACK_MINUTES} min (logged-in admins unaffected)")
        elif len(self._total) > self.total:
            self.attack_until = now_t + ATTACK_MINUTES * 60
        if trusted:
            return None
        if (left := self.banned(ip, now_t)) is not None:
            return self._block(ip, 429, "Too many requests – try again later.", int(left) + 1)
        if self.inflight >= self.max_inflight:
            return self._block(ip, 503, "The server is busy – try again in a moment.", 5)
        limiter = self._assets if asset else (self._ip_attack if self._attacking(now_t) else self._ip)
        wait = limiter.hit(ip, mono)
        if wait is not None:
            if self._strikes.hit(ip, mono) is not None:
                self.ban(ip, why=f"over {limiter.limit} requests / {WINDOW} s again and again")
            return self._block(ip, 429, "Too many requests – please slow down.", int(wait) + 1)
        return None

    def _block(self, ip: str, status: int, text: str, retry: int) -> tuple[int, str, int]:
        self.blocked += 1
        self.blocked_by_ip[ip] += 1
        if len(self.blocked_by_ip) > 5000:
            self.blocked_by_ip = Counter(dict(self.blocked_by_ip.most_common(500)))
        return status, text, max(1, retry)

    def status(self) -> dict:
        now = time.time()
        return {"under_attack": self.under_attack, "attack_until": int(self.attack_until) if self.under_attack else None,
                "requests_last_10s": len(self._total), "inflight": self.inflight, "blocked": self.blocked,
                "per_ip": self.per_ip, "total": self.total, "max_inflight": self.max_inflight,
                "bans": [{"ip": ip, "until": int(until)} for ip, until in sorted(self.bans.items()) if until > now],
                "top_blocked": [{"ip": ip, "count": n} for ip, n in self.blocked_by_ip.most_common(10)],
                "events": [{"at": t, "text": x} for t, x in list(self.events)[-15:]]}
