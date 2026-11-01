"""Every website through one Cloudflare tunnel: the portal listener passes <slug>.<domain> on to the server, and the
setup (fake Cloudflare API) creates the tunnel, the wildcard record and keeps old addresses of adopted servers."""
import asyncio
import os
import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from control_plane import cftunnel, provisioning, websettings
from tests.test_control_plane import TOKEN, cp, customer, paid  # noqa: F401


@pytest.fixture(autouse=True)
def own_env(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))


def test_portal_passes_each_website_on(cp):  # noqa: F811
    from control_plane.web import build_portal
    cp.settings.base_domain = "kryvon3x.com"
    cid = customer(cp)
    paid(cp, cid)
    s = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Main", actor="t")
    provisioning.set_bot_token(cp.db, cp.box, s["id"], TOKEN, actor="t")
    seen = []

    async def tenant_page(request):
        seen.append((request.headers.get("Host"), request.headers.get("X-Forwarded-For"), request.path_qs,
                     await request.text()))
        resp = web.Response(text="RCE Bot Panel")
        resp.set_cookie("a", "1")
        resp.set_cookie("b", "2")
        return resp

    async def changed():
        pass

    async def scenario():
        tenant = TestServer(web.Application())
        tenant.app.router.add_route("*", "/{tail:.*}", tenant_page)
        await tenant.start_server()
        cp.db.run("UPDATE servers SET web_port = ?, status = 'active' WHERE id = ?", (tenant.port, s["id"]))
        c = TestClient(TestServer(build_portal(cp.db, cp.box, cp.settings, changed)))
        await c.start_server()
        try:
            host = f"{s['slug']}.kryvon3x.com"
            r = await c.post("/panel-x/api/kits?x=1", data="hello",
                             headers={"Host": host, "CF-Connecting-IP": "203.0.113.9", "X-Forwarded-For": "6.6.6.6"})
            assert r.status == 200 and await r.text() == "RCE Bot Panel"
            assert seen[-1] == (host, "203.0.113.9", "/panel-x/api/kits?x=1", "hello")    # visitor IP, not spoofed
            assert len(r.headers.getall("Set-Cookie")) == 2
            r = await c.get("/", headers={"Host": "portal.kryvon3x.com"})
            assert "Sign up" in await r.text() and len(seen) == 1                          # the portal itself
            assert (await c.get("/", headers={"Host": "nope.kryvon3x.com"})).status == 404
            cp.db.run("UPDATE servers SET status = 'suspended' WHERE id = ?", (s["id"],))
            r = await c.get("/", headers={"Host": host}, allow_redirects=False)
            assert r.headers["Location"].endswith(f"/paused/{s['slug']}")
            cp.db.run("INSERT INTO settings_kv (key, value) VALUES ('host_alias:panel.old.net', ?)", (str(s["id"]),))
            cp.db.run("UPDATE servers SET status = 'active' WHERE id = ?", (s["id"],))
            await c.get("/x", headers={"Host": "panel.old.net"})
            assert seen[-1][0] == "panel.old.net"                                         # an old address still works
        finally:
            await c.close()
            await tenant.close()
    asyncio.run(scenario())


class FakeCF:
    def __init__(self):
        self.calls, self.records = [], {"panel.kryvon3x.com": {"id": "r1", "type": "CNAME",
                                                              "content": "old.cfargotunnel.com", "proxied": True}}

    async def find_zone(self, host):
        return {"id": "z1", "name": "kryvon3x.com", "account": {"id": "acc"}}

    async def tunnel(self, account, name):
        self.calls.append(("tunnel", name))
        return {"id": "t1", "token": "T" * 40}, "created"

    async def dns(self, zone, host, tunnel_id):
        self.calls.append(("dns", host))
        return "updated" if host in self.records else "created"

    async def _call(self, method, path, json=None, params=None):
        self.calls.append((method, path, json))
        return {}


def test_setup_routes_everything_and_keeps_old_addresses(cp, tmp_path):  # noqa: F811
    cp.settings.base_domain = "kryvon3x.com"
    old = tmp_path / "old"
    old.mkdir()
    (old / ".env").write_text("WEB_PUBLIC_URL=https://panel.kryvon3x.com\n")
    cp.db.run("INSERT INTO settings_kv (key, value) VALUES (?, '7')", (f"adopted:{old}",))
    fake = FakeCF()
    out = asyncio.run(cftunnel.setup(cp.db, cp.box, cp.settings, "x" * 40, api=fake))
    assert out["dns"] == {"*.kryvon3x.com": "created", "panel.kryvon3x.com": "updated"}
    ingress = next(c[2] for c in fake.calls if c[0] == "PUT")["config"]["ingress"]
    hosts = [i.get("hostname") for i in ingress]
    assert hosts == ["panel.kryvon3x.com", "*.kryvon3x.com", "kryvon3x.com", None]
    assert all(i["service"] == f"http://127.0.0.1:{cp.settings.portal_port}" for i in ingress[:-1])
    assert cp.settings.cf_tunnel_token == "T" * 40                        # only the tunnel token is kept …
    raw = cp.db.one("SELECT value FROM settings_kv WHERE key = 'web_settings'")["value"]
    assert "T" * 40 not in raw and "x" * 40 not in str(cp.db.q("SELECT * FROM settings_kv"))   # … encrypted
    assert cp.db.one("SELECT value FROM settings_kv WHERE key = 'host_alias:panel.kryvon3x.com'")["value"] == "7"


def test_setup_needs_a_real_domain(cp):  # noqa: F811
    from support.cloudflare import CloudflareError
    cp.settings.base_domain = "example.com"
    with pytest.raises(CloudflareError):
        asyncio.run(cftunnel.setup(cp.db, cp.box, cp.settings, "x" * 40, api=FakeCF()))


def test_cli_cloudflare_asks_for_the_token_and_prints_links(cp, capsys, monkeypatch):  # noqa: F811
    from control_plane import cli
    monkeypatch.setattr(cli, "Settings", lambda: cp.settings)
    monkeypatch.setattr(cli, "service_user", lambda: None)
    monkeypatch.setenv("CF_API_TOKEN", "x" * 40)

    async def fake_setup(db, box, settings, token, api=None):
        assert token == "x" * 40 and settings.base_domain == "kryvon3x.com"
        return {"domain": "kryvon3x.com", "tunnel": "created", "dns": {"*.kryvon3x.com": "created"}, "aliases": {}}
    monkeypatch.setattr(cftunnel, "setup", fake_setup)
    assert cli.main(["cloudflare", "--domain", "kryvon3x.com"]) == 0
    out = capsys.readouterr().out
    assert "systemctl restart rce-saas" in out and re.search(r"/once/[A-Za-z0-9_-]+", out)
    assert "https://portal.kryvon3x.com/management" in out
