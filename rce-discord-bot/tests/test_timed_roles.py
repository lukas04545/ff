"""Timed roles: give/extend/expire, protection of permanent roles, giveaway prizes with a duration."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from cogs.timed_roles import TimedRoleError, TimedRoles
from storage import Storage
from support.giveaway_wins import prize_duration_minutes
from utils import parse_duration


class FakeRole:
    def __init__(self, rid, name, position, managed=False):
        self.id, self.name, self.position, self.managed = rid, name, position, managed

    def is_default(self):
        return self.position == 0

    def __ge__(self, other):
        return self.position >= other.position

    def __eq__(self, other):
        return isinstance(other, FakeRole) and other.id == self.id

    __hash__ = object.__hash__


class FakeMember:
    def __init__(self, uid, guild, roles=()):
        self.id, self.guild, self.roles = uid, guild, list(roles)
        self.mention = f"<@{uid}>"
        self.log = []

    async def add_roles(self, role, reason=None):
        self.log.append(("add", role.name))
        self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        self.log.append(("remove", role.name))
        self.roles.remove(role)


@pytest.fixture
def env(tmp_path):
    vip, staff, top = FakeRole(55, "VIP", 5), FakeRole(99, "Staff", 20), FakeRole(10, "Bot", 10)
    guild = SimpleNamespace(id=1, name="Srv", me=SimpleNamespace(top_role=top))
    members = {7: FakeMember(7, guild), 8: FakeMember(8, guild, [vip])}
    guild.get_member = members.get
    guild.get_role = {55: vip, 99: staff}.get
    audit = []
    bot = SimpleNamespace(db=Storage(tmp_path / "bot.db"), audit=audit.append, get_guild=lambda gid: guild,
                          get_user=lambda uid: None)
    yield SimpleNamespace(bot=bot, cog=TimedRoles(bot), guild=guild, members=members, vip=vip, staff=staff,
                          audit=audit)
    bot.db.close()


def test_give_extend_and_expire(env):
    m = env.members[7]
    until = asyncio.run(env.cog.grant(m, env.vip, 60, reason="test", by="admin"))
    assert env.vip in m.roles and abs(until - (time.time() + 3600)) < 5
    until2 = asyncio.run(env.cog.grant(m, env.vip, 60, reason="again", by="admin"))
    assert until2 == until + 3600 and m.log == [("add", "VIP")]      # extended, not given twice
    assert "until <t:" in env.audit[0]

    env.bot.db.set_timed_role(1, 7, 55, int(time.time()) - 1, "test", "admin")   # now overdue
    asyncio.run(env.cog.expire_loop.coro(env.cog))
    assert env.vip not in m.roles and env.bot.db.timed_roles() == [] and "ended" in env.audit[-1]


def test_permanent_roles_and_errors(env):
    owner = env.members[8]
    assert env.cog.permanent(owner, env.vip)                          # has VIP without timer
    with pytest.raises(TimedRoleError, match="above the bot"):
        asyncio.run(env.cog.grant(env.members[7], env.staff, 60, reason="x", by="y"))
    with pytest.raises(TimedRoleError):
        asyncio.run(env.cog.grant(env.members[7], env.vip, 0, reason="x", by="y"))


def test_durations():
    assert parse_duration("2w") == 20160 and parse_duration("1w 2d") == 12960 and parse_duration("7d") == 10080
    assert prize_duration_minutes("VIP Kit 7 days") == 10080 and prize_duration_minutes("VIP (2 weeks)") == 20160
    assert prize_duration_minutes("Vip Standard") is None and prize_duration_minutes("5.56 Ammo x100") is None


def test_ai_redeems_prize_with_duration(tmp_path, monkeypatch):
    """"VIP Kit 7 days" -> the kit's role for 7 days; GIVEAWAY_PRIZE_DAYS covers prizes without a duration."""
    for k, v in dict(DISCORD_TOKEN="x", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw",
                     ADMIN_ROLE_IDS="1", DATABASE_PATH=str(tmp_path / "bot.db"), TICKET_CATEGORY_ID="5",
                     SUPPORT_KNOWLEDGE_FILE=str(tmp_path / "k.md"), GIVEAWAY_PRIZE_DAYS="3").items():
        monkeypatch.setenv(k, v)
    from bot import RceBot
    from cogs.custom_kits import CustomKits
    from cogs.tickets import Tickets
    from config import load_config
    bot = RceBot(load_config("/nonexistent"))
    bot.db.create_custom_kit("VIP", None, "admin")
    bot.db.set_custom_kit_roles("VIP", [55])
    for mid, prize in ((901, "VIP Kit 7 days"), (902, "Vip Standard")):
        gid = bot.db.create_giveaway(1, 2, prize, 1, 3, ends_at=1000)
        bot.db.set_giveaway_message(gid, mid)
        bot.db.finish_giveaway(gid, [7])
    vip, top = FakeRole(55, "VIP", 5), FakeRole(10, "Bot", 10)
    guild = SimpleNamespace(id=1, name="Srv", me=SimpleNamespace(top_role=top), get_role={55: vip}.get,
                            get_channel=lambda cid: None)
    member = FakeMember(7, guild)
    member.name, member.display_name = "bob", "Bob"
    guild.get_member = {7: member}.get
    posted = []

    async def send(content=None, **kw):
        posted.append(content)
    channel = SimpleNamespace(id=100, guild=guild, send=send)
    tid = bot.db.create_ticket(7, "prize", None)
    bot.db.update_ticket(tid, channel_id=100)
    monkeypatch.setattr(bot, "get_guild", lambda gid: guild)

    async def no_loop(self):
        return None
    monkeypatch.setattr(TimedRoles, "cog_load", no_loop)     # no expiry loop without a Discord login

    async def add():
        await bot.add_cog(CustomKits(bot))
        await bot.add_cog(TimedRoles(bot))
        t = Tickets(bot)
        await bot.add_cog(t)
        return t
    tickets = asyncio.run(add())
    ticket = bot.db.ticket_by_channel(100)

    def run(name, args):
        call = {"id": "c", "type": "function", "function": {"name": name, "arguments": __import__("json").dumps(args)}}
        return asyncio.run(tickets._run_tool(call, ticket, channel))

    wins = {w["message_id"]: w for w in run("get_my_giveaway_wins", {})["wins"]}
    assert wins["901"]["duration"] == "7d 0h" and wins["902"]["duration"] == "3d 0h"
    r = run("redeem_giveaway_win", {"message_id": "901"})
    assert r["ok"] and r["action"] == "role VIP for 7d 0h" and r["until_discord"].startswith("until <t:")
    row = bot.db.timed_role(1, 7, 55)
    assert row and abs(row["expires_at"] - (time.time() + 7 * 86400)) < 10
    assert "until <t:" in posted[-1]
    r2 = run("redeem_giveaway_win", {"message_id": "902"})                # 3 days (setting) on top
    assert r2["ok"] and abs(bot.db.timed_role(1, 7, 55)["expires_at"] - (time.time() + 10 * 86400)) < 10
    bot.db.close()
