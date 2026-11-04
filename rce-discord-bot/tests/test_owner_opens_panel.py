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
