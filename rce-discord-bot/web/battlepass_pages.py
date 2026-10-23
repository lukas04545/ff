"""Battle pass page of the player website (/battlepass): level and XP, the free and premium reward tracks, buying the
premium pass with the premium currency, the daily bonus and coin packs (web store checkout).

Server-rendered forms (they work without scripts). Players log in with the website's player login; the login cookie
is SameSite=Lax and a POST naming another origin is refused.
"""
from __future__ import annotations

import time
from urllib.parse import quote, urlparse

from aiohttp import web

from web.account_pages import COOKIE, _base_url
from web.store_pages import STORE_CSS, esc, notice


def _n(value: int) -> str:
    return f"{value:,}".replace(",", " ")

BP_CSS = STORE_CSS + """
.bp-hero{display:grid;grid-template-columns:1fr auto;gap:16px;align-items:center}
@media (max-width:640px){.bp-hero{grid-template-columns:1fr}}
.bp-level{font-family:var(--head);font-size:3rem;line-height:1;color:#fff}
.bp-level small{font-size:1rem;color:var(--muted);letter-spacing:.1em}
.xpbar{height:14px;background:rgba(255,255,255,.08);border:1px solid var(--line);margin:10px 0 4px;position:relative;
overflow:hidden}.xpbar i{position:absolute;inset:0 auto 0 0;background:var(--grad);transform-origin:left;
animation:grow 1s cubic-bezier(.2,.8,.3,1) both}@keyframes grow{from{transform:scaleX(0)}}
.wallet{font-family:var(--head);font-size:1.6rem;color:var(--gold);letter-spacing:.04em}
.premium-badge{display:inline-block;padding:3px 12px;background:linear-gradient(90deg,#ffb627,#ff7a1a);color:#1a0d00;
font-weight:800;letter-spacing:.08em;text-transform:uppercase;font-size:.8rem;clip-path:polygon(8px 0,100% 0,
calc(100% - 8px) 100%,0 100%)}
.track{display:flex;gap:10px;overflow-x:auto;padding:6px 2px 14px;scroll-snap-type:x proximity}
.tier{flex:none;width:136px;scroll-snap-align:start;display:flex;flex-direction:column;gap:8px}
.tier .num{text-align:center;font-family:var(--head);color:var(--muted);letter-spacing:.08em}
.tier.reached .num{color:var(--orange)}.tier.current .num{color:#fff;text-shadow:0 0 10px rgba(247,147,30,.8)}
.rw{height:150px;border:1px solid var(--line);background:rgba(0,0,0,.35);display:flex;flex-direction:column;
align-items:center;justify-content:center;gap:4px;padding:8px 6px;text-align:center;position:relative}
.rw.paid{border-color:rgba(255,182,39,.45);background:linear-gradient(180deg,rgba(255,182,39,.12),rgba(0,0,0,.4))}
.rw.none{opacity:.35}.rw .ico{font-size:2rem;line-height:1}.rw img{width:56px;height:56px;object-fit:contain}
.rw .lbl{font-size:.8rem;line-height:1.2}.rw.done{opacity:.55}.rw.done::after{content:"✓";position:absolute;top:4px;
right:8px;color:var(--ok);font-weight:800}.rw .lock{position:absolute;top:4px;right:8px;font-size:.9rem}
.rw form{margin:0}.rw button{padding:4px 10px;border:0;background:var(--grad);color:#fff;font-family:var(--head);
letter-spacing:.06em;text-transform:uppercase;cursor:pointer;font-size:.85rem}
.rowlabel{font-family:var(--head);letter-spacing:.1em;color:var(--muted);margin:2px 0}
.packs{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.pack{text-align:center;padding:16px 10px}.pack .amt{font-family:var(--head);font-size:1.5rem;color:var(--gold)}
.pack button.pay{margin-top:10px}
"""


def _page(site, base: str, body: str, status: int = 200) -> web.Response:
    page = site.page(base, "battlepass", "Battle pass", f"<h1>Battle pass</h1>{body}")
    return web.Response(status=status, text=page.replace("</style>", BP_CSS + "</style>", 1),
                        content_type="text/html", headers={"Cache-Control": "no-store"})


def _same_origin(request) -> bool:
    origin = request.headers.get("Origin")
    if origin is None or origin == "null":
        return True
    return (urlparse(origin).netloc or "").lower() == (request.host or "").lower()


