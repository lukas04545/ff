"""Web store: kits for sale, own custom kits, gift cards, PayPal checkout (faked), delivery, website + admin API."""
import asyncio
import re
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from aiohttp.test_utils import TestClient, TestServer

from support.paypal import PayPalClient, money
from tests.test_kit_requests import env  # noqa: F401

H = {"X-Requested-With": "rce-web"}
PRICES = """currency EUR
rifle.ak = 5 / 1 with metal.facemask
ammo.rifle = 1 / 250
cap 1000: ammo.rifle
timer 30h = 0
timer 12h = 3
"""


class FakePayPal:
    def __init__(self):
        self.orders, self.paid, self.state = {}, {}, {}

    async def create_order(self, *, amount, currency, description, reference, return_url, cancel_url, brand=""):
        oid = f"PP{len(self.orders) + 1}"
        self.orders[oid] = {"amount": amount, "currency": currency, "return": return_url, "cancel": cancel_url}
        return oid, f"https://www.sandbox.paypal.com/checkoutnow?token={oid}"

    async def capture(self, order_id, *, amount, currency):
        paid = self.paid.get(order_id, self.orders[order_id]["amount"])
        data = {"status": "COMPLETED", "payer": {"email_address": "buyer@example.com"},
                "purchase_units": [{"payments": {"captures": [{"id": f"CAP{order_id}", "status": "COMPLETED",
                                                               "amount": {"currency_code": currency,
                                                                          "value": money(paid, currency)}}]}}]}
        return PayPalClient.check(data, amount=amount, currency=currency)

    async def get_order(self, order_id):
        return {"status": self.state.get(order_id, "CREATED")}


@pytest.fixture
def shop(env, monkeypatch, tmp_path):
    from cogs.webshop import WebShop
    (tmp_path / "prices.txt").write_text(PRICES)
    cfg = replace(env.bot.config, guild_id=1, paypal_client_id="id", paypal_secret="sec", paypal_mode="sandbox",
                  store_currency="EUR", store_giftcard_values=(10.0, 25.0), price_list_file=tmp_path / "prices.txt")
    env.bot.config = cfg
    guild = env.buyer.guild
    monkeypatch.setattr(env.bot, "get_guild", lambda gid: guild)
    dms = []

    async def send(embed=None, **kw):
        dms.append(embed.description)
    monkeypatch.setattr(env.bot, "get_user", lambda uid: NS(id=uid, send=send))

    async def no_loop(self):
        return None
    monkeypatch.setattr(WebShop, "cog_load", no_loop)
    s = WebShop(env.bot)
    asyncio.run(env.bot.add_cog(s))
    fake = FakePayPal()
    s._paypal = ((cfg.paypal_client_id, cfg.paypal_secret, cfg.paypal_mode), fake)
    env.db.link_player(7, "Gamer7")
    kit_id = env.db.add_store_kit("AK kit", "Ak Kit", 9.99, 0, "An AK")
    return NS(s=s, env=env, db=env.db, pp=fake, kit_id=kit_id, dms=dms)


def run(coro):
    return asyncio.run(coro)


def test_paypal_money_and_check():
    assert money(9.999, "EUR") == "10.00" and money(5, "JPY") == "5" and money("1.5", "usd") == "1.50"
    ok = PayPalClient.check({"purchase_units": [{"payments": {"captures": [
        {"id": "C1", "status": "COMPLETED", "amount": {"currency_code": "EUR", "value": "9.99"}}]}}]},
        amount=9.99, currency="EUR")
    assert ok.ok and ok.capture_id == "C1"
    low = PayPalClient.check({"purchase_units": [{"payments": {"captures": [
        {"id": "C2", "status": "COMPLETED", "amount": {"currency_code": "EUR", "value": "0.99"}}]}}]},
        amount=9.99, currency="EUR")
    assert not low.ok and "expected 9.99 EUR" in low.problem
    pending = PayPalClient.check({"status": "APPROVED"}, amount=1, currency="EUR")
    assert not pending.ok and pending.status == "APPROVED"


def test_kit_paypal_checkout_delivers_once(shop):
    from cogs.webshop import CheckoutError
    s, db = shop.s, shop.db
    with pytest.raises(CheckoutError, match="isn't linked"):
        run(s.checkout("kit", base_url="https://x/play", kit_id=shop.kit_id, player="Nobody"))
    with pytest.raises(CheckoutError, match="isn't for sale"):
        run(s.checkout("kit", base_url="https://x/play", kit_id=999, player="Gamer7"))
    r = run(s.checkout("kit", base_url="https://x/play", kit_id=shop.kit_id, player="gamer7"))
    assert r["redirect"].startswith("https://www.sandbox.paypal.com/") and shop.pp.orders["PP1"]["amount"] == 9.99
    assert shop.pp.orders["PP1"]["return"] == f"https://x/play/store/return?o={r['token']}"
    order = db.store_order_by("token", r["token"])
    assert order["status"] == "pending" and order["player"] == "Gamer7" and order["user_id"] == 7
    assert shop.env.buyer.roles == []                                      # nothing before the payment
    done = run(s.complete(r["token"]))
    assert done["status"] == "delivered" and done["capture_id"] == "CAPPP1" and done["payer_email"] == "buyer@example.com"
    assert shop.env.existing in shop.env.buyer.roles                       # got the kit role
    req = db.kit_request(done["request_id"])
    assert req["status"] == "approved" and req["source"] == "store" and "CAPPP1" in req["proof"]
    assert "/customkit claim Ak Kit" in shop.dms[-1]
    assert run(s.complete(r["token"]))["status"] == "delivered"             # returning again: nothing twice
    assert len(db.kit_requests(user_id=7)) == 1


