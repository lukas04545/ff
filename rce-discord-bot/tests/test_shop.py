"""The shop: packages decide slots and add-ons, Stripe events apply them, the portal sells them, the owner edits them."""
import asyncio
import re
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from control_plane import billing, features, plans, provisioning, slots
from tests.test_control_plane import cp, csrf_of, customer  # noqa: F401


def _plan(cp, slug):  # noqa: F811
    plans.seed(cp.db)
    return plans.get(cp.db, slug=slug)


def test_package_sets_slots_and_addons_but_keeps_what_the_owner_gave(cp):  # noqa: F811
    cid = customer(cp, own_bot=False)
    starter, pro, network = _plan(cp, "starter"), _plan(cp, "pro"), _plan(cp, "network")
    features.grant(cp.db, cid, "custom_bot", owner="boss")                      # given by the owner by hand
    plans.apply(cp.db, cid, network["id"], actor="t")
    assert slots.slot_limit(cp.db, cid) == 5 and features.active(cp.db, cid) == {"ai", "custom_bot", "custom_domain"}
    plans.apply(cp.db, cid, starter["id"], actor="t")
    assert slots.slot_limit(cp.db, cid) == 1
    assert features.active(cp.db, cid) == {"custom_bot"}                       # AI was the package's, own bot the owner's
    assert plans.current(cp.db, cid)["slug"] == "starter"
    plans.apply(cp.db, cid, pro["id"], actor="t")
    assert features.active(cp.db, cid) == {"ai", "custom_bot"} and slots.slot_limit(cp.db, cid) == 2


def test_stripe_events_apply_the_package(cp):  # noqa: F811
    cid = customer(cp, own_bot=False)
    pro, starter = _plan(cp, "pro"), _plan(cp, "starter")
    cp.db.run("UPDATE plans SET stripe_price_id = 'price_starter' WHERE id = ?", (starter["id"],))
    cp.db.run("UPDATE customers SET base_slots = 3 WHERE id = ?", (cid,))
    for name in ("Alpha", "Beta"):
        provisioning.provision_server(cp.db, cp.box, cp.settings, cid, name, actor="t")
    ev = {"id": "e1", "type": "checkout.session.completed",
          "data": {"object": {"client_reference_id": str(cid), "customer": "cus_9", "subscription": "sub_9",
                              "metadata": {"plan_id": str(pro["id"])}}}}
    billing.handle_event(cp.db, ev)
    assert billing.subscription_ok(cp.db, cid) and plans.current(cp.db, cid)["slug"] == "pro"
    assert "ai" in features.active(cp.db, cid) and slots.slot_limit(cp.db, cid) == 2
    down = {"id": "e2", "type": "customer.subscription.updated",
            "data": {"object": {"customer": "cus_9", "id": "sub_9", "status": "active",
                                "items": {"data": [{"price": {"id": "price_starter"}}]}}}}
    billing.handle_event(cp.db, down)
    assert plans.current(cp.db, cid)["slug"] == "starter" and "ai" not in features.active(cp.db, cid)
    states = [r["status"] for r in cp.db.q("SELECT status FROM servers WHERE customer_id = ? ORDER BY id", (cid,))]
    assert states.count("over_limit") == 1                                     # one server too many – kept, stopped


def test_owner_edits_packages_and_validates(cp):  # noqa: F811
    plans.seed(cp.db)
    pid = plans.save(cp.db, {"name": "Mega", "price": "49,99", "slots": "10", "ai": "1", "active": "1",
                             "perks": "10 servers\n\nEverything"}, actor="t")
    p = plans.get(cp.db, pid)
    assert (p["slug"], p["price_cents"], p["slots"], p["ai"], p["custom_bot"], p["perks"]) == \
        ("mega", 4999, 10, 1, 0, "10 servers\nEverything")
    assert plans.price_text(p) == "€49.99"
    for bad in ({"name": "x", "price": "5"}, {"name": "Ok", "price": "abc"}, {"name": "Ok", "price": "0.1"},
                {"name": "Ok", "price": "5", "slots": "0"}, {"name": "Ok", "price": "5", "currency": "btc"}):
        with pytest.raises(plans.PlanError):
            plans.save(cp.db, bad, actor="t")
    plans.save(cp.db, {"name": "Mega", "price": "39", "slots": "10"}, actor="t", plan_id=pid)    # unticked = off
    assert plans.get(cp.db, pid)["active"] == 0 and "mega" not in [x["slug"] for x in plans.listed(cp.db)]
    assert asyncio.run(plans.ensure_stripe_price(cp.db, cp.settings, pid)) is None              # no Stripe key


