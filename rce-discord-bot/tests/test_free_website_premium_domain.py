"""No more AI code editing; the free website with the server's own pictures; the own-domain premium add-on."""
import asyncio
import io
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from control_plane import billing, features, plans, provisioning
from tests.test_control_plane import cp, customer  # noqa: F401
from tests.test_plugins import pb  # noqa: F401

BOT = Path(__file__).resolve().parent.parent


def test_management_ai_cannot_edit_code_any_more():
    for gone in ("cogs/code_changes.py", "support/code_edit.py", "revert_code.py"):
        assert not (BOT / gone).exists(), gone
    import cogs.manager_ai as m
    names = {t["function"]["name"] for t in m.TOOLS}
    assert not names & {"edit_code_file", "read_code_file", "list_code_files", "revert_code_changes"}
    assert not hasattr(m, "CODE_TOOLS") and not hasattr(m, "CODE_ON")
    import settings_schema
    assert "MANAGER_AI_CODE" not in settings_schema.FIELDS_BY_KEY


def _png(color=(200, 60, 20)) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (64, 40), color).save(out, "PNG")
    return out.getvalue()


def test_website_header_picture_and_gallery(pb):  # noqa: F811
    async def main():
        await pb.m.load_all()
        await pb.m.set_enabled("website", True)
        site = pb.m.running("website")
        page = site.home("")
        assert "📸" not in page and "/banner" not in page
        await pb.m.save_file("website", "banner", _png())
        await pb.m.save_file("website", "gallery_2", _png((10, 120, 200)))
        await pb.m.save_settings("website", {"gallery_title": "Kryvon bases"})
        page = site.home("")
        assert "/banner?v=" in page and "📸 Kryvon bases" in page and "/gallery/2?v=" in page
        assert "/gallery/1?" not in page
    asyncio.run(main())


def test_website_is_on_by_default_for_hosted_servers(monkeypatch):
    from plugin_manager import PluginManager
    from plugins.website import PlayerWebsite
    info = NS(cls=PlayerWebsite, builtin=True, overrides=False)
    monkeypatch.delenv("RCE_SAAS", raising=False)
    assert PluginManager._default_on(info) is False                    # own installs: as before
    monkeypatch.setenv("RCE_SAAS", "1")
    assert PluginManager._default_on(info) is True                     # hosted: free, on from the start


def test_own_domain_addon_price_and_webhook(cp):  # noqa: F811
    cid = customer(cp, own_bot=False)
    plans.seed(cp.db)
    cp.settings.domain_price = ""
    assert plans.addon_price(cp.db, cp.settings) is None
    cp.settings.domain_price = "3,99"
    assert plans.addon_price(cp.db, cp.settings)["text"] == "€3.99"
    cp.db.run("INSERT INTO settings_kv (key, value) VALUES ('addon_domain_price', '{\"id\": \"price_dom\", "
              "\"cents\": 399, \"currency\": \"eur\"}')")
    starter = plans.get(cp.db, slug="starter")
    cp.db.run("UPDATE plans SET stripe_price_id = 'price_starter' WHERE id = ?", (starter["id"],))
    cp.db.run("UPDATE customers SET stripe_customer_id = 'cus_1' WHERE id = ?", (cid,))

    def sub(eid, prices, status="active", kind="customer.subscription.updated"):
        return {"id": eid, "type": kind, "data": {"object": {
            "customer": "cus_1", "id": "sub_1", "status": status,
            "items": {"data": [{"price": {"id": p}} for p in prices]}}}}
    billing.handle_event(cp.db, sub("e1", ["price_starter", "price_dom"]))
    assert plans.current(cp.db, cid)["slug"] == "starter"              # the package item, not the add-on
    assert "custom_domain" in features.active(cp.db, cid)
    billing.handle_event(cp.db, sub("e2", ["price_starter"]))
    assert "custom_domain" not in features.active(cp.db, cid)          # add-on removed
    features.grant(cp.db, cid, "custom_domain", owner="boss")          # the owner's own grant …
    billing.handle_event(cp.db, sub("e3", ["price_starter"]))
    assert "custom_domain" in features.active(cp.db, cid)              # … isn't taken away by Stripe
    features.revoke(cp.db, cid, "custom_domain", owner="boss")
    billing.handle_event(cp.db, sub("e4", ["price_starter", "price_dom"]))
    billing.handle_event(cp.db, sub("e5", ["price_starter", "price_dom"], kind="customer.subscription.deleted"))
    assert "custom_domain" not in features.active(cp.db, cid)          # subscription ended


