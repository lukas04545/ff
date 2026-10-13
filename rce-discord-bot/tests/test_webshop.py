"""Web store: kits for sale, own custom kits, gift cards, PayPal checkout (faked), delivery, website + admin API."""
import asyncio
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
                       timer_hours=12, player="Gamer7"))
    assert shop.pp.orders["PP1"]["amount"] == 10.0
    order = run(s.complete(r["token"]))
    assert order["status"] == "delivered" and order["result"] == "Gamer7 kit"
    kit = db.custom_kit("Gamer7 kit")
    assert kit["cooldown_minutes"] == 720
    assert {i["shortname"]: i["amount"] for i in db.custom_kit_items("Gamer7 kit")} == {"rifle.ak": 1, "ammo.rifle": 300}
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
            page = await (await c.get("/play/store")).text()
            assert "AK kit" in page and "9.99" in page and 'action="/play/store/checkout"' in page
            r = await c.get("/play/store")
            assert "https://www.sandbox.paypal.com" in r.headers["Content-Security-Policy"]
            assert "Test mode" in page
            r = await c.post("/play/store/checkout", data={"kind": "kit", "kit": str(shop.kit_id), "player": "Nobody"})
            assert r.status == 400 and "isn&#x27;t linked" in await r.text()
            r = await c.post("/play/store/checkout", data={"kind": "kit", "kit": str(shop.kit_id), "player": "Gamer7"},
                             allow_redirects=False)
            assert r.status == 303 and r.headers["Location"].startswith("https://www.sandbox.paypal.com/")
            token = db.store_orders()[0]["token"]
            r = await c.get(f"/play/store/return?o={token}", allow_redirects=False)
            assert r.status == 303 and r.headers["Location"] == f"/play/store/order/{token}"
            page = await (await c.get(f"/play/store/order/{token}")).text()
            assert "Done!" in page and "/customkit claim Ak Kit" in page
            page = await (await c.get("/play/store/build?q.rifle.ak=1&q.ammo.rifle=250")).text()
            assert "6.00" in page and 'name="q.rifle.ak" value="1"' in page
            page = await (await c.get("/play/store/giftcards")).text()
            assert "25.00" in page
            assert (await c.get("/play/store/order/nope")).status == 404
            # Admin API (owner only)
            assert (await c.get("/api/webshop")).status == 401
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            d = await (await c.get("/api/webshop")).json()
            assert d["status"]["paypal"] and d["kits"][0]["name"] == "AK kit" and d["orders"][0]["status"] == "delivered"
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
    bot, db = shop.env.bot, shop.db
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
            assert 'name="admin_key"' not in await (await c.get("/play/store")).text()   # hidden normally
            page = await (await c.get("/play/store?admin=1")).text()
            assert 'name="admin_key"' in page and "Admin mode" in page and "store/build?admin=1" in page
            r = await c.post("/play/store/checkout", data={"kind": "kit", "kit": str(shop.kit_id), "player": "Gamer7",
                                                          "admin": "1", "admin_key": "wrong"})
            text = await r.text()
            assert r.status == 400 and "Wrong admin key" in text and "wrong" not in text.split("Wrong admin key")[1][:300]
            r = await c.post("/play/store/checkout", data={"kind": "kit", "kit": str(shop.kit_id), "player": "Gamer7",
                                                          "admin_key": "staff-only-key-123"}, allow_redirects=False)
            assert r.status == 303 and r.headers["Location"].startswith("https://www.sandbox.paypal.com/")  # no ?admin=1
            r = await c.post("/play/store/checkout", data={"kind": "kit", "kit": str(shop.kit_id), "player": "Gamer7",
                                                          "admin": "1", "admin_key": "staff-only-key-123"},
                             allow_redirects=False)
            assert r.status == 303 and r.headers["Location"].startswith("/play/store/order/")
            page = await (await c.get(r.headers["Location"])).text()
            assert "Done!" in page and "free – admin key" in page
        finally:
            await c.close()
            await bot.rcon.close()
    asyncio.run(main())
