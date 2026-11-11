"""JSON API for the Android app "RCE Panel" (android/app) – native screens instead of web pages.

Customers (on the portal):                      Managers (on the owner panel, same IP allow-list as the panel):
  POST /app/api/login    {email, password}        POST /app/api/login    {username, password, code}
  GET  /app/api/overview                          GET  /app/api/overview   (numbers, every customer and server)
  POST /app/api/servers/<id>/panel  -> {url}      POST /app/api/servers/<id>/panel   -> {url}
  POST /app/api/logout                            POST /app/api/servers/<id>/restart (owners, not staff)
                                                  POST /app/api/logout
Login gives a token (Authorization: Bearer …) – stored hashed, valid 90 days, ended by logout. Logins are rate
limited like the web logins; the owner login needs the two-factor code when one is set. "panel" returns a one-time,
60-second link that opens that server's control panel signed in.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time

from aiohttp import web

from control_plane import billing, plans, provisioning
from control_plane.db import DB

TOKEN_TTL = 90 * 86400


def _key(kind: str, token: str) -> str:
    return f"apptok:{kind}:{hashlib.sha256(token.encode()).hexdigest()}"


def issue(db: DB, kind: str, subject: str) -> str:
    token = secrets.token_urlsafe(32)
    db.run("INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
           (_key(kind, token), json.dumps({"sub": subject, "at": int(time.time())})))
    return token


def subject_of(db: DB, kind: str, request) -> str | None:
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    row = db.one("SELECT value FROM settings_kv WHERE key = ?", (_key(kind, token),)) if token else None
    if row is None:
        return None
    data = json.loads(row["value"])
    if time.time() - data["at"] > TOKEN_TTL:
        db.run("DELETE FROM settings_kv WHERE key = ?", (_key(kind, token),))
        return None
    return str(data["sub"])


def revoke(db: DB, kind: str, request) -> None:
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if token:
        db.run("DELETE FROM settings_kv WHERE key = ?", (_key(kind, token),))


def _err(text: str, status: int = 400) -> web.Response:
    return web.json_response({"error": text}, status=status)


async def _json(request) -> dict:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _server(s) -> dict:
    return {"id": s["id"], "name": s["name"], "status": s["status"], "discord": bool(s["discord_ready"]),
            "guild": s["guild_name"], "rcon": bool(s["rcon_connected"]), "players": int(s["players_online"] or 0),
            "error": (s["error"] or "")[:200] or None,
            "can_open": s["status"] in ("active", "awaiting_token")}


def _panel_link(db: DB, box, settings, sid: int, who: str) -> str | None:
    import saas_mode
    s = db.one("SELECT slug, status, customer_id FROM servers WHERE id = ?", (sid,))
    health = provisioning.get_secret(db, box, sid, "health_token") if s else None
    if s is None or s["status"] not in ("active", "awaiting_token") or not health:
        return None
    token = saas_mode.owner_login_token(provisioning.manage_token(health), who)
    db.audit(who if ":" in who else f"owner:{who}", "server.open_panel", customer_id=s["customer_id"], server_id=sid,
             detail="Android app")
    return f"{settings.url_for(s['slug'])}/saas/login?t={token}"


SERVER_SQL = ("SELECT s.*, h.process, h.discord_ready, h.guild_name, h.rcon_connected, h.players_online, h.error "
              "FROM servers s LEFT JOIN health h ON h.server_id = s.id WHERE s.status != 'deleted' ")


# ------------------------------------------------------------------ customers (portal)

def register_portal(app: web.Application, db: DB, box, settings, limiter, client_ip) -> None:
    def me(request) -> dict | None:
        sub = subject_of(db, "c", request)
        row = db.one("SELECT * FROM customers WHERE id = ? AND status != 'deleted'", (int(sub),)) if sub else None
        return dict(row) if row else None

    async def login(request):
        from web.users import verify_password
        data = await _json(request)
        email = str(data.get("email", "")).strip().lower()[:200]
        if limiter.hit("login:" + client_ip(request)) is not None or limiter.hit("login:" + email) is not None:
            return _err("Too many attempts – try again in a few minutes.", 429)
        row = db.one("SELECT id, name, email, password_hash FROM customers WHERE email = ? AND status != 'deleted'",
                     (email,))
        if row is None or not row["password_hash"] or not verify_password(str(data.get("password", "")),
                                                                         row["password_hash"]):
            return _err("Wrong login or password.", 401)
        db.audit(f"customer:{row['id']}", "customer.app_login", customer_id=row["id"])
        return web.json_response({"token": issue(db, "c", str(row["id"])), "name": row["name"] or row["email"]})

    async def overview(request):
        c = me(request)
        if c is None:
            return _err("Log in again.", 401)
        sub = db.one("SELECT status, current_period_end FROM subscriptions WHERE customer_id = ?", (c["id"],))
        plan = plans.current(db, c["id"])
        rows = db.q(SERVER_SQL + "AND s.customer_id = ? ORDER BY s.id", (c["id"],))
        return web.json_response({
            "name": c["name"] or c["email"], "package": plan["name"] if plan else None,
            "running": billing.subscription_ok(db, c["id"]), "subscription": sub["status"] if sub else "none",
            "until": sub["current_period_end"] if sub and sub["status"] in ("trial", "paid") else None,
            "dashboard": f"{settings.portal_url}/dashboard",
            "servers": [{**_server(s), "website": settings.url_for(s["slug"])} for s in rows]},
            headers={"Cache-Control": "no-store"})

    async def panel(request):
        c = me(request)
        if c is None:
            return _err("Log in again.", 401)
        sid = int(request.match_info["sid"])
        if db.one("SELECT 1 FROM servers WHERE id = ? AND customer_id = ? AND status != 'deleted'",
                  (sid, c["id"])) is None:
            return _err("No such server.", 404)
        url = _panel_link(db, box, settings, sid, f"customer:{c['id']}")
        return web.json_response({"url": url}) if url else _err("This server isn't running right now.", 409)

    async def logout(request):
        revoke(db, "c", request)
        return web.json_response({"ok": True})

    app.add_routes([web.post("/app/api/login", login), web.get("/app/api/overview", overview),
                    web.post(r"/app/api/servers/{sid:\d+}/panel", panel), web.post("/app/api/logout", logout)])


# ------------------------------------------------------------------ managers (owner panel)

def register_owner(app: web.Application, db: DB, box, settings, supervisor, limiter, user_limiter, role_of,
                   client_ip) -> None:
    def who(request) -> str | None:
        sub = subject_of(db, "o", request)
        return sub if sub and db.one("SELECT 1 FROM owners WHERE username = ?", (sub,)) else None

    async def login(request):
        from web.totp import matching_step
        from web.users import verify_password
        data = await _json(request)
        ip, name = client_ip(request), str(data.get("username", ""))[:64]
        if limiter.hit("ip:" + ip) is not None or user_limiter.hit("user:" + name) is not None:
            return _err("Too many attempts – try again later.", 429)
        row = db.one("SELECT * FROM owners WHERE username = ?", (name,))
        if row is None or not verify_password(str(data.get("password", "")), row["password_hash"]) \
                or matching_step(row["totp_secret"], str(data.get("code", ""))) is None:
            db.audit("system", "owner.login_failed", detail=f"{ip} (app)")
            return _err("Wrong login (username, password or two-factor code).", 401)
        db.audit(f"owner:{row['username']}", "owner.login", detail=f"{ip} (Android app)")
        return web.json_response({"token": issue(db, "o", row["username"]), "name": row["username"],
                                  "role": role_of(row["username"])})

    async def overview(request):
        user = who(request)
        if user is None:
            return _err("Log in again.", 401)
        servers: dict[int, list] = {}
        rows = db.q(SERVER_SQL + "ORDER BY s.id")
        for s in rows:
            servers.setdefault(s["customer_id"], []).append({**_server(s), "process": s["process"]})
        customers = db.q("SELECT c.id, c.email, c.name, c.status, s.status AS sub, s.current_period_end AS until, "
                         "p.name AS plan FROM customers c LEFT JOIN subscriptions s ON s.customer_id = c.id "
                         "LEFT JOIN plans p ON p.id = s.plan_id WHERE c.status != 'deleted' ORDER BY c.id")
        active = [s for s in rows if s["status"] == "active"]
        return web.json_response({
            "user": user, "role": role_of(user),
            "stats": {"customers": len(customers),
                      "paying": sum(1 for c in customers if c["sub"] in ("active", "trialing", "paid")),
                      "servers": len(rows), "active": len(active),
                      "bots_online": sum(1 for s in active if s["discord_ready"]),
                      "rcon": sum(1 for s in active if s["rcon_connected"]),
                      "players": sum(int(s["players_online"] or 0) for s in active)},
            "customers": [{"id": c["id"], "name": c["name"] or c["email"], "email": c["email"], "status": c["status"],
                           "subscription": c["sub"] or "none", "plan": c["plan"],
                           "until": c["until"] if c["sub"] in ("trial", "paid") else None,
                           "servers": servers.get(c["id"], [])} for c in customers]},
            headers={"Cache-Control": "no-store"})

    async def panel(request):
        user = who(request)
        if user is None:
            return _err("Log in again.", 401)
        url = _panel_link(db, box, settings, int(request.match_info["sid"]), user)
        return web.json_response({"url": url}) if url else _err("This server isn't running right now.", 409)

    async def restart(request):
        user = who(request)
        if user is None:
            return _err("Log in again.", 401)
        if role_of(user) != "owner":
            return _err("Staff accounts can't restart servers.", 403)
        sid = int(request.match_info["sid"])
        if supervisor is None or db.one("SELECT 1 FROM servers WHERE id = ? AND status = 'active'", (sid,)) is None:
            return _err("This server isn't running.", 409)
        await supervisor.restart(sid)
        db.audit(f"owner:{user}", "server.restart", server_id=sid, detail="Android app")
        return web.json_response({"ok": True, "message": "Restarting …"})

    async def logout(request):
        revoke(db, "o", request)
        return web.json_response({"ok": True})

    app.add_routes([web.post("/app/api/login", login), web.get("/app/api/overview", overview),
                    web.post(r"/app/api/servers/{sid:\d+}/panel", panel),
                    web.post(r"/app/api/servers/{sid:\d+}/restart", restart), web.post("/app/api/logout", logout)])
