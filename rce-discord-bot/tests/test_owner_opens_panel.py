"""The platform owner opens a customer's control panel from the owner panel – a one-time, short-lived sign-in."""
import time

import saas_mode
from tests.test_control_plane import cp, csrf_of  # noqa: F401
from tests.test_web import H, bot, run  # noqa: F401


def test_owner_login_token(monkeypatch):
    monkeypatch.setenv("RCE_SAAS", "1")
    monkeypatch.setenv("SAAS_MANAGE_TOKEN", "m" * 64)
    t = saas_mode.owner_login_token("m" * 64, "boss")
    assert saas_mode.check_owner_login(t) == "boss"
    assert saas_mode.check_owner_login(t) is None                                   # once
    assert saas_mode.check_owner_login(saas_mode.owner_login_token("x" * 64, "boss")) is None    # another server
    old = saas_mode.owner_login_token("m" * 64, "boss", now=time.time() - 120)
    assert saas_mode.check_owner_login(old) is None                                 # expired
    t = saas_mode.owner_login_token("m" * 64, "boss")
    payload, sig = t.rsplit(".", 1)
    assert saas_mode.check_owner_login(payload[:-2] + "AA." + sig) is None           # tampered
    monkeypatch.setenv("RCE_SAAS", "0")
    assert saas_mode.check_owner_login(saas_mode.owner_login_token("m" * 64, "boss")) is None   # own installs: never