def test_wrong_amount_is_not_delivered(shop):
    s = shop.s
    r = run(s.checkout("kit", base_url="https://x/play", kit_id=shop.kit_id, player="Gamer7"))
    shop.pp.paid["PP1"] = 0.99                                            # PayPal reports less than the price
    order = run(s.complete(r["token"]))
    assert order["status"] == "review" and "expected 9.99" in order["result"] and shop.env.buyer.roles == []


def test_gift_cards_pay_all_or_part(shop):
    from cogs.webshop import CheckoutError
    s, db = shop.s, shop.db
    code = s.create_gift_cards(20, 1, "owner")[0]
    assert len(code) == 19 and code.count("-") == 3
    # Full payment with a gift card: no PayPal at all
    r = run(s.checkout("kit", base_url="https://x/play", kit_id=shop.kit_id, player="Gamer7",
                       gift_code=code.lower().replace("-", " ")))
    assert "redirect" not in r and db.store_order_by("token", r["token"])["status"] == "delivered"
    assert db.gift_card(code)["balance"] == 10.01 and not shop.pp.orders
    # Partial: the rest with PayPal; cancelling returns the held amount
    db.update_store_kit(shop.kit_id, price=30)
    r = run(s.checkout("kit", base_url="https://x/play", kit_id=shop.kit_id, player="Gamer7", gift_code=code))
    order = db.store_order_by("token", r["token"])
    assert order["gift_amount"] == 10.01 and order["paypal_amount"] == 19.99 and db.gift_card(code)["balance"] == 0
    assert run(s.cancel(r["token"]))["status"] == "cancelled" and db.gift_card(code)["balance"] == 10.01
    # Disabled / expired / wrong codes
    db.set_gift_card_disabled(code, True)
    with pytest.raises(CheckoutError, match="disabled"):
        s.gift_card(code)
    for _ in range(10):
        with pytest.raises(CheckoutError, match="doesn't exist"):
            s.gift_card("AAAA-BBBB-CCCC-DDDD", "1.2.3.4")
    with pytest.raises(CheckoutError, match="Too many"):
        s.gift_card(code, "1.2.3.4")
    with pytest.raises(CheckoutError):
        s.create_gift_cards(0, 1, "owner")


def test_combine_gift_cards(shop):
    """Players move the balance of several gift cards onto one (website + /giftcard combine)."""
    import time as _t
    from cogs.webshop import CheckoutError
    s, db = shop.s, shop.db
    a, b, c = s.create_gift_cards(10, 1, "owner")[0], s.create_gift_cards(5, 1, "owner")[0], \
        s.create_gift_cards(2.5, 1, "owner", days=30)[0]
    r = run(s.combine_gift_cards(a, [b.lower().replace("-", ""), c, a, ""]))     # any spelling; self/blank ignored
    assert r["count"] == 2 and r["moved"] == 7.5 and r["balance"] == 17.5
    assert db.gift_card(a)["balance"] == 17.5 and db.gift_card(b)["balance"] == 0 and db.gift_card(c)["balance"] == 0
    assert db.gift_card(a)["expires_at"] == db.gift_card(c)["expires_at"] == r["expires_at"]   # earliest expiry kept
    assert "merged into" in db.gift_card(b)["note"]
    # The combined card pays a 9.99 kit; a cancelled partial payment returns its held amount fully
    db.update_store_kit(shop.kit_id, price=30)
    t = run(s.checkout("kit", base_url="https://x/play", kit_id=shop.kit_id, player="Gamer7", gift_code=a))
    assert db.gift_card(a)["balance"] == 0
    run(s.cancel(t["token"]))
    assert db.gift_card(a)["balance"] == 17.5                                     # initial raised with the merge
    # Errors: nothing to move, used-up / unknown / disabled cards; all-or-nothing
    with pytest.raises(CheckoutError, match="at least one"):
        run(s.combine_gift_cards(a, [a]))
    with pytest.raises(CheckoutError, match="used up"):
        run(s.combine_gift_cards(a, [b]))
    d = s.create_gift_cards(4, 1, "owner")[0]
    with pytest.raises(CheckoutError, match="doesn't exist"):
        run(s.combine_gift_cards(a, [d, "AAAA-BBBB-CCCC-DDDD"]))
    assert db.gift_card(d)["balance"] == 4 and db.gift_card(a)["balance"] == 17.5   # nothing moved
    db.set_gift_card_disabled(d, True)
    with pytest.raises(CheckoutError, match="disabled"):
        run(s.combine_gift_cards(a, [d]))
    # An empty card can be filled up; expired cards can't be moved
    e = s.create_gift_cards(3, 1, "owner")[0]
    run(s.combine_gift_cards(b, [e]))
    assert db.gift_card(b)["balance"] == 3
    f = s.create_gift_cards(1, 1, "owner")[0]
    db._db.execute("UPDATE gift_cards SET expires_at = ? WHERE code = ?", (int(_t.time()) - 5, f)); db._db.commit()
    with pytest.raises(CheckoutError, match="expired"):
        run(s.combine_gift_cards(a, [f]))
    with pytest.raises(CheckoutError, match="At most"):
        run(s.combine_gift_cards(a, [f"AAAA-BBBB-CCCC-{i:04d}" for i in range(11)]))


