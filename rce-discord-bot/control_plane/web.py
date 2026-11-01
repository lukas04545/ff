"""Two separate web apps on two separate listeners:

* PORTAL (public, behind Caddy): customers sign up, subscribe (Stripe), add servers (only while a slot is free),
  set each server's bot token, open each server's own panel/website, remove servers. Plus the Stripe webhook and the
  "paused" page for suspended sites. There is NO route, form field or parameter here that changes slots.
* OWNER (bind it to 127.0.0.1 / a VPN address only – it doesn't exist on the public listener): every customer,
  subscription, slot, server and health in one place; grant/revoke slots; suspend, reactivate, restart, remove,
  purge. Login: owner password + TOTP code.

Both: signed session cookies (HttpOnly, SameSite=Strict, Secure behind HTTPS), CSRF token on every form, ownership
checks on every customer action (a customer can only ever touch rows with their own customer_id).
"""
from __future__ import annotations

import hashlib
import hmac
import html
import secrets
import time
from typing import Awaitable, Callable

from aiohttp import web

from control_plane import billing, provisioning, slots
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

    def issue(self, resp: web.StreamResponse, subject: str, request: web.Request) -> None:
        msg = f"{subject}.{int(time.time()) + SESSION_TTL}.{secrets.token_hex(8)}"
        resp.set_cookie(self.cookie, f"{msg}.{self._sig(msg)}", httponly=True, samesite="Strict", max_age=SESSION_TTL,
                        secure=request.headers.get("X-Forwarded-Proto") == "https" or request.secure, path="/")

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


# ================================================================== PORTAL (customers)

def build_portal(db: DB, box: SecretBox, settings: Settings, changed: Callable[[], Awaitable[None]]) -> web.Application:
    app = web.Application(client_max_size=256 * 1024)
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
        servers = db.q("SELECT s.*, h.process, h.discord_ready, h.guild_name, h.rcon_connected, h.players_online "
                       "FROM servers s LEFT JOIN health h ON h.server_id = s.id WHERE s.customer_id = ? AND "
                       "s.status != 'deleted' ORDER BY s.id", (c["id"],))
        flash = request.query.get("m", "")
        rows = "".join(
            f"<tr><td><b>{esc(s['name'])}</b><br><a href='{esc(settings.url_for(s['slug']))}' target=_blank>"
            f"{esc(settings.url_for(s['slug']))}</a></td><td>{pill(s['status'])}"
            f"{'<br><span class=muted>' + esc(s['suspend_reason']) + '</span>' if s['suspend_reason'] else ''}</td>"
            f"<td>{'🟢 ' + esc(s['guild_name'] or 'online') if s['discord_ready'] else '⚪'} "
            f"{'· RCON ✅' if s['rcon_connected'] else ''} {'· ' + str(s['players_online']) + ' online' if s['players_online'] else ''}</td>"
            f"<td><form method=post action=/servers/{s['id']}/token><input type=hidden name=csrf value={csrf}>"
            f"<input name=token type=password placeholder='Bot token' required> <button>Save token</button></form>"
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
<form method=post action=/logout class=inline><input type=hidden name=csrf value={csrf}><button>Sign out</button>
</form></div>
<div class=card><h2>Servers <span class=muted>({sl['used']} of {sl['limit']} slots)</span></h2>
<table><tr><th>Server</th><th>Status</th><th>Live</th><th></th></tr>{rows or '<tr><td colspan=4>No server yet.</td></tr>'}
</table>{add}<p class=muted>Each server is fully separate: its own bot (create one per server in the Discord
Developer Portal), website, database, kits and settings. Open the server's link to log in to its panel.</p></div>""")

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
<p>Panel: <a href='{esc(out['url'])}/admin'>{esc(out['url'])}/admin</a></p>
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
        raise web.HTTPSeeOther("/dashboard?m=Token+saved+–+the+bot+starts+in+a+few+seconds")

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
                    web.post("/stripe/webhook", stripe_webhook), web.get("/paused/{slug}", paused)])
    return app


# ================================================================== OWNER (only you)

