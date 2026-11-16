"""The hub: a public page with every listed, running server and a button to its website."""
import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from control_plane import provisioning
from tests.test_control_plane import cp, csrf_of, customer, paid  # noqa: F401


def test_hub_lists_running_servers_and_customers_can_hide_theirs(cp):  # noqa: F811
    from control_plane import web as cpweb

    async def on_change():
        return None

    async def scenario():
        a = TestClient(TestServer(cpweb.build_portal(cp.db, cp.box, cp.settings, on_change)))
        await a.start_server()
        try:
            page = await (await a.get("/servers")).text()
            assert "No servers are listed yet" in page and 'href=/servers>Servers' in await (await a.get("/")).text()
            await a.post("/signup", data={"email": "a@x.io", "password": "password1234"})
            cid = cp.db.one("SELECT id FROM customers WHERE email = 'a@x.io'")["id"]
            paid(cp, cid)
            cp.db.run("UPDATE customers SET base_slots = 2 WHERE id = ?", (cid,))
            one = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Kryvon <Empire>", actor="t")["id"]
            two = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Quiet one", actor="t")["id"]
            other = customer(cp, "b@x.io")                                   # not paid: never listed
            provisioning.provision_server(cp.db, cp.box, cp.settings, other, "Unpaid", actor="t")
            cp.db.run("UPDATE servers SET status = 'active'")
            cp.db.run("INSERT INTO health (server_id, checked_at, process, discord_ready, guild_name, rcon_connected, "
                      "players_online) VALUES (?, ?, 'running', 1, 'Kryvon DC', 1, 47)", (one, int(time.time())))
            b = TestClient(TestServer(cpweb.build_portal(cp.db, cp.box, cp.settings, on_change)))   # a fresh cache
            await b.start_server()
            try:
                page = await (await b.get("/servers")).text()
                assert "Kryvon &lt;Empire&gt;" in page and "<Empire>" not in page and "47 playing" in page
                assert "Quiet one" in page and "Unpaid" not in page and "Kryvon DC" in page
                assert page.index("Kryvon &lt;Empire&gt;") < page.index("Quiet one")          # online and busy first
                slug = cp.db.one("SELECT slug FROM servers WHERE id = ?", (one,))["slug"]
                assert f"href='{cp.settings.url_for(slug)}/'" in page
                # the customer hides one of theirs
                await b.post("/login", data={"email": "a@x.io", "password": "password1234"})
                dash = await (await b.get("/dashboard")).text()
                assert "Listed – hide" in dash
                r = await b.post(f"/servers/{two}/listed", data={"csrf": csrf_of(dash), "on": "0"}, allow_redirects=False)
                assert r.status == 303
                page = await (await b.get("/servers")).text()
                assert "Quiet one" not in page and "Kryvon &lt;Empire&gt;" in page
                assert "Hidden – list it" in await (await b.get("/dashboard")).text()
            finally:
                await b.close()
        finally:
            await a.close()
    asyncio.run(scenario())