def _reward_box(bp, base: str, tier: int, track: str, reward, state: str) -> str:
    """state: locked (tier not reached), premium (needs the pass), ready, done."""
    cls = "rw" + (" paid" if track == "paid" else "")
    if reward is None:
        return f'<div class="{cls} none"><span class="lbl">–</span></div>'
    if reward.kind == "item":
        pic = (f'<img src="/img/items/{quote(reward.item)}.png?ph=1&amp;s=96" alt="" width="56" height="56" '
               'loading="lazy" decoding="async">')
    else:
        pic = f'<div class="ico">{esc(bp.coin_icon) if reward.kind == "coins" else "⭐"}</div>'
    label = f'<div class="lbl">{esc(reward.label(bp.coin_name, bp.bot.config.points_name))}</div>'
    if state == "done":
        return f'<div class="{cls} done">{pic}{label}</div>'
    if state == "ready":
        return (f'<div class="{cls}">{pic}{label}<form method="post" action="{base}/battlepass"><input type="hidden" '
                f'name="action" value="claim"><input type="hidden" name="tier" value="{tier}"><input type="hidden" '
                f'name="track" value="{track}"><button type="submit">Claim</button></form></div>')
    lock = "🔒" if state == "premium" else ""
    return f'<div class="{cls}"><span class="lock">{lock}</span>{pic}{label}</div>'