def test_shop_signup_with_a_package_goes_to_stripe(cp, monkeypatch):  # noqa: F811
    from control_plane.web import build_portal
    cp.settings.shop_name = "Kryvon Bot"
    calls = []

    async def fake_price(db, settings, plan_id):
        return f"price_{plan_id}"

    async def fake_checkout(settings, c, ok, cancel, *, price_id="", plan_id=None):
        calls.append((c["email"], price_id, plan_id))
        return "https://checkout.stripe.com/c/pay/test"
    monkeypatch.setattr(plans, "ensure_stripe_price", fake_price)
    monkeypatch.setattr(billing, "checkout_url", fake_checkout)

    async def changed():
        pass

    async def scenario():
        c = TestClient(TestServer(build_portal(cp.db, cp.box, cp.settings, changed)))
        await c.start_server()
        try:
            home = await (await c.get("/")).text()
            assert "Kryvon" in home and "MOST POPULAR" in home and "€14.99" in home and "/signup?plan=pro" in home
            page = await (await c.get("/signup?plan=pro")).text()
            assert "Pro</b>" in page and "Continue to payment" in page
            r = await c.post("/signup", data={"email": "new@x.io", "password": "password1234", "plan": "pro"},
                             allow_redirects=False)
            assert r.headers["Location"] == "https://checkout.stripe.com/c/pay/test"
            pro = plans.get(cp.db, slug="pro")
            assert calls == [("new@x.io", f"price_{pro['id']}", pro["id"])]
            dash = await (await c.get("/dashboard")).text()                    # signed in; step 1 = package
            assert "Get started" in dash and "Choose your package" in dash and "class='step now'" in dash
            r = await c.post("/billing/checkout", data={"csrf": csrf_of(dash), "plan": str(pro["id"])},
                             allow_redirects=False)
            assert r.headers["Location"].startswith("https://checkout.stripe.com")
            assert (await c.post("/billing/checkout", data={"plan": str(pro["id"])})).status == 403   # CSRF
            # paid (webhook) -> step 2: create the server
            cid = cp.db.one("SELECT id FROM customers WHERE email = 'new@x.io'")["id"]
            cp.db.run("INSERT OR REPLACE INTO subscriptions (customer_id, status, updated_at, plan_id) "
                      "VALUES (?, 'active', 1, ?)", (cid, pro["id"]))
            plans.apply(cp.db, cid, pro["id"], actor="t")
            dash = await (await c.get("/dashboard")).text()
            assert "Pro</b> · €14.99" in dash and "Create your server" in dash
            r = await c.post("/servers", data={"csrf": csrf_of(dash), "name": "Kryvon EU"})
            text = await r.text()
            assert "Panel password" in text and "Add the bot to Discord" in text
            dash = await (await c.get("/dashboard")).text()
            assert re.search(r"step done.*step done.*step now", dash.replace("\n", " ")) and "/discord" in dash
        finally:
            await c.close()
    asyncio.run(scenario())


def test_owner_packages_page_and_customer_package(cp):  # noqa: F811
    from control_plane.web import build_owner
    from web.totp import code_at, new_secret
    from web.users import hash_password
    cid = customer(cp, own_bot=False)
    secret = new_secret()
    cp.db.run("INSERT INTO owners (username, password_hash, totp_secret) VALUES ('boss', ?, ?)",
              (hash_password("owner-password-123"), secret))

    async def changed():
        pass

    async def scenario():
        c = TestClient(TestServer(build_owner(cp.db, cp.box, cp.settings, changed)))
        await c.start_server()
        try:
            await c.post("/login", data={"username": "boss", "password": "owner-password-123",
                                         "code": code_at(secret, int(time.time() // 30))})
            page = await (await c.get("/plans")).text()
            assert "Starter" in page and "Network" in page and "New package" in page
            t = csrf_of(page)
            r = await c.post("/plans", data={"csrf": t, "name": "Duo", "price": "11", "slots": "2", "active": "1",
                                             "perks": "2 servers"})
            assert "Saved" in await r.text()
            duo = plans.get(cp.db, slug="duo")
            await c.post(f"/customers/{cid}/plan", data={"csrf": t, "plan": str(duo["id"]), "comp": "1"})
            assert plans.current(cp.db, cid)["slug"] == "duo" and slots.slot_limit(cp.db, cid) == 2
            assert billing.subscription_ok(cp.db, cid)                         # comped: runs without Stripe
            assert "Duo" in await (await c.get(f"/customers/{cid}")).text()
        finally:
            await c.close()
    asyncio.run(scenario())