def build_owner(db: DB, box: SecretBox, settings: Settings, changed: Callable[[], Awaitable[None]],
                supervisor=None) -> web.Application:
    app = web.Application(client_max_size=64 * 1024)
    sess = Sessions(settings.session_secret, "owner", "cp_o")
    limiter = RateLimiter(5, 900)

    def owner(request) -> str:
        sub = sess.subject(request)
        if not sub or not sub.startswith("o:") or db.one("SELECT 1 FROM owners WHERE username = ?", (sub[2:],)) is None:
            raise web.HTTPSeeOther("/login")
        return sub[2:]

    def hidden(request) -> str:
        return f"<input type=hidden name=csrf value={sess.csrf(request)}>"

    def button(request, action: str, label: str, *, danger: bool = False, confirm: str = "") -> str:
        js = f" onsubmit=\"return confirm('{esc(confirm)}')\"" if confirm else ""
        return (f"<form method=post action='{esc(action)}' class=inline{js}>{hidden(request)}"
                f"<button{' class=danger' if danger else ''}>{esc(label)}</button></form>")

    async def login_page(request):
        msg = esc(request.query.get("e", ""))
        return page("Owner login", f"""<h1>Owner panel</h1>{f'<p class=err>{msg}</p>' if msg else ''}<div class=card>
<form method=post action=/login><input name=username placeholder=Username required> <input name=password
type=password placeholder=Password required> <input name=code inputmode=numeric placeholder="2FA code" required>
<button>Sign in</button></form></div>""")

    async def login(request):
        from web.totp import matching_step
        from web.users import verify_password
        _same_origin(request)
        data = dict(await request.post())
        ip = _client_ip(request)
        if limiter.hit("ip:" + ip) is not None:
            raise web.HTTPSeeOther("/login?e=Too+many+attempts")
        row = db.one("SELECT * FROM owners WHERE username = ?", (str(data.get("username", "")),))
        if row is None or not verify_password(str(data.get("password", "")), row["password_hash"]) \
                or matching_step(row["totp_secret"], str(data.get("code", ""))) is None:
            db.audit("system", "owner.login_failed", detail=ip)
            raise web.HTTPSeeOther("/login?e=Wrong+login")
        db.audit(f"owner:{row['username']}", "owner.login", detail=ip)
        resp = web.HTTPSeeOther("/")
        sess.issue(resp, f"o:{row['username']}", request)
        raise resp

    async def logout(request):
        resp = web.HTTPSeeOther("/login")
        resp.del_cookie(sess.cookie, path="/")
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
                acts.append(button(request, f"/servers/{sid}/remove", "Remove", danger=True,
                                   confirm="Remove this server? (kept for restore until purged)"))
            live = (f"{'🟢' if s['discord_ready'] else '🔴'} {esc(s['bot_user'] or '')}<br>"
                    f"<span class=muted>Discord: {esc(s['guild_name'] or '–')} {esc(s['guild_id'] or '')}</span><br>"
                    f"RCON {'✅' if s['rcon_connected'] else '❌'} · {s['players_online'] or 0} online"
                    f"{' · setup mode' if s['setup_mode'] else ''}") if s["checked_at"] else "–"
            out.append(f"<tr><td>#{sid} <b>{esc(s['name'])}</b><br><a href='{esc(settings.url_for(s['slug']))}' "
                       f"target=_blank>{esc(s['slug'])}</a> · port {s['web_port']}</td>"
                       f"<td><a href=/customers/{s['customer_id']}>{esc(s['email'])}</a></td>"
                       f"<td>{pill(s['status'])}<br>{pill(s['process'] or 'not started')}"
                       f"{' ↻' + str(s['restarts']) if s['restarts'] else ''}"
                       f"{'<br><span class=err>' + esc(s['error'])[:120] + '</span>' if s['error'] else ''}</td>"
                       f"<td>{live}</td><td>{' '.join(acts)}</td></tr>")
        return ("<table><tr><th>Server</th><th>Customer</th><th>State</th><th>Health</th><th></th></tr>"
                + ("".join(out) or "<tr><td colspan=5>None.</td></tr>") + "</table>")

    async def dashboard(request):
        who = owner(request)
        customers = db.q("SELECT c.*, s.status AS sub_status, s.grace_until FROM customers c LEFT JOIN subscriptions s "
                         "ON s.customer_id = c.id WHERE c.status != 'deleted' ORDER BY c.id")
        crows = "".join(
            f"<tr><td><a href=/customers/{c['id']}>#{c['id']} {esc(c['email'])}</a><br><span class=muted>"
            f"{esc(c['name'])}</span></td><td>{pill(c['status'])}</td><td>{pill(c['sub_status'] or 'none')}</td>"
            f"<td>{slots.slots_used(db, c['id'])} / {slots.slot_limit(db, c['id'])}</td></tr>" for c in customers)
        counts = db.one("SELECT COUNT(*) AS n, SUM(status = 'active') AS a FROM servers WHERE status != 'deleted'")
        running = sum(1 for p in (supervisor.procs.values() if supervisor else []) if p.state == "running")
        return page("Owner", f"""<h1>Owner panel <span class=muted>({esc(who)})</span></h1>
<p>{len(customers)} customers · {counts['n'] or 0} servers ({counts['a'] or 0} active, {running} processes running)
· <a href=/audit>audit log</a> · {button(request, '/logout', 'Sign out')}</p>
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
        acct = (button(request, f"/customers/{cid}/reactivate", "Reactivate account") if c["status"] == "suspended"
                else button(request, f"/customers/{cid}/suspend", "Suspend account", danger=True,
                            confirm="Stop all servers of this customer?"))
        comp = button(request, f"/customers/{cid}/comp", "Comp (free, no Stripe)" if (sub and sub["status"]) != "comped"
                      else "Remove comp")
        return page(f"Customer {cid}", f"""<p><a href=/>← all</a></p><h1>#{cid} {esc(c['email'])}</h1>
<div class=card>Account {pill(c['status'])} {esc(c['suspend_reason'] or '')} · Subscription
{pill(sub['status'] if sub else 'none')} {esc(sub['stripe_subscription_id'] or '') if sub else ''}
{('· grace until ' + time.strftime('%Y-%m-%d %H:%M', time.gmtime(sub['grace_until']))) if sub and sub['grace_until'] else ''}
<br><br>{acct} {comp} {button(request, f'/customers/{cid}/remove', 'Remove customer', danger=True,
                                       confirm='Remove the customer and all their servers?')}</div>
<div class=card><h2>Slots: {sl['used']} used of {sl['limit']} ({c['base_slots']} in the plan)</h2>
<form method=post action=/customers/{cid}/grant>{hidden(request)}<input name=slots type=number min=1 max=100 value=1>
<input name=reason placeholder="Reason (e.g. paid add-on)"> <button>Unlock slots</button></form>
<table><tr><th>Grant</th><th>Slots</th><th>Reason</th><th>Granted</th><th></th></tr>{grows or '<tr><td colspan=5>No extra slots.</td></tr>'}</table></div>
<div class=card><h2>Servers</h2>{server_rows(request, "WHERE s.customer_id = ?", (cid,))}</div>""")

    async def act(request):
        who = owner(request)
        data = await sess.form(request)
        actor = f"owner:{who}"
        path = request.path
        try:
            if path.startswith("/customers/"):
                cid = int(request.match_info["id"])
                action = request.match_info["action"]
                if action == "grant":
                    slots.grant(db, cid, int(data.get("slots") or 1), owner=who, reason=str(data.get("reason", "")))
                    slots.release_over_limit(db, cid, actor=actor)
                elif action in ("suspend", "reactivate"):
                    provisioning.set_customer_status(db, cid, "suspended" if action == "suspend" else "active",
                                                     actor=actor, reason="by the owner" if action == "suspend" else None)
                elif action == "comp":
                    row = db.one("SELECT status FROM subscriptions WHERE customer_id = ?", (cid,))
                    new = "none" if row and row["status"] == "comped" else "comped"
                    db.run("UPDATE subscriptions SET status = ?, updated_at = ? WHERE customer_id = ?",
                           (new, int(time.time()), cid))
                    db.audit(actor, f"subscription.{new}", customer_id=cid)
                elif action == "remove":
                    for s in db.q("SELECT id FROM servers WHERE customer_id = ? AND status != 'deleted'", (cid,)):
                        provisioning.remove_server(db, s["id"], actor=actor)
                    db.run("UPDATE customers SET status = 'deleted' WHERE id = ?", (cid,))
                    db.audit(actor, "customer.remove", customer_id=cid)
                back = f"/customers/{cid}" if action != "remove" else "/"
            elif path.startswith("/grants/"):
                gid = int(request.match_info["id"])
                cid = db.one("SELECT customer_id FROM slot_grants WHERE id = ?", (gid,))["customer_id"]
                slots.revoke(db, gid, owner=who)
                back = f"/customers/{cid}"
            else:
                sid = int(request.match_info["id"])
                action = request.match_info["action"]
                if action == "suspend":
                    provisioning.set_server_status(db, sid, "suspended", actor=actor, reason="by the owner")
                elif action == "reactivate":
                    provisioning.set_server_status(db, sid, "active", actor=actor)
                elif action == "restart" and supervisor is not None:
                    await supervisor.restart(sid)
                elif action == "remove":
                    provisioning.remove_server(db, sid, actor=actor)
                elif action == "purge":
                    provisioning.purge_server(db, settings, sid, actor=actor)
                back = "/"
        except (slots.SlotError, provisioning.ProvisionError, TypeError, ValueError) as exc:
            return page("Not possible", f"<div class=card><p class=err>{esc(exc)}</p><a href=/>Back</a></div>")
        await changed()
        raise web.HTTPSeeOther(back)

    async def audit(request):
        owner(request)
        rows = db.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT 300")
        body = "".join(f"<tr><td>{time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['at']))}</td><td>{esc(r['actor'])}</td>"
                       f"<td>{esc(r['action'])}</td><td>{esc(r['customer_id'])}</td><td>{esc(r['server_id'])}</td>"
                       f"<td>{esc(r['detail'])}</td></tr>" for r in rows)
        return page("Audit", f"<p><a href=/>← back</a></p><h1>Audit log</h1><table><tr><th>When (UTC)</th><th>Who</th>"
                             f"<th>What</th><th>Customer</th><th>Server</th><th>Detail</th></tr>{body}</table>")

    app.add_routes([web.get("/login", login_page), web.post("/login", login), web.post("/logout", logout),
                    web.get("/", dashboard), web.get(r"/customers/{cid:\d+}", customer), web.get("/audit", audit),
                    web.post(r"/customers/{id:\d+}/{action:grant|suspend|reactivate|comp|remove}", act),
                    web.post(r"/grants/{id:\d+}/revoke", act),
                    web.post(r"/servers/{id:\d+}/{action:suspend|reactivate|restart|remove|purge}", act)])
    return app
