"""Player website: a public website for your players – live status, next wipe, leaderboards, player profiles,
the points shop (with item pictures), rules and how to join.

Address: the root of your web interface link (shown on the plugin card). Share it in server lists, ads, Discord and
social media bios. Read-only: no login, no settings, no Discord IDs – only what's already public on Discord.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from plugin_api import Plugin, Setting
from web.caching import YEAR, conditional
from utils import format_duration, strip_rich_text

INVITE = re.compile(r"^https://(discord\.gg|discord\.com/invite)/[\w-]{2,40}$")
# Scripts only from the website itself (no inline or third-party scripts – injected code can't run)
CSP = ("default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; "
       "img-src 'self' data:; base-uri 'none'; "
       "form-action 'self' https://www.paypal.com https://www.sandbox.paypal.com; frame-ancestors 'none'")
BOARDS = {"kills": "Kills", "kd": "K/D", "playtime": "Playtime", "teams": "Teams"}
CACHE_SECONDS = 20
SCRIPTS = ("casino.js", "poker.js")             # scripts served from website_assets/
HEADER_LOGO = 96                     # px of the small logo in the header (2× its 42 px for sharp phone screens)

ASSETS = Path(__file__).resolve().parent / "website_assets"     # default logo (replace it on the Plugins page)
NOISE = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E"
         "%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E"
         "%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.09'/%3E%3C/svg%3E")

# Dark, burnt "Rust" look: black background with a sunset glow, rusty red/orange accents, rugged cut corners
STYLE = """
:root{--bg:#060606;--card:rgba(18,13,11,.88);--card2:rgba(34,22,17,.9);--text:#f1ebe6;--muted:#a8998f;
--line:rgba(229,84,40,.22);--red:ACCENT;--orange:#f7931e;--ok:#3ddc6a;--bad:#e5341c;--gold:#ffb627;
--grad:linear-gradient(90deg,var(--red),var(--orange));--cut:14px;
--head:Impact,"Haettenschweiler","Arial Narrow Bold","Franklin Gothic Heavy","Oswald",sans-serif}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%;background:var(--bg)}
body{margin:0;color:var(--text);font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;min-height:100vh}
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
background:url("NOISE"),radial-gradient(ellipse 90% 55% at 50% -8%,rgba(247,110,30,.32),transparent 70%),
radial-gradient(ellipse 60% 40% at 85% 10%,rgba(229,52,28,.18),transparent 70%),
linear-gradient(180deg,#120a07 0%,var(--bg) 55%)}
a{color:inherit}a:hover{color:var(--orange)}td a,.card p a{text-decoration:none;border-bottom:1px solid rgba(247,147,30,.4)}
h1,h2,h3,.brand,nav a,.tabs a,.btn,th,.big,.kicker{font-family:var(--head);text-transform:uppercase;letter-spacing:.06em;
font-weight:400}
header{background:rgba(6,6,6,.94);position:sticky;top:0;z-index:5}
header::after{content:"";display:block;height:2px;background:var(--grad);opacity:.85}
.bar{max-width:1080px;margin:0 auto;padding:8px 16px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.brand{font-size:1.3rem;text-decoration:none;display:flex;align-items:center;gap:10px;color:#fff}
.brand img{width:42px;height:42px;border-radius:50%;box-shadow:0 0 14px rgba(247,110,30,.45)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--bad);display:inline-block;box-shadow:0 0 8px currentColor;
color:var(--bad)}.dot.on{background:var(--ok);color:var(--ok)}
nav{display:flex;gap:2px;flex-wrap:wrap;margin-left:auto}nav a{text-decoration:none;padding:7px 12px;color:var(--muted);
font-size:1.02rem;position:relative}nav a:hover{color:#fff}
nav a.active{color:#fff}nav a.active::after{content:"";position:absolute;left:10px;right:10px;bottom:2px;height:3px;
background:var(--grad);clip-path:polygon(0 0,100% 0,96% 100%,4% 100%)}
main{max-width:1080px;margin:0 auto;padding:26px 16px 40px}
h1{font-size:2.3rem;margin:.2rem 0 1rem;color:#fff;text-shadow:0 2px 0 #000,0 0 22px rgba(229,52,28,.35)}
h1::after{content:"";display:block;width:90px;height:4px;margin-top:6px;background:var(--grad);
clip-path:polygon(0 0,100% 0,94% 100%,0 100%)}
.hero{text-align:center;padding:10px 0 26px}
.hero .logo{width:min(360px,74vw);aspect-ratio:1;filter:drop-shadow(0 0 28px rgba(247,90,30,.45)) drop-shadow(0 6px 18px #000)}
.hero h1{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.kicker{display:flex;align-items:center;justify-content:center;gap:14px;color:#e9dfd8;font-size:1.15rem;
letter-spacing:.32em;margin:10px 0 2px}.kicker::before,.kicker::after{content:"";height:3px;width:min(70px,12vw);
background:var(--grad)}.motto{color:var(--red);font-family:var(--head);letter-spacing:.25em;text-transform:uppercase;
font-size:.95rem;margin:6px 0 0;text-shadow:0 0 12px rgba(229,52,28,.5)}
.btn{display:inline-block;margin:18px 6px 0;background:var(--grad);color:#fff;padding:11px 26px;text-decoration:none;
font-size:1.15rem;clip-path:polygon(10px 0,100% 0,calc(100% - 10px) 100%,0 100%);box-shadow:0 0 20px rgba(229,52,28,.35)}
.btn:hover{color:#fff;filter:brightness(1.12)}.btn.alt{background:rgba(255,255,255,.08);outline:1px solid var(--line)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);padding:18px 20px;margin-bottom:14px;position:relative;
clip-path:polygon(0 0,calc(100% - var(--cut)) 0,100% var(--cut),100% 100%,var(--cut) 100%,0 calc(100% - var(--cut)));
box-shadow:inset 0 1px 0 rgba(255,255,255,.03)}
.card::before{content:"";position:absolute;left:0;top:0;width:60px;height:3px;background:var(--grad)}
.grid .card{margin-bottom:0}.card h2{margin:0 0 10px;font-size:1.25rem;color:#fff}.muted{color:var(--muted)}
.big{font-size:2.4rem;line-height:1.1;background:var(--grad);-webkit-background-clip:text;background-clip:text;
color:transparent}.pill{display:inline-block;padding:1px 10px;font-size:.78rem;font-weight:800;color:#fff;
background:var(--bad);letter-spacing:.08em;text-transform:uppercase;clip-path:polygon(6px 0,100% 0,calc(100% - 6px) 100%,0 100%)}
.pill.on{background:#1f9d4a}
table{width:100%;border-collapse:collapse}td,th{padding:9px 6px;border-bottom:1px solid rgba(255,255,255,.06);text-align:left}
th{font-size:.9rem;color:var(--muted)}td.n{width:2.6rem;font-family:var(--head);color:var(--orange);font-size:1.1rem}
tr:nth-child(1) td.n{color:var(--gold)}td.r,th.r{text-align:right}tr:last-child td{border-bottom:0}
tbody tr:hover,table tr:hover td{background:rgba(229,84,40,.06)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}.tabs a{text-decoration:none;padding:6px 16px;
background:var(--card2);border:1px solid var(--line);font-size:1rem;clip-path:polygon(8px 0,100% 0,calc(100% - 8px) 100%,0 100%)}
.tabs a.active{background:var(--grad);color:#fff;border-color:transparent}
form.search{display:flex;gap:8px}form.search input{flex:1;padding:11px 13px;border:1px solid var(--line);
background:rgba(0,0,0,.45);color:var(--text);font:inherit;outline:none}form.search input:focus{border-color:var(--orange)}
form.search button{padding:10px 20px;border:0;background:var(--grad);color:#fff;font-family:var(--head);font-size:1.05rem;
letter-spacing:.06em;text-transform:uppercase;cursor:pointer}
.items{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}.it{position:relative;width:60px;height:60px;
background:radial-gradient(circle at 50% 40%,rgba(255,255,255,.07),rgba(0,0,0,.35));border:1px solid rgba(255,255,255,.08);
display:flex;align-items:center;justify-content:center}.it img{width:46px;height:46px;object-fit:contain}
.it .q{position:absolute;right:4px;bottom:0;font-size:.74rem;font-weight:800;text-shadow:0 1px 2px #000}
.price{font-family:var(--head);font-size:1.3rem;letter-spacing:.04em;color:var(--gold)}
.badges{display:flex;flex-wrap:wrap;gap:6px}.badge{background:var(--card2);border:1px solid var(--line);padding:3px 12px;
font-size:.9rem}ol.rules{padding-left:1.5rem;margin:0}ol.rules li{margin:8px 0;padding-left:4px}
ol.rules li::marker{color:var(--orange);font-family:var(--head)}.pre{white-space:pre-wrap}
footer{max-width:1080px;margin:0 auto;padding:18px 16px 34px;font-size:.82rem;color:var(--muted);text-align:center;
border-top:1px solid rgba(255,255,255,.06)}footer .motto{font-size:.85rem;margin-bottom:4px}
@media (max-width:640px){h1{font-size:1.8rem}.big{font-size:1.9rem}.bar{gap:6px 10px}
nav{margin:0 -16px;padding:0 10px;width:calc(100% + 32px);flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none}
nav a{padding:6px 9px;font-size:.95rem;white-space:nowrap}.kicker{font-size:.9rem;letter-spacing:.16em;gap:10px;
white-space:nowrap}.kicker::before,.kicker::after{width:22px}.motto{font-size:.8rem;letter-spacing:.15em}
.btn{padding:10px 20px;font-size:1.05rem}}
""".replace("NOISE", NOISE)


def esc(text) -> str:
    return html.escape(str(text), quote=True)


class PlayerWebsite(Plugin):
    NAME = "Player website"
    ICON = "🌍"
    PUBLIC_PATH = "/"                    # the site root (the control panel is at /admin)
    DESCRIPTION = ("A public website for your players: live status, next wipe, leaderboards, player profiles, the "
                   "points shop with item pictures, rules and how to join. Share the link everywhere.")
    SETTINGS = (
        Setting("title", "Website title", "text", "", help="Empty = the server name."),
        Setting("logo", "Logo", "image", help="Square picture (PNG/WebP with a transparent background looks best). "
                                              "Empty = the bundled Kryvon logo."),
        Setting("tagline", "Tagline", "text", "Rust Console Server",
                help="Shown under the logo on the home page."),
        Setting("motto", "Motto", "text", "Build · Survive · Dominate", help="Shown in red under the tagline."),
        Setting("invite", "Discord invite link", "text", "", help="e.g. https://discord.gg/abc123 – shown as a button."),
        Setting("store", "Store link", "text", "", help="Optional, e.g. your Tip4Serv store (https://…)."),
        Setting("about", "About the server", "textarea", "",
                help="A few lines about your server (rates, team size, wipe days …). Plain text."),
        Setting("how_to_join", "How to join", "textarea",
                "Open Rust Console Edition → Play → Community servers → search for our server name.",
                help="Shown on the home page."),
        Setting("leaderboards", "Leaderboards and player profiles", "bool", True),
        Setting("show_store", "Show the web store (kits for real money, PayPal)", "bool", True,
                help="Set it up in the web interface → Store and Settings → Store."),
        Setting("shop", "Show the points shop", "bool", True),
        Setting("rules", "Show the rules", "bool", True),
        Setting("accent", "Main colour", "text", "#e5341c", help="Hex colour, e.g. #e5341c (blends into orange)."),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._status: tuple[float, dict] | None = None
        self._pictures: dict[tuple, tuple[bytes, str]] = {}     # (which, size, logo file state) -> picture
        self._style: tuple[str, str] | None = None              # (accent, finished CSS)
        self._assets: dict[str, tuple] = {}                      # script name -> (file state, bytes, hash)

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

    def store(self):
        """The web store (cogs/webshop.py) if it has something to sell, else None."""
        shop = self.bot.get_cog("WebShop")
        if shop is None or not self.setting("show_store"):
            return None
        kits = bool(self.bot.db.store_kits(enabled_only=True))     # (can also be paid with gift cards alone)
        paypal_only = shop.paypal is not None and (shop.builder_problem() is None or bool(shop.giftcard_values()))
        return shop if kits or paypal_only else None

    def accounts(self):
        """The PlayerAccounts cog if player login is on, else None."""
        cog = self.bot.get_cog("PlayerAccounts")
        return cog if cog is not None and cog.enabled else None

    def casino(self):
        """The Casino plugin if it's on and players can log in, else None."""
        casino = self.plugin("casino")
        return casino if casino is not None and casino.points_enabled and self.accounts() is not None else None

    def find_player(self, name: str):
        name = " ".join(str(name or "").split())[:64]
        return self.bot.db.player(name) if name else None

    # ------------------------------------------------------------ Layout

    def page(self, base: str, active: str, title: str, body: str) -> str:
        links = [("", "Home")]
        if self.setting("leaderboards"):
            links += [("leaderboard", "Leaderboard"), ("player", "Players")]
        if self.store() is not None:
            links.append(("store", "Store"))
        if self.casino() is not None:
            links.append(("casino", "Casino"))
        if self.accounts() is not None:
            links.append(("account", "My account"))
        if self.setting("shop") and self.points_enabled:
            links.append(("shop", "Points"))
        if self.setting("rules"):
            links.append(("rules", "Rules"))
        nav = "".join(f'<a href="{base}/{path}" class="{"active" if path == active else ""}">{esc(label)}</a>'
                      for path, label in links)
        accent = self.setting("accent").strip()
        accent = accent if re.fullmatch(r"#[0-9a-fA-F]{6}", accent) else "#e5341c"
        if self._style is None or self._style[0] != accent:
            self._style = (accent, STYLE.replace("ACCENT", accent))
        version = self.picture_version()
        live = self.live()
        site = self.site_title()
        motto = f'<div class="motto">{esc(self.setting("motto"))}</div>' if self.setting("motto").strip() else ""
        updated = datetime.fromtimestamp(self.now(), self.tz).strftime("%H:%M")
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width, initial-scale=1">'
                f'<title>{esc(title + " – " if title else "")}{esc(site)}</title>'
                f'<meta name="description" content="{esc(self.setting("tagline"))}">'
                f'<meta name="theme-color" content="#060606"><link rel="icon" href="{base}/icon?v={version}">'
                f'<style>{self._style[1]}</style></head><body>'
                f'<header><div class="bar"><a class="brand" href="{base}/"><img src="{base}/logo?s={HEADER_LOGO}'
                f'&amp;v={version}" alt="" width="42" height="42" decoding="async">'
                f'<span class="name">{esc(site)}</span><span class="dot{" on" if live["online"] else ""}" '
                f'title="{"Online" if live["online"] else "Offline"}"></span></a><nav>{nav}</nav></div></header>'
                f'<main>{body}</main><footer>{motto}Updated {esc(updated)} · '
                f'{esc(self.bot.config.rust_server_platform)}</footer></body></html>')

    def item_tiles(self, items) -> str:
        tiles = []
        for short, amount in items[:40]:
            short_q = quote(short)
            tiles.append(f'<div class="it" title="{esc(short)} × {amount}"><img src="/img/items/{short_q}.png?ph=1&amp;'
                         f's=96" alt="{esc(short)}" width="46" height="46" loading="lazy" decoding="async">'
                         f'<span class="q">{"×" + str(amount) if amount > 1 else ""}</span></div>')
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
        tagline, motto = self.setting("tagline").strip(), self.setting("motto").strip()
        body = (f'<section class="hero"><img class="logo" src="{base}/logo?v={self.picture_version()}" '
                f'alt="{esc(self.site_title())}" width="360" height="360" fetchpriority="high">'
                f'<h1>{esc(self.site_title())}</h1>'
                + (f'<div class="kicker">{esc(tagline)}</div>' if tagline else "")
                + (f'<div class="motto">{esc(motto)}</div>' if motto else "")
                + f'{buttons}</section><div class="grid">'
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
        if path == "store" or path.startswith("store/"):
            return 404, self.not_found(base)          # handled in web_public (needs the request)
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

    def asset(self, name: str) -> tuple[bytes, str]:
        """A script from website_assets/ and its content hash (the address carries it, browsers keep it a year)."""
        path = ASSETS / name
        key = (name, path.stat().st_mtime_ns)
        if self._assets.get(name, (None,))[0] != key:
            data = path.read_bytes()
            self._assets[name] = (key, data, hashlib.blake2s(data, digest_size=6).hexdigest())
        return self._assets[name][1], self._assets[name][2]

    def asset_version(self, name: str) -> str:
        return self.asset(name)[1]

    def _logo_state(self) -> tuple:
        custom = self.file("logo")
        try:
            return (custom.name, custom.stat().st_mtime_ns, custom.stat().st_size) if custom is not None else ()
        except OSError:
            return ()

    def picture_version(self) -> str:
        """Changes when another logo is uploaded – the pictures' addresses carry it, so browsers keep them a year."""
        return hashlib.blake2s(repr(self._logo_state()).encode(), digest_size=4).hexdigest()

    def picture(self, which: str, size: int | None = None) -> tuple[bytes, str]:
        """The logo (uploaded on the Plugins page, else the bundled one) or the small icon for the browser tab.
        size: a smaller WebP copy (made once and kept in memory)."""
        key = (which, size, self._logo_state())
        if key not in self._pictures:
            if len(self._pictures) > 8:
                self._pictures.clear()
            self._pictures[key] = self._load_picture(which, size)
        return self._pictures[key]

    def _load_picture(self, which: str, size: int | None) -> tuple[bytes, str]:
        from web.item_images import image_type, shrink
        data, kind = b"", None
        custom = self.file("logo")
        if custom is not None:
            data = custom.read_bytes()
            kind = image_type(data)
        if not kind:
            data = (ASSETS / ("icon.png" if which == "icon" else "logo.webp")).read_bytes()
            kind = image_type(data) or "image/png"
        small = shrink(data, size) if size else None
        return (small, "image/webp") if small else (data, kind)

    async def web_public(self, request, path: str):
        """Public (no login): the site root /… (also /p/website/…); /status.json for other tools."""
        from aiohttp import web
        headers = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "same-origin", "Cache-Control": "public, max-age=20"}
        if path == "status.json":
            data = {"name": self.site_title(), **self.live()}
            return web.Response(text=json.dumps(data), content_type="application/json", headers=headers)
        if path in SCRIPTS:
            data, version = self.asset(path)
            return conditional(request, data, "application/javascript",
                               YEAR if request.query.get("v") == version else "no-cache", charset="utf-8",
                               headers={k: v for k, v in headers.items() if k != "Cache-Control"})
        if path in ("logo", "icon"):
            size = HEADER_LOGO if request.query.get("s") == str(HEADER_LOGO) else None
            data, kind = self.picture(path, size)
            versioned = request.query.get("v") == self.picture_version()
            return conditional(request, data, kind, YEAR if versioned else "public, max-age=3600",
                               headers={k: v for k, v in headers.items() if k != "Cache-Control"})
        # Links inside the site: "" at the root, /p/website/… there, /play/… for forms of a page opened before the move
        if request.path.startswith(f"/p/{self.plugin_id}"):
            base = f"/p/{self.plugin_id}"
        elif request.path == "/play" or request.path.startswith("/play/"):
            base = "/play"
        else:
            base = ""
        if path == "account" or path.startswith("account/"):
            accounts = self.accounts()
            if accounts is None:
                return web.Response(status=404, text=self.not_found(base), content_type="text/html",
                                    headers={**headers, "Cache-Control": "no-store"})
            from web.account_pages import handle as account_handle
            from web.server import WebInterface
            resp = await account_handle(self, accounts, self.store(), request, base, path,
                                        WebInterface._client_ip(request))
            for key, value in headers.items():
                resp.headers.setdefault(key, value)
            resp.headers["Cache-Control"] = "no-store"
            return resp
        if path == "casino" or path.startswith("casino/"):
            casino = self.casino()
            if casino is None:
                return web.Response(status=404, text=self.not_found(base), content_type="text/html",
                                    headers={**headers, "Cache-Control": "no-store"})
            from web.casino_pages import handle as casino_handle
            resp = await casino_handle(self, casino, request, base, path)
            for key, value in headers.items():
                resp.headers.setdefault(key, value)
            resp.headers["Cache-Control"] = "no-store"
            return resp
        if path == "store" or path.startswith("store/"):
            shop = self.store()
            if shop is None:
                status, text = 404, self.not_found(base)
            else:
                from web.server import WebInterface
                from web.store_pages import handle
                out = await handle(self, shop, request, base, path, WebInterface._client_ip(request))
                if isinstance(out, web.StreamResponse):
                    return out
                status, text = out
            headers = {**headers, "Cache-Control": "no-store"}
            return web.Response(status=status, text=text, content_type="text/html", headers=headers)
        if request.method != "GET":
            raise web.HTTPMethodNotAllowed(request.method, ["GET"])
        status, text = self.render(base, path, request.query)
        return web.Response(status=status, text=text, content_type="text/html", headers=headers)
