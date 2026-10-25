"""Streams page of the player website (/streams): who streams on the server – the live streams embedded (watch right
on the site), the others with a link to their channel. Data from plugins/streamers.py.

The players come from the platforms themselves (iframes: player.twitch.tv, player.kick.com, youtube-nocookie.com –
allowed in the website's Content-Security-Policy, plugins/website.py); no scripts of ours or theirs on the page.
"""
from __future__ import annotations

from urllib.parse import quote

from aiohttp import web

from web.store_pages import STORE_CSS, esc

STREAMS_CSS = STORE_CSS + """
.st-feature{position:relative;width:100%;aspect-ratio:16/9;background:#000;border:1px solid var(--line);
box-shadow:0 0 30px rgba(145,70,255,.25)}.st-feature iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
.st-head{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin:10px 0 4px}
.st-head h2{margin:0}.st-live{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;background:#e91916;
color:#fff;font-weight:800;font-size:.75rem;letter-spacing:.1em;text-transform:uppercase}
.st-live::before{content:"";width:8px;height:8px;border-radius:50%;background:#fff;animation:st-pulse 1.4s infinite}
@keyframes st-pulse{50%{opacity:.25}}
.st-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px;margin-top:12px}
.st-card{display:block;text-decoration:none;color:inherit;border:1px solid var(--line);background:rgba(0,0,0,.35)}
.st-card.on{border-color:rgba(145,70,255,.6)}.st-card .thumb{position:relative;aspect-ratio:16/9;background:#111;
overflow:hidden}.st-card .thumb img{width:100%;height:100%;object-fit:cover;display:block}
.st-card .thumb .st-live{position:absolute;left:8px;top:8px}.st-card .thumb .v{position:absolute;right:8px;bottom:8px;
background:rgba(0,0,0,.75);padding:2px 8px;font-size:.8rem}
.st-card .meta{padding:10px 12px}.st-card .meta b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.st-card .meta small{color:var(--muted)}.st-off{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.st-off a{padding:6px 12px;border:1px solid var(--line);text-decoration:none;color:var(--muted)}
.st-off a:hover{color:#fff;border-color:var(--orange)}
"""
NAMES = {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube"}


def player_url(s, info, host: str) -> str:
    """The platform's own embeddable player for a live stream."""
    if s.platform == "twitch":
        return (f"https://player.twitch.tv/?channel={quote(s.channel)}&parent={quote(host)}"
                "&muted=true&autoplay=true")
    if s.platform == "kick":
        return f"https://player.kick.com/{quote(s.channel)}?muted=true&autoplay=true"
    return f"https://www.youtube-nocookie.com/embed/{quote(info.video_id)}?autoplay=1&mute=1"


def render(site, plugin, request, base: str) -> web.Response:
    data = plugin.site_data()
    live, offline = data["live"], data["offline"]
    host = (request.host or "localhost").split(":")[0]
    watch = str(request.query.get("watch", ""))
    featured = next(((s, i) for s, i in live if s.key == watch), live[0] if live else None)
    body = ""
    if featured:
        s, info = featured
        viewers = f" · {info.viewers:,} watching".replace(",", " ") if info.viewers is not None else ""
        body += (f'<div class="st-head"><h2>{esc(s.channel)}</h2><span><span class="st-live">Live</span> '
                 f'<span class="muted">{esc(NAMES[s.platform])}{esc(viewers)}</span></span></div>'
                 f'<div class="st-feature"><iframe src="{esc(player_url(s, info, host))}" allowfullscreen '
                 f'allow="autoplay; fullscreen; picture-in-picture" referrerpolicy="strict-origin-when-cross-origin" '
                 f'title="{esc(s.channel)} live"></iframe></div>'
                 f'<p>{esc(info.title)}{" · " + esc(info.game) if info.game else ""} · '
                 f'<a href="{esc(s.url)}" target="_blank" rel="noopener">open on {esc(NAMES[s.platform])} ↗</a></p>')
        cards = ""
        for s, info in live:
            if s is featured[0]:
                continue
            thumb = f'<img src="{esc(info.thumbnail)}" alt="" loading="lazy">' if info.thumbnail else ""
            viewers = f'<span class="v">{info.viewers:,} watching</span>'.replace(",", " ") \
                if info.viewers is not None else ""
            cards += (f'<a class="st-card on" href="{base}/streams?watch={quote(s.key)}"><div class="thumb">{thumb}'
                      f'<span class="st-live">Live</span>{viewers}</div><div class="meta"><b>{esc(s.channel)}</b>'
                      f'<small>{esc(info.title or NAMES[s.platform])}</small></div></a>')
        if cards:
            body += f'<h2>Also live</h2><div class="st-grid">{cards}</div>'
    else:
        body += ('<div class="card"><h2>Nobody is live right now</h2><p class="muted">When one of our streamers goes '
                 'live, the stream shows up here – and in our Discord.</p></div>')
    if offline:
        links = "".join(f'<a href="{esc(s.url)}" target="_blank" rel="noopener">{esc(s.channel)} '
                        f'<small>· {esc(NAMES[s.platform])}</small></a>' for s in offline)
        body += f'<h2>Our streamers</h2><div class="st-off">{links}</div>'
    if not live and not offline:
        body = '<div class="card"><p class="muted">No streamers yet.</p></div>'
    page = site.page(base, "streams", "Streams", f"<h1>Streams</h1>{body}")
    return web.Response(text=page.replace("</style>", STREAMS_CSS + "</style>", 1), content_type="text/html",
                        headers={"Cache-Control": "no-store"})