def test_combine_gift_cards_on_the_website(shop):
    from web.store_pages import handle
    from types import SimpleNamespace as NSS
    s, db = shop.s, shop.db
    a, b = s.create_gift_cards(10, 1, "owner")[0], s.create_gift_cards(5, 1, "owner")[0]

    class Site:
        def page(self, base, active, title, body):
            return f"<html><style></style>{body}</html>"

        def not_found(self, base):
            return "nf"

    async def post():
        return {"target": a, "codes": f"{b}\n"}
    req = NSS(method="POST", query={}, post=post, headers={}, remote="1.2.3.4", scheme="https", host="x")
    status, html = run(handle(Site(), s, req, "/play", "store/giftcards/combine", "1.2.3.4"))
    assert status == 200 and f"New balance: {s.fmt(15)}" in html and db.gift_card(b)["balance"] == 0

    async def bad():
        return {"target": a, "codes": "nope"}
    req.post = bad
    status, html = run(handle(Site(), s, req, "/play", "store/giftcards/combine", "1.2.3.4"))
    assert status == 400 and "doesn&#x27;t exist" in html and "Combine gift cards" in html
    status, html = run(handle(Site(), s, NSS(method="GET", query={}, headers={}, remote="1.2.3.4"), "/play",
                              "store/giftcards", "1.2.3.4"))
    assert "Combine gift cards" in html and 'action="/play/store/giftcards/combine"' in html


def test_custom_kit_and_gift_card_purchase(shop):
    from cogs.webshop import CheckoutError
    s, db = shop.s, shop.db
    q, items = s.price_custom({"rifle.ak": "1", "metal.facemask": 1, "ammo.rifle": 300, "x": "abc"})
    assert q.total == 7.0 and q.timer_hours == 30 and {i["short"] for i in items} == {"rifle.ak", "metal.facemask",
                                                                                         "ammo.rifle"}
    assert s.price_custom({"rifle.ak": 1}, 12)[0].total == 8.0
    with pytest.raises(CheckoutError, match="limits"):
        s.price_custom({"ammo.rifle": 5000})
    with pytest.raises(CheckoutError, match="can't be bought"):
        s.price_custom({"rocket.launcher": 1})
    with pytest.raises(CheckoutError, match="at least one"):
        s.price_custom({"rifle.ak": 0})
    r = run(s.checkout("custom", base_url="https://x/play", items={"rifle.ak": 1, "ammo.rifle": 300},
                       timer_hours=12, player="Gamer7", name=' My "Raid" Kit '))
    assert shop.pp.orders["PP1"]["amount"] == 10.0
    order = db.store_order_by("token", r["token"])
    assert order["kit_name"] == "My Raid Kit" and order["title"] == "Custom kit: My Raid Kit"   # buyer's name, cleaned
    order = run(s.complete(r["token"]))
    assert order["status"] == "delivered" and order["result"] == "My Raid Kit"
    kit = db.custom_kit("My Raid Kit")
    assert kit["cooldown_minutes"] == 720
    assert {i["shortname"]: i["amount"] for i in db.custom_kit_items("My Raid Kit")} == {"rifle.ak": 1, "ammo.rifle": 300}
    # No name given -> falls back to the player's name; a clash gets a number
    db.create_custom_kit("Gamer7 kit", None, "x")
    r2 = run(s.checkout("custom", base_url="https://x/play", items={"rifle.ak": 1}, player="Gamer7"))
    assert db.store_order_by("token", r2["token"])["kit_name"] is None
    assert run(s.complete(r2["token"]))["result"] == "Gamer7 kit 2"
    # Gift cards for sale
    with pytest.raises(CheckoutError, match="isn't available"):
        run(s.checkout("giftcard", base_url="https://x/play", value="11"))
    r = run(s.checkout("giftcard", base_url="https://x/play", value="25"))
    order = run(s.complete(r["token"]))
    code = order["result"].removeprefix("Gift card ")
    card = db.gift_card(code)
    assert order["status"] == "delivered" and card["balance"] == 25 and card["order_id"] == order["id"]
    with pytest.raises(CheckoutError, match="can't be paid with a gift card"):
        run(s.checkout("giftcard", base_url="https://x/play", value="10", gift_code=code))


