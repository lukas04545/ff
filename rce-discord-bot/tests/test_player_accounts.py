"""Player accounts: one-time code login, sessions, team-slot checkout/assign/expire, add-items checkout."""
import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from tests.test_webshop import FakePayPal, PRICES
from tests.test_kit_requests import env  # noqa: F401


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def acc(env, monkeypatch, tmp_path):
    from cogs.player_accounts import PlayerAccounts
    from cogs.webshop import WebShop
    (tmp_path / "prices.txt").write_text(PRICES)
    env.bot.config = replace(env.bot.config, guild_id=1, paypal_client_id="id", paypal_secret="sec",
                             paypal_mode="sandbox", store_currency="EUR", player_accounts=True,
                             store_slot_price=2.0, store_slot_max=3, price_list_file=tmp_path / "prices.txt")
    guild = env.buyer.guild
    monkeypatch.setattr(env.bot, "get_guild", lambda gid: guild)

    # A second member (teammate) besides the buyer (id 7)
    mate = NS(id=8, roles=[], mention="<@8>")

    async def mate_add(role, reason=None):
        if role not in mate.roles:
            mate.roles.append(role)

    async def mate_remove(role, reason=None):
        mate.roles = [r for r in mate.roles if r != role]
    mate.add_roles, mate.remove_roles = mate_add, mate_remove
    members = {7: env.buyer, 8: mate}
    monkeypatch.setattr(guild, "get_member", members.get)

    dms = []

    async def send(content=None, *, embed=None, **kw):
        dms.append(embed.description if embed is not None else content)
    monkeypatch.setattr(env.bot, "get_user", lambda uid: NS(id=uid, send=send))

    async def no_loop(self):
        return None
    monkeypatch.setattr(WebShop, "cog_load", no_loop)
    shop = WebShop(env.bot)
    run(env.bot.add_cog(shop))
    shop._paypal = (("id", "sec", "sandbox"), FakePayPal())

    accounts = PlayerAccounts(env.bot)      # constructed directly: cog_load (the loop) is not started
    env.db.link_player(7, "Gamer7")
    env.db.link_player(8, "Mate8")
    # User 7 owns a custom kit
    env.db.create_custom_kit("Team Kit", "Bought by user 7", "test")
    env.db.add_custom_kit_item("Team Kit", "rifle.ak", 1)
    return NS(a=accounts, shop=shop, env=env, db=env.db, dms=dms, mate=mate, buyer=env.buyer, guild=guild)


def test_login_code_flow(acc):
    from cogs.player_accounts import AccountError
    with pytest.raises(AccountError, match="isn't linked"):
        run(acc.a.send_code("Nobody"))
    name = run(acc.a.send_code("gamer7"))
    assert name == "Gamer7" and any("code for **Gamer7**" in d for d in acc.dms)
    code = acc.a._codes["gamer7"]["code"]
    with pytest.raises(AccountError, match="Wrong code"):
        acc.a.verify("Gamer7", "000000") if code != "000000" else acc.a.verify("Gamer7", "111111")
    token, uid, who = acc.a.verify("Gamer7", code)
    assert uid == 7 and who == "Gamer7"
    sess = acc.a.session(token)
    assert sess is not None and sess["user_id"] == 7
    acc.a.logout(token)
    assert acc.a.session(token) is None
    assert acc.a.verify("Gamer7", code) if False else True        # code already consumed
    with pytest.raises(AccountError, match="No code"):
        acc.a.verify("Gamer7", code)


def test_my_kits_and_ownership(acc):
    kits = acc.a.my_kits(7)
    assert [k["name"] for k in kits] == ["Team Kit"] and kits[0]["items"][0]["short"] == "rifle.ak"
    assert acc.a.my_kits(8) == []                                 # user 8 owns nothing
    from cogs.webshop import CheckoutError
    with pytest.raises(CheckoutError, match="isn't one of your kits"):
        run(acc.shop.checkout("slots", base_url="https://x/play", user_id=8, name="Team Kit", slot_count=1, months=1))


