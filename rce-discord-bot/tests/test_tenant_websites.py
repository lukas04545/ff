"""Tenant websites: themes, choosing the address on our domain, own domains (paid add-on) and their routing."""
import asyncio

import pytest

from control_plane import cftunnel, features, hostproxy, provisioning
from tests.test_control_plane import cp, customer, paid  # noqa: F401


def _server(cp, name="Kryvon"):  # noqa: F811
    cid = customer(cp, f"{name.lower()}@x.io", own_bot=False)
    paid(cp, cid)
    cp.settings.base_domain = "kryvon3x.com"
    return cid, provisioning.provision_server(cp.db, cp.box, cp.settings, cid, name, actor="t")


def test_customer_picks_an_address_and_the_old_one_keeps_working(cp):  # noqa: F811
    cid, s = _server(cp)
    old = s["slug"]
    for bad in ("ab", "-x-", "admin", "sites", "UPPER CASE", "a" * 40):
        with pytest.raises(provisioning.ProvisionError):
            provisioning.change_address(cp.db, cp.settings, s["id"], bad, actor="t")
    assert provisioning.change_address(cp.db, cp.settings, s["id"], "kryvon-eu", actor="t") == "kryvon-eu"
    assert hostproxy.resolve(cp.db, cp.settings, "kryvon-eu.kryvon3x.com")[1]["id"] == s["id"]
    assert hostproxy.resolve(cp.db, cp.settings, f"{old}.kryvon3x.com")[1]["id"] == s["id"]       # alias
    _, other = _server(cp, "Other")
    with pytest.raises(provisioning.ProvisionError):                           # taken (also an old alias)
        provisioning.change_address(cp.db, cp.settings, other["id"], old, actor="t")
    env = provisioning.environment(cp.db, cp.box, cp.settings, s["id"])
    assert env.get("WEB_PUBLIC_URL", "https://kryvon-eu.kryvon3x.com").endswith("kryvon-eu.kryvon3x.com")


def test_own_domain_needs_the_addon_and_routes_once_added(cp):  # noqa: F811
    cid, s = _server(cp)
    with pytest.raises(provisioning.ProvisionError, match="bigger package"):
        provisioning.add_domain(cp.db, cp.settings, s["id"], "www.kryvonrust.com", actor="t")
    features.grant(cp.db, cid, "custom_domain", owner="boss")
    for bad in ("https://", "kryvon3x.com", "x.kryvon3x.com", "no spaces.com"):
        with pytest.raises(provisioning.ProvisionError):
            provisioning.add_domain(cp.db, cp.settings, s["id"], bad, actor="t")
    host = provisioning.add_domain(cp.db, cp.settings, s["id"], "https://WWW.KryvonRust.com/", actor="t")
    assert host == "www.kryvonrust.com"
    kind, row = hostproxy.resolve(cp.db, cp.settings, host)
    assert row["id"] == s["id"] and kind in ("tenant", "paused")               # routed to this server
    with pytest.raises(provisioning.ProvisionError, match="already"):
        provisioning.add_domain(cp.db, cp.settings, s["id"], host, actor="t")
    provisioning.remove_domain(cp.db, s["id"], host, actor="t")
    assert hostproxy.resolve(cp.db, cp.settings, host)[0] == "portal"         # gone


class FakeCF:
    def __init__(self):
        self.calls = []

    async def _call(self, method, path, json=None, params=None):
        self.calls.append((method, path, json))
        return {"id": "ch_1"}


def test_owner_connects_waiting_domains_without_saving_the_token(cp):  # noqa: F811
    cid, s = _server(cp)
    features.grant(cp.db, cid, "custom_domain", owner="boss")
    provisioning.add_domain(cp.db, cp.settings, s["id"], "www.kryvonrust.com", actor="t")
    from support.cloudflare import CloudflareError
    with pytest.raises(CloudflareError):                                       # Cloudflare setup not done yet
        asyncio.run(cftunnel.request_custom_hostname(cp.db, cp.settings, "t" * 40, "www.kryvonrust.com", api=FakeCF()))
    cp.db.run("INSERT INTO settings_kv (key, value) VALUES ('cf_zone_id', 'z1')")
    fake = FakeCF()
    out = asyncio.run(cftunnel.activate_pending(cp.db, cp.settings, "t" * 40, api=fake))
    assert out == {"www.kryvonrust.com": "connected"}
    assert fake.calls[0][1] == "/zones/z1/custom_hostnames" and fake.calls[0][2]["hostname"] == "www.kryvonrust.com"
    row = cp.db.one("SELECT status, cf_id FROM domains WHERE hostname = 'www.kryvonrust.com'")
    assert (row["status"], row["cf_id"]) == ("active", "ch_1")
    assert "t" * 40 not in str(cp.db.q("SELECT * FROM settings_kv"))