def test_background_check_retry_and_expiry(shop, monkeypatch):
    s, db = shop.s, shop.db
    code = s.create_gift_cards(5, 1, "owner")[0]
    r1 = run(s.checkout("kit", base_url="https://x/play", kit_id=shop.kit_id, player="Gamer7"))       # PP1
    r2 = run(s.checkout("kit", base_url="https://x/play", kit_id=shop.kit_id, player="Gamer7", gift_code=code))  # PP2
    shop.pp.state["PP1"] = "APPROVED"                 # paid, but closed the page before returning
    o1 = db.store_order_by("token", r1["token"])
    run(s.check_pending(now=o1["created_at"] + 11 * 60))
    assert db.store_order_by("token", r1["token"])["status"] == "delivered"
    assert db.store_order_by("token", r2["token"])["status"] == "pending"
    run(s.check_pending(now=o1["created_at"] + 4 * 3600))
    assert db.store_order_by("token", r2["token"])["status"] == "expired" and db.gift_card(code)["balance"] == 5
    # Delivery fails (buyer left the Discord) -> failed; staff fixes it and retries
    guild = shop.env.buyer.guild
    member = guild.get_member
    guild.get_member = lambda uid: None

    async def gone(uid):
        import discord
        raise discord.NotFound(NS(status=404, reason="x"), "gone")
    guild.fetch_member = gone
    r3 = run(s.checkout("kit", base_url="https://x/play", kit_id=shop.kit_id, player="Gamer7"))
    order = run(s.complete(r3["token"]))
    assert order["status"] == "failed" and "no longer on the Discord" in order["result"]
    guild.get_member = member
    assert run(s.retry(order["id"])) is True and db.store_order(order["id"])["status"] == "delivered"
    assert len([r for r in db.kit_requests(user_id=7) if r["status"] == "approved"]) == 2


def test_website_store_and_admin_api(shop, monkeypatch):
    from plugin_api import Plugin
    from plugin_manager import PluginManager
    bot, db = shop.env.bot, shop.db
    monkeypatch.setattr(Plugin, "_loops", lambda self: [])
    bot.config = replace(bot.config, web_password="secret123")

    async def main():
        from web.server import WebInterface
        bot.plugins = PluginManager(bot)
        await bot.plugins.load_all()
        await bot.plugins.set_enabled("website", True)
        tmp = bot.config.database_path.parent
        web = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            page = await (await c.get("/store")).text()
            assert "AK kit" in page and "9.99" in page and 'action="/store/checkout"' in page
            r = await c.get("/store")
            assert "https://www.sandbox.paypal.com" in r.headers["Content-Security-Policy"]
            assert "Test mode" in page
            r = await c.post("/store/checkout", data={"kind": "kit", "kit": str(shop.kit_id), "player": "Nobody"})
            assert r.status == 400 and "isn&#x27;t linked" in await r.text()
            r = await c.post("/store/checkout", data={"kind": "kit", "kit": str(shop.kit_id), "player": "Gamer7"},
                             allow_redirects=False)
            assert r.status == 303 and r.headers["Location"].startswith("https://www.sandbox.paypal.com/")
            token = db.store_orders()[0]["token"]
            r = await c.get(f"/store/return?o={token}", allow_redirects=False)
            assert r.status == 303 and r.headers["Location"] == f"/store/order/{token}"
            page = await (await c.get(f"/store/order/{token}")).text()
            assert "Done!" in page and "/customkit claim Ak Kit" in page
            page = await (await c.get("/store/build?q.rifle.ak=1&q.ammo.rifle=250")).text()
            assert "6.00" in page and 'name="q.rifle.ak" value="1"' in page and 'name="name"' in page
            page = await (await c.get("/store/giftcards")).text()
            assert "25.00" in page
            assert (await c.get("/store/order/nope")).status == 404
            # Admin API (owner only)
            assert (await c.get("/api/webshop")).status == 401
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            d = await (await c.get("/api/webshop")).json()
            assert d["status"]["paypal"] and d["orders"][0]["status"] == "delivered"
            assert any(k["name"] == "AK kit" and k["price"] == 9.99 for k in d["kits"])
            r = await c.post("/api/webshop/kits", json={"name": "Raid", "kit": "nope", "price": 5}, headers=H)
            assert r.status == 400
            r = await c.post("/api/webshop/kits", json={"name": "Raid", "kit": "mega raid kit", "price": "12.5",
                                                        "days": 30}, headers=H)
            kit_id = (await r.json())["id"]
            assert db.store_kit(kit_id)["kit"] == "Mega raid kit" and db.store_kit(kit_id)["days"] == 30
            assert (await c.put(f"/api/webshop/kits/{kit_id}", json={"enabled": False}, headers=H)).status == 200
            assert not db.store_kit(kit_id)["enabled"]
            r = await c.post("/api/webshop/giftcards", json={"amount": 15, "count": 3, "days": 30, "note": "prize"},
                             headers=H)
            codes = (await r.json())["codes"]
            assert len(codes) == 3 and db.gift_card(codes[0])["expires_at"] and db.gift_card(codes[0])["note"] == "prize"
            r = await c.post(f"/api/webshop/giftcards/{codes[0]}/toggle", json={"disabled": True}, headers=H)
            assert r.status == 200 and db.gift_card(codes[0])["disabled"]
            assert (await c.delete(f"/api/webshop/kits/{kit_id}", headers=H)).status == 200
        finally:
            await c.close()
            await bot.rcon.close()
    asyncio.run(main())


