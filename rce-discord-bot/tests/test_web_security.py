"""Hardening of the control panel and the website: security headers, strict CSP, Secure cookie over HTTPS, two-factor
login, password confirmation before risky actions, local-only login link, no cross-site form posts."""
import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from tests.test_web import H, bot, run  # noqa: F401
from web import totp


def test_security_headers_and_strict_panel_csp(bot):  # noqa: F811
    async def scenario(c):
        r = await c.get("/admin")
        page = await r.text()
        csp = r.headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
        assert "frame-ancestors 'none'" in csp and r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["Server"] == "RCE-Bot"
        assert "camera=()" in r.headers["Permissions-Policy"]
        assert "<script>" not in page and "/static/boot.js?v=" in page          # no inline script left
        assert "Strict-Transport-Security" not in r.headers                    # plain http: no HSTS
        r = await c.get("/api/status")
        assert r.status == 401 and r.headers["Cache-Control"] == "no-store" and r.headers["X-Frame-Options"] == "DENY"
        r = await c.get("/admin", headers={"X-Forwarded-Proto": "https"})      # through the tunnel
        assert r.headers["Strict-Transport-Security"].startswith("max-age=")
        r = await c.post("/api/login", json={"password": "secret123"}, headers={**H, "X-Forwarded-Proto": "https"})
        assert "secure" in r.headers["Set-Cookie"].lower()
    run(bot, scenario)


def test_two_factor_login(bot):  # noqa: F811
    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        assert (await (await c.get("/api/2fa")).json()) == {"enabled": False}
        setup = await (await c.post("/api/2fa/setup", headers=H)).json()
        assert setup["uri"].startswith("otpauth://totp/") and len(setup["secret"]) >= 32
        r = await c.post("/api/2fa/enable", json={"code": "000000"}, headers=H)
        assert r.status == 400
        step = int(time.time() // 30)
        r = await c.post("/api/2fa/enable", json={"code": totp.code_at(setup["secret"], step)}, headers=H)
        assert (await r.json()) == {"enabled": True}
        await c.post("/api/logout", headers=H)
        r = await c.post("/api/login", json={"password": "secret123"}, headers=H)
        data = await r.json()
        assert r.status == 401 and data["need_2fa"] and (await c.get("/api/status")).status == 401
        r = await c.post("/api/login", json={"password": "secret123", "code": "123456"}, headers=H)
        assert r.status == 401 and "Wrong" in (await r.json())["error"]
        code = totp.code_at(setup["secret"], step + 1)
        r = await c.post("/api/login", json={"password": "secret123", "code": code}, headers=H)
        assert r.status == 200 and (await c.get("/api/status")).status == 200
        await c.post("/api/logout", headers=H)
        r = await c.post("/api/login", json={"password": "secret123", "code": code}, headers=H)
        assert r.status == 401                                                  # a code works only once
        r = await c.post("/api/login", json={"password": "wrong", "code": totp.code_at(setup["secret"], step - 1)},
                         headers=H)
        assert r.status == 401 and not (await r.json()).get("need_2fa")         # password checked first
    run(bot, scenario)


def test_risky_actions_need_the_password_again(bot):  # noqa: F811
    holder = {}

    async def scenario(c):
        web = holder["web"]
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        r = await c.post("/api/users", json={"username": "mod1", "password": "longpassword1", "role": "admin"},
                         headers=H)
        assert r.status == 200                                                 # just logged in: fine
        web._sudo.clear()                                                      # 15 minutes later …
        r = await c.post("/api/users", json={"username": "mod2", "password": "longpassword2", "role": "admin"},
                         headers=H)
        data = await r.json()
        assert r.status == 403 and data["reauth"] and data["need_2fa"] is False
        assert (await c.get("/api/users")).status == 200                       # reading needs no confirmation
        assert (await c.post("/api/reauth", json={"password": "nope"}, headers=H)).status == 403
        assert (await c.post("/api/reauth", json={"password": "secret123"}, headers=H)).status == 200
        r = await c.post("/api/users", json={"username": "mod2", "password": "longpassword2", "role": "admin"},
                         headers=H)
        assert r.status == 200
        # The owner can reset a user's 2FA (lost phone)
        web.two_factor.enable("mod1", totp.new_secret())
        users = (await (await c.get("/api/users")).json())["users"]
        assert next(u for u in users if u["username"] == "mod1")["two_factor"] is True
        assert (await c.delete("/api/users/mod1/2fa", headers=H)).status == 200 and not web.two_factor.enabled("mod1")

    async def main():
        from web.server import WebInterface
        tmp = bot.config.database_path.parent
        web = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        holder["web"] = web
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            await scenario(c)
        finally:
            await c.close()
            await bot.rcon.close()
    asyncio.run(main())


def test_login_link_only_on_this_pc_and_no_cross_site_posts(bot):  # noqa: F811
    holder = {}

    async def scenario(c):
        token = holder["web"].login_token
        r = await c.get(f"/auth?token={token}", allow_redirects=False,
                        headers={"CF-Connecting-IP": "203.0.113.5"})           # through the public tunnel
        assert r.status == 302 and "rce_session" not in r.headers.get("Set-Cookie", "")
        r = await c.get(f"/auth?token={token}", allow_redirects=False)         # on the bot's PC
        assert r.status == 302 and "rce_session" in r.headers.get("Set-Cookie", "")
        r = await c.post("/store/checkout", data={"kind": "kit"}, headers={"Origin": "https://evil.example"})
        assert r.status == 403
        r = await c.post("/api/logout", headers={**H, "Origin": "https://evil.example"})
        assert r.status == 200                                                  # the API has its own CSRF header

    async def main():
        from web.server import WebInterface
        tmp = bot.config.database_path.parent
        web = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        holder["web"] = web
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            await scenario(c)
        finally:
            await c.close()
            await bot.rcon.close()
    asyncio.run(main())


def test_totp_codes_match_the_rfc():
    # RFC 6238 test vector (SHA-1, secret "12345678901234567890", T = 59 s -> 94287082, 6 digits: 287082)
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp.code_at(secret, 59 // 30) == "287082"
    assert totp.matching_step(secret, "287 082", now=59) == 1 and totp.matching_step(secret, "000000", now=59) is None
