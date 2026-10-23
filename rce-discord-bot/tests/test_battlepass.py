"""Battle pass: premium currency, XP, buying the pass (1100), claiming (premium pays 1200), coin packs, the page."""
import asyncio
import re
import time
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from plugins import battlepass as bpm
from rce.log_parser import KillerType, KillEvent
from tests.test_plugins import pb  # noqa: F401


@pytest.fixture
def bp_env(pb, monkeypatch):  # noqa: F811
    from cogs.player_accounts import PlayerAccounts, _hash
    pb.bot.config = replace(pb.bot.config, player_accounts=True, economy_enabled=True)

    async def no_loop(self):
        return None
    monkeypatch.setattr(PlayerAccounts, "cog_load", no_loop)

    async def setup():
        await pb.bot.add_cog(PlayerAccounts(pb.bot))
        await pb.m.load_all()
        await pb.m.set_enabled("website", True)
        await pb.m.set_enabled("battlepass", True)
    asyncio.run(setup())
    pb.db.add_player_session(_hash("tok"), 7, "Gamer7", int(time.time()) + 3600)
    pb.db.link_player(7, "Gamer7")
    pb.bp = pb.m.running("battlepass")
    return pb


def test_defaults_cost_1100_and_pay_back_1200():
    tiers, problems = bpm.parse_tiers(bpm.DEFAULT_TIERS)
    assert problems == [] and len(tiers) == 30
    assert sum(p.amount for _, p in tiers.values() if p and p.kind == "coins") == 1200
    from rce import items as itemlist
    known = {s for v in itemlist.CATALOGUE.values() for s in v}
    assert all(r.item in known for pair in tiers.values() for r in pair if r and r.kind == "item")
    assert bpm.parse_reward("item rifle.ak 1") == bpm.Reward("item", 1, "rifle.ak")
    assert bpm.parse_reward("-") is None
    with pytest.raises(ValueError):
        bpm.parse_reward("diamonds 5")
    assert bpm.parse_tiers("1 | coins x | -")[1]
    assert bpm.parse_packs(bpm.DEFAULT_PACKS)[1] == (1100, 9.99)


def test_coin_wallet(bp_env):
    db = bp_env.db
    assert db.coins(7) == 0 and db.spend_coins(7, 1, "x") is None
    assert db.add_coins(7, 500, "test") == 500 and db.spend_coins(7, 600, "x") is None
    assert db.spend_coins(7, 200, "x") == 300 and db.add_coins(7, -1000, "x") == 0       # never below 0
    assert [r["delta"] for r in db.coin_history(7)] == [-1000, -200, 500]


def test_xp_from_kills_time_online_and_daily(bp_env):
    bp = bp_env.bp
    asyncio.run(bp.on_rce_kill(KillEvent("Victim", KillerType.PLAYER, "Gamer7", KillerType.PLAYER)))
    asyncio.run(bp.on_rce_kill(KillEvent("Bear", KillerType.NPC, "Gamer7", KillerType.PLAYER)))   # not PvP
    assert bp.progress(7)["xp"] == 50
    bp_env.bot.online_players = {"Gamer7": {}, "Unlinked": {}}
    asyncio.run(bp.online_xp())
    assert bp.progress(7)["xp"] == 60
    assert bp.claim_daily(7) == 300 and bp.progress(7)["xp"] == 360
    with pytest.raises(bpm.PassError, match="tomorrow"):
        bp.claim_daily(7)


def test_buy_pass_and_claim(bp_env):
    bp, db = bp_env.bp, bp_env.db
    with pytest.raises(bpm.PassError, match="costs"):
        bp.buy_pass(7)
    db.add_coins(7, 1100, "test")
    bp.buy_pass(7)
    assert bp.progress(7)["premium"] and db.coins(7) == 0
    with pytest.raises(bpm.PassError, match="already"):
        bp.buy_pass(7)
    with pytest.raises(bpm.PassError, match="Reach tier 1"):
        asyncio.run(bp.claim(7, 1, "free"))
    bp.add_xp(7, 2000)                                                    # tier 2
    assert asyncio.run(bp.claim(7, 1, "free")).startswith("100 ")        # points
    assert db.wallet(7)["points"] == 100
    with pytest.raises(bpm.PassError, match="already claimed"):
        asyncio.run(bp.claim(7, 1, "free"))
    assert asyncio.run(bp.claim(7, 2, "paid")) == "100 Coins" and db.coins(7) == 100
    with pytest.raises(bpm.PassError, match="Join the server"):           # items need the player online
        asyncio.run(bp.claim(7, 1, "paid"))
    assert 1 not in bp.progress(7)["paid"]                                # not lost
    bp_env.bot.online_players = {"Gamer7": {}}
    bp_env.said.clear()
    asyncio.run(bp.claim(7, 1, "paid"))
    assert bp_env.said == ['inventory.giveto "Gamer7" "wood" 2000']