def test_checkout_rate_limit_and_masked_codes(shop):
    from cogs.webshop import CheckoutError
    s, db = shop.s, shop.db
    code = s.create_gift_cards(50, 1, "owner")[0]
    r = run(s.checkout("kit", base_url="https://x/play", kit_id=shop.kit_id, player="Gamer7", gift_code=code,
                       ip="9.9.9.9"))
    req = db.kit_request(db.store_order_by("token", r["token"])["request_id"])
    assert code not in req["proof"] and code[:4] in req["proof"]           # staff logs don't show full codes
    for _ in range(9):
        run(s.checkout("giftcard", base_url="https://x/play", value="10", ip="9.9.9.9"))
    with pytest.raises(CheckoutError, match="Too many orders"):
        run(s.checkout("giftcard", base_url="https://x/play", value="10", ip="9.9.9.9"))
    run(s.checkout("giftcard", base_url="https://x/play", value="10", ip="8.8.8.8"))   # other visitors are fine


def test_admin_key_bypasses_payment(shop, monkeypatch):
    from cogs.webshop import CheckoutError
    from settings_schema import validate
    s, db, bot = shop.s, shop.db, shop.env.bot
    with pytest.raises(CheckoutError, match="isn't enabled"):                # off by default
        run(s.checkout("kit", base_url="https://x/play", kit_id=shop.kit_id, player="Gamer7", admin_key="anything"))
    bot.config = replace(bot.config, store_admin_key="staff-only-key-123", paypal_client_id="")
    s._paypal = None                                                       # works without PayPal too
    alerts = []
    monkeypatch.setattr(bot, "audit", alerts.append)
    r = run(s.checkout("kit", base_url="https://x/play", kit_id=shop.kit_id, player="Gamer7",
                       admin_key=" staff-only-key-123 ", ip="5.5.5.5"))
    order = db.store_order_by("token", r["token"])
    assert "redirect" not in r and order["status"] == "delivered" and order["admin_key"] == 1
    assert order["paypal_amount"] == 0 and order["gift_amount"] == 0 and shop.env.existing in shop.env.buyer.roles
    assert any("🔑" in a and "FREE with the admin key" in a for a in alerts)
    r = run(s.checkout("giftcard", base_url="https://x/play", value="25", admin_key="staff-only-key-123"))
    assert db.store_order_by("token", r["token"])["status"] == "delivered"
    for _ in range(5):
        with pytest.raises(CheckoutError, match="Wrong admin key"):
            run(s.checkout("kit", base_url="https://x/play", kit_id=shop.kit_id, player="Gamer7",
                           admin_key="guess", ip="6.6.6.6"))
    with pytest.raises(CheckoutError, match="Too many wrong admin keys"):
        run(s.checkout("kit", base_url="https://x/play", kit_id=shop.kit_id, player="Gamer7",
                       admin_key="staff-only-key-123", ip="6.6.6.6"))
    assert validate("STORE_ADMIN_KEY", "short") and validate("STORE_ADMIN_KEY", "long-enough-key") is None


def test_admin_key_on_the_website(shop, monkeypatch):
    from plugin_api import Plugin
    from plugin_manager import PluginManager
    bot = shop.env.bot
    monkeypatch.setattr(Plugin, "_loops", lambda self: [])
    bot.config = replace(bot.config, store_admin_key="staff-only-key-123")

    async def main():
        from web.server import WebInterface
        bot.plugins = PluginManager(bot)
        await bot.plugins.load_all()
        await bot.plugins.set_enabled("website", True)
        tmp = bot.config.database_path.parent
        web = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            assert 'name="admin_key"' not in await (await c.get("/store")).text()   # hidden normally
            page = await (await c.get("/store?admin=1")).text()
            assert 'name="admin_key"' in page and "Admin mode" in page and "store/build?admin=1" in page
            r = await c.post("/store/checkout", data={"kind": "kit", "kit": str(shop.kit_id), "player": "Gamer7",
                                                          "admin": "1", "admin_key": "wrong"})
            text = await r.text()
            assert r.status == 400 and "Wrong admin key" in text and "wrong" not in text.split("Wrong admin key")[1][:300]
            r = await c.post("/store/checkout", data={"kind": "kit", "kit": str(shop.kit_id), "player": "Gamer7",
                                                          "admin_key": "staff-only-key-123"}, allow_redirects=False)
            assert r.status == 303 and r.headers["Location"].startswith("https://www.sandbox.paypal.com/")  # no ?admin=1
            r = await c.post("/store/checkout", data={"kind": "kit", "kit": str(shop.kit_id), "player": "Gamer7",
                                                          "admin": "1", "admin_key": "staff-only-key-123"},
                             allow_redirects=False)
            assert r.status == 303 and r.headers["Location"].startswith("/store/order/")
            page = await (await c.get(r.headers["Location"])).text()
            assert "Done!" in page and "free – admin key" in page
        finally:
            await c.close()
            await bot.rcon.close()
    asyncio.run(main())