def page(site, bp, shop, base: str, uid: int, *, ok: str = "", error: str = "") -> web.Response:
    p = bp.progress(uid)
    tiers = bp.tiers()
    top = max(tiers) if tiers else 0
    per = max(1, int(bp.setting("xp_per_tier")))
    level = bp.level(p["xp"])
    into = p["xp"] - level * per if level < top else per
    pct = min(100, round(into * 100 / per))
    coins = bp.bot.db.coins(uid)
    ends = bp.season_end()
    left = ""
    if ends:
        days = max(0, (ends - int(time.time())) // 86400)
        left = f' · ends in {days} day{"s" if days != 1 else ""}'
    price = int(bp.setting("pass_price"))
    if p["premium"]:
        premium = '<span class="premium-badge">Premium pass</span>'
    else:
        premium = (f'<form method="post" action="{base}/battlepass"><input type="hidden" name="action" value="buy">'
                   f'<button class="pay" type="submit">Get the premium pass – {esc(bp.fmt_coins(price))}</button>'
                   f'</form><div class="muted" style="font-size:.85rem">The premium track pays back '
                   f'{esc(bp.fmt_coins(bp.premium_coins()))} over the season.</div>')
    today = time.strftime("%Y-%m-%d", time.gmtime())
    daily = ("" if not int(bp.setting("daily_xp")) else
             '<span class="muted">Daily bonus claimed ✓</span>' if p["daily"] == today else
             f'<form method="post" action="{base}/battlepass" style="display:inline"><input type="hidden" '
             f'name="action" value="daily"><button class="ghost" type="submit">🎁 Daily bonus '
             f'+{int(bp.setting("daily_xp"))} XP</button></form>')
    ready = any(level >= t and ((f and t not in p["free"]) or (pd and p["premium"] and t not in p["paid"]))
                for t, (f, pd) in tiers.items())
    claim_all = (f'<form method="post" action="{base}/battlepass" style="display:inline"><input type="hidden" '
                 'name="action" value="claim_all"><button class="pay" type="submit" style="width:auto;margin:0 0 0 8px;'
                 'padding:8px 18px">Claim all</button></form>' if ready else "")
    xp_text = "Max tier reached!" if level >= top else f"{_n(into)} / {_n(per)} XP to tier {level + 1}"
    hero = (f'<div class="card bp-hero"><div><div class="muted">{esc(bp.setting("season_name"))}{left}</div>'
            f'<div class="bp-level">Tier {level} <small>/ {top}</small></div>'
            f'<div class="xpbar"><i style="width:{pct}%"></i></div>'
            f'<div class="muted">{xp_text}</div><div style="margin-top:10px">{daily}{claim_all}</div></div>'
            f'<div style="text-align:right"><div class="wallet">{esc(bp.fmt_coins(coins))}</div>'
            f'<div style="margin-top:8px">{premium}</div></div></div>')
    columns = []
    for t, (free, paid) in tiers.items():
        reached = level >= t
        fstate = "done" if t in p["free"] else "ready" if reached else "locked"
        pstate = ("done" if t in p["paid"] else "premium" if not p["premium"] else "ready" if reached else "locked")
        cls = "tier" + (" reached" if reached else "") + (" current" if t == level + 1 else "")
        columns.append(f'<div class="{cls}" id="t{t}"><div class="num">{t}</div>'
                       f'{_reward_box(bp, base, t, "free", free, fstate)}'
                       f'{_reward_box(bp, base, t, "paid", paid, pstate)}</div>')
    track = (f'<div class="card"><div class="rowlabel">Free ▸ top row · Premium ▸ bottom row</div>'
             f'<div class="track">{"".join(columns)}</div>'
             f'<p class="muted">XP: {int(bp.setting("xp_per_minute"))} per minute online, '
             f'{int(bp.setting("xp_per_kill"))} per PvP kill, the daily bonus. Item rewards are given in-game – be '
             f'online when you claim them.</p></div>')
    packs = ""
    if shop is not None and bp.packs():
        cards = "".join(
            f'<div class="card pack"><div class="amt">{esc(bp.coin_icon)} {_n(n)}</div><div class="muted">'
            f'{esc(bp.coin_name)}</div><div class="price">{esc(shop.fmt(price))}</div>'
            f'<form method="post" action="{base}/battlepass"><input type="hidden" name="action" value="coins">'
            f'<input type="hidden" name="pack" value="{n}"><input name="gift_code" placeholder="Gift card (optional)" '
            f'maxlength="40" style="width:100%;margin-top:8px;padding:7px;background:rgba(0,0,0,.5);color:var(--text);'
            f'border:1px solid var(--line)"><button class="pay" type="submit">Buy</button></form></div>'
            for n, price in bp.packs())
        packs = f'<div class="card"><h2>Get {esc(bp.coin_name)}</h2><div class="packs">{cards}</div></div>'
    body = notice(ok, "ok") + notice(error, "bad") + hero + track + packs
    return _page(site, base, body)


async def handle(site, bp, request, base: str) -> web.Response:
    accounts = site.accounts()
    if accounts is None:
        return _page(site, base, notice("Player login is switched off.", "bad"), status=404)
    sess = accounts.session(request.cookies.get(COOKIE))
    if sess is None:
        return _page(site, base, '<div class="card"><p>Log in to see your battle pass.</p>'
                                 f'<p><a class="btn" href="{base}/account">Log in</a></p></div>')
    uid = sess["user_id"]
    shop = site.store()
    if request.method != "POST":
        return page(site, bp, shop, base, uid)
    if not _same_origin(request):
        raise web.HTTPForbidden(text="Only from this website.")
    from plugins.battlepass import PassError
    form = await request.post()
    action = str(form.get("action", ""))
    try:
        if action == "buy":
            bp.buy_pass(uid)
            return page(site, bp, shop, base, uid, ok="Premium pass unlocked – enjoy the rewards!")
        if action == "daily":
            xp = bp.claim_daily(uid)
            return page(site, bp, shop, base, uid, ok=f"+{xp} XP daily bonus!")
        if action == "claim":
            tier = int(str(form.get("tier", "0")) or 0) if str(form.get("tier", "")).isdigit() else 0
            got = await bp.claim(uid, tier, str(form.get("track", "")))
            return page(site, bp, shop, base, uid, ok=f"Claimed: {got}")
        if action == "claim_all":
            given, failed = await bp.claim_all(uid)
            return page(site, bp, shop, base, uid, ok=("Claimed: " + ", ".join(given)) if given else "",
                        error=" · ".join(failed[:3]) if failed else ("" if given else "Nothing to claim yet."))
        if action == "coins":
            if shop is None:
                raise PassError("The store isn't open right now.")
            from cogs.webshop import CheckoutError
            from web.server import WebInterface
            try:
                result = await shop.checkout("coins", base_url=_base_url(request, base), user_id=uid,
                                             value=form.get("pack"), gift_code=str(form.get("gift_code", "")),
                                             ip=WebInterface._client_ip(request))
            except CheckoutError as exc:
                raise PassError(str(exc)) from None
            raise web.HTTPSeeOther(result.get("redirect") or f"{base}/store/order/{result['token']}")
        raise PassError("Unknown action.")
    except PassError as exc:
        return page(site, bp, shop, base, uid, error=str(exc))
