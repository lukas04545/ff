"""Honeypot at /admin: from outside the bot's PC, /admin shows a fake login page (the real panel lives at its secret
address, WEB_PANEL_PATH). Every visit and every login attempt there is noted: time, IP, country/city (from
Cloudflare's visitor headers), browser, operating system, language, where the visitor came from and the username
and password tried. The password is only shown on the owner's Honeypot page – never in the Discord log – and one
that is a real panel password (an admin who went to /admin by mistake) is not saved, only flagged.

A web page can't see a visitor's MAC address (it never leaves their own network), so it isn't in the log.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

LIMIT = 500                      # entries kept (oldest dropped)

_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("SamsungBrowser/", "Samsung Internet"), ("Firefox/", "Firefox"),
             ("Chrome/", "Chrome"), ("CriOS/", "Chrome"), ("Safari/", "Safari"))
_SYSTEMS = (("Windows NT 10", "Windows 10/11"), ("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iOS"),
            ("iPad", "iPadOS"), ("Mac OS X", "macOS"), ("CrOS", "ChromeOS"), ("Linux", "Linux"))
_TOOLS = re.compile(r"curl|wget|python|go-http|java|libwww|httpclient|scanner|nmap|masscan|zgrab|nikto|sqlmap|bot",
                    re.I)


def _clean(text: str) -> str:
    """Header text that goes into the admin log channel: letters, digits, spaces, . and - only (no pings)."""
    return re.sub(r"[^\w .-]", "", text or "").strip()[:40]


def describe_agent(agent: str) -> tuple[str, str]:
    """(browser, operating system) from a User-Agent – rough, good enough for a log."""
    agent = agent or ""
    if not agent:
        return "unknown", "unknown"
    if _TOOLS.search(agent):
        browser = "Script / scanner"
    else:
        browser = next((name for key, name in _BROWSERS if key in agent), "Other")
    system = next((name for key, name in _SYSTEMS if key in agent), "unknown")
    return browser, system


def visitor(request, ip: str) -> dict:
    """What the request tells about the visitor (no password, ever)."""
    h = request.headers
    browser, system = describe_agent(h.get("User-Agent", ""))
    place = ", ".join(_clean(p) for p in (h.get("CF-IPCity", ""), h.get("CF-Region", ""), h.get("CF-IPCountry", ""))
                      if _clean(p) and p not in ("XX", "T1"))
    return {
        "time": int(time.time()), "ip": re.sub(r"[^0-9A-Fa-f.:]", "", ip)[:64] or "?", "location": place[:120] or "unknown",
        "country": (h.get("CF-IPCountry") or "")[:4], "browser": browser, "os": system,
        "agent": h.get("User-Agent", "")[:300], "language": (h.get("Accept-Language") or "").split(",")[0][:20],
        "referer": (h.get("Referer") or "")[:200], "method": request.method, "path": request.path[:100],
        "via_tunnel": bool(h.get("CF-Connecting-IP")),
    }


class HoneypotLog:
    """The list in a JSON file. Several servers (servers.py) share it – so it's read again before every change and
    every listing, never kept only in memory."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._entries: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._entries, indent=0), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def add(self, entry: dict) -> None:
        with self._lock:
            self._entries = self._load()                  # another server may have added some meanwhile
            self._entries.append(entry)
            del self._entries[:-LIMIT]
            try:
                self._save()
            except OSError:
                pass

    def entries(self) -> list[dict]:
        return list(reversed(self._load()))           # newest first (also what the other servers logged)

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            try:
                self._save()
            except OSError:
                pass


def fake_login_page(error: bool | str = False) -> str:
    """A plain, believable admin login – posts back to /admin, which never lets anyone in."""
    text = error if isinstance(error, str) else "Invalid username or password."
    message = f'<p class="err">{text}</p>' if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Administration – Sign in</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f3f4f6;font:15px system-ui,sans-serif;
color:#111}}form{{background:#fff;padding:32px;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,.08);width:300px}}
h1{{font-size:20px;margin:0 0 20px}}label{{display:block;font-size:13px;margin:12px 0 4px;color:#444}}
input{{width:100%;box-sizing:border-box;padding:9px 10px;border:1px solid #ccc;border-radius:5px;font:inherit}}
button{{margin-top:20px;width:100%;padding:10px;border:0;border-radius:5px;background:#2563eb;color:#fff;
font:inherit;cursor:pointer}}.err{{color:#b91c1c;font-size:13px;margin:0 0 8px}}
</style></head><body><form method="post" action="/admin"><h1>Administration</h1>{message}
<label for="u">Username</label><input id="u" name="username" autocomplete="username" required>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form></body></html>"""


def summary(entry: dict) -> str:
    """One line for the admin log channel."""
    name = _clean(entry.get("username") or "")[:32]                       # no pings, links or formatting
    who = f" as **{name}**" if name else ""
    what = f"tried to log in at /admin{who}" if entry["method"] == "POST" else "opened /admin"
    return (f"🍯 Honeypot: someone {what} – IP {entry['ip']} ({entry['location']}), "
            f"{entry['browser']} on {entry['os']}")