def test_portal_sells_the_addon(cp, monkeypatch):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer
    from control_plane.web import build_portal
    from tests.test_control_plane import csrf_of
    cp.settings.domain_price = "3.99"
    calls = []

    async def fake_price(db, settings):
        return "price_dom"

    async def fake_change(settings, sub_id, price, add):
        calls.append((sub_id, price, add))
    monkeypatch.setattr(plans, "ensure_addon_stripe_price", fake_price)
    monkeypatch.setattr(plans, "change_addon", fake_change)

    async def changed():
        pass

    async def scenario():
        c = TestClient(TestServer(build_portal(cp.db, cp.box, cp.settings, changed)))
        await c.start_server()
        try:
            await c.post("/signup", data={"email": "s@x.io", "password": "password1234"})
            cid = cp.db.one("SELECT id FROM customers WHERE email = 's@x.io'")["id"]
            cp.db.run("INSERT OR REPLACE INTO subscriptions (customer_id, status, updated_at) VALUES (?, 'active', 1)",
                      (cid,))
            dash = await (await c.get("/dashboard")).text()
            await c.post("/servers", data={"csrf": csrf_of(dash), "name": "Kryvon"})
            dash = await (await c.get("/dashboard")).text()
            assert "website <span class=muted" in dash and "(free)" in dash and "Get your own domain – €3.99" in dash
            r = await c.post("/billing/addon/domain/on", data={"csrf": csrf_of(dash)}, allow_redirects=False)
            assert "Choose+a+package+first" in r.headers["Location"] and calls == []
            cp.db.run("UPDATE subscriptions SET stripe_subscription_id = 'sub_9' WHERE customer_id = ?", (cid,))
            await c.post("/billing/addon/domain/on", data={"csrf": csrf_of(dash)})
            assert calls == [("sub_9", "price_dom", True)]
        finally:
            await c.close()
    asyncio.run(scenario())


def test_website_owner_choices(pb):  # noqa: F811
    async def main():
        await pb.m.load_all()
        await pb.m.set_enabled("website", True)
        site = pb.m.running("website")
        page = site.home("")
        assert "shape-angled font-bold hero-side" in page and 'class="ann"' not in page and ' has-bg"' not in page
        assert site.render("", "info", {})[0] == 404
        await pb.m.save_settings("website", {
            "shape": "rounded", "font": "mono", "hero_layout": "centered", "accent2": "#00e5ff",
            "announcement": "Wipe on <b>Thursday</b>", "footer_text": "Not affiliated with Facepunch.",
            "extra_links": "TikTok | https://www.tiktok.com/@kryvon\nBad | javascript:alert(1)\nPlain | http://x.io\n"
                           "Vote | https://vote.example/kryvon?x=1",
            "page_title": "VIP", "page_text": "# Perks\nQueue skip and a kit.\n\nAsk <admins> on Discord.",
            "show_tiles": False, "show_hall": False, "show_app": False})
        await pb.m.save_file("website", "background", _png((5, 5, 5)))
        page = site.home("")
        assert 'shape-rounded font-mono hero-centered has-bg"' in page and "--orange:#00e5ff" in page
        assert "/background?v=" in page and "Wipe on &lt;b&gt;Thursday&lt;/b&gt;" in page
        assert 'href="https://www.tiktok.com/@kryvon"' in page and "https://vote.example/kryvon?x=1" in page
        assert "javascript:" not in page and "http://x.io" not in page          # only https links
        assert "Not affiliated with Facepunch." in page and 'class="sec">Explore' not in page
        assert 'href="/info"' in page and ">VIP<" in page
        code, info = site.render("", "info", {})
        assert code == 200 and "<h2>Perks</h2>" in info and "Ask &lt;admins&gt; on Discord." in info
        await pb.m.save_settings("website", {"accent2": "red; background:url(x)", "shape": "angled"})
        assert "background:url(x)" not in site.home("")                           # not a hex colour: ignored
    asyncio.run(main())


def test_website_live_bits(pb):  # noqa: F811
    import time

    async def main():
        await pb.m.load_all()
        await pb.m.set_enabled("website", True)
        site = pb.m.running("website")
        page = site.home("")
        assert "/site.js?v=" in page and 'data-base=""' in page and 'property="og:title"' in page
        assert "Players today" not in page and "Online now" not in page          # no data yet: no empty boxes
        now = int(time.time())
        for i in range(30):
            pb.db.add_population_sample(now - 86000 + i * 2800, 10 + (i * 7) % 40, 100, 0)
        pb.bot.online_players = {"Bob<script>": {}, "Ann": {}}
        site._status = None
        page = site.home("")
        assert "Players today" in page and "<polyline" in page and "peak 4" in page
        assert "Online now" in page and "Bob&lt;script&gt;" in page and "<script>" not in page.split("</head>")[1]
        cells = site.countdown(now + 90061)
        assert f'data-until="{now + 90061}"' in cells and '<b data-u="d">1</b>' in cells and '<b data-u="h">01</b>' in cells
        await pb.m.save_settings("website", {"show_chart": False, "show_online": False})
        page = site.home("")
        assert "Players today" not in page and "Online now" not in page
        code, lost = site.render("", "nope", {})
        assert code == 404 and 'class="code">404' in lost
    asyncio.run(main())
