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

from discord.ext import tasks

from plugin_api import Plugin, Setting
from web.caching import YEAR, conditional
from utils import format_duration, strip_rich_text

INVITE = re.compile(r"^https://(discord\.gg|discord\.com/invite)/[\w-]{2,40}$")
# Scripts only from the website itself (no inline or third-party scripts – injected code can't run)
# Streams page (plugins/streamers.py): the platforms' own players in iframes and their stream thumbnails
CSP = ("default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; "
       "img-src 'self' data: https://static-cdn.jtvnw.net https://i.ytimg.com https://*.kick.com; "
       "frame-src https://player.twitch.tv https://player.kick.com https://www.youtube-nocookie.com; base-uri 'none'; "
       "form-action 'self' https://www.paypal.com https://www.sandbox.paypal.com; frame-ancestors 'none'")
BOARDS = {"kills": "Kills", "kd": "K/D", "playtime": "Playtime", "teams": "Teams"}
CACHE_SECONDS = 20
SCRIPTS = ("casino.js", "poker.js", "plinko.js", "site.js")             # scripts served from website_assets/
HEADER_LOGO = 96
GALLERY = 6                                                   # server pictures on the home page

# ------------------------------------------------------------ Seasonal theme: Halloween
# Pure CSS (no images to load): night sky with a moon, a few bats flying past, cobwebs on the cards, a spider on
# its thread under the menu, pumpkins. Bats only move transform (cheap) and stop with "reduce motion".
_BAT = quote('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 28"><path fill="#140a1c" d="M32 9c1.6-3 3.2-4 '
             '4.4-4.4-.4 1.4-.2 2.6.4 3.4 2.4-2.6 6.8-3.6 10.6-2.6-1.6 1-2.4 2.6-2.2 4.2 3.6-1.6 9.4-1.4 14.8 2.4-4.6 '
             '0-8.6 2.2-10.8 6-1.8-2-4.6-2.8-7-2.2.4 1.8 0 3.6-1.4 5.2-1.8-2.4-5.4-3.8-8.8-3.4-3.4-.4-7 1-8.8 3.4-1.4'
             '-1.6-1.8-3.4-1.4-5.2-2.4-.6-5.2.2-7 2.2C10.6 16.8 6.6 14.6 2 14.6c5.4-3.8 11.2-4 14.8-2.4.2-1.6-.6-3.2'
             '-2.2-4.2 3.8-1 8.2 0 10.6 2.6.6-.8.8-2 .4-3.4 1.2.4 2.8 1.4 4.4 4.4z"/></svg>')
_WEB = quote('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 60 60" fill="none" stroke="#d8cfe6" '
             'stroke-width=".8"><path d="M0 0L60 60M0 0L60 22M0 0L22 60M0 0L60 40M0 0L40 60"/><path d="M14 0c0 6 4 '
             '10 0 14C8 10 4 14 0 14M28 0c0 10 6 18 0 28C18 22 10 28 0 28M44 0c0 14 8 26 0 42C28 34 14 42 0 42"/>'
             '</svg>')
HALLOWEEN_CSS = """
body.halloween{--red:#ff7518;--orange:#ffb347;--gold:#ffc65c;--line:rgba(255,117,24,.28);--card:rgba(20,10,26,.9);
--card2:rgba(38,18,46,.9);--grad:linear-gradient(90deg,#ff7518,#ffb347)}
body.halloween::before{background:radial-gradient(circle at 84% 24%,rgba(255,236,200,.95) 0 38px,rgba(255,200,120,.35) 40px,
transparent 120px),radial-gradient(ellipse 90% 55% at 50% -8%,rgba(122,48,160,.42),transparent 70%),
radial-gradient(ellipse 80% 40% at 50% 108%,rgba(255,117,24,.22),transparent 70%),linear-gradient(180deg,#140821 0%,#08050c 60%)}
.spooky{position:fixed;inset:0;pointer-events:none;z-index:2;overflow:hidden}
.bat{position:absolute;left:-80px;width:56px;height:25px;background:url("data:image/svg+xml,BAT") center/contain no-repeat;
animation:fly 22s linear infinite;will-change:transform;filter:drop-shadow(0 0 5px rgba(255,140,60,.7))}
.bat i{display:block;width:100%;height:100%;background:inherit;animation:flap .35s ease-in-out infinite alternate}
.bat{background:none}.b1{top:18%;animation-duration:24s}.b2{top:42%;animation-duration:31s;animation-delay:-9s}
.b3{top:28%;animation-duration:19s;animation-delay:-14s}
.bat i{background:url("data:image/svg+xml,BAT") center/contain no-repeat}
@keyframes fly{0%{transform:translate3d(0,0,0)}25%{transform:translate3d(30vw,-20px,0)}50%{transform:translate3d(60vw,
15px,0)}75%{transform:translate3d(90vw,-10px,0)}100%{transform:translate3d(115vw,5px,0)}}
@keyframes flap{to{transform:scaleY(.45)}}
body.halloween main,body.halloween footer{position:relative;z-index:1}
body.halloween header{position:sticky}
.spider{position:absolute;right:9%;top:100%;width:1px;height:46px;background:rgba(220,210,235,.55);pointer-events:none;
transform-origin:top;animation:dangle 4s ease-in-out infinite alternate}
.spider::after{content:"🕷️";position:absolute;left:-11px;bottom:-18px;font-size:22px;filter:drop-shadow(0 2px 2px #000)}
@keyframes dangle{from{transform:scaleY(.75)}to{transform:scaleY(1.15)}}
body.halloween .card::after{content:"";position:absolute;left:0;top:0;width:54px;height:54px;opacity:.28;
pointer-events:none;background:url("data:image/svg+xml,WEB") 0 0/contain no-repeat}
body.halloween h1{text-shadow:0 2px 0 #000,0 0 26px rgba(255,117,24,.55)}
body.halloween .hero .logo{filter:drop-shadow(0 0 30px rgba(255,117,24,.55)) drop-shadow(0 0 60px rgba(122,48,160,.4))}
.hw-banner{text-align:center;font-family:var(--head);text-transform:uppercase;letter-spacing:.08em;font-size:1.15rem;
color:#ffd3a1;background:linear-gradient(90deg,rgba(122,48,160,.35),rgba(255,117,24,.25),rgba(122,48,160,.35));
border-color:rgba(255,117,24,.45)}.hw-banner .pk{display:inline-block;animation:bob 2.4s ease-in-out infinite}
@keyframes bob{50%{transform:translateY(-4px) rotate(-6deg)}}
.pumpkins{font-size:1.4rem;letter-spacing:.4em;margin-bottom:6px}
@media (prefers-reduced-motion:reduce){.bat,.bat i,.spider,.hw-banner .pk{animation:none}.bat{display:none}}
@media (max-width:640px){.bat{width:40px;height:18px}.b2{display:none}.spider{right:16px;height:30px}
body.halloween::before{background:radial-gradient(circle at 84% 17%,rgba(255,236,200,.95) 0 24px,rgba(255,200,120,.3) 26px,
transparent 80px),radial-gradient(ellipse 120% 50% at 50% -8%,rgba(122,48,160,.42),transparent 70%),
linear-gradient(180deg,#140821 0%,#08050c 60%)}}
""".replace("BAT", _BAT).replace("WEB", _WEB)

# Phone menu: a ☰ button opens the full menu (no script needed – a checkbox and a label)
ASSETS = Path(__file__).resolve().parent / "website_assets"     # default logo (replace it on the Plugins page)
NOISE = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E"
         "%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E"
         "%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.09'/%3E%3C/svg%3E")

