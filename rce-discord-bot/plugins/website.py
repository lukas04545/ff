"""Player website: a public website for your players – live status, next wipe, leaderboards, player profiles,
the points shop (with item pictures), rules and how to join.

Address: your web interface link + /play (shown on the plugin card). Share it in server lists, ads, Discord and
social media bios. Read-only: no login, no settings, no Discord IDs – only what's already public on Discord.
"""
from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime
from urllib.parse import quote, urlencode

from plugin_api import Plugin, Setting
from utils import format_duration, strip_rich_text

INVITE = re.compile(r"^https://(discord\.gg|discord\.com/invite)/[\w-]{2,40}$")
CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")
BOARDS = {"kills": "Kills", "kd": "K/D", "playtime": "Playtime", "teams": "Teams"}
CACHE_SECONDS = 20

STYLE = """
:root{--bg:#0d0f13;--card:#161a21;--card2:#1d222b;--text:#eceef1;--muted:#9aa3ae;--line:#262c36;--accent:ACCENT;
--ok:#2ecc71;--bad:#e5533d;--gold:#f1c40f}
@media (prefers-color-scheme:light){:root{--bg:#f3f4f6;--card:#fff;--card2:#f6f7f9;--text:#1b1e23;--muted:#5c6470;--line:#e3e6ea}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:inherit}header{background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:2}
.bar{max-width:1040px;margin:0 auto;padding:10px 16px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.brand{font-weight:800;font-size:1.1rem;text-decoration:none;display:flex;align-items:center;gap:8px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--bad);display:inline-block}.dot.on{background:var(--ok)}
nav{display:flex;gap:4px;flex-wrap:wrap;margin-left:auto}nav a{text-decoration:none;padding:6px 11px;border-radius:8px;
color:var(--muted);font-weight:600;font-size:.93rem}nav a:hover{color:var(--text);background:var(--card2)}
nav a.active{color:#fff;background:var(--accent)}main{max-width:1040px;margin:0 auto;padding:22px 16px 40px}
.hero{background:linear-gradient(135deg,var(--accent),#1d222b);color:#fff;border-radius:16px;padding:28px 24px;margin-bottom:18px}
.hero h1{margin:0 0 4px;font-size:2rem;line-height:1.15}.hero p{margin:0;opacity:.9}
.btn{display:inline-block;margin:14px 8px 0 0;background:#fff;color:#111;padding:10px 18px;border-radius:10px;
text-decoration:none;font-weight:700}.btn.alt{background:rgba(255,255,255,.15);color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:14px}
.grid .card{margin-bottom:0}.card h2{margin:0 0 10px;font-size:1.05rem}.muted{color:var(--muted)}
.big{font-size:2rem;font-weight:800;line-height:1.2}.pill{display:inline-block;padding:2px 10px;border-radius:99px;
font-size:.8rem;font-weight:700;color:#fff;background:var(--bad)}.pill.on{background:var(--ok)}
table{width:100%;border-collapse:collapse}td,th{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left}
th{font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}td.n{width:2.5rem;color:var(--muted)}
td.r,th.r{text-align:right}tr:last-child td{border-bottom:0}.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.tabs a{text-decoration:none;padding:6px 12px;border-radius:99px;border:1px solid var(--line);font-weight:600;font-size:.9rem}
.tabs a.active{background:var(--accent);border-color:var(--accent);color:#fff}
form.search{display:flex;gap:8px}form.search input{flex:1;padding:10px 12px;border-radius:10px;border:1px solid var(--line);
background:var(--card2);color:var(--text);font:inherit}form.search button{padding:10px 16px;border-radius:10px;border:0;
background:var(--accent);color:#fff;font-weight:700;font:inherit;cursor:pointer}
.items{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}.it{position:relative;width:58px;height:58px;border-radius:10px;
background:var(--card2);border:1px solid var(--line);display:flex;align-items:center;justify-content:center}
.it img{width:44px;height:44px;object-fit:contain}.it .q{position:absolute;right:4px;bottom:1px;font-size:.72rem;font-weight:800}
.it .t{font-size:.6rem;text-align:center;padding:2px;word-break:break-all;color:var(--muted)}
.price{font-weight:800;color:var(--gold)}.badges{display:flex;flex-wrap:wrap;gap:6px}.badge{background:var(--card2);
border:1px solid var(--line);border-radius:99px;padding:3px 10px;font-size:.88rem}ol.rules{padding-left:1.3rem}
ol.rules li{margin:6px 0}.pre{white-space:pre-wrap}footer{max-width:1040px;margin:0 auto;padding:0 16px 30px;
font-size:.8rem;color:var(--muted)}@media (max-width:600px){.hero h1{font-size:1.5rem}.big{font-size:1.6rem}
nav{margin-left:0;width:100%}nav a{padding:6px 9px}}
"""