def test_kits_added_automatically_hidden_until_priced(shop, monkeypatch):
    from cogs import webshop as ws
    s, db, bot = shop.s, shop.db, shop.env.bot
    db.create_custom_kit("Gamer7 kit", "Bought by user 7 (request #3)", "store")     # a player's bought kit
    said = []

    async def command(cmd, **kw):
        said.append(cmd)
        return "kit info"
    monkeypatch.setattr(bot.rcon, "command", command)
    monkeypatch.setattr(type(bot.rcon), "connected", property(lambda self: True))

    async def names(**kw):
        return ["vip", "Starter"]
    monkeypatch.setattr(bot, "get_cog", lambda name, _orig=bot.get_cog: NS(kit_names=NS(get=names))
                        if name == "Kits" else _orig(name))
    monkeypatch.setattr(ws.kitparse, "parse_kit_info", lambda raw: [NS(short_name="rifle.ak", amount=1),
                                                                    NS(short_name="ammo.rifle", amount=128)])
    assert run(s.sync_kits()) == 3                       # Mega raid kit + 2 in-game kits (Ak Kit is already sold)
    rows = {(r["source"], r["kit"]): r for r in db.store_kits()}
    assert ("custom", "Gamer7 kit") not in rows and ("custom", "Mega raid kit") in rows
    vip = rows[("ingame", "vip")]
    assert vip["auto"] and not vip["enabled"] and vip["price"] == 0
    assert s.kit_items(vip) == [("rifle.ak", 1), ("ammo.rifle", 128)]
    assert [r["kit"] for r in db.store_kits(enabled_only=True)] == ["Ak Kit"]           # hidden until priced
    assert run(s.sync_kits()) == 0                                                      # nothing twice
    from cogs.webshop import CheckoutError
    with pytest.raises(CheckoutError, match="isn't for sale"):
        s.store_kit(vip["id"])
    # Removed by the owner: never added again
    assert s.remove_kit(rows[("ingame", "Starter")]["id"])
    assert run(s.sync_kits()) == 0 and ("ingame", "Starter") not in {(r["source"], r["kit"]) for r in db.store_kits()}

    # Buying an in-game kit: offline -> waits, given in-game when the buyer is online
    db.update_store_kit(vip["id"], price=4.5, enabled=1)
    bot.online_players = {}
    r = run(s.checkout("kit", base_url="https://x/play", kit_id=vip["id"], player="Gamer7"))
    order = run(s.complete(r["token"]))
    assert order["status"] == "waiting" and "next time you're on the server" in shop.dms[-1]
    assert not any(c.startswith("kit givetoplayer") for c in said)
    bot.online_players = {"Gamer7": {}}
    assert run(s.deliver_waiting()) == 1
    order = db.store_order(order["id"])
    assert order["status"] == "delivered" and order["result"] == "vip" and order["request_id"] is None
    assert 'kit givetoplayer "vip" "Gamer7"' in said and "given to you in-game" in shop.dms[-1]
    assert run(s.deliver_waiting()) == 0                                                # given once


def test_admin_sets_price_of_auto_kit(shop, monkeypatch):
    bot, db = shop.env.bot, shop.db
    bot.config = replace(bot.config, web_password="secret123")

    async def main():
        from web.server import WebInterface
        tmp = bot.config.database_path.parent
        web = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            d = await (await c.get("/api/webshop")).json()                 # opening the page starts a sync …
            assert d["syncing"] is True
            await asyncio.sleep(0.2)                                         # … in the background
            d = await (await c.get("/api/webshop")).json()
            mega = next(k for k in d["kits"] if k["kit"] == "Mega raid kit")
            assert mega["auto"] and mega["price"] == 0 and not mega["enabled"] and mega["items"] == 1
            r = await c.put(f"/api/webshop/kits/{mega['id']}", json={"enabled": True}, headers=H)
            assert r.status == 400 and "price first" in (await r.json())["error"]
            r = await c.put(f"/api/webshop/kits/{mega['id']}", json={"price": "19.99"}, headers=H)
            assert r.status == 200 and db.store_kit(mega["id"])["enabled"] == 1      # first price: on sale
            assert (await c.delete(f"/api/webshop/kits/{mega['id']}", headers=H)).status == 200
            d = await (await c.get("/api/webshop")).json()
            assert not any(k["kit"] == "Mega raid kit" for k in d["kits"])          # removed stays removed
        finally:
            await c.close()
            await bot.rcon.close()
    asyncio.run(main())