def test_website_themes():
    from plugins.website import THEMES, PlayerWebsite
    assert {"rust", "neon", "arctic", "jungle", "bloodmoon", "gold", "clean"} <= set(THEMES)
    for key, (label, css) in THEMES.items():
        assert label and (key in ("rust", "discord") or f"body.theme-{key}" in css)   # discord: from the icon
    theme = next(s for s in PlayerWebsite.SETTINGS if s.key == "theme")
    assert set(theme.choices) == set(THEMES) and theme.default == "rust"


def test_a_server_waiting_for_its_bot_is_routed_to_its_panel(cp):  # noqa: F811
    cid, s = _server(cp, "Lgh")
    row = cp.db.one("SELECT status FROM servers WHERE id = ?", (s["id"],))
    assert row["status"] == "awaiting_token"                                   # not in a Discord yet
    kind, found = hostproxy.resolve(cp.db, cp.settings, f"{s['slug']}.kryvon3x.com")
    assert kind == "tenant" and found["id"] == s["id"]                          # its panel, not "paused"
    provisioning.set_server_status(cp.db, s["id"], "suspended", actor="t")
    assert hostproxy.resolve(cp.db, cp.settings, f"{s['slug']}.kryvon3x.com")[0] == "paused"


def test_server_address_follows_a_new_domain(cp):  # noqa: F811
    from envfile import read_env
    from pathlib import Path
    cid, s = _server(cp, "Rusty")
    home = Path(cp.db.one("SELECT home_dir FROM servers WHERE id = ?", (s["id"],))["home_dir"])
    assert read_env(home / ".env")["WEB_PUBLIC_URL"].endswith(".kryvon3x.com")
    cp.settings.base_domain = "consolecorrosion.org"
    assert provisioning.sync_public_url(cp.db, cp.settings, s["id"], s["slug"], home)
    assert read_env(home / ".env")["WEB_PUBLIC_URL"] == f"https://{s['slug']}.consolecorrosion.org"
    assert not provisioning.sync_public_url(cp.db, cp.settings, s["id"], s["slug"], home)   # nothing twice
    # the owner's own server keeps its old domain: an own domain (no add-on needed for the owner)
    with pytest.raises(provisioning.ProvisionError):
        provisioning.add_domain(cp.db, cp.settings, s["id"], "kryvon3x.com", actor="customer")     # add-on needed
    provisioning.add_domain(cp.db, cp.settings, s["id"], "kryvon3x.com", actor="owner", owner=True)
    cp.db.run("UPDATE domains SET status = 'active' WHERE hostname = 'kryvon3x.com'")
    assert provisioning.sync_public_url(cp.db, cp.settings, s["id"], s["slug"], home)
    assert read_env(home / ".env")["WEB_PUBLIC_URL"] == "https://kryvon3x.com"
    assert hostproxy.resolve(cp.db, cp.settings, "kryvon3x.com")[1]["id"] == s["id"]
    assert hostproxy.resolve(cp.db, cp.settings, f"{s['slug']}.consolecorrosion.org")[1]["id"] == s["id"]


