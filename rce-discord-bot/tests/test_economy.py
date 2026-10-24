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


def test_nuke_drop_everyone_can_claim_once(eco):
    db = eco.db
    from cogs.economy import NUKE_HOURS
    assert eco.cog.validate_item("nuke", "250", 2, 1000) == "250"
    with pytest.raises(ValueError, match="points every member"):
        eco.cog.validate_item("nuke", "lots", 2, 1000)
    db.add_shop_item("Nuke", "nuke", "250", 2, 1000, None)
    assert "first 2 members" in eco.cog.describe_item(db.shop_item_by_name("Nuke"))

    posted = []

    class Message:
        def __init__(self, embed, view):
            self.id, self.embed, self.view = 900, embed, view

        async def edit(self, embed=None, view=None):
            self.embed, self.view = embed, view

    class Channel:
        id, mention = 42, "#general"

        async def send(self, embed=None, view=None, allowed_mentions=None):
            assert allowed_mentions is not None and not allowed_mentions.everyone      # never pings everyone
            posted.append(Message(embed, view))
            return posted[-1]
    db.add_points(7, 1500, "test")
    eco.bot.online_players = {}                                          # a nuke doesn't need the buyer online
    r = asyncio.run(eco.cog.buy(eco.member, "Nuke", Channel()))
    assert r.ok and "dropped in #general" in r.message and db.wallet(7)["points"] == 500
    msg = posted[0]
    assert "250" in msg.embed.description and msg.embed.footer.text == "Claimed: 0 / 2"
    drop = db.nuke_by_message(900)
    assert drop["expires_at"] - drop["created_at"] == NUKE_HOURS * 3600

    def press(user_id):
        answers = []

        async def send_message(text, ephemeral=False):
            answers.append(text)
        it = SimpleNamespace(message=msg, user=SimpleNamespace(id=user_id),
                             response=SimpleNamespace(send_message=send_message))
        asyncio.run(eco.cog.claim_nuke(it))
        return answers[0]
    assert "for everyone else" in press(7)                               # the buyer can't claim his own nuke
    assert "You got **250** coins" in press(8) and db.wallet(8)["points"] == 250
    assert "already claimed" in press(8) and db.wallet(8)["points"] == 250
    assert msg.embed.footer.text == "Claimed: 1 / 2"
    assert "You got" in press(9)
    assert "All claimed" in msg.embed.description and msg.view.claim.disabled
    assert "Too late" in press(10) and db.wallet(10)["points"] == 0
    assert db.point_history(8)[0]["reason"] == "nuke: Nuke"


def test_nuke_expires_and_refunds_when_it_cant_drop(eco):
    db = eco.db
    db.add_shop_item("Big nuke", "nuke", "10", 0, 100, None)            # 0 = every member
    db.add_points(7, 100, "test")

    class Broken:
        id, mention = 1, "#x"

        async def send(self, **kw):
            raise RuntimeError("missing permissions")
    r = asyncio.run(eco.cog.buy(eco.member, "Big nuke", Broken()))
    assert not r.ok and "refunded" in r.message and db.wallet(7)["points"] == 100
    assert db._db.execute("SELECT COUNT(*) FROM nuke_drops").fetchone()[0] == 0
    drop = db.create_nuke(7, "Big nuke", 10, 0, int(time.time()) + 60)
    assert all(db.claim_nuke(drop, uid) == "ok" for uid in range(100, 130))   # no limit
    assert db.claim_nuke(drop, 200, now=int(time.time()) + 61) == "expired"