def esc(text) -> str:
    return html.escape(str(text), quote=True)


class PlayerWebsite(Plugin):
    NAME = "Player website"
    ICON = "🌍"
    PUBLIC_PATH = "/play"
    DESCRIPTION = ("A public website for your players: live status, next wipe, leaderboards, player profiles, the "
                   "points shop with item pictures, rules and how to join. Share the link everywhere.")
    SETTINGS = (
        Setting("title", "Website title", "text", "", help="Empty = the server name."),
        Setting("tagline", "Tagline", "text", "Rust Console Edition community server",
                help="Shown under the title on the home page."),
        Setting("invite", "Discord invite link", "text", "", help="e.g. https://discord.gg/abc123 – shown as a button."),
        Setting("store", "Store link", "text", "", help="Optional, e.g. your Tip4Serv store (https://…)."),
        Setting("about", "About the server", "textarea", "",
                help="A few lines about your server (rates, team size, wipe days …). Plain text."),
        Setting("how_to_join", "How to join", "textarea",
                "Open Rust Console Edition → Play → Community servers → search for our server name.",
                help="Shown on the home page."),
        Setting("leaderboards", "Leaderboards and player profiles", "bool", True),
        Setting("shop", "Show the points shop", "bool", True),
        Setting("rules", "Show the rules", "bool", True),
        Setting("accent", "Colour", "text", "#ce422b", help="Hex colour, e.g. #ce422b."),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._status: tuple[float, dict] | None = None

    # ------------------------------------------------------------ Data

    def site_title(self) -> str:
        return self.setting("title").strip() or (self.server_name if self.server_name != "the server"
                                                 else "Rust Console server")

    def live(self) -> dict:
        if self._status and time.monotonic() - self._status[0] < CACHE_SECONDS:
            return self._status[1]
        info = self.bot.server_info or {}
        online = bool(self.bot.rcon.connected and info)
        data = {"online": online,
                "players": int(info.get("Players") or len(self.bot.online_players)) if online else 0,
                "max_players": int(info.get("MaxPlayers") or 0) if online else 0,
                "queued": int(info.get("Queued") or 0) if online else 0,
                "next_wipe": self.next_wipe(),
                "peak_24h": int(max((p[1] for p in self.bot.db.population(self.now() - 86400)), default=0))}
        self._status = (time.monotonic(), data)
        return data

    def bounties(self) -> list[tuple[str, int]]:
        plugin = self.plugin("bounties")
        if plugin is None:
            return []
        totals: dict[str, int] = {}
        for b in plugin.bounties():
            totals[b["target"]] = totals.get(b["target"], 0) + b["amount"]
        return sorted(totals.items(), key=lambda x: -x[1])

    def find_player(self, name: str):
        name = " ".join(str(name or "").split())[:64]
        return self.bot.db.player(name) if name else None

    # ------------------------------------------------------------ Layout

    def page(self, base: str, active: str, title: str, body: str) -> str:
        links = [("", "Home")]
        if self.setting("leaderboards"):
            links += [("leaderboard", "Leaderboard"), ("player", "Players")]
        if self.setting("shop") and self.points_enabled:
            links.append(("shop", "Shop"))
        if self.setting("rules"):
            links.append(("rules", "Rules"))
        nav = "".join(f'<a href="{base}/{path}" class="{"active" if path == active else ""}">{esc(label)}</a>'
                      for path, label in links)
        accent = self.setting("accent").strip()
        accent = accent if re.fullmatch(r"#[0-9a-fA-F]{6}", accent) else "#ce422b"
        live = self.live()
        site = self.site_title()
        updated = datetime.fromtimestamp(self.now(), self.tz).strftime("%H:%M")
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width, initial-scale=1">'
                f'<title>{esc(title + " – " if title else "")}{esc(site)}</title>'
                f'<meta name="description" content="{esc(self.setting("tagline"))}">'
                f'<style>{STYLE.replace("ACCENT", accent)}</style></head><body>'
                f'<header><div class="bar"><a class="brand" href="{base}/"><span class="dot{" on" if live["online"] else ""}">'
                f'</span>{esc(site)}</a><nav>{nav}</nav></div></header><main>{body}</main>'
                f'<footer>Updated {esc(updated)} · {esc(self.bot.config.rust_server_platform)}</footer></body></html>')

    def item_tiles(self, items) -> str:
        tiles = []
        for short, amount in items[:40]:
            short_q = quote(short)
            tiles.append(f'<div class="it" title="{esc(short)} × {amount}"><img src="/img/items/{short_q}.png?ph=1" '
                         f'alt="{esc(short)}" loading="lazy"><span class="q">{"×" + str(amount) if amount > 1 else ""}'
                         f'</span></div>')
        return f'<div class="items">{"".join(tiles)}</div>' if tiles else ""

    # ------------------------------------------------------------ Pages

    def home(self, base: str) -> str:
        live, tz = self.live(), self.tz
        invite, store = self.setting("invite").strip(), self.setting("store").strip()
        buttons = ""
        if INVITE.match(invite):
            buttons += f'<a class="btn" href="{esc(invite)}" rel="noopener">Join our Discord</a>'
        if store.startswith("https://") and len(store) < 300:
            buttons += f'<a class="btn alt" href="{esc(store)}" rel="noopener">Store</a>'
        players = (f'{live["players"]} / {live["max_players"]}' if live["max_players"] else str(live["players"])) \
            if live["online"] else "–"
        if live["next_wipe"]:
            wipe = datetime.fromtimestamp(live["next_wipe"], tz).strftime("%a %d %b, %H:%M")
            wipe_sub = f'in {format_duration(max(live["next_wipe"] - self.now(), 0))} ({getattr(tz, "key", "UTC")})'
        else:
            wipe, wipe_sub = self.bot.config.rust_server_wipe or "Announced on Discord", ""
        body = (f'<section class="hero"><h1>{esc(self.site_title())}</h1><p>{esc(self.setting("tagline"))}</p>'
                f'{buttons}</section><div class="grid">'
                f'<div class="card"><h2>Server</h2><span class="pill{" on" if live["online"] else ""}">'
                f'{"Online" if live["online"] else "Offline"}</span><div class="big">{esc(players)}</div>'
                f'<div class="muted">{str(live["queued"]) + " in queue" if live["queued"] else "players online"}</div></div>'
                f'<div class="card"><h2>Next wipe</h2><div class="big" style="font-size:1.3rem">{esc(wipe)}</div>'
                f'<div class="muted">{esc(wipe_sub)}</div></div>')
        if live["peak_24h"]:
            body += (f'<div class="card"><h2>Busiest today</h2><div class="big">{live["peak_24h"]}</div>'
                     f'<div class="muted">players at the peak (24 h)</div></div>')
        body += "</div>"
        about = self.setting("about").strip()
        if about:
            body += f'<div class="card"><h2>About</h2><div class="pre">{esc(about)}</div></div>'
        cols = []
        if self.setting("leaderboards"):
            top = [r for r in self.bot.db.leaderboard("kills", 5) if r["kills"]]
            if top:
                rows = "".join(f'<tr><td class="n">{i}</td><td>{self.player_link(base, r["name"])}</td>'
                               f'<td class="r">{r["kills"]}</td></tr>' for i, r in enumerate(top, 1))
                cols.append(f'<div class="card"><h2>⚔️ Top killers</h2><table>{rows}</table>'
                            f'<p><a href="{base}/leaderboard">Full leaderboard →</a></p></div>')
        bounties = self.bounties()[:5]
        if bounties:
            rows = "".join(f'<tr><td>{self.player_link(base, n)}</td><td class="r price">{esc(self.fmt_points(a))}</td></tr>'
                           for n, a in bounties)
            cols.append(f'<div class="card"><h2>🎯 Bounties</h2><table>{rows}</table>'
                        f'<p class="muted">Kill them to collect – /bounty on Discord.</p></div>')
        if cols:
            body += f'<div class="grid">{"".join(cols)}</div>'
        join = self.setting("how_to_join").strip()
        if join:
            body += f'<div class="card"><h2>How to join</h2><div class="pre">{esc(join)}</div></div>'
        early = self.plugin("early_bird")
        if early is not None and live["next_wipe"]:
            body += (f'<div class="card"><h2>🐦 Wipe day bonus</h2>The first {early.setting("players")} linked players '
                     f'online after the wipe get a reward. Link your player on Discord with /customkit link.</div>')
        return self.page(base, "", "", body)

    def player_link(self, base: str, name: str) -> str:
        if not self.setting("leaderboards"):
            return esc(name)
        return f'<a href="{base}/player?{urlencode({"name": name})}">{esc(name)}</a>'

    def leaderboard(self, base: str, by: str) -> str:
        by = by if by in BOARDS else "kills"
        tabs = "".join(f'<a href="{base}/leaderboard?by={k}" class="{"active" if k == by else ""}">{v}</a>'
                       for k, v in BOARDS.items())
        if by == "teams":
            rows = self.bot.db.team_leaderboard("kills", 25)
            body = "".join(f'<tr><td class="n">{i}</td><td>Team {esc(r["leader"] or "?")}<div class="muted">'
                           f'{esc(r["names"][:120])}</div></td><td class="r">{r["members"]}</td><td class="r">{r["kills"]}'
                           f'</td></tr>' for i, r in enumerate(rows, 1))
            table = (f'<table><tr><th></th><th>Team</th><th class="r">Members</th><th class="r">Kills</th></tr>{body}'
                     f'</table>') if rows else '<p class="muted">No teams yet.</p>'
        else:
            rows = [r for r in self.bot.db.leaderboard(by, 50) if r["kills"] or r["playtime_seconds"]]
            body = "".join(
                f'<tr><td class="n">{i}</td><td>{self.player_link(base, r["name"])}</td><td class="r">{r["kills"]}</td>'
                f'<td class="r">{r["deaths"]}</td><td class="r">{r["kills"] / max(r["deaths"], 1):.2f}</td>'
                f'<td class="r">{esc(format_duration(r["playtime_seconds"]))}</td></tr>' for i, r in enumerate(rows, 1))
            table = (f'<table><tr><th></th><th>Player</th><th class="r">Kills</th><th class="r">Deaths</th>'
                     f'<th class="r">K/D</th><th class="r">Playtime</th></tr>{body}</table>') if rows \
                else '<p class="muted">No stats yet.</p>'
        return self.page(base, "leaderboard", "Leaderboard",
                         f'<h1>Leaderboard</h1><div class="tabs">{tabs}</div><div class="card" style="overflow-x:auto">'
                         f'{table}</div>')

    def player(self, base: str, name: str) -> str:
        form = (f'<form class="search" method="get" action="{base}/player"><input name="name" '
                f'placeholder="Gamertag / PSN name" value="{esc(name)}" maxlength="64" required>'
                f'<button type="submit">Search</button></form>')
        body = f'<h1>Players</h1><div class="card">{form}</div>'
        row = self.find_player(name) if name else None
        if name and row is None:
            matches = self.bot.db.search_names(name.strip(), 20)
            body += ('<div class="card"><h2>Did you mean</h2>' + "".join(
                f'<div>{self.player_link(base, m)}</div>' for m in matches) + "</div>") if matches else \
                f'<div class="card muted">No player called {esc(name)} has been seen yet.</div>'
        elif row is not None:
            body += self.profile(base, row)
        else:
            online = sorted(self.bot.online_players, key=str.lower)
            body += (f'<div class="card"><h2>Online now ({len(online)})</h2>' +
                     (", ".join(self.player_link(base, n) for n in online) or '<span class="muted">Nobody.</span>')
                     + "</div>")
        return self.page(base, "player", row["name"] if row is not None else "Players", body)

    def profile(self, base: str, row) -> str:
        name = row["name"]
        online = self.online_name(name) is not None
        kd = row["kills"] / max(row["deaths"], 1)
        stats = [("Kills", row["kills"]), ("Deaths", row["deaths"]), ("K/D", f"{kd:.2f}"),
                 ("Playtime", format_duration(row["playtime_seconds"]))]
        cards = "".join(f'<div class="card"><h2>{k}</h2><div class="big">{esc(v)}</div></div>' for k, v in stats)
        extra = []
        if row["platform"]:
            extra.append(f"Platform: {esc(row['platform'])}")
        team = self.bot.db.team_of(name)
        if team:
            extra.append("Team: " + ", ".join(esc(m) for m in team[1]))
        bounty = dict(self.bounties()).get(name)
        if bounty:
            extra.append(f'🎯 Bounty: <span class="price">{esc(self.fmt_points(bounty))}</span>')
        html_out = (f'<div class="card"><h2 style="font-size:1.5rem;margin:0">{esc(name)} '
                    f'<span class="pill{" on" if online else ""}">{"online" if online else "offline"}</span></h2>'
                    + ("<p>" + " · ".join(extra) + "</p>" if extra else "") + "</div>"
                    f'<div class="grid">{cards}</div>')
        achievements, user_id = self.plugin("achievements"), self.linked_user(name)
        if achievements is not None and user_id is not None:
            from plugins.achievements import BADGES
            have = set(achievements.unlocked(user_id))
            badges = "".join(f'<span class="badge">{b.icon} {esc(b.name)}</span>' for b in BADGES if b.id in have)
            if badges:
                html_out += f'<div class="card"><h2>🏅 Badges</h2><div class="badges">{badges}</div></div>'
        return html_out

    def shop(self, base: str) -> str:
        rows = self.bot.db.shop_items(enabled_only=True)
        points = self.bot.config.points_name
        cards = []
        guild = self.guild
        for r in rows:
            if r["kind"] == "custom_kit":
                items = [(i["shortname"], i["amount"]) for i in self.bot.db.custom_kit_items(r["target"])]
                what = self.item_tiles(items)
            elif r["kind"] == "item":
                what = self.item_tiles([(r["target"], r["amount"])])
            elif r["kind"] == "kit":
                what = f'<div class="muted">In-game kit {esc(r["target"])}</div>'
            else:
                role = guild.get_role(int(r["target"])) if guild and str(r["target"]).isdigit() else None
                days = f' for {r["amount"]} days' if r["amount"] else ""
                what = f'<div class="muted">Discord role {esc(role.name if role else "")}{days}</div>'
            desc = f'<div class="muted">{esc(r["description"])}</div>' if r["description"] else ""
            price = f"{int(r['price']):,}".replace(",", " ")
            cards.append(f'<div class="card"><h2>{esc(r["name"])}</h2><div class="price">'
                         f'{esc(price)} {esc(points)}</div>{desc}{what}</div>')
        earn = []
        cfg = self.bot.config
        if cfg.points_per_hour:
            earn.append(f"{cfg.points_per_hour} per hour online")
        if cfg.points_per_kill:
            earn.append(f"{cfg.points_per_kill} per PvP kill")
        if cfg.daily_points:
            earn.append(f"{cfg.daily_points}+ with /daily")
        intro = (f'<div class="card"><h2>How it works</h2>Link your player on Discord with <b>/customkit link</b>, '
                 f'earn {esc(points)} ({esc(", ".join(earn) or "by playing")}) and buy with <b>/shop buy</b>. '
                 f'Items are sent to you in-game.</div>')
        body = (f'<h1>Shop</h1>{intro}<div class="grid">{"".join(cards)}</div>' if cards
                else f'<h1>Shop</h1>{intro}<p class="muted">The shop is empty right now.</p>')
        return self.page(base, "shop", "Shop", body)

    def rules(self, base: str) -> str:
        from cogs.community import load_rules
        rules = load_rules(self.bot.config.rules_file)
        items = "".join(f"<li>{esc(strip_rich_text(r))}</li>" for r in rules)
        body = f'<h1>Rules</h1><div class="card"><ol class="rules">{items}</ol></div>' if items else \
            '<h1>Rules</h1><p class="muted">No rules yet.</p>'
        return self.page(base, "rules", "Rules", body)

    def not_found(self, base: str) -> str:
        return self.page(base, "", "Not found", f'<h1>Not found</h1><p><a href="{base}/">Back to the home page</a></p>')

    # ------------------------------------------------------------ Web

    def render(self, base: str, path: str, query) -> tuple[int, str]:
        path = path.strip("/")
        if path in ("", "index.html"):
            return 200, self.home(base)
        if path == "leaderboard" and self.setting("leaderboards"):
            return 200, self.leaderboard(base, str(query.get("by", "kills")))
        if path == "player" and self.setting("leaderboards"):
            return 200, self.player(base, str(query.get("name", ""))[:64])
        if path == "shop" and self.setting("shop") and self.points_enabled:
            return 200, self.shop(base)
        if path == "rules" and self.setting("rules"):
            return 200, self.rules(base)
        return 404, self.not_found(base)

    async def web_public(self, request, path: str):
        """Public (no login): /play/… (and /p/website/…); /play/status.json for other tools."""
        from aiohttp import web
        headers = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer", "Cache-Control": "public, max-age=20"}
        if path == "status.json":
            data = {"name": self.site_title(), **self.live()}
            return web.Response(text=json.dumps(data), content_type="application/json", headers=headers)
        base = "/play" if request.path.startswith("/play") else f"/p/{self.plugin_id}"
        status, text = self.render(base, path, request.query)
        return web.Response(status=status, text=text, content_type="text/html", headers=headers)