def test_customer_moves_their_website_address(cp):  # noqa: F811
    """Move an own domain to another of your servers, pick the main address, release an old address."""
    cid, a = _server(cp)
    from control_plane import slots
    slots.grant(cp.db, cid, 1, owner="boss")
    b = provisioning.provision_server(cp.db, cp.box, cp.settings, cid, "Second", actor="t")
    features.grant(cp.db, cid, "custom_domain", owner="boss")
    host = provisioning.add_domain(cp.db, cp.settings, a["id"], "www.kryvonrust.com", actor="t")
    cp.db.run("UPDATE domains SET status = 'active' WHERE hostname = ?", (host,))
    url = lambda s: provisioning.public_url(cp.db, cp.settings, s["id"], s["slug"])   # noqa: E731
    assert url(a) == "https://www.kryvonrust.com"
    env_before = provisioning.environment(cp.db, cp.box, cp.settings, a["id"])["SAAS_PUBLIC_URL"]
    # the free address as the main one (the domain keeps working)
    provisioning.set_main_address(cp.db, a["id"], provisioning.FREE_ADDRESS, actor="t")
    assert url(a) == cp.settings.url_for(a["slug"])
    assert provisioning.environment(cp.db, cp.box, cp.settings, a["id"])["SAAS_PUBLIC_URL"] != env_before  # restart
    with pytest.raises(provisioning.ProvisionError):
        provisioning.set_main_address(cp.db, a["id"], "www.not-mine.com", actor="t")
    provisioning.set_main_address(cp.db, a["id"], host, actor="t")
    assert url(a) == "https://www.kryvonrust.com"
    # move the domain to the second server
    provisioning.move_domain(cp.db, host, a["id"], b["id"], actor="t")
    assert hostproxy.resolve(cp.db, cp.settings, host)[1]["id"] == b["id"]
    assert url(b) == "https://www.kryvonrust.com" and url(a) == cp.settings.url_for(a["slug"])
    with pytest.raises(provisioning.ProvisionError):                           # not on that server any more
        provisioning.move_domain(cp.db, host, a["id"], b["id"], actor="t")
    _, stranger = _server(cp, "Stranger")
    with pytest.raises(provisioning.ProvisionError, match="your other servers"):
        provisioning.move_domain(cp.db, host, b["id"], stranger["id"], actor="t")
    # an old address forwards until it's released – then it's free again
    old = a["slug"]
    provisioning.change_address(cp.db, cp.settings, a["id"], "kryvon-new", actor="t")
    old_host = f"{old}.kryvon3x.com"
    assert provisioning.old_addresses(cp.db, cp.settings, a["id"]) == [old_host]
    provisioning.release_old_address(cp.db, a["id"], old_host, actor="t")
    assert hostproxy.resolve(cp.db, cp.settings, old_host)[0] not in ("tenant", "paused")   # no longer forwards
    assert provisioning.change_address(cp.db, cp.settings, stranger["id"], old, actor="t") == old
    with pytest.raises(provisioning.ProvisionError):
        provisioning.release_old_address(cp.db, a["id"], old_host, actor="t")


def test_dashboard_buttons_to_move_the_website(cp):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer

    from control_plane import slots
    from control_plane import web as cpweb
    from tests.test_control_plane import csrf_of
    cp.settings.base_domain = "kryvon3x.com"

    async def changed():
        pass

    async def scenario():
        a = TestClient(TestServer(cpweb.build_portal(cp.db, cp.box, cp.settings, changed)))
        await a.start_server()
        try:
            await a.post("/signup", data={"email": "a@x.io", "password": "password1234"})
            cid = cp.db.one("SELECT id FROM customers WHERE email = 'a@x.io'")["id"]
            paid(cp, cid)
            slots.grant(cp.db, cid, 1, owner="boss")
            features.grant(cp.db, cid, "custom_domain", owner="boss")
            for name in ("One", "Two"):
                await a.post("/servers", data={"name": name, "csrf": csrf_of(await (await a.get("/dashboard")).text())})
            one, two = [r["id"] for r in cp.db.q("SELECT id FROM servers ORDER BY id")]
            provisioning.add_domain(cp.db, cp.settings, one, "www.kryvonrust.com", actor="t")
            cp.db.run("UPDATE domains SET status = 'active'")
            page = await (await a.get("/dashboard")).text()
            assert "Move to this server" in page and "Use as main address" in page
            t = csrf_of(page)
            r = await a.post(f"/servers/{one}/main-address", data={"csrf": t, "choice": "free"}, allow_redirects=False)
            assert "Main+address" in r.headers["Location"] and "kryvon3x.com" in r.headers["Location"]
            r = await a.post(f"/servers/{one}/domains/move", data={"csrf": t, "hostname": "www.kryvonrust.com",
                                                                   "to": str(two)}, allow_redirects=False)
            assert "now+shows" in r.headers["Location"]
            assert cp.db.one("SELECT server_id FROM domains")["server_id"] == two
        finally:
            await a.close()
    asyncio.run(scenario())
