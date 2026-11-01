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

import hashlib
import hmac
import html
import re
import secrets
import time
from typing import Awaitable, Callable

from aiohttp import web

from control_plane import actions, billing, hostproxy, features, manage_ai, provisioning, slots, updates, websettings
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

    async def index(request):
        if me(request):
            raise web.HTTPSeeOther("/dashboard")
        msg = esc(request.query.get("e", ""))
        return page("Rust bot – sign in", f"""<h1>Rust community bot</h1>{f'<p class=err>{msg}</p>' if msg else ''}
<div class=card><h2>Sign in</h2><form method=post action=/login><input name=email type=email placeholder=E-mail
required> <input name=password type=password placeholder=Password required> <button>Sign in</button></form></div>
<div class=card><h2>Create an account</h2><form method=post action=/signup><input name=name placeholder="Community
name"> <input name=email type=email placeholder=E-mail required> <input name=password type=password minlength=10
placeholder="Password (10+)" required> <button>Sign up</button></form></div>""")

    async def signup(request):
        _same_origin(request)
        data = dict(await request.post())
        if limiter.hit("signup:" + _client_ip(request)) is not None:
            raise web.HTTPSeeOther("/?e=Too+many+attempts,+try+later")
        try:
            cid = provisioning.create_customer(db, str(data.get("email", "")), str(data.get("password", "")),
                                               str(data.get("name", "")))
        except provisioning.ProvisionError as exc:
            raise web.HTTPSeeOther("/?e=" + str(exc).replace(" ", "+")) from None
        resp = web.HTTPSeeOther("/dashboard")
        sess.issue(resp, f"c{cid}", request)
        raise resp

    async def login(request):
        from web.users import verify_password
        _same_origin(request)
        data = dict(await request.post())
        email = str(data.get("email", "")).strip().lower()
        if limiter.hit("login:" + _client_ip(request)) is not None or limiter.hit("login:" + email) is not None:
            raise web.HTTPSeeOther("/?e=Too+many+attempts,+try+later")
        row = db.one("SELECT id, password_hash FROM customers WHERE email = ? AND status != 'deleted'", (email,))
        if row is None or not verify_password(str(data.get("password", "")), row["password_hash"]):
            raise web.HTTPSeeOther("/?e=Wrong+e-mail+or+password")
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
        sub = db.one("SELECT * FROM subscriptions WHERE customer_id = ?", (c["id"],))
        sl = slots.summary(db, c["id"])
        addons = features.active(db, c["id"])
        own_bot = "custom_bot" in addons
        servers = db.q("SELECT s.*, h.process, h.discord_ready, h.guild_name, h.rcon_connected, h.players_online, "
                       "h.bot_user "
                       "FROM servers s LEFT JOIN health h ON h.server_id = s.id WHERE s.customer_id = ? AND "
                       "s.status != 'deleted' ORDER BY s.id", (c["id"],))
        flash = request.query.get("m", "")
        rows = "".join(
            f"<tr><td><b>{esc(s['name'])}</b><br><a href='{esc(settings.url_for(s['slug']))}' target=_blank>"
            f"{esc(settings.url_for(s['slug']))}</a><br><a href='{esc(provisioning.panel_url(settings, s))}' "
            f"target=_blank>Admin panel</a></td><td>{pill(s['status'])}"
            f"{'<br><span class=muted>' + esc(s['suspend_reason']) + '</span>' if s['suspend_reason'] else ''}</td>"
            f"<td>{'🟢 ' + esc(s['guild_name'] or 'online') if s['discord_ready'] else '⚪'} "
            f"{'· RCON ✅' if s['rcon_connected'] else ''} {'· ' + str(s['players_online']) + ' online' if s['players_online'] else ''}</td>"
            f"<td>{bot_cell(s, csrf, own_bot)}"
            f"<form method=post action=/servers/{s['id']}/remove class=inline onsubmit=\"return confirm('Remove?')\">"
            f"<input type=hidden name=csrf value={csrf}><button class=danger>Remove</button></form></td></tr>"
            for s in servers)
        billing_btn = (f"<form method=post action=/billing/portal class=inline><input type=hidden name=csrf "
                       f"value={csrf}><button>Manage billing</button></form>" if c.get("stripe_customer_id") else
                       f"<form method=post action=/billing/checkout class=inline><input type=hidden name=csrf "
                       f"value={csrf}><button>Subscribe</button></form>")
        add = (f"<form method=post action=/servers><input type=hidden name=csrf value={csrf}><input name=name "
               f"placeholder='Server name' required> <button>Add server</button></form>" if sl["free"] else
               "<p class=muted>All slots in use. Extra server slots are an add-on – contact us to unlock one.</p>")
        return page("Dashboard", f"""<h1>{esc(c['name'] or c['email'])}</h1>
{f'<div class=card>{esc(flash)}</div>' if flash else ''}
<div class=card><b>Subscription:</b> {pill(sub['status'] if sub else 'none')} {billing_btn}
<br><span class=muted>Add-ons: own bot {'✅' if own_bot else '– (contact us)'} · AI features
{'✅' if 'ai' in addons else '– (contact us)'} · management AI in Discord: on the servers we switched it on for
({sum(1 for x in servers if x['manager_ai'])})</span>
<form method=post action=/logout class=inline><input type=hidden name=csrf value={csrf}><button>Sign out</button>
</form></div>
<div class=card><h2>Servers <span class=muted>({sl['used']} of {sl['limit']} slots)</span></h2>
<table><tr><th>Server</th><th>Status</th><th>Live</th><th></th></tr>{rows or '<tr><td colspan=4>No server yet.</td></tr>'}
</table>{add}<p class=muted>Each server is fully separate: its own bot (create one per server in the Discord
Developer Portal), website, database, kits and settings. Open the server's link to log in to its panel.</p></div>""")

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
            return page("Not possible", f"<div class=card><p class=err>{esc(exc)}</p><a href=/dashboard>Back</a></div>")
        await changed()
        return page("Server created", f"""<div class=card><h2>✅ {esc(data.get('name'))} created</h2>
<p>Panel (secret address – bookmark it; /admin is a trap for intruders): <a href='{esc(out['panel_url'])}'>{esc(out['panel_url'])}</a></p>
<p>Panel password (shown ONCE – save it): <code>{esc(out['panel_password'])}</code></p>
<p>Next: create a bot in the Discord Developer Portal, invite it to your Discord and paste its token on the
dashboard.</p><a href=/dashboard>Back to the dashboard</a></div>""")

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

    async def remove(request):
        c = need(request)
        s = own_server(c, int(request.match_info["sid"]))
        await sess.form(request)
        provisioning.remove_server(db, s["id"], actor=f"customer:{c['id']}")
        await changed()
        raise web.HTTPSeeOther("/dashboard?m=Server+removed")

    async def checkout(request):
        c = need(request)
        await sess.form(request)
        base = f"https://portal.{settings.base_domain}"
        try:
            url = await billing.checkout_url(settings, c, f"{base}/dashboard?m=Thanks!", f"{base}/dashboard")
        except billing.BillingError as exc:
            raise web.HTTPSeeOther("/dashboard?m=" + str(exc).replace(" ", "+")) from None
        raise web.HTTPSeeOther(url)

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
        return page("Paused", f"<div class=card><h1>⏸️ {esc(s['name'] if s else 'This server')}</h1><p>This "
                              "server's bot and website are paused right now. The community's admins can reactivate "
                              "it – everything is kept.</p></div>")

    app.add_routes([web.get("/", index), web.post("/signup", signup), web.post("/login", login),
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
                ai_history: dict | None = None) -> web.Application:
    """The owner panel. prefix="" on its private listener; prefix="/management" when it's also mounted on the
    public portal (a sub-app there) – then allow_ips (if set) limits who even sees the login page."""
    P = prefix.rstrip("/")

    @web.middleware
    async def guard(request, handler):
        if allow_ips and _client_ip(request) not in allow_ips:
            raise web.HTTPNotFound()                     # don't even show that a panel exists here
        resp = await handler(request)
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        return resp

    app = web.Application(client_max_size=64 * 1024, middlewares=[guard])
    sess = Sessions(settings.session_secret, "owner", "cp_o")
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
            lines.append("<span class=err>Restart the control plane service so it runs the new code too.</span>")
        return (f"<div class=card><h2>Updates (only here – tenants can't update)</h2>{'<br>'.join(lines)}<br><br>"
                f"{button(request, '/update/check', 'Check for updates')} "
                f"{button(request, '/update/install', 'Install update for ALL servers', danger=True, confirm='Install the newest version and restart every server one by one?')}</div>")

    async def dashboard(request):
        who = owner(request)
        customers = db.q("SELECT c.*, s.status AS sub_status, s.grace_until FROM customers c LEFT JOIN subscriptions s "
                         "ON s.customer_id = c.id WHERE c.status != 'deleted' ORDER BY c.id")
        crows = "".join(
            f"<tr><td><a href={P}/customers/{c['id']}>#{c['id']} {esc(c['email'])}</a><br><span class=muted>"
            f"{esc(c['name'])}</span></td><td>{pill(c['status'])}</td><td>{pill(c['sub_status'] or 'none')}</td>"
            f"<td>{slots.slots_used(db, c['id'])} / {slots.slot_limit(db, c['id'])}</td></tr>" for c in customers)
        counts = db.one("SELECT COUNT(*) AS n, SUM(status = 'active') AS a FROM servers WHERE status != 'deleted'")
        running = sum(1 for p in (supervisor.procs.values() if supervisor else []) if p.state == "running")
        return page("Owner", f"""<h1>Owner panel <span class=muted>({esc(who)})</span></h1>
<p>{len(customers)} customers · {counts['n'] or 0} servers ({counts['a'] or 0} active, {running} processes running)
· <a href={P}/settings>⚙️ settings</a> · <a href={P}/audit>audit log</a> · {button(request, '/logout', 'Sign out')}</p>
{update_card(request)}
<div class=card><h2>Customers</h2><table><tr><th>Customer</th><th>Account</th><th>Subscription</th><th>Slots
(used / limit)</th></tr>{crows or '<tr><td colspan=4>None yet.</td></tr>'}</table></div>
<div class=card><h2>All servers</h2>{server_rows(request, "WHERE s.status != 'deleted'")}</div>
<div class=card><h2>Removed (restorable until purged)</h2>{server_rows(request, "WHERE s.status = 'deleted'")}</div>""")

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

    async def update_action(request):
        who = owner(request)
        await sess.form(request)
        if request.match_info["action"] == "check":
            await updates.check(db, settings)
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
                    web.get("/", dashboard), web.get(r"/customers/{cid:\d+}", customer), web.get("/audit", audit),
                    web.post(r"/customers/{id:\d+}/{action:grant|suspend|reactivate|comp|remove}", act),
                    web.post(r"/grants/{id:\d+}/revoke", act),
                    web.post(r"/servers/{id:\d+}/{action:suspend|reactivate|restart|remove|purge}", act),
                    web.post(r"/customers/{id:\d+}/feature/{feature:custom_bot|ai}/{on:on|off}", feature_action),
                    web.post("/update/{action:check|install}", update_action),
                    web.get(r"/servers/{id:\d+}/ai", ai_page), web.post(r"/servers/{id:\d+}/ai", ai_send),
                    web.post(r"/servers/{id:\d+}/ai/clear", ai_clear),
                    web.post(r"/servers/{id:\d+}/manager-ai/{on:on|off}", manager_toggle),
                    web.post(r"/servers/{id:\d+}/token", set_token)])
    return app


def mount_owner(portal: web.Application, owner_app: web.Application, prefix: str) -> None:
    """Puts the owner panel on the public portal under prefix (e.g. /management). Its own cookie (path = prefix),
    password + 2FA login, CSRF and rate limits stay the same; customers' portal sessions don't give access."""
    async def to_slash(request):
        raise web.HTTPSeeOther(prefix + "/")
    portal.router.add_get(prefix, to_slash)
    portal.add_subapp(prefix, owner_app)
