"""Two separate web apps on two separate listeners:

* PORTAL (public, behind Caddy): customers sign up, subscribe (Stripe), add servers (only while a slot is free),
  set each server's bot token, open each server's own panel/website, remove servers. Plus the Stripe webhook and the
  "paused" page for suspended sites. There is NO route, form field or parameter here that changes slots.
* OWNER: every customer, subscription, slot, server and health in one place; grant/revoke slots; suspend,
  reactivate, restart, remove, purge. Login: owner password + TOTP code. Runs on its private listener (127.0.0.1) and
  – unless CP_OWNER_PATH=off – also as a website on the portal under /management (mount_owner): its own cookie
  (path /management), noindex, rate limits per IP and per username, optional IP allowlist (CP_OWNER_ALLOW_IPS).

Both: signed session cookies (HttpOnly, SameSite=Strict, Secure behind HTTPS), CSRF token on every form, ownership
checks on every customer action (a customer can only ever touch rows with their own customer_id).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import re
import secrets
import time
from typing import Awaitable, Callable

from aiohttp import web

from urllib.parse import quote_plus

from control_plane import (actions, billing, features, hostproxy, manage_ai, owner_ui, plans, provisioning, shop_ui,
                           slots, updates, websettings)
from control_plane.crypto import SecretBox
from control_plane.db import DB
from control_plane.settings import Settings
from web.ratelimit import RateLimiter

SESSION_TTL = 12 * 3600
CSS = """body{font-family:system-ui,sans-serif;background:#111418;color:#e8e8e8;margin:0}main{max-width:1100px;
margin:2rem auto;padding:0 1rem}a{color:#f7931e}table{width:100%;border-collapse:collapse;margin:.5rem 0 1.5rem}
td,th{padding:.4rem .5rem;border-bottom:1px solid #2a2f36;text-align:left;font-size:.9rem;vertical-align:top}
.card{background:#1a1e24;border:1px solid #2a2f36;border-radius:10px;padding:1rem 1.2rem;margin:1rem 0}
input,button,select{padding:.45rem .6rem;border-radius:6px;border:1px solid #3a404a;background:#0e1114;color:#eee}
button{background:#f7931e;color:#111;border:0;font-weight:600;cursor:pointer}button.danger{background:#c0392b;color:#fff}
.pill{padding:.1rem .5rem;border-radius:99px;font-size:.8rem;background:#2a2f36}.ok{background:#1e7e34}
.warn{background:#9a6a00}.bad{background:#a12a2a}form.inline{display:inline}.muted{color:#999}.err{color:#ff8a80}
code{background:#0e1114;padding:.1rem .3rem;border-radius:4px}"""
OK_STATES = {"active", "running", "trialing", "comped"}


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def pill(text: str) -> str:
    tone = "ok" if text in OK_STATES else ("bad" if text in ("suspended", "canceled", "crashloop", "unpaid",
                                                              "over_limit", "deleted", "crashed") else "warn")
    return f'<span class="pill {tone}">{esc(text)}</span>'


def page(title: str, body: str) -> web.Response:
    return web.Response(text=f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport "
                             f"content='width=device-width,initial-scale=1'><title>{esc(title)}</title><style>{CSS}"
                             f"</style></head><body><main>{body}</main></body></html>", content_type="text/html",
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                                            "form-action 'self' https://checkout.stripe.com "
                                                            "https://billing.stripe.com; frame-ancestors 'none'",
                                 "Referrer-Policy": "no-referrer"})


class Sessions:
    """HMAC-signed cookie "<subject>.<expires>.<nonce>.<sig>"; a different key per app (portal/owner)."""

    def __init__(self, secret: str, purpose: str, cookie: str) -> None:
        if len(secret) < 32:
            raise ValueError("CP_SESSION_SECRET must be at least 32 characters.")
        self.key = hmac.new(secret.encode(), purpose.encode(), hashlib.sha256).digest()
        self.cookie = cookie

    def _sig(self, msg: str) -> str:
        return hmac.new(self.key, msg.encode(), hashlib.sha256).hexdigest()

    def issue(self, resp: web.StreamResponse, subject: str, request: web.Request, *, path: str = "/") -> None:
        msg = f"{subject}.{int(time.time()) + SESSION_TTL}.{secrets.token_hex(8)}"
        resp.set_cookie(self.cookie, f"{msg}.{self._sig(msg)}", httponly=True, samesite="Strict", max_age=SESSION_TTL,
                        secure=request.headers.get("X-Forwarded-Proto") == "https" or request.secure, path=path)

    def subject(self, request: web.Request) -> str | None:
        raw = request.cookies.get(self.cookie, "")
        parts = raw.split(".")
        if len(parts) != 4 or not parts[1].isdigit() or int(parts[1]) < time.time():
            return None
        msg = ".".join(parts[:3])
        return parts[0] if hmac.compare_digest(parts[3], self._sig(msg)) else None

    def csrf(self, request: web.Request) -> str:
        return self._sig("csrf:" + request.cookies.get(self.cookie, ""))[:32]

    async def form(self, request: web.Request) -> dict:
        """The POSTed form after checking the CSRF token (and that the request comes from this site)."""
        data = dict(await request.post())
        token = str(data.get("csrf", ""))
        if not request.cookies.get(self.cookie) or not hmac.compare_digest(token, self.csrf(request)):
            raise web.HTTPForbidden(text="Form expired – go back and reload the page.")
        origin = request.headers.get("Origin")
        if origin and origin != "null" and origin.split("://", 1)[-1] != request.host:
            raise web.HTTPForbidden(text="Cross-site request refused.")
        return data


def _same_origin(request: web.Request) -> None:
    """Forms that work without a session (sign in / sign up) only from this site – no login CSRF."""
    origin = request.headers.get("Origin")
    if origin and origin != "null" and origin.split("://", 1)[-1] != request.host:
        raise web.HTTPForbidden(text="Cross-site request refused.")


def _client_ip(request: web.Request) -> str:
    return (request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0]
            or request.remote or "?").strip()[:64]


class OAuthError(Exception):
    pass


async def exchange_code(settings: Settings, code: str) -> str:
    """Discord OAuth2 "add bot" code -> the id of the Discord server it was added to (from Discord, not the URL)."""
    import aiohttp
    if not code:
        raise OAuthError("Discord didn't send a code – try again.")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.post("https://discord.com/api/v10/oauth2/token",
                                data={"grant_type": "authorization_code", "code": code,
                                      "redirect_uri": f"{settings.portal_url}/discord/callback"},
                                auth=aiohttp.BasicAuth(settings.shared_client_id, settings.shared_client_secret)) as resp:
            data = await resp.json(content_type=None)
    guild = (data or {}).get("guild") or {}
    if not guild.get("id"):
        raise OAuthError("Discord didn't confirm the server – try again.")
    return str(guild["id"])


# ================================================================== PORTAL (customers)

def build_portal(db: DB, box: SecretBox, settings: Settings, changed: Callable[[], Awaitable[None]]) -> web.Application:
    # every website (<slug>.<domain>) can come in through this listener too (one Cloudflare tunnel): hostproxy
    app = web.Application(client_max_size=hostproxy.MAX_BODY, middlewares=[hostproxy.middleware(db, settings)])
    sess = Sessions(settings.session_secret, "portal", "cp_c")
    limiter = RateLimiter(10, 300)
    plans.seed(db)                                       # a new shop starts with example packages

    def me(request) -> dict | None:
        sub = sess.subject(request)
        if not sub or not sub.startswith("c"):
            return None
        row = db.one("SELECT * FROM customers WHERE id = ? AND status != 'deleted'", (int(sub[1:]),))
        return dict(row) if row else None

    def need(request) -> dict:
        c = me(request)
        if c is None:
            raise web.HTTPSeeOther("/")
        return c

    def own_server(customer: dict, server_id: int) -> dict:
        """Ownership check: the server must belong to THIS customer – otherwise it doesn't exist for them."""
        row = db.one("SELECT * FROM servers WHERE id = ? AND customer_id = ? AND status != 'deleted'",
                     (server_id, customer["id"]))
        if row is None:
            raise web.HTTPNotFound(text="No such server.")
        return dict(row)

    def shop(request, title: str, body: str) -> web.Response:
        c = me(request)
        if c:
            nav = (f"<a href=/dashboard>Dashboard</a><form method=post action=/logout class=inline>"
                   f"<input type=hidden name=csrf value={sess.csrf(request)}><button class='ghost small'>Sign out"
                   f"</button></form>")
        else:
            nav = "<a href=/#plans>Packages</a><a href=/login>Sign in</a><a class='btn small' href=/signup>Get started</a>"
        return shop_ui.shop_page(settings.shop_name, title, body, nav=nav)

    def flash(request) -> str:
        msg = request.query.get("m") or request.query.get("e") or ""
        return f"<div class=flash>{esc(msg)}</div>" if msg else ""

    async def index(request):
        """The shop: what the bot does and the packages."""
        signed_in = me(request) is not None

        def choose(p):
            if signed_in:
                return (f"<form method=post action=/billing/checkout><input type=hidden name=csrf "
                        f"value={sess.csrf(request)}><input type=hidden name=plan value={p['id']}>"
                        f"<button>Choose {esc(p['name'])}</button></form>")
            return f"<a class=btn href='/signup?plan={esc(p['slug'])}'>Choose {esc(p['name'])}</a>"
        items = plans.listed(db)
        cards = (shop_ui.plan_cards(items, action=choose) if items else
                 "<p class=section>Packages are coming soon.</p>")
        return shop(request, "Discord bot for Rust Console servers", f"""{flash(request)}
<section class=hero><h1>Your Rust Console server,<br><em>run by one smart bot</em></h1>
<p>Kits, shop, points, tickets, stats, events and an AI that does the admin work – set up in minutes, hosted for
you. No PC that has to stay on.</p><a class=btn href=#plans>See the packages</a>
{'' if signed_in else ' <a class="btn ghost" href=/login>Sign in</a>'}</section>
<div class=feats><div class=feat><b>🎁 Kits &amp; shop</b><span>Kit claims with emotes, a points shop and a web store
for your players.</span></div><div class=feat><b>🎫 Tickets &amp; moderation</b><span>Support tickets, reports,
watchlists – with AI help if you want it.</span></div><div class=feat><b>📊 Stats &amp; events</b><span>Leaderboards,
bounties, battle royale, weekly recaps and more.</span></div><div class=feat><b>🛡️ Hosted &amp; protected</b>
<span>Your own admin website, encrypted database, DDoS protection, daily backups.</span></div></div>
<h2 class=section id=plans>Packages</h2><p class=section>Monthly, cancel any time. Sign up to create an account.</p>
{cards}""")

    async def signup_page(request):
        if me(request):
            raise web.HTTPSeeOther("/dashboard")
        plan = plans.get(db, slug=request.query.get("plan", "")) if request.query.get("plan") else None
        chosen = (f"<p>Package: <b>{esc(plan['name'])}</b> – {esc(plans.price_text(plan))} / month "
                  f"<a href=/#plans>change</a></p><input type=hidden name=plan value={esc(plan['slug'])}>"
                  if plan and plan["active"] else "")
        return shop(request, "Create your account", f"""<div class=auth>{flash(request)}<div class=card>
<h2>Create your account</h2><form method=post action=/signup>{chosen}
<label>Community name<input name=name placeholder="e.g. Kryvon Empire"></label>
<label>E-mail<input name=email type=email required autocomplete=email></label>
<label>Password<input name=password type=password minlength=10 required autocomplete=new-password
placeholder="10+ characters"></label><button style="width:100%">{'Continue to payment' if chosen else 'Sign up'}
</button></form><p class=muted>Already have an account? <a href=/login>Sign in</a></p></div></div>""")

    async def login_page(request):
        if me(request):
            raise web.HTTPSeeOther("/dashboard")
        return shop(request, "Sign in", f"""<div class=auth>{flash(request)}<div class=card><h2>Sign in</h2>
<form method=post action=/login><label>E-mail<input name=email type=email required autocomplete=email></label>
<label>Password<input name=password type=password required autocomplete=current-password></label>
<button style="width:100%">Sign in</button></form><p class=muted>New here? <a href=/signup>Create an account</a></p>
</div></div>""")

    async def start_checkout(request, c: dict, plan: dict) -> web.HTTPSeeOther:
        """Stripe Checkout for this package (its Stripe price is created on the fly when needed)."""
        try:
            price = await plans.ensure_stripe_price(db, settings, plan["id"])
            if not price:
                raise billing.BillingError("Payments aren't set up yet – we'll switch you on by hand.")
            base = settings.portal_url
            url = await billing.checkout_url(settings, c, f"{base}/dashboard?m=Thanks!+Your+package+is+active.",
                                             f"{base}/dashboard", price_id=price, plan_id=plan["id"])
        except (billing.BillingError, plans.PlanError) as exc:
            return web.HTTPSeeOther("/dashboard?m=" + quote_plus(str(exc)))
        return web.HTTPSeeOther(url)

    async def signup(request):
        _same_origin(request)
        data = dict(await request.post())
        if limiter.hit("signup:" + _client_ip(request)) is not None:
            raise web.HTTPSeeOther("/signup?e=Too+many+attempts,+try+later")
        try:
            cid = provisioning.create_customer(db, str(data.get("email", "")), str(data.get("password", "")),
                                               str(data.get("name", "")))
        except provisioning.ProvisionError as exc:
            raise web.HTTPSeeOther("/signup?e=" + quote_plus(str(exc))) from None
        plan = plans.get(db, slug=str(data.get("plan", ""))) if data.get("plan") else None
        if plan and plan["active"]:
            resp = await start_checkout(request, dict(db.one("SELECT * FROM customers WHERE id = ?", (cid,))), plan)
        else:
            resp = web.HTTPSeeOther("/dashboard")
        sess.issue(resp, f"c{cid}", request)
        raise resp

    async def login(request):
        from web.users import verify_password
        _same_origin(request)
        data = dict(await request.post())
        email = str(data.get("email", "")).strip().lower()
        if limiter.hit("login:" + _client_ip(request)) is not None or limiter.hit("login:" + email) is not None:
            raise web.HTTPSeeOther("/login?e=Too+many+attempts,+try+later")
        row = db.one("SELECT id, password_hash FROM customers WHERE email = ? AND status != 'deleted'", (email,))
        if row is None or not verify_password(str(data.get("password", "")), row["password_hash"]):
            raise web.HTTPSeeOther("/login?e=Wrong+e-mail+or+password")
        resp = web.HTTPSeeOther("/dashboard")
        sess.issue(resp, f"c{row['id']}", request)
        raise resp

    async def logout(request):
        resp = web.HTTPSeeOther("/")
        resp.del_cookie(sess.cookie, path="/")
        raise resp

    async def dashboard(request):
        c = need(request)
        csrf = sess.csrf(request)
        hidden = f"<input type=hidden name=csrf value={csrf}>"
        sub = db.one("SELECT * FROM subscriptions WHERE customer_id = ?", (c["id"],))
        paid_ok = billing.subscription_ok(db, c["id"])
        plan = plans.current(db, c["id"])
        sl = slots.summary(db, c["id"])
        addons = features.active(db, c["id"])
        own_bot = "custom_bot" in addons
        servers = db.q("SELECT s.*, h.process, h.discord_ready, h.guild_name, h.rcon_connected, h.players_online, "
                       "h.bot_user "
                       "FROM servers s LEFT JOIN health h ON h.server_id = s.id WHERE s.customer_id = ? AND "
                       "s.status != 'deleted' ORDER BY s.id", (c["id"],))
        first = dict(servers[0]) if servers else None
        # ---- the set-up steps
        has_server = first is not None
        in_discord = bool(first and ((first["bot_mode"] == "shared" and first["guild_id"]) or
                                     first["bot_mode"] == "custom"))
        rcon = any(s["rcon_connected"] for s in servers)
        order = [paid_ok, has_server, in_discord, rcon]
        now = order.index(False) if False in order else 4

        def state(i):
            return "done" if order[i] else ("now" if i == now else "later")
        pick = shop_ui.plan_cards(plans.listed(db), action=lambda p: (
            f"<form method=post action=/billing/checkout>{hidden}<input type=hidden name=plan value={p['id']}>"
            f"<button class=small>Choose {esc(p['name'])}</button></form>"))
        steps = "".join([
            shop_ui.step(1, state(0), "Choose your package",
                         (f"{esc(plan['name'])} – {esc(plans.price_text(plan))} / month" if plan and paid_ok else
                          "Pick a package – you pay securely with Stripe."), pick),
            shop_ui.step(2, state(1), "Create your server", "Give it a name – you get its own admin website.",
                         f"<form method=post action=/servers class=row>{hidden}<input name=name required "
                         f"placeholder='Server name, e.g. Kryvon EU'><button>Create</button></form>"),
            shop_ui.step(3, state(2), "Add the bot to your Discord",
                         "One click – Discord asks which server, and the bot is locked to it.",
                         (f"<a class=btn href=/servers/{first['id']}/discord>➕ Add the bot to Discord</a>"
                          if first else "")),
            shop_ui.step(4, state(3), "Connect your Rust server",
                         "Open your admin panel → Settings and enter your G-Portal RCON IP, port and password.",
                         (f"<a class=btn href='{esc(provisioning.panel_url(settings, first))}' target=_blank>"
                          f"Open my admin panel</a>" if first else "")),
        ])
        done = now == 4
        rows = "".join(
            f"<tr><td><b>{esc(s['name'])}</b><br><a href='{esc(provisioning.panel_url(settings, s))}' "
            f"target=_blank>Admin panel</a> · <a href='{esc(settings.url_for(s['slug']))}' target=_blank>Website</a>"
            f"</td><td>{pill(s['status'])}"
            f"{'<br><span class=muted>' + esc(s['suspend_reason']) + '</span>' if s['suspend_reason'] else ''}</td>"
            f"<td>{'🟢 ' + esc(s['guild_name'] or 'online') if s['discord_ready'] else '⚪ offline'}"
            f"{' · RCON ✅' if s['rcon_connected'] else ''}"
            f"{' · ' + str(s['players_online']) + ' online' if s['players_online'] else ''}</td>"
            f"<td>{bot_cell(s, csrf, own_bot)}"
            f"<form method=post action=/servers/{s['id']}/remove class=inline onsubmit=\"return confirm('Remove "
            f"this server? Its data is kept for a while.')\">{hidden}<button class='danger small'>Remove</button>"
            f"</form></td></tr>"
            for s in servers)
        add = (f"<form method=post action=/servers class=row>{hidden}<input name=name placeholder='Server name' "
               f"required><button>Add server</button></form>" if sl["free"] and has_server else
               ("" if not has_server else "<p class=muted>All your server slots are in use – a bigger package gives "
                                          "you more.</p>"))
        billing_btn = (f"<form method=post action=/billing/portal class=inline>{hidden}<button class='ghost small'>"
                       f"Invoices &amp; payment</button></form>" if c.get("stripe_customer_id") else "")
        switch = ""
        if paid_ok and sub and sub["stripe_subscription_id"]:
            others = [p for p in plans.listed(db) if not plan or p["id"] != plan["id"]]
            switch = "".join(
                f"<form method=post action=/billing/switch class=inline>{hidden}<input type=hidden name=plan "
                f"value={p['id']}><button class='ghost small'>Switch to {esc(p['name'])} "
                f"({esc(plans.price_text(p))})</button></form> " for p in others)
        package = (f"<b>{esc(plan['name'])}</b> · {esc(plans.price_text(plan))} / month · " if plan else "") + \
            f"{pill(sub['status'] if sub else 'none')}"
        return shop(request, "Dashboard", f"""{flash(request)}
<h1>Hi {esc(c['name'] or c['email'])} 👋</h1>
{'' if done else f"<div class=card><h2>Get started</h2><div class=steps>{steps}</div></div>"}
<div class=card><h2>Your package</h2><p>{package} {billing_btn}</p>
<p class=muted>{sl['used']} of {sl['limit']} slots used · AI features {'✅' if 'ai' in addons else '–'} · own bot
{'✅' if own_bot else '–'}</p>{switch}</div>
<div class=card><h2>Servers <span class=muted>({sl['used']} of {sl['limit']} slots)</span></h2>
<table><tr><th>Server</th><th>Status</th><th>Live</th><th></th></tr>
{rows or '<tr><td colspan=4 class=muted>No server yet – start with the steps above.</td></tr>'}</table>{add}</div>
{''.join(website_card(dict(s), hidden, 'custom_domain' in addons) for s in servers)}""")

    def website_card(s: dict, hidden: str, can_domain: bool) -> str:
        """The server's websites: its address on our domain (changeable), its own domains, the look."""
        sid, base = s["id"], settings.base_domain
        own = provisioning.domains(db, sid)
        rows = "".join(
            f"<tr><td><a href='https://{esc(d['hostname'])}' target=_blank>{esc(d['hostname'])}</a></td><td>"
            f"{pill('active' if d['status'] == 'active' else 'waiting')}"
            f"{'<br><span class=muted>' + esc(d['note']) + '</span>' if d['note'] else ''}</td><td>"
            f"<form method=post action=/servers/{sid}/domains/remove class=inline>{hidden}<input type=hidden "
            f"name=hostname value='{esc(d['hostname'])}'><button class='danger small'>Remove</button></form></td></tr>"
            for d in own)
        if can_domain:
            domain_part = (f"<h3>Your own domain</h3><table>{rows}</table>"
                           f"<form method=post action=/servers/{sid}/domains class=row>{hidden}<input name=hostname "
                           f"placeholder='www.yourserver.com' required><button>Connect domain</button></form>"
                           f"<p class=muted>At your domain's DNS add a <b>CNAME</b> record: <code>www</code> (or the "
                           f"name you entered) → <code>sites.{esc(base)}</code>. The certificate (https) comes by "
                           f"itself – it can take a few minutes after we've connected it.</p>")
        else:
            domain_part = ("<p class=muted>🌐 Want your <b>own domain</b> (e.g. www.yourserver.com)? It's in the "
                           "bigger packages – <a href=/#plans>see the packages</a>.</p>")
        return (f"<div class=card><h2>🌐 {esc(s['name'])} – website</h2>"
                f"<p>Address: <a href='{esc(settings.url_for(s['slug']))}' target=_blank>"
                f"{esc(settings.url_for(s['slug']))}</a></p>"
                f"<form method=post action=/servers/{sid}/address class=row>{hidden}<input name=address "
                f"value='{esc(s['slug'])}' pattern='[a-z0-9-]{{3,30}}' required><span class=muted>.{esc(base)}</span>"
                f"<button class=ghost>Change address</button></form><p class=muted>The old address keeps working.</p>"
                f"{domain_part}<p class=muted>🎨 Look: open your <a href='{esc(provisioning.panel_url(settings, s))}' "
                f"target=_blank>admin panel</a> → Plugins → Website → <b>Look (theme)</b> – logo, colours, texts.</p>"
                f"</div>")

    def bot_cell(s, csrf: str, own_bot: bool) -> str:
        sid = s["id"]
        hidden = f"<input type=hidden name=csrf value={csrf}>"
        if s["bot_mode"] == "custom":
            out = (f"🤖 <b>Own bot</b> {'· ' + esc(s['bot_user']) if s['bot_user'] else ''}<form method=post "
                   f"action=/servers/{sid}/token>{hidden}<input name=token type=password placeholder='New bot token' "
                   f"required> <button>Save token</button></form><form method=post action=/servers/{sid}/shared "
                   f"class=inline>{hidden}<button>Use our bot instead</button></form> ")
        else:
            out = (f"🤖 Our bot – {'connected to Discord ' + esc(s['guild_name'] or s['guild_id']) if s['guild_id'] else 'not in a Discord yet'}"
                   f"<br><a href=/servers/{sid}/discord>{'Move to another Discord' if s['guild_id'] else '➕ Add the bot to your Discord'}</a><br>")
            if own_bot:
                out += (f"<form method=post action=/servers/{sid}/token>{hidden}<input name=token type=password "
                        f"placeholder='Own bot token' required> <button>Use my own bot</button></form> ")
        return out

    def install_state(customer_id: int, server_id: int) -> str:
        exp = int(time.time()) + 900
        return f"{server_id}.{exp}.{sess._sig(f'install:{customer_id}:{server_id}:{exp}')[:40]}"

    async def discord_install(request):
        """"Add the bot to your Discord": Discord's own install page; the answer tells us WHICH server it went to
        (and only someone allowed to add bots there can do it)."""
        c = need(request)
        s = own_server(c, int(request.match_info["sid"]))
        if s["bot_mode"] != "shared":
            raise web.HTTPSeeOther("/dashboard")
        if not settings.shared_client_id:
            raise web.HTTPSeeOther("/dashboard?m=The+shared+bot+isn't+set+up+yet")
        from urllib.parse import urlencode
        query = urlencode({"client_id": settings.shared_client_id, "permissions": settings.shared_permissions,
                           "scope": "bot applications.commands", "response_type": "code",
                           "redirect_uri": f"{settings.portal_url}/discord/callback",
                           "state": install_state(c["id"], s["id"]), "integration_type": "0"})
        raise web.HTTPSeeOther(f"https://discord.com/oauth2/authorize?{query}")

    async def discord_callback(request):
        c = need(request)
        state = request.query.get("state", "").split(".")
        if len(state) != 3 or not state[0].isdigit() or not state[1].isdigit() or int(state[1]) < time.time() or \
                not hmac.compare_digest(state[2], sess._sig(f"install:{c['id']}:{state[0]}:{state[1]}")[:40]):
            raise web.HTTPSeeOther("/dashboard?m=That+link+expired+–+try+again")
        s = own_server(c, int(state[0]))
        try:
            guild = await exchange_code(settings, request.query.get("code", ""))
            provisioning.bind_guild(db, s["id"], guild, actor=f"customer:{c['id']}")
        except (provisioning.ProvisionError, OAuthError) as exc:
            raise web.HTTPSeeOther("/dashboard?m=" + str(exc).replace(" ", "+")) from None
        await changed()
        raise web.HTTPSeeOther("/dashboard?m=Bot+added+–+it+starts+in+a+few+seconds")

    async def use_shared(request):
        c = need(request)
        s = own_server(c, int(request.match_info["sid"]))
        await sess.form(request)
        provisioning.use_shared_bot(db, s["id"], actor=f"customer:{c['id']}")
        await changed()
        raise web.HTTPSeeOther("/dashboard?m=Switched+to+our+bot+–+add+it+to+your+Discord")

    async def add_server(request):
        c = need(request)
        data = await sess.form(request)
        try:
            out = provisioning.provision_server(db, box, settings, c["id"], str(data.get("name", "")),
                                                actor=f"customer:{c['id']}")
        except (slots.SlotError, provisioning.ProvisionError) as exc:
            raise web.HTTPSeeOther("/dashboard?m=" + quote_plus(str(exc))) from None
        await changed()
        return shop(request, "Server created", f"""<div class=card><h2>✅ {esc(data.get('name'))} is ready</h2>
<p><b>Your admin panel</b> (a secret address – bookmark it):<br><a href='{esc(out['panel_url'])}' target=_blank>
{esc(out['panel_url'])}</a></p>
<p><b>Panel password</b> – shown only this once, save it now:<br><code style="font-size:1.15rem">
{esc(out['panel_password'])}</code></p>
<p class=muted>Next: add the bot to your Discord, then enter your Rust server's RCON details in the panel.</p>
<a class=btn href=/servers/{out['id']}/discord>➕ Add the bot to Discord</a>
<a class="btn ghost" href=/dashboard>Back to the dashboard</a></div>""")

    async def set_token(request):
        c = need(request)
        s = own_server(c, int(request.match_info["sid"]))
        data = await sess.form(request)
        try:
            provisioning.set_bot_token(db, box, s["id"], str(data.get("token", "")), actor=f"customer:{c['id']}")
        except provisioning.ProvisionError as exc:
            raise web.HTTPSeeOther("/dashboard?m=" + str(exc).replace(" ", "+")) from None
        await changed()
        raise web.HTTPSeeOther("/dashboard?m=Own+bot+saved+–+it+starts+in+a+few+seconds")

    async def change_address(request):
        c = need(request)
        s = own_server(c, int(request.match_info["sid"]))
        data = await sess.form(request)
        try:
            new = provisioning.change_address(db, settings, s["id"], str(data.get("address", "")),
                                              actor=f"customer:{c['id']}")
        except provisioning.ProvisionError as exc:
            raise web.HTTPSeeOther("/dashboard?m=" + quote_plus(str(exc))) from None
        await changed()                                  # its website gets the new address (restart)
        raise web.HTTPSeeOther("/dashboard?m=" + quote_plus(f"New address: {settings.url_for(new)}"))

    async def add_domain(request):
        from control_plane import cftunnel
        from support.cloudflare import CloudflareError
        c = need(request)
        s = own_server(c, int(request.match_info["sid"]))
        data = await sess.form(request)
        try:
            host = provisioning.add_domain(db, settings, s["id"], str(data.get("hostname", "")),
                                           actor=f"customer:{c['id']}")
        except provisioning.ProvisionError as exc:
            raise web.HTTPSeeOther("/dashboard?m=" + quote_plus(str(exc))) from None
        msg = f"{host} added – now add the CNAME record at your DNS. We connect it shortly."
        if settings.cf_saas_token:                       # the owner chose automatic connecting
            try:
                await cftunnel.request_custom_hostname(db, settings, settings.cf_saas_token, host)
                msg = f"{host} connected – add the CNAME record at your DNS and it's live in a few minutes."
            except CloudflareError as exc:
                db.run("UPDATE domains SET note = ? WHERE hostname = ?", (str(exc)[:300], host))
        raise web.HTTPSeeOther("/dashboard?m=" + quote_plus(msg))

    async def remove_domain(request):
        c = need(request)
        s = own_server(c, int(request.match_info["sid"]))
        data = await sess.form(request)
        provisioning.remove_domain(db, s["id"], str(data.get("hostname", "")), actor=f"customer:{c['id']}")
        raise web.HTTPSeeOther("/dashboard?m=Domain+removed")

    async def remove(request):
        c = need(request)
        s = own_server(c, int(request.match_info["sid"]))
        await sess.form(request)
        provisioning.remove_server(db, s["id"], actor=f"customer:{c['id']}")
        await changed()
        raise web.HTTPSeeOther("/dashboard?m=Server+removed")

    async def checkout(request):
        c = need(request)
        data = await sess.form(request)
        plan = plans.get(db, str(data.get("plan", "0"))) if str(data.get("plan", "")).isdigit() else None
        if plan is None or not plan["active"]:
            raise web.HTTPSeeOther("/#plans")
        raise await start_checkout(request, c, plan)

    async def switch_plan(request):
        """A subscriber moves to another package (Stripe prorates; the webhook applies the new package)."""
        c = need(request)
        data = await sess.form(request)
        plan = plans.get(db, str(data.get("plan", "0"))) if str(data.get("plan", "")).isdigit() else None
        sub = db.one("SELECT stripe_subscription_id FROM subscriptions WHERE customer_id = ?", (c["id"],))
        if plan is None or not plan["active"] or not sub or not sub["stripe_subscription_id"]:
            raise web.HTTPSeeOther("/dashboard")
        try:
            price = await plans.ensure_stripe_price(db, settings, plan["id"])
            if not price:
                raise plans.PlanError("Payments aren't set up yet.")
            await plans.switch_subscription(settings, sub["stripe_subscription_id"], price)
        except plans.PlanError as exc:
            raise web.HTTPSeeOther("/dashboard?m=" + quote_plus(str(exc))) from None
        db.audit(f"customer:{c['id']}", "plan.switch_requested", customer_id=c["id"], detail=plan["slug"])
        raise web.HTTPSeeOther("/dashboard?m=" + quote_plus(f"Switching to {plan['name']} – it's active in a few "
                                                            "seconds."))

    async def billing_portal(request):
        c = need(request)
        await sess.form(request)
        try:
            url = await billing.portal_url(settings, c["stripe_customer_id"],
                                           f"https://portal.{settings.base_domain}/dashboard")
        except billing.BillingError as exc:
            raise web.HTTPSeeOther("/dashboard?m=" + str(exc).replace(" ", "+")) from None
        raise web.HTTPSeeOther(url)

    async def stripe_webhook(request):
        payload = await request.read()
        try:
            event = billing.verify_signature(payload, request.headers.get("Stripe-Signature", ""),
                                             settings.stripe_webhook_secret)
        except billing.BillingError as exc:
            return web.Response(status=400, text=str(exc))
        billing.handle_event(db, event, grace_days=settings.grace_days)
        await changed()
        return web.json_response({"received": True})

    async def paused(request):
        s = db.one("SELECT name FROM servers WHERE slug = ?", (request.match_info["slug"],))
        return shop(request, "Paused", f"<div class=card><h1>⏸️ {esc(s['name'] if s else 'This server')}</h1><p>This "
                                       "server's bot and website are paused right now. The community's admins can "
                                       "reactivate it – everything is kept.</p></div>")

    app.add_routes([web.get("/", index), web.get("/signup", signup_page), web.get("/login", login_page),
                    web.post("/billing/switch", switch_plan),
                    web.post(r"/servers/{sid:\d+}/address", change_address),
                    web.post(r"/servers/{sid:\d+}/domains", add_domain),
                    web.post(r"/servers/{sid:\d+}/domains/remove", remove_domain),
                    web.post("/signup", signup), web.post("/login", login),
                    web.post("/logout", logout), web.get("/dashboard", dashboard), web.post("/servers", add_server),
                    web.post(r"/servers/{sid:\d+}/token", set_token), web.post(r"/servers/{sid:\d+}/remove", remove),
                    web.post("/billing/checkout", checkout), web.post("/billing/portal", billing_portal),
                    web.post("/stripe/webhook", stripe_webhook), web.get("/paused/{slug}", paused),
                    web.get(r"/servers/{sid:\d+}/discord", discord_install), web.get("/discord/callback", discord_callback),
                    web.post(r"/servers/{sid:\d+}/shared", use_shared)])
    return app


# ================================================================== OWNER (only you)

def build_owner(db: DB, box: SecretBox, settings: Settings, changed: Callable[[], Awaitable[None]],
                supervisor=None, *, prefix: str = "", allow_ips: frozenset[str] = frozenset(),
                ai_history: dict | None = None, restart: Callable[[], None] | None = None) -> web.Application:
    """The owner panel. prefix="" on its private listener; prefix="/management" when it's also mounted on the
    public portal (a sub-app there) – then allow_ips (if set) limits who even sees the login page."""
    P = prefix.rstrip("/")

    def page(title: str, body: str) -> web.Response:      # the owner panel's own look (owner_ui.py)
        return owner_ui.owner_frame(title, body, prefix=P, shop=settings.shop_name)

    @web.middleware
    async def guard(request, handler):
        if allow_ips and _client_ip(request) not in allow_ips:
            raise web.HTTPNotFound()                     # don't even show that a panel exists here
        resp = await handler(request)
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        return resp

    app = web.Application(client_max_size=64 * 1024, middlewares=[guard])
    sess = Sessions(settings.session_secret, "owner", "cp_o")
    plans.seed(db)
    limiter = RateLimiter(5, 900)
    user_limiter = RateLimiter(10, 900)                  # per username too (IP headers can be spoofed)

    def go(path: str) -> web.HTTPSeeOther:
        return web.HTTPSeeOther(P + path)

    def owner(request) -> str:
        sub = sess.subject(request)
        if not sub or not sub.startswith("o:") or db.one("SELECT 1 FROM owners WHERE username = ?", (sub[2:],)) is None:
            raise go("/login")
        return sub[2:]

    def hidden(request) -> str:
        return f"<input type=hidden name=csrf value={sess.csrf(request)}>"

    def button(request, action: str, label: str, *, danger: bool = False, confirm: str = "") -> str:
        js = f" onsubmit=\"return confirm('{esc(confirm)}')\"" if confirm else ""
        return (f"<form method=post action='{esc(P + action)}' class=inline{js}>{hidden(request)}"
                f"<button{' class=danger' if danger else ''}>{esc(label)}</button></form>")

    async def login_page(request):
        msg = esc(request.query.get("e", ""))
        return page("Owner login", f"""<h1>Owner panel</h1>{f'<p class=err>{msg}</p>' if msg else ''}<div class=card>
<form method=post action={P}/login><input name=username placeholder=Username required> <input name=password
type=password placeholder=Password required> <input name=code inputmode=numeric placeholder="2FA code" required>
<button>Sign in</button></form><p class=muted>No login yet, or locked out? On the server run
<code>python3 -m control_plane login-link</code> – it prints a one-time link.</p></div>""")

    async def login(request):
        from web.totp import matching_step
        from web.users import verify_password
        _same_origin(request)
        data = dict(await request.post())
        ip = _client_ip(request)
        if limiter.hit("ip:" + ip) is not None or user_limiter.hit("user:" + str(data.get("username", ""))[:64]) is not None:
            raise go("/login?e=Too+many+attempts")
        row = db.one("SELECT * FROM owners WHERE username = ?", (str(data.get("username", "")),))
        if row is None or not verify_password(str(data.get("password", "")), row["password_hash"]) \
                or matching_step(row["totp_secret"], str(data.get("code", ""))) is None:
            db.audit("system", "owner.login_failed", detail=ip)
            raise go("/login?e=Wrong+login")
        db.audit(f"owner:{row['username']}", "owner.login", detail=ip)
        resp = go("/")
        sess.issue(resp, f"o:{row['username']}", request, path=P or "/")
        raise resp

    async def logout(request):
        resp = go("/login")
        resp.del_cookie(sess.cookie, path=P or "/")
        raise resp

    def server_rows(request, where: str = "", args: tuple = ()) -> str:
        rows = db.q("SELECT s.*, c.email, h.process, h.restarts, h.discord_ready, h.bot_user, h.guild_id, h.guild_name, "
                    "h.rcon_connected, h.players_online, h.latency_ms, h.setup_mode, h.error, h.checked_at "
                    f"FROM servers s JOIN customers c ON c.id = s.customer_id LEFT JOIN health h ON h.server_id = s.id "
                    f"{where} ORDER BY s.id", args)
        out = []
        for s in rows:
            sid = s["id"]
            acts = []
            if s["status"] == "deleted":
                acts.append(button(request, f"/servers/{sid}/purge", "Purge", danger=True,
                                   confirm="Shred the keys and delete all data of this server for good?"))
            else:
                if s["status"] in ("suspended", "stopped"):
                    acts.append(button(request, f"/servers/{sid}/reactivate", "Reactivate"))
                elif s["status"] == "active":
                    acts.append(button(request, f"/servers/{sid}/suspend", "Suspend"))
                    acts.append(button(request, f"/servers/{sid}/restart", "Restart"))
                acts.append(f"<a href={P}/servers/{sid}/ai>🧠 Management AI</a>")
                acts.append(button(request, f"/servers/{sid}/manager-ai/{'off' if s['manager_ai'] else 'on'}",
                                   ("Discord AI: on → switch off" if s["manager_ai"] else "Discord AI: off → switch on")))
                acts.append(button(request, f"/servers/{sid}/remove", "Remove", danger=True,
                                   confirm="Remove this server? (kept for restore until purged)"))
                if s["bot_mode"] != "shared" or not s["guild_id"]:
                    acts.append(f"<form method=post action='{P}/servers/{sid}/shared-bot' class=inline>"
                                f"{hidden(request)}<input name=guild_id inputmode=numeric size=14 placeholder='"
                                f"{esc(provisioning.tenant_guild(dict(s))) or 'Discord server id'}'>"
                                f"<button>Use the shared bot</button></form>")
                acts.append(f"<form method=post action='{P}/servers/{sid}/token' class=inline>{hidden(request)}"
                            f"<input name=token type=password autocomplete=off placeholder='"
                            f"{'Bot token needed!' if s['status'] == 'awaiting_token' else 'New bot token'}' size=14>"
                            f"<button>Set bot token</button></form>")
            live = (f"{'🟢' if s['discord_ready'] else '🔴'} {esc(s['bot_user'] or '')}<br>"
                    f"<span class=muted>Discord: {esc(s['guild_name'] or '–')} {esc(s['guild_id'] or '')}</span><br>"
                    f"RCON {'✅' if s['rcon_connected'] else '❌'} · {s['players_online'] or 0} online"
                    f"{' · setup mode' if s['setup_mode'] else ''}") if s["checked_at"] else "–"
            out.append(f"<tr><td>#{sid} <b>{esc(s['name'])}</b><br><a href='{esc(settings.url_for(s['slug']))}' "
                       f"target=_blank>{esc(s['slug'])}</a> · port {s['web_port']}<br><span class=muted>"
                       f"{'own bot' if s['bot_mode'] == 'custom' else 'shared bot · guild ' + esc(s['guild_id'] or '–')}"
                       f"</span></td>"
                       f"<td><a href={P}/customers/{s['customer_id']}>{esc(s['email'])}</a></td>"
                       f"<td>{pill(s['status'])}<br>{pill(s['process'] or 'not started')}"
                       f"{' ↻' + str(s['restarts']) if s['restarts'] else ''}"
                       f"{'<br><span class=err>' + esc(s['error'])[:120] + '</span>' if s['error'] else ''}</td>"
                       f"<td>{live}</td><td>{' '.join(acts)}</td></tr>")
        return ("<table><tr><th>Server</th><th>Customer</th><th>State</th><th>Health</th><th></th></tr>"
                + ("".join(out) or "<tr><td colspan=5>None.</td></tr>") + "</table>")

    def update_card(request) -> str:
        st = updates.status(db)
        cur, latest, last = st.get("current") or {}, st.get("latest") or {}, st.get("last_install") or {}
        lines = [f"Installed: <code>{esc((cur.get('sha') or '?')[:10])}</code> {esc(cur.get('title') or '')}"]
        if latest.get("sha"):
            lines.append(f"Newest: <code>{esc(latest['sha'][:10])}</code> {esc(latest.get('title') or '')} "
                         + (pill("update available") if st.get("available") else pill("active")))
        if last:
            lines.append(f"Last install: {esc(last.get('status'))} {esc(last.get('message') or '')} – "
                         f"{last.get('restarted', 0)} server(s) restarted")
        if st.get("control_plane_restart_needed"):
            lines.append("<span class=err>The control plane still runs the old code – press <b>Restart control "
                         "plane</b>.</span>")
        again = (" " + button(request, '/update/restart', 'Restart control plane (new code)', danger=bool(
            st.get("control_plane_restart_needed")), confirm="Restart the control plane? Every server restarts too "
                                                              "(back within seconds).")) if restart else ""
        return (f"<div class=card><h2>Updates (only here – tenants can't update)</h2>{'<br>'.join(lines)}<br><br>"
                f"{button(request, '/update/check', 'Check for updates')} "
                f"{button(request, '/update/install', 'Install update for ALL servers', danger=True, confirm='Install the newest version and restart every server one by one?')}"
                f"{again}</div>")

    def customer_rows(customers) -> str:
        return "".join(
            f"<tr><td><a href={P}/customers/{c['id']}>#{c['id']} {esc(c['email'])}</a><br><span class=muted>"
            f"{esc(c['name'])}</span></td><td>{pill(c['status'])}</td><td>{pill(c['sub_status'] or 'none')}"
            f"{'<br><span class=muted>' + esc(c['plan_name']) + '</span>' if c['plan_name'] else ''}</td>"
            f"<td>{slots.slots_used(db, c['id'])} / {slots.slot_limit(db, c['id'])}</td></tr>" for c in customers)

    def all_customers():
        return db.q("SELECT c.*, s.status AS sub_status, s.grace_until, p.name AS plan_name FROM customers c "
                    "LEFT JOIN subscriptions s ON s.customer_id = c.id LEFT JOIN plans p ON p.id = s.plan_id "
                    "WHERE c.status != 'deleted' ORDER BY c.id")

    async def dashboard(request):
        who = owner(request)
        customers = all_customers()
        counts = db.one("SELECT COUNT(*) AS n, SUM(status = 'active') AS a FROM servers WHERE status != 'deleted'")
        running = sum(1 for p in (supervisor.procs.values() if supervisor else []) if p.state == "running")
        live = db.one("SELECT SUM(h.discord_ready) AS d, SUM(h.rcon_connected) AS r, SUM(h.players_online) AS p "
                      "FROM health h JOIN servers s ON s.id = h.server_id WHERE s.status = 'active'")
        mrr = db.q("SELECT p.price_cents, p.currency FROM subscriptions s JOIN plans p ON p.id = s.plan_id "
                   "WHERE s.status IN ('active', 'trialing')")
        totals: dict[str, int] = {}
        for r in mrr:
            totals[r["currency"]] = totals.get(r["currency"], 0) + r["price_cents"]
        money = " + ".join(f"{plans.CURRENCIES.get(cur, cur)}{cents / 100:.2f}" for cur, cents in totals.items()) or "–"
        problems = db.q("SELECT s.id, s.name, h.error, h.process FROM servers s LEFT JOIN health h ON h.server_id = s.id "
                        "WHERE s.status = 'active' AND (h.error IS NOT NULL OR h.discord_ready = 0 OR "
                        "h.process IN ('crashed', 'crashloop'))")
        attention = "".join(f"<tr><td>#{p['id']} {esc(p['name'])}</td><td>{pill(p['process'] or 'not started')}</td>"
                            f"<td class=err>{esc((p['error'] or 'Discord offline')[:160])}</td></tr>" for p in problems)
        paying = sum(1 for c in customers if c["sub_status"] in ("active", "trialing"))
        return page("Owner", f"""<div class=kicker>LIVE PLATFORM OPERATIONS</div><h1>Owner panel · Overview <span class=muted
style="font-size:1rem">· {esc(who)}</span></h1>
<div class=stats>{owner_ui.stat("Customers", len(customers), f"{paying} paying")}
{owner_ui.stat("Servers", f"{counts['a'] or 0} / {counts['n'] or 0}", f"{running} processes running")}
{owner_ui.stat("Bots online", live['d'] or 0, f"RCON {live['r'] or 0}")}
{owner_ui.stat("Players now", live['p'] or 0)}
{owner_ui.stat("Monthly revenue", money, "active subscriptions")}
{owner_ui.stat("Need attention", len(problems))}</div>
{f"<div class=card><h2>⚠️ Need attention</h2><table><tr><th>Server</th><th>Process</th><th>Problem</th></tr>{attention}</table></div>" if problems else ""}
{update_card(request)}
{f"<div class=card><h2>🤖 Shared bot</h2><p>One bot for every server. Add it to a Discord: <a href='{esc(provisioning.shared_invite(settings))}' target=_blank>invite link</a>. Then press <b>Use the shared bot</b> on that server's row (its Discord server id is filled in when known).</p></div>" if settings.shared_client_id else ""}
<div class=card><h2>Newest customers</h2><table><tr><th>Customer</th><th>Account</th><th>Package</th><th>Slots</th></tr>
{customer_rows(list(customers)[-5:][::-1]) or '<tr><td colspan=4>None yet.</td></tr>'}</table>
<a href={P}/customers>All customers →</a></div>
<div class=card><h2>All servers</h2>{server_rows(request, "WHERE s.status != 'deleted'")}</div>""")

    async def customers_page(request):
        owner(request)
        return page("Customers", f"""<div class=kicker>CUSTOMERS</div><h1>Customers</h1><div class=card><table>
<tr><th>Customer</th><th>Account</th><th>Package</th><th>Slots (used / limit)</th></tr>
{customer_rows(all_customers()) or '<tr><td colspan=4>None yet.</td></tr>'}</table></div>""")

    async def servers_page(request):
        owner(request)
        return page("Servers", f"""<div class=kicker>CUSTOMERS</div><h1>Servers</h1>
<div class=card><h2>All servers</h2>{server_rows(request, "WHERE s.status != 'deleted'")}</div>
<div class=card><h2>Removed (restorable until purged)</h2>{server_rows(request, "WHERE s.status = 'deleted'")}</div>""")

    async def system_page(request):
        owner(request)
        info = host_stats(supervisor, db)
        rows = "".join(f"<tr><td>#{p['id']} {esc(p['name'])}</td><td>{esc(p['pid'] or '–')}</td>"
                       f"<td>{esc(p['mem'])}</td><td>{pill(p['state'])}</td></tr>" for p in info["procs"])
        return page("System", f"""<div class=kicker>CONTROL</div><h1>System</h1>
<p class=muted>The VPS every server runs on (what used to be each server's System page – it belongs here).</p>
<div class=stats>{owner_ui.stat("CPU load", info['load'], f"{info['cores']} cores")}
{owner_ui.stat("Memory", info['mem'], info['mem_sub'])}{owner_ui.stat("Disk", info['disk'], info['disk_sub'])}
{owner_ui.stat("Up for", info['uptime'])}</div>
<div class=card><h2>Server processes</h2><table><tr><th>Server</th><th>Process ID</th><th>Memory</th><th>State</th></tr>
{rows or '<tr><td colspan=4>No processes.</td></tr>'}</table></div>""")

    async def customer(request):
        owner(request)
        cid = int(request.match_info["cid"])
        c = db.one("SELECT * FROM customers WHERE id = ?", (cid,))
        if c is None:
            raise web.HTTPNotFound()
        sub = db.one("SELECT * FROM subscriptions WHERE customer_id = ?", (cid,))
        sl = slots.summary(db, cid)
        grants = db.q("SELECT * FROM slot_grants WHERE customer_id = ? ORDER BY id DESC", (cid,))
        grows = "".join(
            f"<tr><td>#{g['id']}</td><td>+{g['slots']}</td><td>{esc(g['reason'])}</td><td>{esc(g['granted_by'])} "
            f"{time.strftime('%Y-%m-%d', time.gmtime(g['granted_at']))}</td><td>"
            + (f"revoked by {esc(g['revoked_by'])}" if g["revoked_at"] else
               button(request, f"/grants/{g['id']}/revoke", "Revoke", danger=True, confirm="Revoke these slots?"))
            + "</td></tr>" for g in grants)
        on = features.active(db, cid)
        addon_rows = "".join(
            f"<p>{esc(label)}: {pill('active' if f in on else 'off')} "
            + (button(request, f"/customers/{cid}/feature/{f}/off", "Switch off", danger=True,
                      confirm=f"Switch {label} off for this customer?") if f in on
               else button(request, f"/customers/{cid}/feature/{f}/on", "Switch on")) + "</p>"
            for f, label in features.FEATURES.items())
        acct = (button(request, f"/customers/{cid}/reactivate", "Reactivate account") if c["status"] == "suspended"
                else button(request, f"/customers/{cid}/suspend", "Suspend account", danger=True,
                            confirm="Stop all servers of this customer?"))
        comp = button(request, f"/customers/{cid}/comp", "Comp (free, no Stripe)" if (sub and sub["status"]) != "comped"
                      else "Remove comp")
        return page(f"Customer {cid}", f"""<p><a href={P}/>← all</a></p><h1>#{cid} {esc(c['email'])}</h1>
<div class=card>Account {pill(c['status'])} {esc(c['suspend_reason'] or '')} · Subscription
{pill(sub['status'] if sub else 'none')} {esc(sub['stripe_subscription_id'] or '') if sub else ''}
{('· grace until ' + time.strftime('%Y-%m-%d %H:%M', time.gmtime(sub['grace_until']))) if sub and sub['grace_until'] else ''}
<br><br>{acct} {comp} {button(request, f'/customers/{cid}/remove', 'Remove customer', danger=True,
                                       confirm='Remove the customer and all their servers?')}</div>
<div class=card><h2>Package</h2><p>{esc((plans.current(db, cid) or {}).get('name') or 'none')}
<span class=muted>– sets the server slots and the add-ons below (paid packages come from Stripe by themselves)</span></p>
<form method=post action={P}/customers/{cid}/plan>{hidden(request)}<select name=plan>{''.join(f"<option value={p['id']}>{esc(p['name'])} – {esc(plans.price_text(p))} ({p['slots']} servers)</option>" for p in plans.listed(db, all_=True))}</select>
<label><input type=checkbox name=comp value=1 checked> free (comped – no Stripe)</label> <button>Set package</button></form></div>
<div class=card><h2>Add-ons</h2>{addon_rows}</div>
<div class=card><h2>Slots: {sl['used']} used of {sl['limit']} ({c['base_slots']} in the plan)</h2>
<form method=post action={P}/customers/{cid}/grant>{hidden(request)}<input name=slots type=number min=1 max=100 value=1>
<input name=reason placeholder="Reason (e.g. paid add-on)"> <button>Unlock slots</button></form>
<table><tr><th>Grant</th><th>Slots</th><th>Reason</th><th>Granted</th><th></th></tr>{grows or '<tr><td colspan=5>No extra slots.</td></tr>'}</table></div>
<div class=card><h2>Servers</h2>{server_rows(request, "WHERE s.customer_id = ?", (cid,))}</div>""")

    async def act(request):
        who = owner(request)
        data = await sess.form(request)
        actor = f"owner:{who}"
        path = request.path[len(P):]
        try:
            if path.startswith("/customers/"):
                cid = int(request.match_info["id"])
                action = request.match_info["action"]
                actions.customer(db, cid, action, actor=actor, owner=who, slots_n=int(data.get("slots") or 1),
                                 reason=str(data.get("reason", "")))
                back = f"/customers/{cid}" if action != "remove" else "/"
            elif path.startswith("/grants/"):
                gid = int(request.match_info["id"])
                cid = db.one("SELECT customer_id FROM slot_grants WHERE id = ?", (gid,))["customer_id"]
                slots.revoke(db, gid, owner=who)
                back = f"/customers/{cid}"
            else:
                await actions.server(db, settings, supervisor, int(request.match_info["id"]),
                                     request.match_info["action"], actor=actor)
                back = "/"
        except (slots.SlotError, provisioning.ProvisionError, TypeError, ValueError) as exc:
            return page("Not possible", f"<div class=card><p class=err>{esc(exc)}</p><a href={P}/>Back</a></div>")
        await changed()
        raise go(back)

    def plan_form(p: dict | None, request) -> str:
        p = p or {"name": "", "tagline": "", "price_cents": 999, "currency": "eur", "slots": 1, "ai": 0,
                  "custom_bot": 0, "custom_domain": 0, "perks": "", "highlight": 0, "sort": 0, "active": 1}
        action = f"{P}/plans/{p['id']}" if p.get("id") else f"{P}/plans"
        cur = "".join(f"<option value={k}{' selected' if p['currency'] == k else ''}>{k.upper()}</option>"
                      for k in plans.CURRENCIES)
        def chk(name, label):
            return f"<label><input type=checkbox name={name} value=1{' checked' if p[name] else ''}> {label}</label> "
        return (f"<form method=post action={action}>{hidden(request)}<input name=name value='{esc(p['name'])}' "
                f"placeholder=Name required> <input name=price value='{p['price_cents'] / 100:.2f}' size=7> "
                f"<select name=currency>{cur}</select> / month · <input name=slots type=number min=1 max=100 "
                f"value={p['slots']} style='width:4rem'> servers · order <input name=sort type=number "
                f"value={p['sort']} style='width:4rem'><br><input name=tagline value='{esc(p['tagline'])}' "
                f"placeholder='Short line under the name' style='width:100%'><br><textarea name=perks rows=4 "
                f"style='width:100%' placeholder='One perk per line'>{esc(p['perks'])}</textarea><br>"
                f"{chk('ai', 'AI features')}{chk('custom_bot', 'own bot')}{chk('custom_domain', 'own domain')}"
                f"{chk('highlight', 'most popular')}"
                f"{chk('active', 'for sale')}<button>{'Save' if p.get('id') else 'Create package'}</button></form>")

    async def domains_page(request):
        owner(request)
        msg = esc(request.query.get("m", ""))
        rows = db.q("SELECT d.*, s.name, s.slug, c.email FROM domains d JOIN servers s ON s.id = d.server_id "
                    "JOIN customers c ON c.id = s.customer_id ORDER BY d.status = 'pending' DESC, d.created_at DESC")
        table = "".join(f"<tr><td><a href='https://{esc(r['hostname'])}' target=_blank>{esc(r['hostname'])}</a></td>"
                        f"<td>#{r['server_id']} {esc(r['name'])}<br><span class=muted>{esc(r['email'])}</span></td>"
                        f"<td>{pill(r['status'])}</td><td class=muted>{esc(r['note'])}"
                        f"{' – remove it in Cloudflare (SSL/TLS → Custom Hostnames)' if r['status'] == 'removed' and r['cf_id'] else ''}"
                        f"</td></tr>" for r in rows)
        waiting = sum(1 for r in rows if r["status"] == "pending")
        return page("Domains", f"""<p><a href={P}/>← owner panel</a></p><h1>🌐 Customers' own domains</h1>
{f'<p class=muted>{msg}</p>' if msg else ''}<p class=muted>Customers with the <b>own domain</b> add-on point a CNAME
at <code>sites.{esc(settings.base_domain)}</code>. Cloudflare then has to accept the domain (Cloudflare for SaaS,
SSL/TLS → Custom Hostnames – enable it once; the first 100 are free).
{'Automatic: a token for this is saved in Settings.' if settings.cf_saas_token else 'Connect waiting domains here with a Cloudflare token that has <b>Zone › SSL and Certificates › Edit</b> – it is used once and not saved.'}</p>
<div class=card><form method=post action={P}/domains/activate>{hidden(request)}<input name=token type=password
autocomplete=off placeholder="Cloudflare API token (not saved)" style="width:60%"> <button>Connect {waiting} waiting
domain(s)</button></form></div><table><tr><th>Domain</th><th>Server</th><th>State</th><th></th></tr>
{table or '<tr><td colspan=4>No own domains yet.</td></tr>'}</table>""")

    async def domains_activate(request):
        from control_plane import cftunnel
        from support.cloudflare import CloudflareError
        who = owner(request)
        data = await sess.form(request)
        token = str(data.get("token", "")).strip() or settings.cf_saas_token
        try:
            results = await cftunnel.activate_pending(db, settings, token)
        except CloudflareError as exc:
            raise go("/domains?m=" + quote_plus(str(exc))) from None
        db.audit(f"owner:{who}", "domains.activate", detail=", ".join(f"{h}: {v}" for h, v in results.items())[:500])
        raise go("/domains?m=" + quote_plus("; ".join(f"{h}: {v}" for h, v in results.items()) or "Nothing waiting."))

    async def plans_page(request):
        owner(request)
        msg = esc(request.query.get("m", ""))
        cards = "".join(
            f"<div class=card><h2>{esc(p['name'])} {pill('active' if p['active'] else 'off')} "
            f"<span class=muted>{esc(plans.price_text(p))} / month · Stripe: "
            f"{esc(p['stripe_price_id'] or 'created at the first sale')}</span></h2>{plan_form(p, request)}</div>"
            for p in plans.listed(db, all_=True))
        return page("Packages", f"""<p><a href={P}/>← owner panel</a> · <a href='{esc(settings.portal_url)}/#plans'
target=_blank>see the shop</a></p><h1>🛒 Packages</h1>{f'<p class=muted>{msg}</p>' if msg else ''}
<p class=muted>What customers can buy. A package decides how many servers they get and the AI / own-bot add-ons.
With Stripe set up (Settings), its monthly price is created in Stripe when you save – nothing to copy. Changing a
price makes a new Stripe price; current subscribers keep theirs. Packages are never deleted, only switched off.</p>
{cards}<div class=card><h2>New package</h2>{plan_form(None, request)}</div>""")

    async def plan_save(request):
        who = owner(request)
        data = await sess.form(request)
        try:
            pid = plans.save(db, data, actor=f"owner:{who}",
                             plan_id=int(request.match_info["id"]) if "id" in request.match_info else None)
            note = "Saved."
            if settings.stripe_secret_key:
                await plans.ensure_stripe_price(db, settings, pid)
                note = "Saved – Stripe price ready."
        except plans.PlanError as exc:
            return page("Not saved", f"<div class=card><p class=err>{esc(exc)}</p><a href={P}/plans>Back</a></div>")
        raise go("/plans?m=" + quote_plus(note))

    async def customer_plan(request):
        who = owner(request)
        data = await sess.form(request)
        cid = int(request.match_info["id"])
        try:
            plans.apply(db, cid, int(data.get("plan") or 0), actor=f"owner:{who}")
            if data.get("comp"):
                db.run("UPDATE subscriptions SET status = 'comped', updated_at = ? WHERE customer_id = ?",
                       (int(time.time()), cid))
                db.audit(f"owner:{who}", "subscription.comped", customer_id=cid)
        except (plans.PlanError, ValueError) as exc:
            return page("Not possible", f"<div class=card><p class=err>{esc(exc)}</p><a href={P}/>Back</a></div>")
        await changed()
        raise go(f"/customers/{cid}")

    async def update_action(request):
        who = owner(request)
        await sess.form(request)
        if request.match_info["action"] == "check":
            await updates.check(db, settings)
        elif request.match_info["action"] == "restart":
            if restart is None:
                raise go("/")
            db.audit(f"owner:{who}", "control_plane.restart")
            asyncio.get_running_loop().call_later(1.0, restart)       # after this page has been sent
            return page("Restarting", f"""<meta http-equiv=refresh content="20;url={P}/"><div class=card>
<h1>♻️ Restarting the control plane …</h1><p>Every server stops and starts again with the new code. This page
reloads by itself in 20 seconds (sign in again if it asks).</p><p><a href={P}/>Back to the owner panel</a></p></div>""")
        else:
            await updates.install(db, settings, supervisor, owner=who)
        raise go("/")

    async def feature_action(request):
        who = owner(request)
        await sess.form(request)
        cid, feature = int(request.match_info["id"]), request.match_info["feature"]
        try:
            if request.match_info["on"] == "on":
                features.grant(db, cid, feature, owner=who)
            else:
                features.revoke(db, cid, feature, owner=who)
        except features.FeatureError as exc:
            return page("Not possible", f"<div class=card><p class=err>{esc(exc)}</p><a href={P}/>Back</a></div>")
        await changed()                                  # running servers restart with the new setup
        raise go(f"/customers/{cid}")

    ai_history = {} if ai_history is None else ai_history      # shared by the private and the public mount

    async def ai_page(request):
        owner(request)
        sid = int(request.match_info["id"])
        srv = db.one("SELECT s.*, c.email FROM servers s JOIN customers c ON c.id = s.customer_id WHERE s.id = ?", (sid,))
        if srv is None:
            raise web.HTTPNotFound()
        hist = ai_history.get(sid, [])
        chat = "".join(
            f"<div class=card><b>{'You' if h['role'] == 'user' else '🧠 AI'}</b><br>"
            f"{esc(h['content']).replace(chr(10), '<br>')}"
            + (f"<br><span class=muted>{esc(' · '.join(h.get('done') or []))}</span>" if h.get("done") else "")
            + (f"<br><span class=muted>{esc(' · '.join(h.get('asked') or []))}</span>" if h.get("asked") else "")
            + "</div>" for h in hist[-20:])
        return page(f"Management AI – {srv['name']}", f"""<p><a href={P}/>← all</a></p>
<h1>🧠 Management AI – {esc(srv['name'])}</h1><p class=muted>Runs on this server's own bot with all its tools (kits,
emote binds, teleports, ZORP, custom kits, its admin website …). Customer: {esc(srv['email'])} · in their Discord:
{'on' if srv['manager_ai'] else 'off (only you, here)'}</p>{chat or '<p class=muted>Ask something, e.g. "list the emote binds".</p>'}
<div class=card><form method=post action={P}/servers/{sid}/ai>{hidden(request)}
<textarea name=message rows=3 style="width:100%" required placeholder="What should it do on this server?"></textarea>
<label><input type=checkbox name=allow_risky value=1> allow risky actions for this message (console commands,
deleting, a kit for everyone …)</label><br><br><button>Send</button></form>
{button(request, f'/servers/{sid}/ai/clear', 'Clear the conversation')}</div>""")

    async def ai_send(request):
        owner(request)
        data = await sess.form(request)
        sid = int(request.match_info["id"])
        text = str(data.get("message", "")).strip()[:3000]
        if not text:
            raise go(f"/servers/{sid}/ai")
        hist = ai_history.setdefault(sid, [])
        out = await manage_ai.ask(db, box, sid, text, [{"role": h["role"], "content": h["content"]} for h in hist],
                                  allow_risky=data.get("allow_risky") == "1")
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": out.get("answer") or f"⚠️ {out.get('error', 'no answer')}",
                     "done": out.get("done") or [], "asked": out.get("asked") or []})
        del hist[:-40]
        db.audit(f"owner:{owner(request)}", "manager_ai.request", server_id=sid,
                 detail=f"{text[:200]} → {', '.join(out.get('done') or []) or 'no changes'}")
        raise go(f"/servers/{sid}/ai")

    async def ai_clear(request):
        owner(request)
        await sess.form(request)
        ai_history.pop(int(request.match_info["id"]), None)
        raise go(f"/servers/{request.match_info['id']}/ai")

    async def set_token(request):
        who = owner(request)
        data = await sess.form(request)
        sid = int(request.match_info["id"])
        row = db.one("SELECT customer_id FROM servers WHERE id = ?", (sid,))
        try:
            if row is None:
                raise provisioning.ProvisionError("No such server.")
            if not features.has(db, row["customer_id"], "custom_bot"):      # the owner may always set one
                features.grant(db, row["customer_id"], "custom_bot", owner=who)
            provisioning.set_bot_token(db, box, sid, str(data.get("token", "")), actor=f"owner:{who}")
        except (provisioning.ProvisionError, features.FeatureError) as exc:
            return page("Not possible", f"<div class=card><p class=err>{esc(exc)}</p><a href={P}/>Back</a></div>")
        await changed()                                  # the server (re)starts on its own bot
        raise go("/")

    async def shared_bot(request):
        who = owner(request)
        data = await sess.form(request)
        try:
            provisioning.move_to_shared_bot(db, settings, int(request.match_info["id"]),
                                            str(data.get("guild_id", "")), actor=f"owner:{who}")
        except provisioning.ProvisionError as exc:
            return page("Not possible", f"<div class=card><p class=err>{esc(exc)}</p><a href={P}/>Back</a></div>")
        await changed()                                  # it restarts on the shared bot, fenced to that Discord
        raise go("/")

    async def manager_toggle(request):
        who = owner(request)
        await sess.form(request)
        try:
            provisioning.set_manager_ai(db, int(request.match_info["id"]), request.match_info["on"] == "on",
                                        actor=f"owner:{who}")
        except provisioning.ProvisionError as exc:
            return page("Not possible", f"<div class=card><p class=err>{esc(exc)}</p><a href={P}/>Back</a></div>")
        await changed()                                  # the server restarts with it on/off
        raise go("/")

    once_limiter = RateLimiter(10, 900)

    async def once_page(request):
        token = request.match_info["token"]
        if not websettings.use_login_link(db, token, consume=False):
            return page("Link used", "<div class=card><h1>This link doesn't work (any more)</h1><p>A one-time link "
                                     "works once and for 15 minutes. Make a new one on the server: <code>python3 -m "
                                     "control_plane login-link</code></p></div>")
        first = db.one("SELECT 1 FROM owners") is None
        fields_ = ("<p>No owner account exists yet – create yours (you'll get a 2FA key for your authenticator app):"
                   "</p><input name=username placeholder=Username required pattern='[A-Za-z0-9_.-]{3,32}'> "
                   "<input name=password type=password placeholder='Password (16+ characters)' required minlength=16>"
                   "<br><br>") if first else ""
        return page("One-time login", f"""<h1>Owner panel – one-time login</h1><div class=card>
<form method=post action={P}/once/{esc(token)}>{fields_}<button>{'Create account and sign in' if first else 'Sign in'}
</button></form><p class=muted>The link stops working after this.</p></div>""")

    async def once_use(request):
        from web.totp import new_secret, otpauth_uri
        from web.users import hash_password
        _same_origin(request)
        ip = _client_ip(request)
        if once_limiter.hit("once:" + ip) is not None:
            raise web.HTTPTooManyRequests(text="Too many tries – wait a few minutes.")
        data = dict(await request.post())
        first = db.one("SELECT 1 FROM owners") is None
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        if first and (not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username) or len(password) < 16):
            return page("Not possible", "<div class=card><p class=err>Username: 3–32 letters/digits; password: 16+ "
                                        f"characters.</p><a href={P}/once/{esc(request.match_info['token'])}>Back</a></div>")
        if not websettings.use_login_link(db, request.match_info["token"]):
            db.audit("system", "owner.login_link_failed", detail=ip)
            raise go("/login?e=Link+expired+or+used")
        extra = ""
        if first:
            totp = new_secret()
            db.run("INSERT INTO owners (username, password_hash, totp_secret) VALUES (?, ?, ?)",
                   (username, hash_password(password), totp))
            db.audit(f"owner:{username}", "owner.create", detail=f"one-time link from {ip}")
            extra = (f"<div class=card><h2>Your 2FA key – add it now</h2><p>Authenticator app → add account → enter "
                     f"key manually (time-based, 6 digits):</p><p><code style='font-size:1.2rem'>{esc(totp)}</code></p>"
                     f"<p class=muted>Or open: <code>{esc(otpauth_uri(totp, username, 'RCE SaaS owner'))}</code></p>"
                     "<p>You need it with your password for every normal login.</p></div>")
        else:
            username = db.one("SELECT username FROM owners ORDER BY rowid LIMIT 1")["username"]
        db.audit(f"owner:{username}", "owner.login", detail=f"one-time link from {ip}")
        resp = page("Signed in", f"<h1>Signed in as {esc(username)}</h1>{extra}<p><a href={P}/settings>⚙️ Settings"
                                 f"</a> · <a href={P}/>Owner panel</a></p>")
        sess.issue(resp, f"o:{username}", request, path=P or "/")
        return resp

    def settings_form(request, msg: str = "") -> web.Response:
        insecure = request.headers.get("X-Forwarded-Proto", request.scheme) != "https" and request.host.split(":")[0] \
            not in ("127.0.0.1", "localhost")
        groups: dict[str, list[str]] = {}
        for f in websettings.FIELDS:
            cur = websettings.shown(f.key)
            if f.secret:
                inp = (f"<input name={f.key} type=password autocomplete=off style='width:100%' "
                       f"placeholder='{esc(cur)} – leave empty to keep'>"
                       + (f"<label><input type=checkbox name=clear_{f.key} value=1> remove</label>"
                          if cur != "not set" else ""))
            else:
                inp = f"<input name={f.key} value='{esc(cur)}' style='width:100%'>"
            groups.setdefault(f.group, []).append(
                f"<p><b>{esc(f.label)}</b>{' <span class="pill warn">restart</span>' if f.restart else ''}<br>{inp}"
                + (f"<br><span class=muted>{esc(f.help)}</span>" if f.help else "") + "</p>")
        body = "".join(f"<div class=card><h2>{esc(g)}</h2>{''.join(items)}</div>" for g, items in groups.items())
        warn = ("<p class=err>⚠️ This page isn't on HTTPS – keys you type travel unencrypted. Prefer the "
                "https://portal.&lt;domain&gt;/management address once it works.</p>") if insecure else ""
        return page("Settings", f"""<p><a href={P}/>← owner panel</a></p><h1>⚙️ Settings</h1>{warn}
{f'<p class=muted>{msg}</p>' if msg else ''}<p class=muted>Saved encrypted in the control plane's database; they win over
/etc/rce-saas.env. The master key and the session secret stay in that file only.</p>
<form method=post action={P}/settings>{hidden(request)}{body}<button>Save</button></form>
<div class=card><h2>☁️ Set up Cloudflare (every website through one tunnel)</h2><p class=muted>Paste a Cloudflare API
token with <b>Account › Cloudflare Tunnel › Edit</b>, <b>Zone › DNS › Edit</b> and <b>Zone › Zone › Read</b>. It's used
once for the setup and <b>not stored</b>. Creates the tunnel, the record *.{esc(settings.base_domain)} and moves the old
addresses of your adopted servers. No Caddy and no open ports needed.</p>
<form method=post action={P}/cloudflare>{hidden(request)}<input name=api_token type=password autocomplete=off
style="width:100%" placeholder="Cloudflare API token" required><br><br><button>Set up Cloudflare</button></form></div>""")

    async def settings_page(request):
        owner(request)
        return settings_form(request)

    async def settings_save(request):
        who = owner(request)
        data = await sess.form(request)
        try:
            changed_keys = websettings.save(db, box, data, actor=f"owner:{who}")
        except websettings.SettingsError as exc:
            return page("Not saved", f"<div class=card><p class=err>{esc(exc)}</p><a href={P}/settings>Back</a></div>")
        websettings.refresh(settings, list(websettings.saved(db, box)))
        await changed()                                  # servers pick up e.g. a new AI key (restart by themselves)
        restart = [k for k in changed_keys if websettings.BY_KEY[k].restart]
        msg = ("✅ Saved: " + ", ".join(changed_keys)) if changed_keys else "Nothing changed."
        if restart:
            msg += (" – restart the control plane for " + ", ".join(restart) +
                    " (systemctl restart rce-saas, or stop and start `run`).")
        return settings_form(request, esc(msg))

    async def cloudflare_setup(request):
        from control_plane import cftunnel
        from support.cloudflare import CloudflareError
        who = owner(request)
        data = await sess.form(request)
        try:
            out = await cftunnel.setup(db, box, settings, str(data.get("api_token", "")))
        except CloudflareError as exc:
            return page("Not done", f"<div class=card><p class=err>{esc(exc)}</p><a href={P}/settings>Back</a></div>")
        db.audit(f"owner:{who}", "cloudflare.setup.web")
        rows = "".join(f"<li><code>{esc(h)}</code>: {esc(v)}</li>" for h, v in out["dns"].items())
        return page("Cloudflare", f"""<p><a href={P}/settings>← settings</a></p><h1>☁️ Cloudflare is set up</h1>
<div class=card><p>Tunnel {esc(out['tunnel'])} for <b>{esc(out['domain'])}</b>. DNS:</p><ul>{rows}</ul>
<p>The API token wasn't saved. <b>Restart the control plane</b> (<code>systemctl restart rce-saas</code>) – it then
starts the tunnel and every website is online at its address.</p></div>""")

    async def audit(request):
        owner(request)
        rows = db.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT 300")
        body = "".join(f"<tr><td>{time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['at']))}</td><td>{esc(r['actor'])}</td>"
                       f"<td>{esc(r['action'])}</td><td>{esc(r['customer_id'])}</td><td>{esc(r['server_id'])}</td>"
                       f"<td>{esc(r['detail'])}</td></tr>" for r in rows)
        return page("Audit", f"<p><a href={P}/>← back</a></p><h1>Audit log</h1><table><tr><th>When (UTC)</th><th>Who</th>"
                             f"<th>What</th><th>Customer</th><th>Server</th><th>Detail</th></tr>{body}</table>")

    app.add_routes([web.get("/login", login_page), web.post("/login", login), web.post("/logout", logout),
                    web.get("/once/{token}", once_page), web.post("/once/{token}", once_use),
                    web.get("/settings", settings_page), web.post("/settings", settings_save),
                    web.post("/cloudflare", cloudflare_setup),
                    web.get("/plans", plans_page), web.post("/plans", plan_save),
                    web.get("/domains", domains_page), web.post("/domains/activate", domains_activate),
                    web.post(r"/plans/{id:\d+}", plan_save), web.post(r"/customers/{id:\d+}/plan", customer_plan),
                    web.get("/", dashboard), web.get(r"/customers/{cid:\d+}", customer), web.get("/audit", audit),
                    web.get("/customers", customers_page), web.get("/servers", servers_page),
                    web.get("/system", system_page),
                    web.post(r"/customers/{id:\d+}/{action:grant|suspend|reactivate|comp|remove}", act),
                    web.post(r"/grants/{id:\d+}/revoke", act),
                    web.post(r"/servers/{id:\d+}/{action:suspend|reactivate|restart|remove|purge}", act),
                    web.post(r"/customers/{id:\d+}/feature/{feature:custom_bot|ai|custom_domain}/{on:on|off}", feature_action),
                    web.post("/update/{action:check|install|restart}", update_action),
                    web.get(r"/servers/{id:\d+}/ai", ai_page), web.post(r"/servers/{id:\d+}/ai", ai_send),
                    web.post(r"/servers/{id:\d+}/ai/clear", ai_clear),
                    web.post(r"/servers/{id:\d+}/manager-ai/{on:on|off}", manager_toggle),
                    web.post(r"/servers/{id:\d+}/token", set_token),
                    web.post(r"/servers/{id:\d+}/shared-bot", shared_bot)])
    return app


def host_stats(supervisor, db: DB) -> dict:
    """CPU load, memory, disk and uptime of the VPS plus each server process' memory (Linux /proc; best effort)."""
    import os
    import shutil

    def fmt(n: float) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"
    out = {"cores": os.cpu_count() or "?", "load": "–", "mem": "–", "mem_sub": "", "disk": "–", "disk_sub": "",
           "uptime": "–", "procs": []}
    try:
        out["load"] = " / ".join(f"{x:.2f}" for x in os.getloadavg())
    except (OSError, AttributeError):
        pass
    try:
        info = {k.strip(): int(v.split()[0]) * 1024 for k, v in
                (line.split(":", 1) for line in open("/proc/meminfo", encoding="utf-8") if ":" in line)}
        used = info["MemTotal"] - info.get("MemAvailable", info.get("MemFree", 0))
        out["mem"] = f"{100 * used / info['MemTotal']:.0f}%"
        out["mem_sub"] = f"{fmt(used)} of {fmt(info['MemTotal'])}"
    except (OSError, KeyError, ValueError):
        pass
    try:
        du = shutil.disk_usage("/")
        out["disk"], out["disk_sub"] = f"{100 * du.used / du.total:.0f}%", f"{fmt(du.free)} free"
    except OSError:
        pass
    try:
        secs = float(open("/proc/uptime", encoding="utf-8").read().split()[0])
        out["uptime"] = f"{int(secs // 86400)}d {int(secs % 86400 // 3600)}h"
    except (OSError, ValueError, IndexError):
        pass
    names = {r["id"]: r["name"] for r in db.q("SELECT id, name FROM servers")}
    for sid, p in sorted((supervisor.procs.items() if supervisor else [])):
        pid = getattr(getattr(p, "process", None), "pid", None) or getattr(p, "pid", None)
        mem = "–"
        try:
            for line in open(f"/proc/{pid}/status", encoding="utf-8"):
                if line.startswith("VmRSS:"):
                    mem = fmt(int(line.split()[1]) * 1024)
        except (OSError, TypeError, ValueError):
            pass
        out["procs"].append({"id": sid, "name": names.get(sid, "?"), "pid": pid, "mem": mem,
                             "state": getattr(p, "state", "?")})
    return out


def mount_owner(portal: web.Application, owner_app: web.Application, prefix: str) -> None:
    """Puts the owner panel on the public portal under prefix (e.g. /management). Its own cookie (path = prefix),
    password + 2FA login, CSRF and rate limits stay the same; customers' portal sessions don't give access."""
    async def to_slash(request):
        raise web.HTTPSeeOther(prefix + "/")
    portal.router.add_get(prefix, to_slash)
    portal.add_subapp(prefix, owner_app)
