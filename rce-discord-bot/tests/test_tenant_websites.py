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
        assert label and (key == "rust" or f"body.theme-{key}" in css)
    theme = next(s for s in PlayerWebsite.SETTINGS if s.key == "theme")
    assert set(theme.choices) == set(THEMES) and theme.default == "rust"