def test_giftcard_discord_commands(shop):
    s, db = shop.s, shop.db

    def interaction(uid, admin=False, owner_id=999):
        sent = []

        async def send_message(text=None, embed=None, ephemeral=False):
            sent.append(NS(text=text, embed=embed, ephemeral=ephemeral))
        class User:
            id, guild, guild_permissions = uid, NS(owner_id=owner_id), NS(administrator=admin)

            def __str__(self):
                return f"user{uid}"
        user = User()
        return NS(user=user, response=NS(send_message=send_message)), sent

    # Not an owner: refused, nothing created
    i, sent = interaction(50)
    run(s.gc_create.callback(s, i, 10.0, 2, 0, None))
    assert "Only owners" in sent[-1].text and sent[-1].ephemeral and not db.gift_cards()
    # Server owner / Administrator: codes only shown to them
    i, sent = interaction(999)
    run(s.gc_create.callback(s, i, 15.0, 3, 30, "prize"))
    codes = sent[-1].embed.description.strip("`\n").split("\n")
    assert sent[-1].ephemeral and len(codes) == 3 and db.gift_card(codes[0])["note"] == "prize"
    assert db.gift_card(codes[0])["created_by"] == "user999" and db.gift_card(codes[0])["expires_at"]
    i, sent = interaction(51, admin=True)
    run(s.gc_list.callback(s, i))
    assert codes[0] in sent[-1].embed.description and "15.00" in sent[-1].embed.description
    run(s.gc_disable.callback(s, i, codes[1].lower().replace("-", ""), True))
    assert db.gift_card(codes[1])["disabled"] and "disabled" in sent[-1].text
    # Anyone can check a balance
    i, sent = interaction(52)
    run(s.gc_check.callback(s, i, codes[0]))
    assert "€15.00 of €15.00" in sent[-1].text and "active" in sent[-1].text
    run(s.gc_check.callback(s, i, codes[1]))
    assert "disabled" in sent[-1].text
    run(s.gc_disable.callback(s, i, codes[0], True))
    assert "Only owners" in sent[-1].text and not db.gift_card(codes[0])["disabled"]


def test_kit_sync_is_quick(shop, monkeypatch):
    """Opening the Store page never waits for the game server; kit contents are read a few at a time."""
    from cogs import webshop as ws
    s, db, bot = shop.s, shop.db, shop.env.bot
    for i in range(12):
        db.add_store_kit(f"kit{i}", f"kit{i}", 0, 0, None, source="ingame", auto=True, enabled=False)
    asked = []

    async def command(cmd, **kw):
        asked.append(cmd)
        return ""
    monkeypatch.setattr(bot.rcon, "command", command)
    monkeypatch.setattr(type(bot.rcon), "connected", property(lambda self: True))
    monkeypatch.setattr(ws.kitparse, "parse_kit_info", lambda raw: [NS(short_name="wood", amount=1)])
    run(s._refresh_ingame_items())
    assert len(asked) == ws.ITEMS_PER_SYNC
    run(s._refresh_ingame_items())
    run(s._refresh_ingame_items())
    assert len(asked) == 12                                    # every kit read once …
    run(s._refresh_ingame_items())
    assert len(asked) == 12                                    # … and not again for hours

    async def slow_sync():
        await asyncio.sleep(10)

    async def main():
        monkeypatch.setattr(s, "sync_kits", slow_sync)
        assert s.start_sync() is True and s.start_sync() is True     # one at a time, returns at once
        s._sync_task.cancel()
    run(main())


def test_store_kit_categories(shop, monkeypatch):
    from plugin_api import Plugin
    from plugin_manager import PluginManager
    bot, db = shop.env.bot, shop.db
    monkeypatch.setattr(Plugin, "_loops", lambda self: [])
    bot.config = replace(bot.config, web_password="secret123")

    async def main():
        from web.server import WebInterface
        bot.plugins = PluginManager(bot)
        await bot.plugins.load_all()
        await bot.plugins.set_enabled("website", True)
        tmp = bot.config.database_path.parent
        web = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            page = await (await c.get("/store")).text()
            assert "AK kit" in page and 'class="cats"' not in page              # one group: no category bar
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            r = await c.post("/api/webshop/kits", json={"name": "Raid", "kit": "mega raid kit", "price": 20,
                                                        "category": "  Raid   kits "}, headers=H)
            raid = (await r.json())["id"]
            assert db.store_kit(raid)["category"] == "Raid kits"
            assert (await c.put(f"/api/webshop/kits/{shop.kit_id}", json={"category": "Weapons"},
                                headers=H)).status == 200
            third = db.add_store_kit("Starter", "Ak Kit", 2.5, 0, None)                # no category -> "Other kits"
            page = await (await c.get("/store")).text()
            assert '<a href="#cat-raid-kits">Raid kits (1)</a>' in page and '<a href="#cat-weapons">Weapons (1)</a>' in page
            heads = re.findall(r'<h2 class="cat-head" id="([^"]+)">([^<]+)</h2>', page)
            assert heads == [("cat-raid-kits", "Raid kits"), ("cat-weapons", "Weapons"), ("cat-other", "Other kits")]
            assert page.index("<h2>Raid</h2>") < page.index("<h2>AK kit</h2>") < page.index("<h2>Starter</h2>")
            assert (await c.put(f"/api/webshop/kits/{raid}", json={"category": ""}, headers=H)).status == 200
            assert db.store_kit(raid)["category"] is None and third
        finally:
            await c.close()
            await bot.rcon.close()
    asyncio.run(main())