def test_free_player_cant_claim_premium(bp_env):
    bp = bp_env.bp
    bp.add_xp(7, 5000)
    with pytest.raises(bpm.PassError, match="premium"):
        asyncio.run(bp.claim(7, 2, "paid"))


def test_full_season_pays_back_1200(bp_env):
    bp, db = bp_env.bp, bp_env.db
    db.add_coins(7, 1100, "test")
    bp.buy_pass(7)
    bp_env.bot.online_players = {"Gamer7": {}}
    bp.add_xp(7, 30 * 1000)
    given, failed = asyncio.run(bp.claim_all(7))
    assert failed == [] and db.coins(7) == 1200                           # enough for the next pass
    assert asyncio.run(bp.claim_all(7)) == ([], [])


def test_new_season_starts_over(bp_env):
    bp = bp_env.bp
    bp.add_xp(7, 5000)
    asyncio.run(bp_env.m.save_settings("battlepass", {"season": 2, "season_name": "Season 2"}))
    assert bp.progress(7)["xp"] == 0 and not bp.progress(7)["premium"]


def test_coin_pack_in_the_store(bp_env, monkeypatch):
    from cogs.webshop import CheckoutError, WebShop

    async def no_loop(self):
        return None
    monkeypatch.setattr(WebShop, "cog_load", no_loop)
    shop = WebShop(bp_env.bot)
    asyncio.run(bp_env.bot.add_cog(shop))

    async def quiet(*args, **kwargs):
        return None
    monkeypatch.setattr(shop, "_dm", quiet)
    monkeypatch.setattr(shop, "_log", quiet)
    code = shop.create_gift_cards(20, 1, "test")[0]
    out = asyncio.run(shop.checkout("coins", base_url="http://x", user_id=7, value="1100", gift_code=code))
    order = bp_env.db.store_order_by("token", out["token"])
    assert order["status"] == "delivered" and order["coins"] == 1100 and bp_env.db.coins(7) == 1100
    with pytest.raises(CheckoutError, match="isn't available"):
        asyncio.run(shop.checkout("coins", base_url="http://x", user_id=7, value="999", gift_code=code))
    with pytest.raises(CheckoutError, match="Log in"):
        asyncio.run(shop.checkout("coins", base_url="http://x", value="1100", gift_code=code))


def test_battlepass_page(bp_env):
    async def main():
        from web.server import WebInterface
        web = WebInterface(bp_env.bot, env_path=bp_env.tmp / ".env", secret_path=bp_env.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            page = await (await c.get("/battlepass")).text()
            assert "Log in to see your battle pass" in page and 'href="/battlepass"' in page
            c.session.cookie_jar.update_cookies({"rce_player": "tok"})
            page = await (await c.get("/battlepass")).text()
            assert "Tier 0" in page and "Get the premium pass – 💎 1 100 Coins" in page and "1 200 Coins" in page
            assert page.count('class="tier') == 30
            r = await c.post("/battlepass", data={"action": "buy"})
            assert "costs" in await r.text()
            bp_env.db.add_coins(7, 1100, "test")
            r = await c.post("/battlepass", data={"action": "buy"})
            assert "Premium pass unlocked" in await r.text()
            await c.post("/battlepass", data={"action": "daily"})
            bp_env.bp.add_xp(7, 1700)
            page = await (await c.post("/battlepass", data={"action": "claim", "tier": "2", "track": "paid"})).text()
            assert "Claimed: 100 Coins" in page and re.search(r"💎 100 Coins", page)
            assert (await c.post("/battlepass", data={"action": "buy"},
                                 headers={"Origin": "https://evil.example"})).status == 403
        finally:
            await c.close()
            await bp_env.bot.rcon.close()
    asyncio.run(main())

