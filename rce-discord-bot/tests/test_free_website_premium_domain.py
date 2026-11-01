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
