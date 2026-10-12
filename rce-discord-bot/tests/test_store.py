"""Tip4Serv purchases: price list, polling, matching payments to kit requests, auto-approve, giveaway prizes."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from support import pricing
from support.tip4serv import parse_command, signature
from tests.test_kit_requests import env  # noqa: F401  (fixture: bot, cogs, ticket channel, buyer id 7)

PRICES = Path(__file__).resolve().parent.parent / "prices.txt"


# ------------------------------------------------------------------ price list

def q(items, timer=None):
    return pricing.quote(pricing.load(PRICES), [{"short": s, "amount": a} for s, a in items], timer)


def test_default_price_list_reads_cleanly():
    pl = pricing.load(PRICES)
    assert pl.warnings == [] and pl.currency == "EUR" and pl.default_timer == 30
    from rce import items as itemlist
    names = {s for r in pl.rules for s in r.items | r.extras} | {s for c in pl.caps for s in c.items}
    assert names <= set(itemlist.COMMON_ITEMS)


def test_quote_prices_bundles_extras_caps_and_timer():
    r = q([("lmg.m249", 2), ("ammo.rocket.basic", 50), ("metal.refined", 50000)])
    assert r.total == 12 + 100 + 250 and r.complete and r.timer_hours == 30
    assert q([("metal.fragments", 501)]).total == 2                    # rounded up to full bundles of 500
    assert q([("metal.fragments", 300), ("stones", 300)]).total == 2   # one rule shares its bundles
    ak = q([("rifle.ak", 10), ("metal.facemask", 10)])
    assert ak.total == 50 and ak.complete                              # armour included with the AK kits
    more = q([("rifle.ak", 1), ("metal.facemask", 3)])
    assert more.unpriced == ["metal.facemask"]                         # only 1 included, the rest has no price
    assert q([("rug", 1)]).unpriced == ["rug"]
    assert q([("ammo.rocket.basic", 600), ("ammo.rocket.hv", 500)]).over_cap   # all rockets: max 1000 together
    assert q([("wood", 2_000_001)]).over_cap
    assert q([("lmg.m249", 1)], 24).total == 156                       # 24h claim timer costs 150
    assert "not available" in q([("lmg.m249", 1)], 5).over_cap[0]


def test_parser_reports_bad_lines():
    pl = pricing.parse("currency usd\nfoo = bar\nwood = 1 / 0\nwood = 1,5 / 1.000\ntimer 12h = 300\ncap 1.000 each: wood")
    assert pl.currency == "USD" and len(pl.warnings) == 2
    assert pl.rules[0].price == 1.5 and pl.rules[0].per == 1000 and pl.timers == {12: 300} and pl.caps[0].limit == 1000


# ------------------------------------------------------------------ Tip4Serv protocol

def test_signature_and_command_parsing():
    assert signature("123.abc.xyz", "1700000000") == \
        "OTcyOGFkOGUxY2Y4N2I0ZjNjMjRhNGI2OTA2NDYwZDAyZGZjNGU1MjM5N2I2NzZiMjg4MTExZTQ5ZTYyMmUxMw=="
    assert parse_command("rcebot 1385231250033475675 40.00 EUR Zim is good") == {
        "discord_id": 1385231250033475675, "amount": 40.0, "currency": "EUR", "player": "Zim is good"}
    assert parse_command("rcebot {discord_id} 25,50 EUR {PlayerName}") == {
        "discord_id": None, "amount": 25.5, "currency": "EUR", "player": None}
    assert parse_command("inventory.giveto x wood 5") is None


class FakeClient:
    def __init__(self, payments):
        self.payments, self.acked = payments, []

    async def pending(self):
        return self.payments

    async def mark_executed(self, done):
        self.acked.append(done)


def store(env, monkeypatch):
    from cogs.store import Store
    s = Store(env.bot)
    monkeypatch.setattr(env.bot, "get_cog", lambda name, _orig=env.bot.get_cog: s if name == "Store" else _orig(name))
    return s


def test_poll_stores_once_and_acks_only_bot_commands(env, monkeypatch):
    s = store(env, monkeypatch)
    client = FakeClient([{"id": "P1", "action": "purchase", "username": "zim", "cmds": [
        {"id": 11, "str": "rcebot 1385231250033475675 40.00 EUR Zim", "state": ""},
        {"id": 12, "str": "say thanks", "state": ""}]}])
    assert asyncio.run(s.poll(client)) == 1
    assert client.acked == [{"P1": ("purchase", ["11"])}]                # the game command is left alone
    assert asyncio.run(s.poll(client)) == 0                              # re-sent -> not stored twice
    row = env.db.store_payments()[0]
    assert (row["payment_id"], row["amount"], row["currency"], row["player"], row["username"]) == \
        ("P1", 40.0, "EUR", "Zim", "zim")


# ------------------------------------------------------------------ matching + auto-approve

PRICED = [{"item": "m249", "amount": 2}, {"item": "ammo.rocket.basic", "amount": 50},
          {"item": "High Quality Metal", "amount": 50000}]


@pytest.fixture
def shop(env, tmp_path, monkeypatch):
    import time
    from cogs.store import Store
    env.bot.config = replace(env.bot.config, tip4serv_key="srv.secret.x", price_list_file=PRICES)

    async def add():
        await env.bot.add_cog(Store(env.bot))
    monkeypatch.setattr(Store, "cog_load", lambda self: asyncio.sleep(0))  # no polling loop in tests
    asyncio.run(add())
    env.store = env.bot.get_cog("Store")
    env.store.last_ok = time.time()                                       # connected
    env.db.add_store_payment("OLD:1", "OLD", amount=1, currency="EUR", discord_id=1, player=None, username=None,
                             customer=None, command="rcebot", raw=None)   # the bot received purchases before
    env.db.use_store_payments(["OLD:1"], 999999)
    return env


def new_kit(env, args):
    call = {"id": "c", "type": "function", "function": {"name": "request_new_custom_kit", "arguments": json.dumps(args)}}
    return asyncio.run(env.tickets._run_tool(call, env.ticket, env.channel))


def pay(env, pid, amount, *, discord_id=7, player=None, currency="EUR"):
    env.db.add_store_payment(f"{pid}:1", pid, amount=amount, currency=currency, discord_id=discord_id, player=player,
                             username=None, customer=None, command="rcebot …")


def test_payment_arrives_later_and_approves_automatically(shop):
    env = shop
    r = new_kit(env, {"kit_name": "Zim raid", "items": PRICED, "proof": "Tebex", "claim_timer_hours": 24})
    assert r["result"] == "waiting_for_staff" and r["payment"] == "not_found_yet"
    assert r["price"]["total_price"] == "€512.00"                        # 362 + 150 for the 24h timer
    embed = env.edits[-1][0]                                             # the request message is updated
    assert any("approved automatically as soon as it arrives" in f.value for f in embed.fields)
    assert any(f.name == "Price (price list)" and f.value == "€512.00" for f in embed.fields)

    pay(env, "OTHER", 999, discord_id=8)                                 # someone else's payment doesn't count
    pay(env, "LOW", 100)                                                 # too little on its own …
    asyncio.run(env.requests.on_new_payments())
    req = env.db.kit_request(r["request_id"])
    assert req["status"] == "pending" and "missing" in req["pay_note"]

    pay(env, "MAIN", 412)                                                # … together with this one it's enough
    asyncio.run(env.requests.on_new_payments())
    req = env.db.kit_request(r["request_id"])
    assert req["status"] == "approved" and req["decided_by"] == "Tip4Serv (automatic)"
    assert {p["payment_id"] for p in env.db.store_payments() if p["used_by"] == req["id"]} == {"LOW", "MAIN"}
    kit = env.db.custom_kit("Zim raid")
    assert kit is not None and kit["cooldown_minutes"] == 24 * 60       # bought claim timer applied
    assert env.created[-1] in env.buyer.roles and "/customkit claim Zim raid" in env.posted[-1][0]


def test_payment_first_single_payment_used_once(shop):
    env = shop
    pay(env, "A", 400)
    r = new_kit(env, {"items": PRICED, "proof": "paid"})
    assert r["payment"] == "approved_automatically"
    r2 = new_kit(env, {"kit_name": "Again", "items": PRICED, "proof": "paid"})
    assert r2["payment"] == "not_found_yet"                               # the payment can't be used twice


def test_cases_staff_still_decides(shop):
    env = shop
    pay(env, "B", 1000)
    over = new_kit(env, {"items": [{"item": "ammo.rocket.basic", "amount": 5000}], "proof": "paid"})
    assert over["result"] == "over_limits"
    unpriced = new_kit(env, {"items": PRICED + [{"item": "rug", "amount": 1}], "proof": "paid"})
    assert unpriced["payment"] == "found" and "staff decides" in unpriced["payment_note"]
    assert env.db.kit_request(unpriced["request_id"])["status"] == "pending"

    prize = new_kit(env, {"kit_name": "Prize kit", "items": PRICED, "proof": "won it", "won_in_giveaway": True})
    assert "payment" not in prize and env.db.kit_request(prize["request_id"])["status"] == "pending"
    assert env.posted[-1][1].title.startswith("🎉 Giveaway prize")

    # staff approves the unpriced one by hand -> the found payment is used up too
    ok, _ = asyncio.run(env.requests.decide(unpriced["request_id"], True, env.staff))
    assert ok and next(p for p in env.db.store_payments() if p["payment_id"] == "B")["used_by"] == \
        unpriced["request_id"]


def test_name_matching_and_switch(shop):
    env = shop
    env.db.link_player(7, "BobTheBuilder")
    pay(env, "N", 400, discord_id=None, player="bobthebuilder")
    env.bot.config = replace(env.bot.config, tip4serv_name_match=False)
    r = new_kit(env, {"items": PRICED, "proof": "paid"})
    assert r["payment"] == "not_found_yet"                               # only Discord IDs count now
    env.bot.config = replace(env.bot.config, tip4serv_name_match=True)
    asyncio.run(env.requests.check_payment(r["request_id"]))
    assert env.db.kit_request(r["request_id"])["status"] == "approved"


def test_auto_approve_off_and_store_off(shop):
    env = shop
    pay(env, "C", 400)
    env.bot.config = replace(env.bot.config, tip4serv_auto_approve=False)
    r = new_kit(env, {"items": PRICED, "proof": "paid"})
    assert r["payment"] == "found" and env.db.kit_request(r["request_id"])["status"] == "pending"
    asyncio.run(env.requests.decide(r["request_id"], False, env.staff))
    env.bot.config = replace(env.bot.config, tip4serv_key="")
    r2 = new_kit(env, {"kit_name": "Other", "items": PRICED, "proof": "paid"})
    assert "payment" not in r2 and "price" in r2                         # price still shown without the store


def test_quote_tool(shop):
    env = shop
    call = {"id": "c", "type": "function", "function": {"name": "quote_custom_kit", "arguments": json.dumps(
        {"items": PRICED + [{"item": "rug", "amount": 2}]})}}
    r = asyncio.run(env.tickets._run_tool(call, env.ticket, env.channel))
    assert r["price"]["total_price"] == "€362.00" and r["price"]["items_without_price"] == ["rug"]
    assert env.posted == [] and env.db.kit_requests() == []              # nothing filed


# ------------------------------------------------------------------ add-ons for a kit the member bought

def addon(env, args):
    call = {"id": "c", "type": "function", "function": {"name": "request_kit_addon", "arguments": json.dumps(args)}}
    return asyncio.run(env.tickets._run_tool(call, env.ticket, env.channel))


def bought_kit(env, name="Zim raid"):
    pay(env, "FIRST", 400)
    r = new_kit(env, {"kit_name": name, "items": PRICED, "proof": "paid"})
    assert r["payment"] == "approved_automatically"
    return name


def test_addon_paid_merges_items(shop):
    env = shop
    kit = bought_kit(env)
    call = {"id": "c", "type": "function", "function": {"name": "get_my_kits", "arguments": "{}"}}
    assert asyncio.run(env.tickets._run_tool(call, env.ticket, env.channel))["kits_you_bought"] == [kit]

    r = addon(env, {"kit": "zim RAID", "proof": "add-on order",
                    "items": [{"item": "lmg.m249", "amount": 3}, {"item": "explosive.timed", "amount": 10}]})
    assert r["result"] == "waiting_for_staff" and r["price"]["total_price"] == "€43.00"   # 3×6 + 10×2.5, no timer
    assert r["payment"] == "not_found_yet" and env.edits[-1][0].title.startswith("➕ Kit add-on")
    pay(env, "ADDON", 43)
    asyncio.run(env.requests.on_new_payments())
    assert env.db.kit_request(r["request_id"])["status"] == "approved"
    items = {i["shortname"]: i["amount"] for i in env.db.custom_kit_items(kit)}
    assert items["lmg.m249"] == 5 and items["explosive.timed"] == 10 and items["metal.refined"] == 50000
    assert len(env.db.custom_kit_items(kit)) == 4                          # merged, no duplicate M249 row
    assert "Add-on confirmed" in env.posted[-1][0]


def test_addon_only_for_original_buyer_and_caps(shop):
    env = shop
    r = addon(env, {"kit": "Ak Kit", "proof": "x", "items": [{"item": "lmg.m249", "amount": 1}]})
    assert r["result"] == "not_your_kit"                                   # made by hand: nobody bought it
    kit = bought_kit(env)
    other = env.db.kit_requests(status="approved")[0]
    env.db._db.execute("UPDATE kit_requests SET user_id = 99 WHERE id = ?", (other["id"],))   # someone else bought it
    env.db._db.commit()
    assert addon(env, {"kit": kit, "proof": "x", "items": [{"item": "lmg.m249", "amount": 1}]})["result"] == "not_your_kit"
    env.db._db.execute("UPDATE kit_requests SET user_id = 7 WHERE id = ?", (other["id"],))
    env.db._db.commit()
    over = addon(env, {"kit": kit, "proof": "x", "items": [{"item": "ammo.rocket.basic", "amount": 960}]})
    assert over["result"] == "over_limits" and "after adding" in over["over_limits"][0]   # 50 + 960 > 1000


def test_addon_won_in_giveaway_needs_staff(shop):
    env = shop
    kit = bought_kit(env)
    pay(env, "SPARE", 1000)
    r = addon(env, {"kit": kit, "proof": "won the add-on", "won_in_giveaway": True,
                    "items": [{"item": "samsite", "amount": 2}]})
    assert "payment" not in r and env.db.kit_request(r["request_id"])["status"] == "pending"
    ok, _ = asyncio.run(env.requests.decide(r["request_id"], True, env.staff))
    assert ok and {i["shortname"]: i["amount"] for i in env.db.custom_kit_items(kit)}["samsite"] == 2
    assert "Prize confirmed" in env.posted[-1][0]


# ------------------------------------------------------------------ one purchase = one kit

def test_order_ids():
    from cogs.kit_requests import order_ids
    assert order_ids("Tebex order tbx-1a2b3c4d paid 40.00 EUR on 2026-10-12") == {"tbx-1a2b3c4d"}
    assert order_ids("PayPal 5RX12345AB678901C, 12345678") == {"5rx12345ab678901c", "12345678"}
    assert order_ids("50000 hqm, 12/10/2026, order #1234") == set()


def test_same_order_id_only_once(shop):
    env = shop
    r = new_kit(env, {"items": PRICED, "proof": "Tebex order tbx-9f8e7d6c"})
    assert r["result"] == "waiting_for_staff"
    asyncio.run(env.requests.decide(r["request_id"], True, env.staff))
    again = new_kit(env, {"kit_name": "Second", "items": PRICED, "proof": "order TBX-9F8E7D6C again"})
    assert again["result"] == "order_already_used" and again["used_for_request"] == r["request_id"]
    access = asyncio.run(env.tickets._run_tool({"id": "c", "type": "function", "function": {
        "name": "request_kit_access", "arguments": json.dumps({"kit": "Mega raid kit", "proof": "tbx-9f8e7d6c"})}},
        env.ticket, env.channel))
    assert access["result"] == "order_already_used"
    prize = new_kit(env, {"kit_name": "Won", "items": PRICED, "proof": "tbx-9f8e7d6c", "won_in_giveaway": True})
    assert prize["result"] == "waiting_for_staff"                         # giveaway prizes aren't purchases
    # a rejected request frees its order ID
    env.db._db.execute("UPDATE kit_requests SET status = 'rejected' WHERE id = ?", (r["request_id"],))
    env.db._db.commit()
    assert new_kit(env, {"kit_name": "Third", "items": PRICED, "proof": "tbx-9f8e7d6c"})["result"] == \
        "waiting_for_staff"


def test_staff_cant_approve_with_a_payment_used_elsewhere(shop):
    env = shop
    env.bot.config = replace(env.bot.config, tip4serv_auto_approve=False)
    pay(env, "ONE", 400)
    first = new_kit(env, {"items": PRICED, "proof": "paid"})
    assert first["payment"] == "found"
    # The same payment also shows up for an add-on request filed meanwhile (pretend it matched there too)
    env.db._db.execute("UPDATE store_payments SET used_by = 999 WHERE payment_id = 'ONE'")
    env.db._db.commit()
    ok, text = asyncio.run(env.requests.decide(first["request_id"], True, env.staff))
    assert not ok and "already used for request #999" in text
    req = env.db.kit_request(first["request_id"])
    assert req["status"] == "pending" and env.buyer.roles == []
    assert "No matching Tip4Serv payment found yet" in req["pay_note"]  # status refreshed for staff


# ------------------------------------------------------------------ order IDs, setup problems, staff commands

def test_order_id_found_anywhere_in_the_payment(shop):
    env = shop
    env.db.add_store_payment("T1:1", "88001", amount=400, currency="EUR", discord_id=None, player=None,
                             username=None, customer=None, command="rcebot",
                             raw=json.dumps({"id": "88001", "order": {"number": "1110065"}}))
    r = new_kit(env, {"kit_name": "Geek kit", "items": PRICED, "proof": "T4s, order ID #1110065"})
    assert r["payment"] == "approved_automatically"


def test_setup_problems_are_reported_not_hidden(shop):
    env = shop
    env.store.last_error = "HTTP 401 – check TIP4SERV_KEY: bad signature"
    r = new_kit(env, {"items": PRICED, "proof": "order #1110065"})
    assert r["payment"] == "store_unavailable" and "HTTP 401" in r["payment_note"]
    assert "by hand" in r["tell_player"]
    env.store.last_error = None
    env.db._db.execute("DELETE FROM store_payments")                     # nothing ever received
    env.db._db.commit()
    asyncio.run(env.requests.check_payment(r["request_id"]))
    assert "hasn't received any Tip4Serv purchase" in env.db.kit_request(r["request_id"])["pay_note"]
    env.db.add_store_payment("X:1", "X", amount=1, currency="EUR", discord_id=1, player=None, username=None,
                             customer=None, command="rcebot", raw=None)
    asyncio.run(env.requests.check_payment(r["request_id"]))
    note = env.db.kit_request(r["request_id"])["pay_note"]
    assert note.startswith("⏳ No matching Tip4Serv payment found yet (order 1110065 not received")


def test_tip4serv_find_and_assign(shop):
    from types import SimpleNamespace as NS
    env = shop
    env.db.add_store_payment("P9:1", "1110065", amount=400, currency="EUR", discord_id=None, player="SomeoneElse",
                             username=None, customer=None, command="rcebot", raw=None)
    r = new_kit(env, {"items": PRICED, "proof": "paid on the store"})
    assert r["payment"] == "not_found_yet"
    out = []

    async def send_message(text, **kw):
        out.append(text)

    async def defer(**kw):
        return None
    staff = NS(id=2, roles=env.staff.roles, __str__=lambda: "Mod")
    inter = NS(user=staff, response=NS(send_message=send_message, defer=defer), followup=NS(send=send_message))
    asyncio.run(env.store.find_cmd.callback(env.store, inter, "#1110065"))
    assert "`1110065` 400.00 EUR – SomeoneElse" in out[-1] and "unused" in out[-1]
    asyncio.run(env.store.find_cmd.callback(env.store, inter, "424242"))
    assert "has not received order" in out[-1]
    asyncio.run(env.store.assign_cmd.callback(env.store, inter, "1110065", env.buyer))
    assert env.db.kit_request(r["request_id"])["status"] == "approved"       # checked again -> paid
    async def no_poll(client=None):
        return 0
    env.store.poll = no_poll
    asyncio.run(env.store.status_cmd.callback(env.store, inter))
    assert "✅ working" in out[-1] and "used by request #" in out[-1]
    stranger = NS(id=8, roles=[], __str__=lambda: "x")
    asyncio.run(env.store.assign_cmd.callback(env.store, NS(user=stranger, response=NS(send_message=send_message)),
                                              "1110065", env.buyer))
    assert "Staff only" in out[-1]


def verify(env, order):
    call = {"id": "c", "type": "function", "function": {"name": "verify_payment",
                                                        "arguments": json.dumps({"order_id": order})}}
    return asyncio.run(env.tickets._run_tool(call, env.ticket, env.channel))


def test_verify_payment_with_staff_authorisation(shop):
    import time
    env = shop
    env.bot.config = replace(env.bot.config, tip4serv_name_match=False)   # order ID alone isn't enough …
    r = new_kit(env, {"kit_name": "Geek kit", "items": [{"item": "rifle.ak", "amount": 5}],
                      "proof": "T4s receipt"})
    assert verify(env, "#424242")["result"] == "not_received"
    env.db.add_store_payment("G:1", "1110065", amount=25, currency="EUR", discord_id=None, player="geek",
                             username=None, customer=None, command="rcebot", raw=None)
    res = verify(env, "#1110065")
    assert res["result"] == "found_staff_decides" and env.db.kit_request(r["request_id"])["status"] == "pending"
    assert "order 1110065" in env.db.kit_request(r["request_id"])["proof"]
    # … until staff says "approve if you find it" with /aiconfirm
    env.tickets._staff_ok[env.channel.id] = (time.monotonic() + 60, "Lukas")
    res = verify(env, "1110065")
    assert res["result"] == "approved"
    req = env.db.kit_request(r["request_id"])
    assert req["status"] == "approved" and req["decided_by"] in ("Lukas (via AI, /aiconfirm)", "Tip4Serv (automatic)")
    assert env.db.find_store_payments("1110065")[0]["used_by"] == r["request_id"]
    assert verify(env, "1110065")["result"] == "already_used"            # one payment, one kit


def test_verify_payment_staff_approves_even_if_unpriced(shop):
    import time
    env = shop
    r = new_kit(env, {"items": [{"item": "rug", "amount": 3}], "proof": "paid"})   # no price -> no auto-approve
    env.db.add_store_payment("U:1", "777001", amount=5, currency="EUR", discord_id=None, player=None,
                             username=None, customer=None, command="rcebot", raw=None)
    env.tickets._staff_ok[env.channel.id] = (time.monotonic() + 60, "Lukas")
    res = verify(env, "777001")
    assert res["result"] == "approved"
    assert env.db.kit_request(r["request_id"])["decided_by"] == "Lukas (via AI, /aiconfirm)"


def test_verify_payment_other_buyer(shop):
    env = shop
    new_kit(env, {"items": PRICED, "proof": "paid"})
    env.db.add_store_payment("O:1", "555111", amount=999, currency="EUR", discord_id=123456789012345678,
                             player=None, username=None, customer=None, command="rcebot", raw=None)
    assert verify(env, "555111")["result"] == "other_buyer"


# ------------------------------------------------------------------ talking to Tip4Serv (local fake server)

def test_client_request_format_and_errors():
    import base64
    import hashlib
    import hmac as hm
    from aiohttp import web
    from support.tip4serv import Tip4ServClient, Tip4ServError, split_key
    seen = {}

    async def handler(request):
        seen.update(path=request.path, query=dict(request.query), auth=request.headers.get("Authorization"),
                    method=request.method, body=await request.text())
        if request.match_info["sid"] == "500":
            return web.Response(status=500, text="")
        return web.json_response([{"id": "P1", "action": "purchase", "cmds": [{"id": 1, "str": "rcebot 5 EUR"}]}])

    async def run():
        app = web.Application()
        app.router.add_route("*", "/v1/store/server/{sid}/commands", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}/v1/store/server/"
        try:
            ok = Tip4ServClient("4242.secret.pub", base=base)
            payments = await ok.pending()
            ts = seen["query"]["time"]
            digest = hm.new(b"secret", f"4242pub{ts}".encode(), hashlib.sha256).hexdigest()
            assert seen["path"] == "/v1/store/server/4242/commands" and seen["method"] == "GET"
            assert seen["auth"] == "Bearer SERVER_KEY:" + base64.b64encode(digest.encode()).decode()
            assert payments[0]["id"] == "P1"
            await ok.mark_executed({"P1": ("purchase", ["1"])})
            assert seen["method"] == "POST" and json.loads(seen["body"]) == {
                "P1": {"action": "purchase", "cmds": {"1": "Executed"}}}
            try:
                await Tip4ServClient("500.secret.pub", base=base).pending()
                raise AssertionError("no error")
            except Tip4ServError as exc:
                assert "HTTP 500" in str(exc) and "Connect with RCON" in str(exc) and "server #500" in str(exc)
        finally:
            await runner.cleanup()
    asyncio.run(run())
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc"
    try:
        split_key(jwt)
        raise AssertionError("JWT accepted")
    except Tip4ServError as exc:
        assert "store API key" in str(exc)


# ------------------------------------------------------------------ webhook ("live payments")

def test_parse_webhook_flexible():
    from support.tip4serv import parse_webhook, strip_private
    p = parse_webhook({"event": "payment.success", "data": {"id": 1110065, "total_paid": "25.00", "currency": "eur",
                       "customer": {"discord_id": "1385231250033475675", "email": "a@b.c", "username": "Geek"}}})
    assert p == {"kind": "payment", "payment_id": "1110065", "amount": 25.0, "currency": "EUR",
                 "discord_id": 1385231250033475675, "player": "Geek"}
    assert parse_webhook({"type": "payment_refunded", "payment_id": "#77"})["kind"] == "refund"
    assert parse_webhook({"type": "subscription_renewal", "payment_id": "78"})["kind"] == "other"
    assert parse_webhook({"hello": "world"}) is None and parse_webhook("x") is None
    assert strip_private({"customer": {"email": "a@b.c", "ip": "1.2.3.4", "name": "x"}}) == {"customer": {"name": "x"}}


def test_webhook_endpoint_stores_and_approves(shop):
    from aiohttp.test_utils import TestClient, TestServer
    env = shop
    r = new_kit(env, {"kit_name": "Geek kit", "items": [{"item": "rifle.ak", "amount": 5}], "proof": "T4s #1110065"})
    env.bot.config = replace(env.bot.config, tip4serv_key="")              # webhook only

    async def scenario():
        from web.server import WebInterface
        tmp = env.bot.config.database_path.parent
        web_ = WebInterface(env.bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        client = TestClient(TestServer(web_.app))
        await client.start_server()
        try:
            payload = {"event": "Payment successful", "payment": {"id": "1110065", "amount": 25, "currency": "EUR",
                                                                   "discord_id": "7"}, "player": "Geek"}
            assert (await client.post("/hooks/tip4serv/wrongtoken", json=payload)).status == 404
            res = await client.post(f"/hooks/tip4serv/{web_.webhook_token}", json=payload)
            assert res.status == 200 and (await res.json())["stored"] is True
            again = await client.post(f"/hooks/tip4serv/{web_.webhook_token}", json=payload)
            assert (await again.json())["reason"] == "known"                   # sent twice -> counted once
            refund = await client.post(f"/hooks/tip4serv/{web_.webhook_token}",
                                       json={"event": "payment_refunded", "payment_id": "1110065"})
            assert (await refund.json())["voided"] is True
        finally:
            await client.close()
    asyncio.run(scenario())
    # "discord_id": "7" isn't a snowflake -> matched by the order ID in the proof (name matching on)
    assert env.db.kit_request(r["request_id"])["status"] == "approved"
    assert env.store.problem() is None                                      # webhook counts as connected
    row = env.db.find_store_payments("1110065")[0]
    assert row["id"] == "1110065:webhook" and row["used_by"] == r["request_id"]


# ------------------------------------------------------------------ Tip4Serv bot's purchase messages in a channel

def test_parse_announcement():
    from support.tip4serv import parse_announcement
    assert parse_announcement("Thank you <@1385231250033475675> for your 25.00 EUR donation !") == {
        "discord_id": 1385231250033475675, "amount": 25.0, "currency": "EUR", "order": None}
    assert parse_announcement("Thanks <@!1385231250033475675>, you paid €12,50 – order #1110065")["order"] == "1110065"
    assert parse_announcement("<@1385231250033475675> paid 40 USD")["amount"] == 40.0   # "paid" isn't an order ID
    assert parse_announcement("<@1385231250033475675> paid 40 USD")["order"] is None
    assert parse_announcement("Thank you for your 25 EUR donation") is None               # no buyer
    assert parse_announcement("Thank you <@1385231250033475675>!") is None                # no amount


def announce(env, text, *, bot=True, mid=1, channel_id=321, author_id=4242):
    from datetime import datetime, timezone
    from types import SimpleNamespace as NS
    msg = NS(id=mid, content=text, embeds=[], author=NS(id=author_id, bot=bot), channel=NS(id=channel_id),
             created_at=datetime.now(timezone.utc))
    asyncio.run(env.store.on_message(msg))


def test_channel_announcement_confirms_purchase(shop):
    env = shop
    buyer = 1385231250033475675
    env.bot.config = replace(env.bot.config, tip4serv_key="", tip4serv_channel_id=321)
    env.channel.guild.get_member = {buyer: env.buyer, 7: env.buyer}.get
    r = new_kit(env, {"kit_name": "Geek kit", "items": [{"item": "rifle.ak", "amount": 5}], "proof": "paid on T4s"})
    env.db._db.execute("UPDATE kit_requests SET user_id = ? WHERE id = ?", (buyer, r["request_id"]))
    env.db._db.commit()
    assert r["payment"] == "not_found_yet"                                # channel set: waiting, not "can't check"
    text = f"Thank you <@{buyer}> for your 25.00 EUR donation !"
    announce(env, text, bot=False, mid=10)                                 # members can't fake purchases
    announce(env, text, channel_id=999, mid=11)                            # other channels don't count
    assert env.db.find_store_payments("dc-10") == [] and env.db.find_store_payments("dc-11") == []
    announce(env, text, mid=12)
    row = env.db.find_store_payments("dc-12")[0]
    assert (row["id"], row["discord_id"], row["amount"], row["currency"]) == ("dc-12:discord", buyer, 25, "EUR")
    assert env.db.kit_request(r["request_id"])["status"] == "approved" and row["id"] and \
        env.db.find_store_payments("dc-12")[0]["used_by"] == r["request_id"]
    announce(env, text, mid=12)                                            # same message again -> nothing new
    assert len(env.db.find_store_payments("dc-12")) == 1
    # the same purchase via the webhook a moment later is not counted a second time
    res = asyncio.run(env.store.on_webhook({"event": "payment successful", "id": "1110099", "amount": 25,
                                            "currency": "EUR", "discord_id": str(buyer)}))
    assert res["stored"] is False
