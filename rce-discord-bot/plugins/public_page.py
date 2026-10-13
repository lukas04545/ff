"""Public stats page: a read-only web page anyone can open – live status, next wipe, top players, how to join.

Address: your web interface link + /p/public_page (shown on the plugin card). Share it in server lists, ads and
social media bios. It shows only what's already public on Discord – no settings, no logins, no secrets.
"""
from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime

from plugin_api import Plugin, Setting
from utils import format_duration

INVITE = re.compile(r"^https://(discord\.gg|discord\.com/invite)/[\w-]{2,40}$")
CACHE_SECONDS = 30
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="60">
<title>{title}</title><style>
:root{{--bg:#0f1115;--card:#181b22;--text:#e8e8ea;--muted:#9aa0aa;--accent:{accent};--ok:#2ecc71;--bad:#e74c3c}}
@media (prefers-color-scheme:light){{:root{{--bg:#f4f5f7;--card:#fff;--text:#1d1f24;--muted:#5f6670}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,sans-serif}}
main{{max-width:900px;margin:0 auto;padding:24px 16px}}h1{{margin:0 0 4px;font-size:1.8rem}}
.muted{{color:var(--muted)}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin-top:18px}}
.card{{background:var(--card);border-radius:12px;padding:16px}}.card h2{{margin:0 0 10px;font-size:1.05rem}}
.big{{font-size:2rem;font-weight:700}}.pill{{display:inline-block;padding:2px 10px;border-radius:99px;font-size:.85rem;color:#fff}}
.on{{background:var(--ok)}}.off{{background:var(--bad)}}ol{{margin:0;padding-left:1.4rem}}li{{margin:2px 0}}
.btn{{display:inline-block;margin-top:14px;background:var(--accent);color:#fff;padding:10px 18px;border-radius:10px;
text-decoration:none;font-weight:600}}.join{{white-space:pre-wrap}}footer{{margin-top:24px;font-size:.8rem}}
</style></head><body><main>
<h1>{name}</h1><div class="muted">{platform}</div>{invite}
<div class="grid">
<div class="card"><h2>Server</h2><div><span class="pill {state_class}">{state}</span></div>
<div class="big">{players}</div><div class="muted">{queue}</div></div>
<div class="card"><h2>Next wipe</h2><div class="big" style="font-size:1.3rem">{wipe}</div><div class="muted">{wipe_in}</div></div>
{peak}
</div>
<div class="grid">{boards}</div>
{join}
<footer class="muted">Updated {updated} · refreshes every minute</footer>
</main></body></html>"""


def esc(text) -> str:
    return html.escape(str(text), quote=True)


class PublicPage(Plugin):
    NAME = "Public stats page"
    ICON = "🌍"
    DESCRIPTION = ("A public web page with live player count, next wipe, top players and how to join – share the "
                   "link in server lists, ads and TikTok/YouTube bios. Read-only, no secrets.")
    SETTINGS = (
        Setting("invite", "Discord invite link", "text", "", help="e.g. https://discord.gg/abc123 – shown as a button."),
        Setting("how_to_join", "How to join", "textarea",
                "Open Rust Console Edition → Play → Community servers → search for our server name.",
                help="Shown at the bottom of the page."),
        Setting("leaderboards", "Show top players", "bool", True),
        Setting("accent", "Colour", "text", "#ce422b", help="Hex colour, e.g. #ce422b."),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._cache: tuple[float, dict] | None = None

    def snapshot(self) -> dict:
        if self._cache and time.monotonic() - self._cache[0] < CACHE_SECONDS:
            return self._cache[1]
        info = self.bot.server_info or {}
        online = bool(self.bot.rcon.connected and info)
        name = self.server_name
        data = {"name": name if name != "the server" else "Rust Console server",
                "platform": self.bot.config.rust_server_platform, "online": online,
                "players": int(info.get("Players") or len(self.bot.online_players)) if online else 0,
                "max_players": int(info.get("MaxPlayers") or 0) if online else 0,
                "queued": int(info.get("Queued") or 0) if online else 0, "next_wipe": self.next_wipe(),
                "peak_24h": int(max((p[1] for p in self.bot.db.population(self.now() - 86400)), default=0)),
                "top_kills": [], "top_playtime": [], "updated": self.now()}
        if self.setting("leaderboards"):
            data["top_kills"] = [{"name": r["name"], "kills": r["kills"], "deaths": r["deaths"]}
                                 for r in self.bot.db.leaderboard("kills", 10) if r["kills"]]
            data["top_playtime"] = [{"name": r["name"], "seconds": r["playtime_seconds"]}
                                    for r in self.bot.db.leaderboard("playtime", 10) if r["playtime_seconds"]]
        bounties = self.plugin("bounties")
        if bounties is not None:
            totals: dict[str, int] = {}
            for b in bounties.bounties():
                totals[b["target"]] = totals.get(b["target"], 0) + b["amount"]
            data["bounties"] = [{"name": n, "amount": a} for n, a in sorted(totals.items(), key=lambda x: -x[1])[:5]]
        self._cache = (time.monotonic(), data)
        return data

    def render(self, data: dict) -> str:
        tz = self.tz
        if data["next_wipe"]:
            when = datetime.fromtimestamp(data["next_wipe"], tz)
            wipe = when.strftime("%a %d %b, %H:%M") + f" ({getattr(tz, 'key', 'UTC')})"
            wipe_in = "in " + format_duration(max(data["next_wipe"] - self.now(), 0))
        else:
            wipe, wipe_in = esc(self.bot.config.rust_server_wipe or "Announced on Discord"), ""
        boards = []
        if data["top_kills"]:
            boards.append("<div class='card'><h2>⚔️ Top killers</h2><ol>" + "".join(
                f"<li>{esc(r['name'])} – {r['kills']} kill{'s' if r['kills'] != 1 else ''}</li>" for r in data["top_kills"]) + "</ol></div>")
        if data["top_playtime"]:
            boards.append("<div class='card'><h2>⏱️ Most playtime</h2><ol>" + "".join(
                f"<li>{esc(r['name'])} – {esc(format_duration(r['seconds']))}</li>" for r in data["top_playtime"])
                + "</ol></div>")
        if data.get("bounties"):
            boards.append("<div class='card'><h2>🎯 Bounties</h2><ol>" + "".join(
                f"<li>{esc(r['name'])} – {esc(self.fmt_points(r['amount']))}</li>" for r in data["bounties"])
                + "</ol></div>")
        invite = self.setting("invite").strip()
        accent = self.setting("accent").strip()
        join = self.setting("how_to_join").strip()
        players = (f"{data['players']} / {data['max_players']}" if data["max_players"] else str(data["players"])) \
            if data["online"] else "–"
        return PAGE.format(
            title=esc(data["name"]), name=esc(data["name"]), platform=esc(data["platform"]),
            accent=accent if re.fullmatch(r"#[0-9a-fA-F]{6}", accent) else "#ce422b",
            invite=f"<a class='btn' href='{esc(invite)}' rel='noopener'>Join our Discord</a>" if INVITE.match(invite)
            else "",
            state="Online" if data["online"] else "Offline", state_class="on" if data["online"] else "off",
            players=esc(players), queue=f"{data['queued']} in queue" if data["queued"] else "players online",
            wipe=esc(wipe) if data["next_wipe"] else wipe, wipe_in=esc(wipe_in),
            peak=f"<div class='card'><h2>Busiest today</h2><div class='big'>{data['peak_24h']}</div>"
                 f"<div class='muted'>players at the peak (24 h)</div></div>" if data["peak_24h"] else "",
            boards="".join(boards),
            join=f"<div class='card' style='margin-top:14px'><h2>How to join</h2><div class='join'>{esc(join)}</div></div>"
            if join else "",
            updated=esc(datetime.fromtimestamp(data["updated"], tz).strftime("%H:%M")))

    async def web_public(self, request, path: str):
        """Public (no login): /p/public_page and /p/public_page/data.json"""
        from aiohttp import web
        headers = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer", "Cache-Control": "public, max-age=30"}
        data = self.snapshot()
        if path == "data.json":
            return web.Response(text=json.dumps(data), content_type="application/json", headers=headers)
        if path not in ("", "index.html"):
            raise web.HTTPNotFound()
        return web.Response(text=self.render(data), content_type="text/html", headers=headers)

    def status(self) -> dict:
        return {"Page": "/p/public_page"}
