"""Points, shop, /daily, playtime rewards, /profile and the web shop endpoints."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from rce.kill_sources import KillerType
from rce.log_parser import KillEvent

H = {"X-Requested-With": "rce-web"}


class FakeRole:
    def __init__(self, rid, name, position):
        self.id, self.name, self.position, self.managed = rid, name, position, False

    def is_default(self):
        return False

    def __ge__(self, other):
        return self.position >= other.position

    def __lt__(self, other):
        return self.position < other.position

    def __eq__(self, other):
        return isinstance(other, FakeRole) and other.id == self.id

    __hash__ = object.__hash__


@pytest.fixture
def eco(tmp_path, monkeypatch):
    for k, v in dict(DISCORD_TOKEN="x", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw",
                     ADMIN_ROLE_IDS="1", WEB_PASSWORD="secret123", DATABASE_PATH=str(tmp_path / "bot.db"),
                     SUPPORT_KNOWLEDGE_FILE=str(tmp_path / "k.md"), POINTS_NAME="coins", POINTS_PER_HOUR="60",
                     POINTS_PER_KILL="5", DAILY_POINTS="50", DAILY_STREAK_BONUS="10", PLAYTIME_REWARD_HOURS="2",
                     PLAYTIME_REWARD_KIT="Starter", PLAYTIME_REWARD_POINTS="25").items():
        monkeypatch.setenv(k, v)
    from bot import RceBot
    from cogs.custom_kits import CustomKits
    from cogs.economy import Economy
    from cogs.timed_roles import TimedRoles
    from config import load_config
    bot = RceBot(load_config("/nonexistent"))
    sent = []

    async def command(cmd, **kw):
        sent.append(cmd)
        return ""
    monkeypatch.setattr(bot.rcon, "command", command)
    monkeypatch.setattr(type(bot.rcon), "connected", property(lambda self: True))
    bot.online_players = {"GamerTag42": {}, "Stranger": {}}
    db = bot.db
    db.link_player(7, "GamerTag42")
    db.create_custom_kit("Starter", None, "admin")
    db.add_custom_kit_item("Starter", "rifle.ak", 1)

    async def no_loop(self):
        return None
    for cls in (Economy, TimedRoles):
        monkeypatch.setattr(cls, "cog_load", no_loop)

    async def add():
        await bot.add_cog(CustomKits(bot))
        await bot.add_cog(TimedRoles(bot))
        e = Economy(bot)
        await bot.add_cog(e)
        return e
    cog = asyncio.run(add())
    vip, top = FakeRole(55, "VIP", 5), FakeRole(10, "Bot", 10)
    guild = SimpleNamespace(id=1, name="Srv", me=SimpleNamespace(top_role=top), get_role={55: vip}.get, roles=[vip])
    member = SimpleNamespace(id=7, guild=guild, roles=[], mention="<@7>", display_name="Bob", __str__=lambda: "Bob")

    async def add_roles(role, reason=None):
        member.roles.append(role)
    member.add_roles = add_roles
    guild.get_member = {7: member}.get
    yield SimpleNamespace(bot=bot, cog=cog, db=db, sent=sent, member=member, guild=guild, vip=vip)
    db.close()


def test_earning(eco):
    asyncio.run(eco.cog.credit_playtime(30 * 60))                      # 30 min online
    assert eco.db.wallet(7)["points"] == 30                            # 60/h, unlinked "Stranger" gets nothing
    assert eco.db.point_history(7) == []                               # playtime isn't spammed into the history
    asyncio.run(eco.cog.credit_playtime(90 * 60))                      # 2 h reached -> playtime reward
    w = eco.db.wallet(7)
    assert w["points"] == 120 + 25 and w["playtime_progress"] == 0
    assert 'inventory.giveto "GamerTag42" "rifle.ak" 1' in eco.sent and any(c.startswith("say ") for c in eco.sent)

    kill = KillEvent("Stranger", KillerType.PLAYER, "GamerTag42", KillerType.PLAYER)
    asyncio.run(eco.cog.on_rce_kill(kill))
    asyncio.run(eco.cog.on_rce_kill(kill))                             # same victim again -> no farming
    npc = KillEvent("wolf", KillerType.NPC, "GamerTag42", KillerType.PLAYER)
    asyncio.run(eco.cog.on_rce_kill(npc))
    assert eco.db.wallet(7)["points"] == 150
    assert eco.db.point_history(7)[0]["reason"] == "kill: Stranger"


def test_daily_streak(eco, monkeypatch):
    ok, text = eco.cog.daily(7)
    assert ok and "+**50** coins" in text
    ok, text = eco.cog.daily(7)
    assert not ok and "Already collected" in text
    eco.db.set_daily(7, int(time.time()) - 25 * 3600, 1)                # yesterday -> streak 2
    ok, text = eco.cog.daily(7)
    assert ok and "+**60** coins" in text and "2 days in a row" in text
    eco.db.set_daily(7, int(time.time()) - 3 * 86400, 5)                # missed days -> streak resets
    assert "+**50** coins" in eco.cog.daily(7)[1]
    assert not eco.cog.daily(99)[0]                                    # not linked


def test_shop_buy_refund_and_roles(eco):
    db = eco.db
    db.add_shop_item("AK kit", "custom_kit", "Starter", 1, 100, None)
    db.add_shop_item("Rockets", "item", "ammo.rocket.basic", 3, 40, None)
    db.add_shop_item("VIP week", "role", "55", 7, 200, None)
    buy = lambda name: asyncio.run(eco.cog.buy(eco.member, name))
    r = buy("AK kit")
    assert not r.ok and "costs **100** coins" in r.message                # not enough points
    db.add_points(7, 500, "test")
    r = buy("AK kit")
    assert r.ok and db.wallet(7)["points"] == 400 and 'inventory.giveto "GamerTag42" "rifle.ak" 1' in eco.sent
    assert buy("Rockets").ok and 'inventory.giveto "GamerTag42" "ammo.rocket.basic" 3' in eco.sent
    r = buy("VIP week")
    assert r.ok and eco.vip in eco.member.roles and db.timed_role(1, 7, 55) and db.wallet(7)["points"] == 160

    async def broken(cmd, **kw):
        raise RuntimeError("RCON down")
    eco.bot.rcon.command = broken
    r = buy("Rockets")
    assert not r.ok and "refunded" in r.message and db.wallet(7)["points"] == 160
    eco.bot.online_players = {}
    assert "not online" in buy("AK kit").message
    assert "no shop item" in buy("Nothing").message


def test_profile(eco):
    eco.db.add_points(7, 42, "test")
    eco.member.display_avatar = SimpleNamespace(url="https://x/a.png")
    embed = eco.cog.profile_embed(eco.member)
    fields = {f.name: f.value for f in embed.fields}
    assert "GamerTag42" in fields["In-game"] and fields["Coins"] == "42" and fields["/daily"].startswith("ready")
    assert "Next playtime reward" in fields


def test_web_shop(eco, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from web.server import WebInterface
    eco.bot.get_guild = lambda gid: eco.guild
    eco.guild.roles = [eco.vip]
    eco.bot.config = eco.bot.config.__class__(**{**eco.bot.config.__dict__, "guild_id": 1})

    async def scenario():
        web = WebInterface(eco.bot, env_path=tmp_path / ".env", secret_path=tmp_path / "k")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            bad = await c.post("/api/shop/items", json={"name": "X", "kind": "custom_kit", "target": "Nope",
                                                        "price": 10}, headers=H)
            assert bad.status == 400 and "does not exist" in (await bad.json())["error"]
            ok = await c.post("/api/shop/items", json={"name": "VIP week", "kind": "role", "target": "55",
                                                       "amount": 7, "price": 200}, headers=H)
            assert ok.status == 200
            item_id = (await ok.json())["id"]
            shop = await (await c.get("/api/shop")).json()
            assert shop["points_name"] == "coins" and shop["items"][0]["summary"] == "Discord role VIP for 7 days"
            assert shop["roles"] == [{"id": "55", "name": "VIP"}]
            await c.put(f"/api/shop/items/{item_id}", json={"enabled": False}, headers=H)
            assert eco.db.shop_items(enabled_only=True) == []
            r = await c.post("/api/points", json={"user_id": "7", "delta": 75, "reason": "event win"}, headers=H)
            assert (await r.json())["balance"] == 75
            shop = await (await c.get("/api/shop")).json()
            assert shop["wallets"][0]["player"] == "GamerTag42"
            assert (await c.delete(f"/api/shop/items/{item_id}", headers=H)).status == 200
        finally:
            await c.close()
    asyncio.run(scenario())