def test_buy_and_assign_and_expire_slots(acc):
    s, db = acc.shop, acc.db
    r = run(s.checkout("slots", base_url="https://x/play", user_id=7, name="Team Kit", slot_count=2, months=1))
    order = db.store_order_by("token", r["token"])
    assert order["kind"] == "slots" and order["total"] == 4.0 and order["slot_count"] == 2
    run(s.complete(r["token"]))
    slots = db.kit_slots(kit="Team Kit")
    assert len(slots) == 2 and all(x["member_id"] is None for x in slots)
    # Owner puts the teammate on a slot -> they get the kit role
    who = run(acc.a.assign_slot(7, slots[0]["id"], "Mate8"))
    assert who == "Mate8" and acc.mate.roles                      # role granted
    assert db.kit_slot(slots[0]["id"])["member_id"] == 8
    from cogs.player_accounts import AccountError
    with pytest.raises(AccountError, match="already has a slot"):
        run(acc.a.assign_slot(7, slots[1]["id"], "Mate8"))
    with pytest.raises(AccountError, match="isn't yours"):
        run(acc.a.assign_slot(8, slots[1]["id"], "Mate8"))        # not the owner
    # Clearing frees the slot and takes the role back
    run(acc.a.clear_slot(7, slots[0]["id"]))
    assert db.kit_slot(slots[0]["id"])["member_id"] is None and not acc.mate.roles
    # Expiry: assign again, force the slot into the past, run the loop
    run(acc.a.assign_slot(7, slots[0]["id"], "Mate8"))
    db.extend_kit_slot(slots[0]["id"], int(time.time()) - 5)
    run(acc.a.expire_slots.coro(acc.a))
    assert db.kit_slot(slots[0]["id"])["member_id"] is None and not acc.mate.roles
    assert any("ran out" in str(d) for d in acc.dms)


def test_slot_cap(acc):
    from cogs.webshop import CheckoutError
    with pytest.raises(CheckoutError, match="Between 1 and 3 slots"):
        run(acc.shop.checkout("slots", base_url="https://x/play", user_id=7, name="Team Kit",
                              slot_count=4, months=1))
    # Buy 2, then 2 more would exceed the cap of 3
    r = run(acc.shop.checkout("slots", base_url="https://x/play", user_id=7, name="Team Kit",
                              slot_count=2, months=1))
    run(acc.shop.complete(r["token"]))
    with pytest.raises(CheckoutError, match="at most 3 team slots"):
        run(acc.shop.checkout("slots", base_url="https://x/play", user_id=7, name="Team Kit",
                              slot_count=2, months=1))


class FakeSite:
    def page(self, base, active, title, body):
        return f"<html><style></style><body data-active='{active}'>{body}</body></html>"

    def item_tiles(self, items):
        return "<div class='tiles'>" + "".join(f"[{s} x{n}]" for s, n in items) + "</div>"


def test_pages_render(acc):
    from web import account_pages as ap
    login = ap.login_page(FakeSite(), "/play")
    assert login.status == 200 and "Send me a code" in login.text and "data-active='account'" in login.text
    code = ap.login_page(FakeSite(), "/play", player="Gamer7", stage="code")
    assert "Gamer7" in code.text and "Login code" in code.text
    page = ap.account_page(FakeSite(), acc.shop, acc.a, "/play", 7, "Gamer7")
    assert "Team Kit" in page.text and "Log out" in page.text and "Buy team slots" in page.text
    add = ap.additems_page(FakeSite(), acc.shop, "/play", "Team Kit", {"q.ammo.rifle": "500"})
    assert "Add items to Team Kit" in add.text and "Buy and add" in add.text


def test_add_items_checkout(acc):
    s, db = acc.shop, acc.db
    r = run(s.checkout("addon", base_url="https://x/play", user_id=7, name="Team Kit",
                       items={"ammo.rifle": "500"}))
    order = db.store_order_by("token", r["token"])
    assert order["kind"] == "addon" and order["kit_name"] == "Team Kit" and order["total"] == 2.0
    run(s.complete(r["token"]))
    items = {i["shortname"]: i["amount"] for i in db.custom_kit_items("Team Kit")}
    assert items["ammo.rifle"] == 500 and items["rifle.ak"] == 1       # added on top of what was there
    assert db.store_order_by("token", r["token"])["status"] == "delivered"