def test_nuke_for_real_money_drops_on_discord(shop, monkeypatch):
    """A nuke bought in the web store (here with a gift card) drops in the nuke channel; everyone but the buyer
    claims the set points once."""
    from cogs.economy import Economy
    from cogs.webshop import CheckoutError
    from plugin_api import Plugin
    from plugin_manager import PluginManager
    import settings_schema
    bot, db, s = shop.env.bot, shop.db, shop.s
    nuke_fields = [f for f in settings_schema.FIELDS if f.key.startswith("STORE_NUKE_")]
    assert len(nuke_fields) == 4 and {f.group for f in nuke_fields} == {"nukes"}       # on the Store tab only,
    assert "nukes" not in dict(settings_schema.GROUPS)                                # not the Settings page

    async def no_loop(self):
        return None
    monkeypatch.setattr(Economy, "cog_load", no_loop)
    run(bot.add_cog(Economy(bot)))
    with pytest.raises(CheckoutError, match="aren't for sale"):
        run(s.checkout("nuke", base_url="https://x", player="Gamer7"))
    bot.config = replace(bot.config, store_nuke_price=4.99, store_nuke_points=50, store_nuke_claims=2,
                         store_nuke_channel_id=42, web_password="secret123")
    posted = []

    class Message:
        def __init__(self, embed, view):
            self.id, self.embed, self.view = 555, embed, view

        async def edit(self, embed=None, view=None):
            self.embed, self.view = embed, view

    class Channel:
        id, name, mention = 42, "nukes", "#nukes"

        async def send(self, embed=None, view=None, allowed_mentions=None):
            assert not allowed_mentions.everyone
            posted.append(Message(embed, view))
            return posted[-1]
    monkeypatch.setattr(bot, "get_channel", lambda cid: Channel() if cid == 42 else None)
    with pytest.raises(CheckoutError, match="isn't linked"):
        run(s.checkout("nuke", base_url="https://x", player="Nobody"))
    r = run(s.checkout("nuke", base_url="https://x", player="Gamer7"))              # PayPal first
    order = db.store_order_by("token", r["token"])
    assert "redirect" in r and order["total"] == 4.99 and order["title"] == "💣 Nuke – 50 points for the first 2 members"
    code = s.create_gift_cards(10, 1, "owner")[0]
    r = run(s.checkout("nuke", base_url="https://x", player="Gamer7", gift_code=code))
    order = db.store_order_by("token", r["token"])
    assert order["status"] == "delivered" and order["result"] == "Nuke dropped in #nukes"
    assert len(posted) == 1 and "<@7>" in posted[0].embed.description and "50" in posted[0].embed.description
    assert any("nuke dropped" in (d or "") for d in shop.dms)
    eco = bot.get_cog("Economy")
    drop = db.nuke_by_message(555)
    assert db.claim_nuke(drop["id"], 7) == "own" and db.claim_nuke(drop["id"], 8) == "ok"
    assert db.wallet(8)["points"] == 50 and eco is not None

    # the website: a Nukes tab and page; a failed drop is paid but "failed" (staff retry it)
    monkeypatch.setattr(Plugin, "_loops", lambda self: [])

    async def main():
        from web.server import WebInterface
        bot.plugins = PluginManager(bot)
        await bot.plugins.load_all()
        await bot.plugins.set_enabled("website", True)
        tmp = bot.config.database_path.parent
        web = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            page = await (await c.get("/store/nukes")).text()
            assert 'href="/store/nukes"' in page and "4.99" in page and 'value="nuke"' in page
            assert "the first <b>2</b> members" in page
            r = await c.post("/store/checkout", data={"kind": "nuke", "player": "Nobody"})
            assert r.status == 400 and "Nuke" in await r.text()
        finally:
            await c.close()
            await bot.rcon.close()
    run(main())
    monkeypatch.setattr(bot, "get_channel", lambda cid: None)
    bot.config = replace(bot.config, guild_id=None)
    monkeypatch.setattr(eco, "guild", lambda: None)
    r = run(s.checkout("nuke", base_url="https://x", player="Gamer7", gift_code=code))
    order = db.store_order_by("token", r["token"])
    assert order["status"] == "failed" and "no channel for nukes" in order["result"]
