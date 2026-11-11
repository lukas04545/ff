"""The Android app's JSON API: customer login/overview/panel link on the portal, manager login/overview on the owner
panel."""
import asyncio

from aiohttp.test_utils import TestClient, TestServer

from control_plane import provisioning, slots, websettings
from tests.test_control_plane import cp, csrf_of, paid  # noqa: F401


def test_customer_app_api(cp):  # noqa: F811
    from control_plane import web as cpweb

    async def changed():
        pass

    async def scenario():
        a = TestClient(TestServer(cpweb.build_portal(cp.db, cp.box, cp.settings, changed)))
        await a.start_server()
        try:
            await a.post("/signup", data={"email": "a@x.io", "password": "password1234"})
            cid = cp.db.one("SELECT id FROM customers")["id"]
            paid(cp, cid)
            await a.post("/servers", data={"name": "EU", "csrf": csrf_of(await (await a.get("/dashboard")).text())})
            sid = cp.db.one("SELECT id FROM servers")["id"]
            cp.db.run("UPDATE servers SET status = 'active'")
            a.session.cookie_jar.clear()
            assert (await a.post("/app/api/login", json={"email": "a@x.io", "password": "nope"})).status == 401
            assert (await a.get("/app/api/overview")).status == 401
            tok = (await (await a.post("/app/api/login", json={"email": "A@x.io", "password": "password1234"})).json())["token"]
            h = {"Authorization": f"Bearer {tok}"}
            o = await (await a.get("/app/api/overview", headers=h)).json()
            assert o["running"] and o["servers"][0]["name"] == "EU" and o["servers"][0]["can_open"]
            url = (await (await a.post(f"/app/api/servers/{sid}/panel", headers=h)).json())["url"]
            assert "/saas/login?t=" in url
            assert (await a.post("/app/api/servers/9999/panel", headers=h)).status == 404
            assert tok not in str(cp.db.q("SELECT key, value FROM settings_kv"))          # stored hashed
            await a.post("/app/api/logout", headers=h)
            assert (await a.get("/app/api/overview", headers=h)).status == 401
        finally:
            await a.close()
    asyncio.run(scenario())


def test_manager_app_api(cp):  # noqa: F811
    from control_plane import web as cpweb
    restarted = []

    class Sup:
        procs = {}

        async def restart(self, sid):
            restarted.append(sid)

    async def changed():
        pass

    async def scenario():
        c = TestClient(TestServer(cpweb.build_owner(cp.db, cp.box, cp.settings, changed, Sup())))
        await c.start_server()
        try:
            token = websettings.new_login_link(cp.db)
            await c.post(f"/once/{token}", data={"username": "boss", "password": "owner-password-123"})
            c.session.cookie_jar.clear()
            cid = provisioning.create_customer(cp.db, "k@x.io", "password1234", "Kunde")
            paid(cp, cid)
            s = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="t")
            cp.db.run("UPDATE servers SET status = 'active'")
            assert (await c.post("/app/api/login", json={"username": "boss", "password": "x"})).status == 401
            assert (await c.get("/app/api/overview")).status == 401
            import time
            from web.totp import STEP, code_at
            assert (await c.post("/app/api/login", json={"username": "boss", "password": "owner-password-123"})).status == 401
            code = code_at(cp.db.one("SELECT totp_secret FROM owners")["totp_secret"], int(time.time() // STEP))
            r = await (await c.post("/app/api/login", json={"username": "boss", "password": "owner-password-123",
                                                            "code": code})).json()
            h = {"Authorization": f"Bearer {r['token']}"}
            o = await (await c.get("/app/api/overview", headers=h)).json()
            assert o["stats"]["customers"] == 1 and o["customers"][0]["servers"][0]["name"] == "Main"
            assert (await c.post(f"/app/api/servers/{s['id']}/restart", headers=h)).status == 200 and restarted == [s["id"]]
            cp.db.run("UPDATE owners SET role = 'staff'")
            assert (await c.post(f"/app/api/servers/{s['id']}/restart", headers=h)).status == 403
            assert "/saas/login?t=" in (await (await c.post(f"/app/api/servers/{s['id']}/panel", headers=h)).json())["url"]
        finally:
            await c.close()
    asyncio.run(scenario())