# ------------------------------------------------------------ Looks (themes) the server owner picks
# Each one only changes colours, the background glow and the heading font – the layout stays the same. The
# Halloween look (seasonal) still wins in October; a custom "Main colour" wins over a theme's accent.
THEMES = {
    "rust": ("Rust (burnt orange)", ""),
    "neon": ("Neon night", """
body.theme-neon{--bg:#05040d;--card:rgba(14,10,32,.86);--card2:rgba(26,16,54,.9);--text:#efeaff;--muted:#a59bc9;
--line:rgba(176,76,255,.3);--red:#c13cff;--orange:#00e5ff;--gold:#ffe66d;--ok:#39ff88}
body.theme-neon::before{background:radial-gradient(ellipse 80% 50% at 15% -5%,rgba(193,60,255,.35),transparent 70%),
radial-gradient(ellipse 70% 45% at 90% 0%,rgba(0,229,255,.25),transparent 70%),linear-gradient(180deg,#0b0620,#05040d 60%)}
body.theme-neon h1{text-shadow:0 0 18px rgba(193,60,255,.6),0 0 40px rgba(0,229,255,.25)}"""),
    "arctic": ("Arctic (ice blue)", """
body.theme-arctic{--bg:#04090f;--card:rgba(10,20,32,.86);--card2:rgba(16,32,50,.9);--text:#eaf6ff;--muted:#93aac0;
--line:rgba(110,190,255,.26);--red:#3aa0ff;--orange:#7fe7ff;--gold:#d6f3ff;--ok:#5ef0b0}
body.theme-arctic::before{background:radial-gradient(ellipse 90% 55% at 50% -8%,rgba(120,200,255,.3),transparent 70%),
radial-gradient(ellipse 60% 40% at 10% 15%,rgba(200,240,255,.12),transparent 70%),linear-gradient(180deg,#0a1726,#04090f 60%)}
body.theme-arctic h1{text-shadow:0 2px 0 #000,0 0 22px rgba(127,231,255,.45)}"""),
    "jungle": ("Jungle (military green)", """
body.theme-jungle{--bg:#050805;--card:rgba(14,20,12,.88);--card2:rgba(24,34,20,.9);--text:#eef3e6;--muted:#9fae92;
--line:rgba(140,190,70,.24);--red:#6fae2c;--orange:#c8d94a;--gold:#e8d36a;--ok:#5fe07a}
body.theme-jungle::before{background:radial-gradient(ellipse 90% 55% at 50% -8%,rgba(120,170,50,.28),transparent 70%),
linear-gradient(180deg,#0d150a,#050805 60%)}"""),
    "bloodmoon": ("Blood moon (deep red)", """
body.theme-bloodmoon{--bg:#070203;--card:rgba(24,6,9,.88);--card2:rgba(40,10,14,.9);--text:#f6e9ea;--muted:#b49397;
--line:rgba(220,30,50,.28);--red:#d1112c;--orange:#ff4d5e;--gold:#ffb3a1;--ok:#4be07a}
body.theme-bloodmoon::before{background:radial-gradient(circle at 82% 16%,rgba(255,80,80,.55) 0 30px,rgba(200,20,40,.25)
34px,transparent 110px),radial-gradient(ellipse 90% 55% at 50% -8%,rgba(160,10,30,.4),transparent 70%),
linear-gradient(180deg,#1a0407,#070203 60%)}"""),
    "gold": ("Royal gold", """
body.theme-gold{--bg:#070605;--card:rgba(20,17,10,.88);--card2:rgba(34,28,14,.9);--text:#f7f1e3;--muted:#b3a684;
--line:rgba(230,190,90,.26);--red:#c99a2e;--orange:#ffd36b;--gold:#fff0b8;--ok:#62e08a}
body.theme-gold::before{background:radial-gradient(ellipse 90% 55% at 50% -8%,rgba(230,180,60,.3),transparent 70%),
linear-gradient(180deg,#15110a,#070605 60%)}
body.theme-gold h1{text-shadow:0 2px 0 #000,0 0 24px rgba(255,211,107,.4)}"""),
    "clean": ("Clean dark (no grit)", """
body.theme-clean{--bg:#0d1117;--card:#161b22;--card2:#1c2330;--text:#e6edf3;--muted:#8b98a8;--line:#2a3340;
--red:#ff7a1a;--orange:#ffb15c;--gold:#ffd27a;--cut:8px;--head:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
body.theme-clean::before{background:linear-gradient(180deg,#111723,#0d1117 60%)}
body.theme-clean h1,body.theme-clean h2{text-transform:none;letter-spacing:0;font-weight:800;text-shadow:none}"""),
    "ocean": ("Deep ocean (teal)", """
body.theme-ocean{--bg:#020a0d;--card:rgba(6,24,30,.86);--card2:rgba(10,38,46,.9);--text:#e6fbff;--muted:#8fb4bd;
--line:rgba(30,200,210,.25);--red:#14b8c4;--orange:#4ef0d0;--gold:#b8fff0;--ok:#4ef08a}
body.theme-ocean::before{background:radial-gradient(ellipse 90% 55% at 50% -8%,rgba(20,184,196,.32),transparent 70%),
radial-gradient(ellipse 70% 40% at 85% 110%,rgba(10,90,140,.35),transparent 70%),linear-gradient(180deg,#05161c,#020a0d 60%)}
body.theme-ocean h1{text-shadow:0 2px 0 #000,0 0 24px rgba(78,240,208,.4)}"""),
    "toxic": ("Radtown (toxic green)", """
body.theme-toxic{--bg:#050703;--card:rgba(16,20,8,.88);--card2:rgba(26,32,12,.9);--text:#f2f7df;--muted:#a3ad84;
--line:rgba(170,230,30,.26);--red:#9be21e;--orange:#f5e53a;--gold:#fff27a;--ok:#5ff07a}
body.theme-toxic::before{background:radial-gradient(ellipse 90% 55% at 50% -8%,rgba(155,226,30,.3),transparent 70%),
repeating-linear-gradient(-45deg,rgba(245,229,58,.04) 0 14px,transparent 14px 28px),linear-gradient(180deg,#10150a,#050703 60%)}
body.theme-toxic h1{text-shadow:0 2px 0 #000,0 0 22px rgba(155,226,30,.55)}"""),
    "synthwave": ("Synthwave (pink & purple)", """
body.theme-synthwave{--bg:#0a0414;--card:rgba(24,10,40,.86);--card2:rgba(38,14,60,.9);--text:#fbeaff;--muted:#b79ccc;
--line:rgba(255,60,172,.3);--red:#ff3cac;--orange:#ffb03b;--gold:#ffe66d;--ok:#3ff0b0}
body.theme-synthwave::before{background:radial-gradient(circle at 50% 108%,rgba(255,176,59,.55) 0 60px,rgba(255,60,172,.3)
70px,transparent 240px),radial-gradient(ellipse 90% 55% at 50% -8%,rgba(120,40,200,.4),transparent 70%),
linear-gradient(180deg,#1a0833,#0a0414 60%)}
body.theme-synthwave h1{text-shadow:0 0 16px rgba(255,60,172,.7),0 0 40px rgba(255,176,59,.3)}"""),
    "steel": ("Gunmetal (steel grey)", """
body.theme-steel{--bg:#08090b;--card:rgba(22,25,30,.9);--card2:rgba(32,36,43,.92);--text:#eef1f5;--muted:#9aa3ad;
--line:rgba(160,175,190,.22);--red:#7d93ad;--orange:#c4d2e0;--gold:#e8eef5;--ok:#55d98a}
body.theme-steel::before{background:radial-gradient(ellipse 90% 55% at 50% -8%,rgba(150,170,190,.18),transparent 70%),
linear-gradient(180deg,#14171c,#08090b 60%)}
body.theme-steel h1{text-shadow:0 2px 0 #000}"""),
    "desert": ("Desert (sand)", """
body.theme-desert{--bg:#0b0805;--card:rgba(30,22,14,.88);--card2:rgba(44,32,20,.9);--text:#fbf2e4;--muted:#bba78b;
--line:rgba(230,170,90,.26);--red:#d9893a;--orange:#f2c46d;--gold:#ffe3a3;--ok:#7ad66a}
body.theme-desert::before{background:radial-gradient(circle at 80% 10%,rgba(255,214,140,.45) 0 34px,transparent 140px),
radial-gradient(ellipse 90% 55% at 50% -8%,rgba(217,137,58,.3),transparent 70%),linear-gradient(180deg,#1c140b,#0b0805 60%)}"""),
    "sakura": ("Sakura (soft pink)", """
body.theme-sakura{--bg:#0c0609;--card:rgba(30,14,22,.86);--card2:rgba(44,20,32,.9);--text:#fff0f6;--muted:#c3a0b2;
--line:rgba(255,140,190,.26);--red:#ff6fa8;--orange:#ffb3d1;--gold:#ffe0ec;--ok:#6fe0a0}
body.theme-sakura::before{background:radial-gradient(ellipse 90% 55% at 50% -8%,rgba(255,111,168,.28),transparent 70%),
radial-gradient(ellipse 60% 40% at 10% 20%,rgba(255,200,225,.1),transparent 70%),linear-gradient(180deg,#1d0c15,#0c0609 60%)}
body.theme-sakura h1{text-shadow:0 2px 0 #000,0 0 22px rgba(255,111,168,.45)}"""),
    "discord": ("Your Discord icon's colours", ""),   # built from the server icon: discord_theme_css()
}


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def icon_palette(data: bytes) -> tuple[str, str] | None:
    """The two strongest colours of a picture (the Discord server's icon): (main, second) as hex – vivid, not
    black/white/grey. None if there's nothing colourful in it."""
    import colorsys
    import io
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGBA").resize((64, 64))
    except Exception:
        return None
    data = img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata()   # Pillow 14 drops getdata
    pixels = [(r, g, b) for r, g, b, a in data if a > 128]
    if not pixels:
        return None
    buckets: dict[tuple, list] = {}
    for r, g, b in pixels:
        h, l, sat = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        if sat < 0.25 or l < 0.12 or l > 0.9:
            continue                                    # greys, black, white: no theme colour
        key = (int(h * 24) % 24,)
        buckets.setdefault(key, []).append((r, g, b, sat))
    if not buckets:
        return None
    scored = sorted(buckets.items(), key=lambda kv: -sum(p[3] for p in kv[1]))

    def mean(px):
        n = len(px)
        rgb = (sum(p[0] for p in px) / n, sum(p[1] for p in px) / n, sum(p[2] for p in px) / n)
        h, l, sat = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
        r, g, b = colorsys.hls_to_rgb(h, min(max(l, 0.45), 0.62), max(sat, 0.55))   # readable on dark
        return (r * 255, g * 255, b * 255), h
    main, h1 = mean(scored[0][1])
    second = None
    for _key, px in scored[1:]:
        rgb, h2 = mean(px)
        if min(abs(h2 - h1), 1 - abs(h2 - h1)) > 0.08 and len(px) >= len(scored[0][1]) * 0.08:
            second = rgb
            break
    if second is None:                                  # one colour only: a lighter shade of it
        r, g, b = main
        h, l, sat = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        second = tuple(c * 255 for c in colorsys.hls_to_rgb((h + 0.06) % 1, min(l + 0.15, 0.78), sat))
    return _hex(main), _hex(second)