def test_tenant_panel_signs_the_owner_in(bot, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RCE_SAAS", "1")
    monkeypatch.setenv("SAAS_MANAGE_TOKEN", "k" * 64)

    async def scenario(c):
        assert (await c.get("/api/me")).status in (401, 404)
        r = await c.get("/saas/login?t=bad")
        assert r.status == 404
        t = saas_mode.owner_login_token("k" * 64, "boss")
        r = await c.get(f"/saas/login?t={t}")
        assert r.status == 200 and "refresh" in await r.text()
        me = await (await c.get("/api/me")).json()
        assert me["user"] == "owner"                                                  # signed in as the owner
        assert (await c.get(f"/saas/login?t={t}")).status == 404                      # the link is used up
    run(bot, scenario)


def test_owner_panel_open_button(cp, monkeypatch):  # noqa: F811
    import asyncio
    import html as _html
    import re

    from aiohttp.test_utils import TestClient, TestServer

    from control_plane import provisioning, websettings
    from control_plane.web import build_owner

    cid = provisioning.create_customer(cp.db, "a@x.io", "password1234")
    cp.db.run("INSERT INTO servers (customer_id, slug, name, status, home_dir, web_port, created_at) "
              "VALUES (?, 'kryvon', 'Kryvon', 'active', ?, 21001, ?)", (cid, str(cp.tmp / "h"), int(time.time())))
    sid = cp.db.one("SELECT id FROM servers WHERE slug = 'kryvon'")["id"]
    provisioning.put_secret(cp.db, cp.box, sid, "health_token", "h" * 40)

    async def changed():
        pass

    async def scenario():
        c = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        await c.start_server()
        try:
            token = websettings.new_login_link(cp.db)
            await c.post(f"/once/{token}", data={"username": "boss", "password": "owner-password-123"})
            page = await (await c.get("/servers")).text()
            assert "Open panel" in page
            assert (await c.post(f"/servers/{sid}/open", data={})).status == 403                 # CSRF
            r = await c.post(f"/servers/{sid}/open", data={"csrf": csrf_of(page)})
            url = _html.unescape(re.search(r'href="([^"]+)"', await r.text())[1])
            assert url.startswith("https://kryvon.rust.test/saas/login?t=")
            monkeypatch.setenv("RCE_SAAS", "1")
            monkeypatch.setenv("SAAS_MANAGE_TOKEN", provisioning.manage_token("h" * 40))
            assert saas_mode.check_owner_login(url.split("t=", 1)[1]) == "boss"
            assert cp.db.one("SELECT 1 FROM audit_log WHERE action = 'server.open_panel'")
        finally:
            await c.close()
    asyncio.run(scenario())


def test_password_link_lets_the_customer_choose_the_panel_password(bot, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RCE_SAAS", "1")
    monkeypatch.setenv("SAAS_MANAGE_TOKEN", "p" * 64)
    link = saas_mode.owner_login_token("p" * 64, "boss", purpose="password")
    assert saas_mode.check_owner_login(link) is None                         # not a sign-in link
    assert saas_mode.check_owner_login(saas_mode.owner_login_token("p" * 64, "boss"), purpose="password") is None

    async def scenario(c):
        r = await c.get(f"/saas/password?t={link}")
        assert r.status == 200 and "Choose your panel password" in await r.text()
        assert (await c.get(f"/saas/password?t={link}")).status == 200          # looking doesn't use it up
        r = await c.post("/saas/password", data={"t": link, "password": "short", "confirm": "short"})
        assert "10 to 256" in await r.text()
        r = await c.post("/saas/password", data={"t": link, "password": "new-panel-pass", "confirm": "other-pass1"})
        assert "passwords aren" in await r.text() and "the same" in await r.text()
        r = await c.post("/saas/password", data={"t": link, "password": "new-panel-pass", "confirm": "new-panel-pass"})
        assert "Password saved" in await r.text()
        assert (await (await c.get("/api/me")).json())["user"] == "owner"         # signed in
        await c.post("/api/logout", headers=H)
        assert (await c.post("/api/login", json={"password": "secret123"}, headers=H)).status == 401   # old one
        assert (await c.post("/api/login", json={"password": "new-panel-pass"}, headers=H)).status == 200
        assert (await c.get(f"/saas/password?t={link}")).status == 404          # used up
        saas_mode._used_logins.clear()                                          # a restart: still used up (on disk)
        r = await c.post("/saas/password", data={"t": link, "password": "another-pass1", "confirm": "another-pass1"})
        assert r.status == 404
    run(bot, scenario)


def test_owner_and_customer_make_password_links(cp, monkeypatch):  # noqa: F811
    import asyncio
    import html as _html
    import re

    from aiohttp.test_utils import TestClient, TestServer

    from control_plane import provisioning, websettings
    from control_plane.web import build_owner, build_portal

    cid = provisioning.create_customer(cp.db, "a@x.io", "password1234")
    other = provisioning.create_customer(cp.db, "b@x.io", "password1234")
    cp.db.run("INSERT INTO servers (customer_id, slug, name, status, home_dir, web_port, created_at) "
              "VALUES (?, 'kryvon', 'Kryvon', 'active', ?, 21001, ?)", (cid, str(cp.tmp / "h"), int(time.time())))
    sid = cp.db.one("SELECT id FROM servers WHERE slug = 'kryvon'")["id"]
    provisioning.put_secret(cp.db, cp.box, sid, "health_token", "h" * 40)
    monkeypatch.setenv("RCE_SAAS", "1")
    monkeypatch.setenv("SAAS_MANAGE_TOKEN", provisioning.manage_token("h" * 40))

    async def changed():
        pass

    async def scenario():
        o = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        await o.start_server()
        p = TestClient(TestServer(build_portal(cp.db, cp.box, cp.settings, changed)))
        await p.start_server()
        try:
            token = websettings.new_login_link(cp.db)
            await o.post(f"/once/{token}", data={"username": "boss", "password": "owner-password-123"})
            page = await (await o.get("/servers")).text()
            assert "Password link" in page
            r = await o.post(f"/servers/{sid}/password-link", data={"csrf": csrf_of(page)})
            link = _html.unescape(re.search(r"(https://kryvon\.rust\.test/saas/password\?t=\S+?)<", await r.text())[1])
            assert saas_mode.check_owner_login(link.split("t=", 1)[1], purpose="password", consume=False) == "boss"
            # the customer makes one for their own server – never for someone else's
            await p.post("/login", data={"email": "a@x.io", "password": "password1234"})
            dash = await (await p.get("/dashboard")).text()
            assert "Panel password" in dash
            t = re.search(r"name=csrf value=([0-9a-f]+)", dash)[1]
            r = await p.post(f"/servers/{sid}/password-link", data={"csrf": t})
            assert "/saas/password?t=" in await r.text()
            p.session.cookie_jar.clear()
            await p.post("/login", data={"email": "b@x.io", "password": "password1234"})
            dash = await (await p.get("/dashboard")).text()
            t = re.search(r"name=csrf value=([0-9a-f]+)", dash)[1]
            assert (await p.post(f"/servers/{sid}/password-link", data={"csrf": t})).status == 404
            assert other
        finally:
            await p.close()
            await o.close()
    asyncio.run(scenario())


def test_hosted_panel_hides_the_platforms_ai_model_and_balance(bot, monkeypatch):  # noqa: F811
    from types import SimpleNamespace as NS

    async def balance():
        return {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "6.58"}]}
    ai = NS(enabled=True, model="deepseek-flash", balance=balance)
    real = bot.get_cog
    monkeypatch.setattr(bot, "get_cog", lambda name: NS(ai=ai) if name == "Tickets" else real(name))

    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        own = await (await c.get("/api/ai")).json()
        assert own["model"] == "deepseek-flash" and own["balance"]["is_available"]       # own install: shown
        monkeypatch.setenv("RCE_SAAS", "1")
        hosted = await (await c.get("/api/ai")).json()
        assert hosted == {"enabled": True, "hosted": True}                              # hosted: nothing of it
    run(bot, scenario)
