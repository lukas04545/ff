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
def shop(env, tmp_path):
    env.bot.config = replace(env.bot.config, tip4serv_key="srv.secret.x", price_list_file=PRICES)
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
    assert ok and env.db.store_payments()[0]["used_by"] == unpriced["request_id"]


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
    assert "No Tip4Serv payment found yet" in req["pay_note"]           # status refreshed for staff