def discord_theme_css(palette: tuple[str, str] | None) -> str:
    """The "discord" theme: background, cards and accents tinted with the icon's colours."""
    import colorsys
    if not palette:
        return ""
    main, second = palette
    r, g, b = (int(main[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, _l, _s = colorsys.rgb_to_hls(r, g, b)

    def tint(light, sat):
        return _hex(c * 255 for c in colorsys.hls_to_rgb(h, light, sat))

    def rgba(hexcol, alpha):
        return f"rgba({int(hexcol[1:3], 16)},{int(hexcol[3:5], 16)},{int(hexcol[5:7], 16)},{alpha})"
    bg, card, card2, top = tint(.03, .45), tint(.08, .4), tint(.12, .38), tint(.09, .5)
    return (f"body.theme-discord{{--bg:{bg};--card:{rgba(card, .88)};--card2:{rgba(card2, .9)};--text:#f3f3f7;"
            f"--muted:{tint(.7, .15)};--line:{rgba(main, .28)};--red:{main};--orange:{second};"
            f"--gold:{tint(.82, .6)};--ok:#4be07a}}"
            f"body.theme-discord::before{{background:radial-gradient(ellipse 90% 55% at 50% -8%,{rgba(main, .32)},"
            f"transparent 70%),radial-gradient(ellipse 60% 40% at 90% 10%,{rgba(second, .18)},transparent 70%),"
            f"linear-gradient(180deg,{top},{bg} 60%)}}"
            f"body.theme-discord h1{{text-shadow:0 2px 0 #000,0 0 24px {rgba(main, .45)}}}")

# Dark, burnt "Rust" look: black background with a sunset glow, rusty red/orange accents, rugged cut corners
# The look lives in website_assets/style.css – it is put into every page (the site's CSP allows no outside files).
STYLE = (ASSETS / "style.css").read_text(encoding="utf-8").replace("NOISE", NOISE)

DEFAULT_ACCENT, OLD_ACCENT = "#ff5533", "#e5341c"          # the main colour when the owner picked none

# Line icons (24×24, drawn here – no icon font, no outside files)
ICONS = {
    "home": '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h5v-6h4v6h5V10"/>',
    "trophy": '<path d="M8 4h8v5a4 4 0 0 1-8 0z"/><path d="M8 6H4a4 4 0 0 0 4 4M16 6h4a4 4 0 0 1-4 4M12 13v4M8 20h8"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    "cart": '<path d="M3 4h2l2.4 11h10.2l2-8H6.2"/><circle cx="9" cy="19" r="1.5"/><circle cx="17" cy="19" r="1.5"/>',
    "book": '<path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h11M9 8h6"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/>',
    "ban": '<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/>',
    "calendar": '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M8 3v4M16 3v4M4 10h16"/>',
    "coins": '<circle cx="12" cy="12" r="9"/><path d="M14.5 9.2a2.5 2 0 0 0-5 0c0 2.8 5 2 5 5a2.5 2 0 0 1-5 0M12 6v1.2M12 16.8V18"/>',
    "star": '<path d="M12 3l2.7 5.9 6.3.7-4.7 4.3 1.3 6.3L12 17l-5.6 3.2 1.3-6.3L3 9.6l6.3-.7z"/>',
    "dice": '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M9 9h.01M15 9h.01M12 12h.01M9 15h.01M15 15h.01"/>',
    "play": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 9l5 3-5 3z"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "users": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.3a6.5 6.5 0 0 1 3.5 5.7"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "phone": '<rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/>',
    "chart": '<path d="M4 20V4M4 20h16"/><path d="M8 16l4-5 3 3 5-7"/>',
    "megaphone": '<path d="M3 10v4l11 4V6z"/><path d="M14 8a4 4 0 0 1 0 8M7 15v4"/>',
    "chat": '<path d="M4 5h16v11H9l-5 4z"/>',
    "swords": '<path d="M5 3l9 9M3 5l9 9M14 18l4 4M17 15l4 4M19 3l-9 9M21 5l-9 9M10 18l-4 4M7 15l-4 4"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    "copy": '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M4 16V6a2 2 0 0 1 2-2h10"/>',
    "image": '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.5"/><path d="M21 16l-5-5-8 8"/>',
    "gift": '<rect x="4" y="9" width="16" height="11" rx="1"/><path d="M4 13h16M12 9v11M12 9c-2-4-6-3-5 0M12 9c2-4 6-3 5 0"/>',
}
# page -> (icon, the line under its name on the home page)
PAGES = {"": ("home", ""), "leaderboard": ("trophy", "Who rules the island"), "player": ("search", "Look up any player's stats"),
         "store": ("cart", "Kits and more for your base"), "battlepass": ("star", "Play, level up, earn rewards"),
         "casino": ("dice", "Bet your points"), "streams": ("play", "Who is live right now"),
         "account": ("user", "Your kits, orders and coins"), "shop": ("coins", "Spend your points"),
         "rules": ("book", "Read them before you play"), "staff": ("shield", "The team behind the server"),
         "bans": ("ban", "Who got banned and why"), "wipes": ("calendar", "Past and next wipes"),
         "info": ("info", "More about this server")}
MAIN_PAGES = ("", "leaderboard", "player", "store", "rules")     # in the menu bar; the rest sits under "More"


def icon(name: str) -> str:
    return f'<svg class="i" viewBox="0 0 24 24" aria-hidden="true">{ICONS.get(name, ICONS["info"])}</svg>'


def esc(text) -> str:
    return html.escape(str(text), quote=True)


class PlayerWebsite(Plugin):
    NAME = "Player website"
    ICON = "🌍"
    PUBLIC_PATH = "/"                    # the site root (the control panel is at /admin)
    DESCRIPTION = ("A public website for your players: live status, next wipe, leaderboards, player profiles, the "
                   "points shop with item pictures, rules and how to join. Share the link everywhere.")
    HOSTED_DEFAULT = True                      # hosted servers: the website is on (and free) from the start
    SETTINGS = (
        Setting("title", "Website title", "text", "", help="Empty = the server name."),
        Setting("logo", "Logo", "image", help="Square picture (PNG/WebP with a transparent background looks best). "
                                              "Empty = your Discord server's icon (the bundled logo if it "
                                              "has none)."),
        Setting("banner", "Header picture", "image",
                help="A wide picture of your server behind the logo on the home page (a screenshot works great). "
                     "Big pictures are made smaller automatically."),
        Setting("gallery_title", "Gallery title", "text", "Our server",
                help="Shown above your server pictures on the home page."),
        *(Setting(f"gallery_{i}", f"Server picture {i}", "image",
                  help="Screenshots of your bases, events, map … shown in a gallery on the home page." if i == 1
                  else "") for i in range(1, GALLERY + 1)),
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
        Setting("show_staff", "Show the staff page", "bool", True,
                help="Lists the members with your admin and support roles, plus anyone you add below."),
        Setting("staff_extra", "Extra staff (one per line: Name | Role)", "textarea", ""),
        Setting("show_bans", "Show the bans page", "bool", False,
                help="Who is banned from the server and why (recorded from now on: bans in the server log and /ban)."),
        Setting("show_wipes", "Show the wipe history", "bool", True),
        Setting("theme", "Look (theme)", "choice", "rust", choices=tuple(THEMES),
                help="rust = burnt orange · neon = purple/cyan night · arctic = ice blue · jungle = military green · "
                     "bloodmoon = deep red · gold = royal gold · clean = calm, no grit · ocean = deep teal · toxic = "
                     "radtown green · synthwave = pink & purple · steel = gunmetal grey · desert = sand · sakura = "
                     "soft pink · discord = the colours of your Discord server's icon (picked by the bot). Changes "
                     "colours, background and headings for the website and the store; the layout stays."),
        Setting("accent", "Main colour", "text", DEFAULT_ACCENT, help=f"Hex colour, e.g. #e5341c (blends into the second "
                f"colour). Leave {DEFAULT_ACCENT} to use the look's own colour."),
        Setting("season_theme", "Seasonal theme", "choice", "auto", choices=("auto", "halloween", "off"),
                help="auto = Halloween look (bats, moon, cobwebs, pumpkins) during October; halloween = always; "
                     "off = never."),
        Setting("halloween_message", "Halloween banner on the home page", "text",
                "Happy Halloween! Spooky loot and frightful raids all month long.",
                help="Shown at the top of the home page while the Halloween look is on. Empty = no banner."),
        # ---- make it yours
        Setting("accent2", "Second colour", "text", "",
                help="Hex colour, e.g. #f7931e – the colour the main colour blends into (buttons, lines, numbers). "
                     "Empty = the look's own."),
        Setting("shape", "Shape of cards and buttons", "choice", "rounded", choices=("rounded", "angled", "square"),
                help="rounded: soft corners · angled: cut corners (the old Rust look) · square: plain boxes."),
        Setting("font", "Heading font", "choice", "modern", choices=("modern", "bold", "mono"),
                help="modern: clean sans-serif · bold: heavy condensed capitals · mono: typewriter / terminal."),
        Setting("hero_layout", "Top of the home page", "choice", "side", choices=("side", "centered"),
                help="side: logo on the left, name and buttons next to it · centered: everything in the middle."),
        Setting("background", "Background picture", "image",
                help="A picture behind every page (dimmed, so text stays readable). A dark screenshot works best."),
        Setting("announcement", "Announcement on the home page", "text", "",
                help="One line at the very top of the home page, e.g. \"Wipe on Thursday 19:00 – 2x gather all "
                     "weekend\". Empty = none."),
        Setting("extra_links", "Your own buttons (one per line: Label | https://…)", "textarea", "",
                help="Up to 6 – e.g. \"TikTok | https://www.tiktok.com/@you\" or \"Vote for us | https://…\". They show "
                     "as buttons at the top of the home page and as links at the bottom of every page."),
        Setting("show_tiles", "Home page: quick-link tiles (Explore)", "bool", True),
        Setting("show_hall", "Home page: top killers and bounties", "bool", True),
        Setting("show_app", "Home page: the Companion app", "bool", True),
        Setting("show_chart", "Home page: players over the last 24 hours (chart)", "bool", True),
        Setting("show_online", "Home page: who is online right now", "bool", True),
        Setting("page_title", "Your own page: title", "text", "",
                help="Adds a page to the menu with your own text – e.g. \"VIP\", \"Events\" or \"FAQ\"."),
        Setting("page_text", "Your own page: text", "textarea", "",
                help="Plain text. An empty line starts a new block; a line that starts with # is a heading."),
        Setting("footer_text", "Text at the bottom of every page", "text", "",
                help="E.g. \"Not affiliated with Facepunch or Double Eleven.\""),
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

    async def refresh_servers(self) -> None:
        """Several servers (servers.py): the others' live status for the home page (asked every CACHE_SECONDS)."""
        import servers
        if servers.is_child() or not servers.load_registry():
            self._servers_live = []
            return
        cached = getattr(self, "_servers_live_at", 0)
        if time.monotonic() - cached < CACHE_SECONDS and getattr(self, "_servers_live", None) is not None:
            return
        out = []
        for s in servers.load_registry():
            if not s.get("enabled", True):
                continue
            state = await servers.status_of(s["id"]) or {"online": False, "players": 0, "max_players": 0, "queued": 0}
            out.append({"id": s["id"], "name": servers.server_name(s["id"]), **state})
        self._servers_live, self._servers_live_at = out, time.monotonic()

    def all_servers_live(self) -> list[dict]:
        """This server first, then the others (empty with one server)."""
        import servers
        others = getattr(self, "_servers_live", None) or []
        if not others:
            return []
        return [{"id": servers.MAIN_ID, "name": servers.server_name(servers.MAIN_ID), **self.live()}] + others

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
        battlepass = shop._battlepass()
        selling_pass = battlepass is not None and battlepass.money_price() > 0   # the premium pass for money
        return shop if kits or paypal_only or selling_pass else None

    def accounts(self):
        """The PlayerAccounts cog if player login is on, else None."""
        cog = self.bot.get_cog("PlayerAccounts")
        return cog if cog is not None and cog.enabled else None

    def battlepass(self):
        """The Battle pass plugin if it's on and players can log in, else None."""
        bp = self.plugin("battlepass")
        return bp if bp is not None and self.accounts() is not None else None

    def casino(self):
        """The Casino plugin if it's on and players can log in, else None."""
        casino = self.plugin("casino")
        return casino if casino is not None and casino.points_enabled and self.accounts() is not None else None

    def streams(self):
        """The Streamers plugin if it's on and its page is shown, else None."""
        plugin = self.plugin("streamers")
        return plugin if plugin is not None and plugin.setting("website") else None

    def seasonal(self) -> str:
        """The seasonal look that's on right now: "halloween" or ""."""
        mode = self.setting("season_theme")
        if mode == "halloween":
            return "halloween"
        if mode == "auto" and datetime.fromtimestamp(self.now(), self.tz).month == 10:
            return "halloween"
        return ""

    def find_player(self, name: str):
        name = " ".join(str(name or "").split())[:64]
        return self.bot.db.player(name) if name else None

    # ------------------------------------------------------------ Layout

    def extra_links(self) -> list[tuple[str, str]]:
        """The owner's own buttons: (label, https address) – only https, at most 6."""
        out = []
        for line in str(self.setting("extra_links") or "").splitlines():
            label, _, url = line.partition("|")
            label, url = label.strip()[:30], url.strip()
            if label and re.fullmatch(r"https://[^\s\"'<>]{4,300}", url):
                out.append((label, url))
        return out[:6]

    def own_page(self) -> tuple[str, str] | None:
        title, text = str(self.setting("page_title") or "").strip()[:24], str(self.setting("page_text") or "").strip()
        return (title, text) if title and text else None

    def info_page(self, base: str) -> str:
        title, text = self.own_page()
        blocks = []
        for block in re.split(r"\n\s*\n", text):
            block = block.strip()
            if block.startswith("#"):
                head, _, rest = block.partition("\n")
                blocks.append(f"<h2>{esc(head.lstrip('#').strip())}</h2>" + (f"<p>{esc(rest.strip())}</p>" if rest.strip() else ""))
            elif block:
                blocks.append(f"<p>{esc(block)}</p>")
        return self.page(base, "info", title, f'<h1>{esc(title)}</h1><div class="card prose">{"".join(blocks)}</div>')

    def nav_links(self) -> list[tuple[str, str]]:
        """(path, label) of every page that is switched on – the menu, the footer and the home page's tiles."""
        links = [("", "Home")]
        if self.setting("leaderboards"):
            links += [("leaderboard", "Leaderboard"), ("player", "Players")]
        if self.store() is not None:
            links.append(("store", "Store"))
        if self.battlepass() is not None:
            links.append(("battlepass", "Battle pass"))
        if self.casino() is not None:
            links.append(("casino", "Casino"))
        if self.streams() is not None:
            links.append(("streams", "Streams"))
        if self.accounts() is not None:
            links.append(("account", "My account"))
        if self.setting("shop") and self.points_enabled:
            links.append(("shop", "Points"))
        if self.setting("rules"):
            links.append(("rules", "Rules"))
        if self.setting("show_staff"):
            links.append(("staff", "Staff"))
        if self.setting("show_bans"):
            links.append(("bans", "Bans"))
        if self.setting("show_wipes"):
            links.append(("wipes", "Wipes"))
        if self.own_page() is not None:
            links.append(("info", self.own_page()[0]))
        return links

    def page(self, base: str, active: str, title: str, body: str) -> str:
        links = self.nav_links()
        def link(path, label, *, with_icon=False):
            return (f'<a href="{base}/{path}"{' class="active" aria-current="page"' if path == active else ""}>'
                    f'{icon(PAGES.get(path, PAGES["info"])[0]) if with_icon else ""}<span>{esc(label)}</span></a>')
        # the bar shows the pages everyone needs; with more than six pages the rest folds into "More"
        main_links = [x for x in links if x[0] in MAIN_PAGES] if len(links) > 6 else links
        more_links = [x for x in links if x not in main_links]
        nav = "".join(link(path, label) for path, label in main_links)
        if more_links:
            nav += (f'<details><summary{' class="active"' if any(p == active for p, _ in more_links) else ""}>More</summary>'
                    f'<div class="menu">{"".join(link(p, lab, with_icon=True) for p, lab in more_links)}</div></details>')
        foot = "".join(f'<a href="{base}/{path}">{esc(label)}</a>' for path, label in links)
        search = (f'<form class="hsearch" method="get" action="{base}/player" role="search">{icon("search")}'
                  f'<input name="name" placeholder="Find a player" aria-label="Find a player" maxlength="64" required>'
                  f'</form>' if self.setting("leaderboards") else "")
        invite = self.setting("invite").strip()
        cta = (f'<a class="hcta" href="{esc(invite)}" rel="noopener">{icon("chat")}Join Discord</a>'
               if INVITE.match(invite) else "")
        # phones: four pages on a tab bar at the thumb, everything in the "More" sheet
        tabs = [x for x in links if x[0] in ("", "leaderboard", "store", "player", "rules")][:4]
        short = {"": "Home", "leaderboard": "Ranks", "player": "Players"}
        tabbar = ("".join(f'<a href="{base}/{path}"{' class="active"' if path == active else ""}>'
                          f'{icon(PAGES[path][0])}<span>{esc(short.get(path, label))}</span></a>' for path, label in tabs)
                  + f'<label for="mnav" class="burger" aria-label="Menu">{icon("menu")}<span>More</span></label>')
        sheet = (f'<div class="sheet">{search}<h3>All pages</h3><div class="links">'
                 f'{"".join(link(p, lab, with_icon=True) for p, lab in links)}</div>{cta}</div>')
        accent = self.setting("accent").strip()
        accent = accent if re.fullmatch(r"#[0-9a-fA-F]{6}", accent) else DEFAULT_ACCENT
        if accent.lower() == OLD_ACCENT:                      # the old default: follow the new one
            accent = DEFAULT_ACCENT
        if self._style is None or self._style[0] != accent:
            self._style = (accent, STYLE.replace("ACCENT", accent))
        version = self.picture_version()
        live = self.live()
        site = self.site_title()
        motto = f'<div class="motto">{esc(self.setting("motto"))}</div>' if self.setting("motto").strip() else ""
        updated = datetime.fromtimestamp(self.now(), self.tz).strftime("%H:%M")
        season = self.seasonal()
        theme = self.setting("theme") if self.setting("theme") in THEMES else "rust"
        theme_css = (self.discord_css() if theme == "discord" else THEMES[theme][1]) + (
            f"body.theme-{theme}{{--red:{accent}}}"
                                        if theme != "rust" and accent.lower() != DEFAULT_ACCENT else "")
        # --grad is set on :root with :root's colours – on body it follows the theme's --red/--orange too
        accent2 = str(self.setting("accent2") or "").strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", accent2):
            theme_css += f"body.theme-{theme}{{--orange:{accent2}}}"
        shape = self.setting("shape") if self.setting("shape") in ("angled", "rounded", "square") else "rounded"
        font = self.setting("font") if self.setting("font") in ("bold", "modern", "mono") else "modern"
        looks = f"shape-{shape} font-{font} hero-{'centered' if self.setting('hero_layout') == 'centered' else 'side'}"
        if self.file("background") is not None:
            looks += " has-bg"
            theme_css += f"body{{--bgimg:url('{base}/background?v={self._file_version('background')}')}}"
        # the preview Discord, WhatsApp … show when someone shares the link
        origin = (self.bot.config.web_public_url or "").rstrip("/")
        social = (f'<meta property="og:type" content="website"><meta property="og:site_name" content="{esc(site)}">'
                  f'<meta property="og:title" content="{esc(title + " – " if title else "")}{esc(site)}">'
                  f'<meta property="og:description" content="{esc(self.setting("tagline"))}">'
                  f'<meta name="twitter:card" content="summary">'
                  + (f'<meta property="og:image" content="{esc(origin)}{base}/logo?s=512&amp;v={version}">'
                     if origin.startswith("https://") else ""))
        ext = "".join(f'<a href="{esc(url)}" rel="noopener nofollow" target="_blank">{esc(label)}</a>'
                      for label, url in self.extra_links())
        foot_note = str(self.setting("footer_text") or "").strip()[:200]
        extra_css = (theme_css + "body{--grad:linear-gradient(90deg,var(--red),var(--orange))}"
                     + (HALLOWEEN_CSS if season == "halloween" else ""))
        decor = spider = pumpkins = ""
        if season == "halloween":
            decor = ('<div class="spooky" aria-hidden="true"><span class="bat b1"><i></i></span>'
                     '<span class="bat b2"><i></i></span><span class="bat b3"><i></i></span></div>')
            spider = '<span class="spider" aria-hidden="true"></span>'
            pumpkins = '<div class="pumpkins" aria-hidden="true">🎃🦇🎃</div>'
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width, initial-scale=1">'
                f'<title>{esc(title + " – " if title else "")}{esc(site)}</title>'
                f'<meta name="description" content="{esc(self.setting("tagline"))}">'
                f'<meta name="theme-color" content="#060606"><link rel="icon" href="{base}/icon?v={version}">{social}'
                f'<script src="{base}/site.js?v={self.asset_version("site.js")}" defer></script>'
                f'<style>{self._style[1]}{extra_css}</style></head><body class="{season} theme-{theme} {looks}" data-base="{base}">{decor}'
                f'<a class="skip" href="#main">Skip to the content</a><div class="prog" aria-hidden="true"></div>'
                f'<input type="checkbox" id="mnav" hidden>'
                f'<header><div class="bar"><a class="brand" href="{base}/"><img src="{base}/logo?s={HEADER_LOGO}'
                f'&amp;v={version}" alt="" width="42" height="42" decoding="async">'
                f'<span class="name">{esc(site)}</span><span class="dot{" on" if live["online"] else ""}" '
                f'title="{"Online" if live["online"] else "Offline"}"></span></a>'
                f'<nav aria-label="Pages">{nav}</nav>{search}{cta}</div>{spider}</header>'
                f'<main id="main">{body}</main><footer>{pumpkins}<div class="fbrand"><img src="{base}/logo?s={HEADER_LOGO}'
                f'&amp;v={version}" alt="" width="34" height="34" loading="lazy">{esc(site)}</div><nav aria-label="All pages">'
                f'{foot}</nav>{f'<div class="ext">{ext}</div>' if ext else ""}{motto}Updated {esc(updated)} · '
                f'{esc(self.bot.config.rust_server_platform)}'
                + (f'<div class="foot-note">{esc(foot_note)}</div>' if foot_note else "")
                + f'</footer>{sheet}<nav class="tabbar" aria-label="Main pages">{tabbar}</nav></body></html>')

    def item_tiles(self, items) -> str:
        tiles = []
        for short, amount in items[:40]:
            short_q = quote(short)
            tiles.append(f'<div class="it" title="{esc(short)} × {amount}"><img src="/img/items/{short_q}.png?ph=1&amp;'
                         f's=96&amp;v=2" alt="{esc(short)}" width="46" height="46" loading="lazy" decoding="async">'
                         f'<span class="q">{"×" + str(amount) if amount > 1 else ""}</span></div>')
        return f'<div class="items">{"".join(tiles)}</div>' if tiles else ""

    # ------------------------------------------------------------ Pages

    @staticmethod
    def _server_card(title: str, online: bool, players: int, max_players: int, queued: int, *, main: bool = False) -> str:
        live, live_meter = (' data-live="players"', ' data-live="meter"') if main else ("", "")     # site.js refreshes
        count = (f"{players} / {max_players}" if max_players else str(players)) if online else "–"
        meter = (f'<div class="meter"><i{live_meter} style="width:{min(100, round(100 * players / max_players))}%"></i></div>'
                 if online and max_players else "")
        return (f'<div class="card"><h2><span class="ic">{icon("users")}</span>{esc(title)} <span class="pill{" on" if online else ""}">'
                f'{"Online" if online else "Offline"}</span></h2><div class="big"{live}>{esc(count)}</div>'
                f'<div class="muted">{str(queued) + " in queue" if queued else "players online"}</div>{meter}</div>')

    def _server_cards(self, players: str, live: dict) -> str:
        """The "Server" tile – one per server when this bot runs several (name, online, players, how full)."""
        every = self.all_servers_live()
        if not every:
            return self._server_card("Server", bool(live["online"]), int(live["players"] or 0),
                                     int(live["max_players"] or 0), int(live["queued"] or 0), main=True)
        return "".join(self._server_card(s["name"], bool(s.get("online")), int(s.get("players") or 0),
                                         int(s.get("max_players") or 0), int(s.get("queued") or 0)) for s in every)

    def tiles(self, base: str) -> str:
        """A big link to every page that is on (the home page's "Explore", the 404 page)."""
        return "".join(f'<a class="tile" href="{base}/{path}"><span class="ic">{icon(PAGES[path][0])}</span><span>'
                       f'<b>{esc(label)}</b><small>{PAGES[path][1]}</small></span></a>'
                       for path, label in self.nav_links() if path and path in PAGES)

    def home(self, base: str) -> str:
        live, tz, now = self.live(), self.tz, self.now()
        invite, store = self.setting("invite").strip(), self.setting("store").strip()
        buttons = ""
        if INVITE.match(invite):
            buttons += f'<a class="btn" href="{esc(invite)}" rel="noopener">{icon("chat")}Join our Discord</a>'
        buttons += f'<a class="btn alt" href="#join">{icon("play")}How to join</a>'
        if store.startswith("https://") and len(store) < 300:
            buttons += f'<a class="btn alt" href="{esc(store)}" rel="noopener">{icon("cart")}Store</a>'
        for label, url in self.extra_links():
            buttons += f'<a class="btn alt" href="{esc(url)}" rel="noopener nofollow" target="_blank">{esc(label)}</a>'
        if self.plugin("companion") is not None and self.setting("show_app"):          # the players' app
            buttons += f'<a class="btn alt" href="#app">{icon("phone")}Get the app</a>'
        players = (f'{live["players"]} / {live["max_players"]}' if live["max_players"] else str(live["players"])) \
            if live["online"] else "–"
        wipe_soon = bool(live["next_wipe"] and live["next_wipe"] > now)
        if live["next_wipe"]:
            wipe = datetime.fromtimestamp(live["next_wipe"], tz).strftime("%a %d %b, %H:%M")
            wipe_sub = f'in {format_duration(max(live["next_wipe"] - now, 0))} ({getattr(tz, "key", "UTC")})'
        else:
            wipe, wipe_sub = self.bot.config.rust_server_wipe or "Announced on Discord", ""
        tagline, motto = self.setting("tagline").strip(), self.setting("motto").strip()
        banner = ""
        message = str(self.setting("halloween_message") or "").strip()
        if self.seasonal() == "halloween" and message:
            banner = (f'<div class="card hw-banner"><span class="pk">🎃</span> {esc(message)} '
                      '<span class="pk">🦇</span></div>')
        news = str(self.setting("announcement") or "").strip()[:240]
        if news:
            banner += f'<div class="ann"><span class="ic">{icon("megaphone")}</span><span>{esc(news)}</span></div>'
        hero_pic = ""
        if self.file("banner") is not None:
            hero_pic = (f' style="background:linear-gradient(100deg,rgba(0,0,0,.82) 20%,rgba(0,0,0,.45)),'
                        f'url(\'{base}/banner?v={self._file_version("banner")}\') center/cover"')
        chips = (f'<div class="live"><span class="dot{" on" if live["online"] else ""}"></span>'
                 + (f'<span><b data-live="players">{esc(players)}</b> players online</span>' if live["online"]
                    else "<span>Server offline</span>") + "</div>")
        if wipe_soon:
            chips += f'<div class="live">{icon("calendar")}<span>Next wipe <b>{esc(wipe)}</b></span></div>'
        body = (banner + f'<section class="hero{" pic" if hero_pic else ""}"{hero_pic}><span class="logo-wrap tilt" data-tilt="9"><img class="logo" '
                f'src="{base}/logo?v={self.picture_version()}" alt="" width="300" height="300" fetchpriority="high"></span>'
                + (f'<div class="kicker">{esc(tagline)}</div>' if tagline else "")
                + f'<h1>{esc(self.site_title())}</h1>'
                + (f'<p class="motto">{esc(motto)}</p>' if motto else "")
                + f'<div class="chips">{chips}</div><div class="acts">{buttons}</div></section>')

        # ---- right now: players, next wipe, today's peak
        body += ('<div class="stats">' + self._server_cards(players, live)
                 + f'<div class="card"><h2><span class="ic">{icon("calendar")}</span>Next wipe</h2>'
                 + (self.countdown(live["next_wipe"]) + f'<div class="muted" style="margin-top:8px">{esc(wipe)} '
                    f'({esc(getattr(tz, "key", "UTC"))})</div>' if wipe_soon else
                    f'<div class="big" style="font-size:1.4rem;line-height:1.3">{esc(wipe)}</div>'
                    f'<div class="muted">{esc(wipe_sub)}</div>') + '</div>')
        if live["peak_24h"]:
            body += (f'<div class="card"><h2><span class="ic">{icon("chart")}</span>Busiest today</h2>'
                     f'<div class="big" data-count>{live["peak_24h"]}</div><div class="muted">players at the peak (24 h)</div></div>')
        body += "</div>"

        # ---- how to get in: the first thing a new player needs
        join = self.setting("how_to_join").strip()
        lines = [ln.strip() for ln in join.splitlines() if ln.strip()]
        numbered = [re.match(r"^\d{1,2}[.)]\s+(.*)$", ln) for ln in lines]
        if len(lines) > 1 and all(numbered):
            how = f'<ol class="steps">{"".join(f"<li><span>{esc(m.group(1))}</span></li>" for m in numbered)}</ol>'
        elif len(lines) == 1 and 2 <= join.count("→") <= 6:        # "Open … → Play → …": one step per arrow
            how = f'<ol class="steps">{"".join(f"<li><span>{esc(part.strip()[:1].upper() + part.strip()[1:])}</span></li>" for part in join.split("→"))}</ol>'
        elif join:
            how = f'<div class="pre">{esc(join)}</div>'
        else:
            how = ('<ol class="steps"><li><span>Open Rust Console Edition and go to <b>Play</b></span></li>'
                   '<li><span>Open <b>Community servers</b> and search for our name</span></li>'
                   '<li><span>Join – and link your player on our Discord with <b>/link</b></span></li></ol>')
        name = self.server_name if self.server_name != "the server" else self.site_title()
        body += (f'<div class="card join" id="join"><div><h2>How to join</h2>{how}</div><div>'
                 f'<div class="muted">Search for this name in the server list:</div><div class="name"><code>{esc(name)}</code>'
                 f'<button class="copy" type="button" data-copy="{esc(name)}">{icon("copy")}<span>Copy</span></button></div>'
                 + (f'<a class="btn" style="margin-top:14px" href="{esc(invite)}" rel="noopener">{icon("chat")}Join our Discord</a>'
                    if INVITE.match(invite) else "") + '</div></div>')

        # ---- everything the site has
        tiles = self.tiles(base)
        if tiles and self.setting("show_tiles"):
            body += f'<div class="sec">Explore <small>everything this server has</small></div><div class="tiles">{tiles}</div>'

        # ---- who is on, how the day went
        online_now = sorted(self.bot.online_players, key=str.lower)
        if self.setting("show_online") and online_now:
            people = "".join(
                (f'<a href="{base}/player?{urlencode({"name": n})}">{self.avatar(n)}{esc(n)}</a>'
                 if self.setting("leaderboards") else f'<span class="p">{self.avatar(n)}{esc(n)}</span>')
                for n in online_now[:30])
            more = (f'<span class="muted" style="align-self:center">+{len(online_now) - 30} more</span>'
                    if len(online_now) > 30 else "")
            body += (f'<div class="sec">{icon("users")}Online now <small>{len(online_now)} playing</small></div>'
                     f'<div class="online">{people}{more}</div>')
        if self.setting("show_chart"):
            chart = self.population_chart()
            body += (f'<div class="sec">{icon("chart")}Activity</div>{chart}') if chart else ""

        # ---- the best
        cols = []
        if self.setting("leaderboards"):
            top = [r for r in self.bot.db.leaderboard("kills", 5) if r["kills"]]
            if top:
                rows = "".join(f'<tr>{self.rank(i)}<td>{self.who(base, r["name"])}<div class="barline">'
                               f'<i style="width:{round(100 * r["kills"] / top[0]["kills"])}%"></i></div></td>'
                               f'<td class="r">{r["kills"]}</td></tr>' for i, r in enumerate(top, 1))
                cols.append(f'<div class="card"><h2>{icon("swords")} Top killers</h2><table>{rows}</table>'
                            f'<a class="btn alt" style="margin-top:12px" href="{base}/leaderboard">Full leaderboard →</a></div>')
        bounties = self.bounties()[:5]
        if bounties:
            rows = "".join(f'<tr><td>{self.who(base, n)}</td><td class="r price">{esc(self.fmt_points(a))}</td></tr>'
                           for n, a in bounties)
            cols.append(f'<div class="card"><h2>{icon("target")} Bounties</h2><table>{rows}</table>'
                        f'<p class="muted">Kill them to collect – /bounty on Discord.</p></div>')
        if cols and self.setting("show_hall"):
            body += f'<div class="sec">{icon("trophy")}Hall of fame</div><div class="grid">{"".join(cols)}</div>'

        # ---- about the server, pictures
        about = self.setting("about").strip()
        shots = [i for i in range(1, GALLERY + 1) if self.file(f"gallery_{i}") is not None]
        if about or shots:
            body += f'<div class="sec">{icon("info")}The server</div>'
        if about:
            body += f'<div class="card"><h2>About</h2><div class="pre">{esc(about)}</div></div>'
        if shots:
            pics = "".join(f'<a href="{base}/gallery/{i}?v={self._file_version(f"gallery_{i}")}" target="_blank">'
                           f'<img src="{base}/gallery/{i}?v={self._file_version(f"gallery_{i}")}" alt="" loading="lazy">'
                           f'</a>' for i in shots)
            body += (f'<div class="card"><h2>📸 {esc(self.setting("gallery_title") or "Our server")}</h2>'
                     f'<div class="gallery">{pics}</div></div>')
        if self.plugin("companion") is not None and self.setting("show_app"):          # the players' app
            body += (f'<div class="card appcard" id="app"><span class="ph" aria-hidden="true">{icon("phone")}</span>'
                     '<div class="txt"><h2>Companion app</h2>Your team, the game chat, a team map, your kits with their '
                     'timers and raid alarms as notifications on your phone. Pair it with <b>/companion</b> on our '
                     'Discord. <span class="muted">iPhone: open it in Safari and choose Share → Add to Home Screen.'
                     '</span></div><div><a class="btn" href="/app/rce-companion.apk" download>Android app</a>'
                     '<a class="btn alt" href="/p/companion/app">iPhone app</a></div></div>')
        early = self.plugin("early_bird")
        if early is not None and live["next_wipe"]:
            body += (f'<div class="card"><h2>{icon("gift")} Wipe day bonus</h2>The first {early.setting("players")} linked '
                     f'players online after the wipe get a reward. Link your player on Discord with /link.</div>')
        return self.page(base, "", "", body)

    @staticmethod
    def avatar(name: str, cls: str = "") -> str:
        """A round badge with the player's first letter – its colour comes from the name, so it's always the same."""
        hue = sum(ord(ch) * (i + 7) for i, ch in enumerate(str(name).lower())) % 360
        letter = next((ch for ch in str(name) if ch.isalnum()), "?").upper()
        return (f'<span class="av {cls}" style="background:linear-gradient(135deg,hsl({hue} 62% 46%),'
                f'hsl({(hue + 40) % 360} 58% 30%))" aria-hidden="true">{esc(letter)}</span>')

    def countdown(self, until: int) -> str:
        """Days / hours / minutes / seconds until a time – site.js makes it tick; without it the numbers stand."""
        left = max(0, int(until) - self.now())
        cells = (("d", left // 86400, "days"), ("h", f"{left % 86400 // 3600:02d}", "hours"),
                 ("m", f"{left % 3600 // 60:02d}", "min"), ("s", f"{left % 60:02d}", "sec"))
        return (f'<div class="count" data-until="{int(until)}">'
                + "".join(f'<span><b data-u="{k}">{v}</b><small>{name}</small></span>' for k, v, name in cells) + "</div>")

    def population_chart(self) -> str:
        """Players over the last 24 hours as a small area chart (drawn here – no script, no outside files)."""
        now = self.now()
        rows = self.bot.db.population(now - 86400, 1800)
        if len(rows) < 4:
            return ""
        top = max(max(p for _, p, _ in rows), 1)
        width, height = 600, 110
        pts = [((t - (now - 86400)) / 86400 * width, height - 6 - (p / top) * (height - 16)) for t, p, _ in rows]
        line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        area = f"{pts[0][0]:.1f},{height} {line} {pts[-1][0]:.1f},{height}"
        peak_i = max(range(len(rows)), key=lambda i: rows[i][1])
        px, py = pts[peak_i]
        peak_at = datetime.fromtimestamp(rows[peak_i][0], self.tz).strftime("%H:%M")
        return (f'<div class="card chart"><h2>{icon("chart")} Players today <small>peak {round(rows[peak_i][1])} at {peak_at}</small></h2>'
                f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
                f'aria-label="Players online over the last 24 hours"><defs><linearGradient id="pg" x1="0" y1="0" x2="0" '
                f'y2="1"><stop offset="0" style="stop-color:var(--orange);stop-opacity:.45"/><stop offset="1" '
                f'style="stop-color:var(--red);stop-opacity:0"/></linearGradient></defs>'
                f'<polygon class="ar" points="{area}" fill="url(#pg)"/><polyline class="ln" pathLength="1" points="{line}" fill="none" '
                f'style="stroke:var(--orange)" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'
                f'<circle class="pk" cx="{px:.1f}" cy="{py:.1f}" r="3.5" style="fill:var(--gold)"/></svg>'
                f'<div class="ax"><span>24 h ago</span><span>12 h ago</span><span>now</span></div></div>')

    @staticmethod
    def rank(i: int) -> str:
        """The rank cell of a table: medals for the first three."""
        return (f'<td class="n m">{("🥇", "🥈", "🥉")[i - 1]}</td>' if 1 <= i <= 3 else f'<td class="n">{i}</td>')

    def who(self, base: str, name: str) -> str:
        """Avatar + linked name, for tables."""
        return f'<span class="who">{self.avatar(name)}{self.player_link(base, name)}</span>'

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
                f'<tr>{self.rank(i)}<td>{self.who(base, r["name"])}</td><td class="r">{r["kills"]}</td>'
                f'<td class="r wide-only">{r["deaths"]}</td><td class="r">{r["kills"] / max(r["deaths"], 1):.2f}</td>'
                f'<td class="r wide-only">{esc(format_duration(r["playtime_seconds"]))}</td></tr>' for i, r in enumerate(rows, 1))
            table = (f'<table><tr><th></th><th>Player</th><th class="r">Kills</th><th class="r wide-only">Deaths</th>'
                     f'<th class="r">K/D</th><th class="r wide-only">Playtime</th></tr>{body}</table>') if rows \
                else '<p class="muted">No stats yet.</p>'
        podium = ""
        if by != "teams" and len(rows) >= 3:                 # the top three get a stage (1st in the middle)
            def value(r):
                return (f'{r["kills"] / max(r["deaths"], 1):.2f}' if by == "kd" else
                        format_duration(r["playtime_seconds"]) if by == "playtime" else str(r["kills"]))
            spots = "".join(
                f'<div class="card p{place}"><span class="medal">{medal}</span>{self.avatar(r["name"])}'
                f'<span class="nm">{self.player_link(base, r["name"])}</span><div class="big"{" data-count" if value(r).isdigit() else ""}>{esc(value(r))}</div>'
                f'<small>{esc(BOARDS[by])}</small></div>'
                for place, medal, r in ((2, "🥈", rows[1]), (1, "🥇", rows[0]), (3, "🥉", rows[2])))
            podium = f'<div class="podium">{spots}</div>'
        return self.page(base, "leaderboard", "Leaderboard",
                         f'<div class="phead"><h1>Leaderboard</h1><p>Kills, K/D, playtime and teams – updated live from the server.</p></div><div class="tabs">{tabs}</div>{podium}'
                         f'<div class="card" style="overflow-x:auto">{table}</div>')

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
        cards = "".join(f'<div class="card"><h2>{k}</h2><div class="big"{" data-count" if str(v).isdigit() else ""}>{esc(v)}</div></div>'
                        for k, v in stats)
        extra = []
        if row["platform"]:
            extra.append(f"Platform: {esc(row['platform'])}")
        team = self.bot.db.team_of(name)
        if team:
            extra.append("Team: " + ", ".join(esc(m) for m in team[1]))
        bounty = dict(self.bounties()).get(name)
        if bounty:
            extra.append(f'🎯 Bounty: <span class="price">{esc(self.fmt_points(bounty))}</span>')
        ranks = []
        for by, label in (("kills", "kills"), ("kd", "K/D"), ("playtime", "playtime")):
            board = [r["name"].lower() for r in self.bot.db.leaderboard(by, 200)]
            if name.lower() in board:
                place = board.index(name.lower()) + 1
                medal = {1: "🥇 ", 2: "🥈 ", 3: "🥉 "}.get(place, "")
                ranks.append(f'<span class="badge">{medal}#{place} in {label}</span>')
        rankline = f'<div class="rankline">{"".join(ranks)}</div>' if ranks else ""
        html_out = (f'<div class="card profile">{self.avatar(name, "xl")}<div><h2>{esc(name)} '
                    f'<span class="pill{" on" if online else ""}">{"online" if online else "offline"}</span></h2>'
                    + ('<div class="muted">' + " · ".join(extra) + "</div>" if extra else "") + rankline + "</div></div>"
                    f'<div class="stats">{cards}</div>')
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
        intro = (f'<div class="card"><h2>How it works</h2>Link your player on Discord with <b>/link</b>, '
                 f'earn {esc(points)} ({esc(", ".join(earn) or "by playing")}) and buy with <b>/shop buy</b>. '
                 f'Items are sent to you in-game.</div>')
        body = (f'<h1>Shop</h1>{intro}<div class="grid">{"".join(cards)}</div>' if cards
                else f'<h1>Shop</h1>{intro}<p class="muted">The shop is empty right now.</p>')
        return self.page(base, "shop", "Shop", body)

    def rules(self, base: str) -> str:
        from cogs.community import load_rules
        rules = load_rules(self.bot.config.rules_file)
        items = "".join(f'<div class="rule"><div>{esc(strip_rich_text(r))}</div></div>' for r in rules)
        head = ('<div class="phead"><h1>Rules</h1><p>Play fair and everyone has a good wipe. Breaking them can get '
                'you kicked or banned.</p></div>')
        body = f'{head}<div class="rulelist">{items}</div>' if items else \
            '<h1>Rules</h1><p class="muted">No rules yet.</p>'
        return self.page(base, "rules", "Rules", body)

    # ------------------------------------------------------------ Staff, bans, wipe history

    def staff_list(self) -> list[tuple[str, str]]:
        """(name, role): members with the admin / support roles (if the bot sees them) + the extra lines."""
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        guild = self.guild
        cfg = self.bot.config
        if guild is not None:
            for rid in list(cfg.admin_role_ids) + [r for r in cfg.support_role_ids if r not in cfg.admin_role_ids]:
                role = guild.get_role(rid)
                for m in (getattr(role, "members", None) or [] if role is not None else []):
                    if not m.bot and m.id not in seen:
                        seen.add(m.id)
                        out.append((m.display_name, role.name))
        for line in str(self.setting("staff_extra") or "").splitlines():
            name, _, role = line.partition("|")
            if name.strip():
                out.append((name.strip()[:40], role.strip()[:40] or "Staff"))
        return out

    def staff_page(self, base: str) -> str:
        rows = self.staff_list()
        cards = "".join(f'<div class="card">{self.avatar(n)}<div><b>{esc(n)}</b><span class="role">{esc(r)}</span></div>'
                        f'</div>' for n, r in rows)
        head = ('<div class="phead"><h1>Staff</h1><p>The people who keep the server running. Need help? Open a '
                'ticket on our Discord.</p></div>')
        body = (f'{head}<div class="people">{cards}</div>' if cards else
                '<h1>Staff</h1><p class="muted">The team list is coming soon.</p>')
        return self.page(base, "staff", "Staff", body)

    @Plugin.listener()
    async def on_rce_ban(self, ev) -> None:
        bans = dict(self.data.get("bans", {}))
        key = ev.player.lower()
        if ev.banned:
            old = bans.get(key, {})
            bans[key] = {"player": ev.player, "at": self.now(), "reason": old.get("reason", "") if old.get("pending")
                         else "", "pending": False}
        else:
            bans.pop(key, None)
        self.data.set("bans", bans)

    @Plugin.listener()
    async def on_rce_ban_reason(self, player: str, reason: str) -> None:
        """The reason given with /ban (the server log line itself has none)."""
        bans = dict(self.data.get("bans", {}))
        row = bans.get(player.lower()) or {"player": player, "at": self.now(), "pending": True}
        row["reason"] = str(reason or "")[:200]
        bans[player.lower()] = row
        self.data.set("bans", bans)

    def bans_page(self, base: str) -> str:
        rows = sorted(self.data.get("bans", {}).values(), key=lambda r: -r["at"])
        body = "".join(f'<tr><td><span class="who">{self.avatar(r["player"])}<span class="nm">{esc(r["player"])}</span></span>'
                       f'</td><td>{datetime.fromtimestamp(r["at"]).strftime("%Y-%m-%d")}</td>'
                       f'<td>{esc(r.get("reason") or "–")}</td></tr>' for r in rows)
        html = ('<h1>Bans</h1><div class="card"><table><tr><th>Player</th><th>Since</th><th>Reason</th></tr>'
                f'{body}</table></div>' if body else '<h1>Bans</h1><p class="muted">Nobody is banned. 🎉</p>')
        return self.page(base, "bans", "Bans", html)

    @Plugin.listener()
    async def on_rce_wiped(self, next_wipe) -> None:
        wipes = list(self.data.get("wipes", []))
        wipes.append(self.now())
        self.data.set("wipes", wipes[-100:])

    def wipes_page(self, base: str) -> str:
        wipes = sorted(self.data.get("wipes", []), reverse=True)
        nxt, now = self.next_wipe(), self.now()

        def ago(ts):
            days = (now - ts) // 86400
            return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"
        items = "".join(f"<li><b>{datetime.fromtimestamp(w, self.tz).strftime('%A, %d %B %Y')}</b> "
                        f"{datetime.fromtimestamp(w, self.tz).strftime('%H:%M')}<small>{ago(w)}</small></li>"
                        for w in wipes[:50])
        body = ('<div class="phead"><h1>Wipes</h1><p>When the map was wiped – and when the next fresh start is.</p></div>'
                + (f'<div class="card"><h2>{icon("calendar")} Next wipe</h2>'
                   + (self.countdown(nxt) if nxt > now else "")
                   + f'<div class="muted" style="margin-top:8px">{datetime.fromtimestamp(nxt, self.tz).strftime("%A, %d %B %Y · %H:%M")}'
                     f' ({esc(getattr(self.tz, "key", "UTC"))})</div></div>' if nxt else "")
                + (f'<div class="card"><h2>Past wipes ({len(wipes)})</h2><ul class="timeline">{items}</ul></div>' if items else
                   '<p class="muted">No wipes recorded yet – they are added here from the next wipe on.</p>'))
        return self.page(base, "wipes", "Wipes", body)

    def not_found(self, base: str) -> str:
        tiles = self.tiles(base)
        return self.page(base, "", "Not found",
                         f'<div class="lost"><div class="code">404</div><h1 style="display:inline-block">Not found</h1>'
                         f'<p>This page got raided – nothing left here.</p><a class="btn" href="{base}/">Back to the '
                         f'home page</a></div>' + (f'<div class="tiles">{tiles}</div>' if tiles else ""))

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
        if path == "staff" and self.setting("show_staff"):
            return 200, self.staff_page(base)
        if path == "bans" and self.setting("show_bans"):
            return 200, self.bans_page(base)
        if path == "wipes" and self.setting("show_wipes"):
            return 200, self.wipes_page(base)
        if path == "info" and self.own_page() is not None:
            return 200, self.info_page(base)
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
            if custom is not None:
                return (custom.name, custom.stat().st_mtime_ns, custom.stat().st_size)
        except OSError:
            pass
        icon = getattr(self, "_guild_icon", None)
        return ("discord", icon[0]) if icon else ()

    # ------------------------------------------------------------ the Discord server's icon (the default logo)

    @tasks.loop(minutes=20)
    async def guild_icon_loop(self) -> None:
        await self.refresh_guild_icon()

    @guild_icon_loop.before_loop
    async def _icon_wait(self) -> None:
        await self.bot.wait_until_ready()

    @Plugin.listener()
    async def on_guild_update(self, before, after) -> None:
        if self.guild is not None and after.id == self.guild.id:
            await self.refresh_guild_icon()

    def discord_css(self) -> str:
        """The "discord" theme for the current server icon (worked out once per icon)."""
        icon = getattr(self, "_guild_icon", None)
        key = icon[0] if icon else None
        cached = getattr(self, "_discord_css", None)
        if cached is None or cached[0] != key:
            css = discord_theme_css(icon_palette(icon[1])) if icon else ""
            self._discord_css = cached = (key, css)
        return cached[1]

    async def refresh_guild_icon(self) -> None:
        """Keeps the Discord server's icon in memory – the website's logo when none was uploaded."""
        guild = self.guild
        icon = getattr(guild, "icon", None) if guild is not None else None
        if icon is None:
            self._guild_icon = None
            return
        asset = icon.replace(size=512, format="png")
        current = getattr(self, "_guild_icon", None)
        if current and current[0] == asset.key:
            return
        try:
            self._guild_icon = (asset.key, await asset.read())
        except Exception as exc:                       # Discord unreachable: keep what we had
            self.log.debug("Server icon: %s", exc)

    def _file_version(self, key: str) -> str:
        custom = self.file(key)
        try:
            state = (custom.name, custom.stat().st_mtime_ns, custom.stat().st_size) if custom is not None else ()
        except OSError:
            state = ()
        return hashlib.blake2s(repr(state).encode(), digest_size=4).hexdigest()

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
        icon = getattr(self, "_guild_icon", None)
        if not kind and icon:                          # no own logo: the Discord server's icon
            data = icon[1]
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
        if path in ("banner", "background") or (path.startswith("gallery/") and path[8:].isdigit()):
            key = path if path in ("banner", "background") else f"gallery_{path[8:]}"
            custom = self.file(key) if key in ("banner", "background") or 1 <= int(path[8:]) <= GALLERY else None
            if custom is None or not custom.exists():
                return web.Response(status=404, text="Not found.", headers=headers)
            from web.item_images import image_type
            data = custom.read_bytes()
            return conditional(request, data, image_type(data) or "image/png", YEAR if request.query.get("v")
                               else "public, max-age=3600",
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
        if path == "battlepass":
            bp = self.battlepass()
            if bp is None:
                return web.Response(status=404, text=self.not_found(base), content_type="text/html",
                                    headers={**headers, "Cache-Control": "no-store"})
            from web.battlepass_pages import handle as bp_handle
            resp = await bp_handle(self, bp, request, base)
            for key, value in headers.items():
                resp.headers.setdefault(key, value)
            resp.headers["Cache-Control"] = "no-store"
            return resp
        if path == "streams":
            streams = self.streams()
            if streams is None:
                return web.Response(status=404, text=self.not_found(base), content_type="text/html",
                                    headers={**headers, "Cache-Control": "no-store"})
            from web.streams_pages import render as streams_render
            resp = streams_render(self, streams, request, base)
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
        if path.strip("/") in ("", "index.html"):
            await self.refresh_servers()             # several servers: every one on the home page
        status, text = self.render(base, path, request.query)
        return web.Response(status=status, text=text, content_type="text/html", headers=headers)
