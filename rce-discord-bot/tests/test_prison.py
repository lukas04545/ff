"""Prison (jail): /prison jail/free/list, lockdown overwrites, role removal and restore, ticket stays visible."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS

import discord
import pytest

from storage import Storage


class FakeRole:
    def __init__(self, rid, name, position, *, default=False, managed=False):
        self.id, self.name, self.position, self.managed = rid, name, position, managed
        self._default = default

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position

    def __eq__(self, other):
        return isinstance(other, FakeRole) and other.id == self.id

    __hash__ = object.__hash__


class FakeChannel:
    def __init__(self, cid, name):
        self.id, self.name = cid, name
        self.mention = f"#{name}"
        self.overwrites = {}
        self.sent = []

    def overwrites_for(self, target):
        return self.overwrites.get(getattr(target, "id", target), discord.PermissionOverwrite())

    async def set_permissions(self, target, reason=None, **perms):
        self.overwrites[target.id] = discord.PermissionOverwrite(**perms)

    async def send(self, content=None, allowed_mentions=None, **kw):
        self.sent.append(content)



@pytest.fixture
def jail(tmp_path, monkeypatch):
    for k, v in dict(DISCORD_TOKEN="x", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw", ADMIN_ROLE_IDS="1",
                     DATABASE_PATH=str(tmp_path / "bot.db"), SUPPORT_KNOWLEDGE_FILE=str(tmp_path / "k.md"),
                     PRISON_ROLE_ID="50", PRISON_CHANNEL_ID="900").items():
        monkeypatch.setenv(k, v)
    from bot import RceBot
    from cogs.prison import Prison
    from config import load_config
    bot = RceBot(load_config("/nonexistent"))
    audits = []
    bot.audit = audits.append
    cog = Prison(bot)

    bot_role = FakeRole(99, "Bot", 100)
    prison_role = FakeRole(50, "Prison", 5)
    vip = FakeRole(10, "VIP", 20)
    everyone = FakeRole(1, "@everyone", 0, default=True)
    booster = FakeRole(11, "Booster", 30, managed=True)
    channels = {900: FakeChannel(900, "prison"), 200: FakeChannel(200, "general"),
                201: FakeChannel(201, "events"), 300: FakeChannel(300, "cat")}
    roles = {r.id: r for r in (bot_role, prison_role, vip, everyone, booster)}

    dms = []

    class Member:
        def __init__(self, uid, name, member_roles):
            self.id, self.name, self.roles = uid, name, list(member_roles)
            self.mention = f"<@{uid}>"
            self.guild = guild

        @property
        def top_role(self):
            return max(self.roles, key=lambda r: r.position)

        async def add_roles(self, *rs, reason=None):
            for r in rs:
                if r not in self.roles:
                    self.roles.append(r)

        async def remove_roles(self, *rs, reason=None):
            self.roles = [r for r in self.roles if r not in rs]

        async def send(self, embed=None, **kw):
            dms.append(embed)

        def __str__(self):
            return self.name

    guild = NS(id=1, name="Kryvon", me=NS(top_role=bot_role), channels=list(channels.values()))
    guild.get_role = roles.get
    guild.get_channel = channels.get
    member = Member(7, "Troll", [everyone, vip, booster])
    return NS(bot=bot, cog=cog, guild=guild, member=member, roles=roles, channels=channels, audits=audits, dms=dms,
              prison_role=prison_role, vip=vip, booster=booster, everyone=everyone, Member=Member)


def interaction(jail, user):
    out = NS(followups=[], replies=[])

    async def send_message(text=None, embed=None, ephemeral=False):
        out.replies.append(NS(text=text, embed=embed))

    async def defer(ephemeral=False):
        pass

    async def followup_send(text=None, embed=None, ephemeral=False):
        out.followups.append(NS(text=text, embed=embed))
    i = NS(guild=jail.guild, user=user, response=NS(send_message=send_message, defer=defer),
           followup=NS(send=followup_send))
    return i, out


def run(coro):
    return asyncio.run(coro)


def test_jail_locks_down_and_removes_roles(jail):
    staff = jail.Member(2, "Mod", [jail.roles[99]])
    i, out = interaction(jail, staff)
    run(jail.cog.jail.callback(jail.cog, i, jail.member, "spamming"))

    # Member now has only the prison role (VIP removed; @everyone and the managed booster stay)
    assert jail.prison_role in jail.member.roles and jail.vip not in jail.member.roles
    assert jail.booster in jail.member.roles and jail.everyone in jail.member.roles
    # Stored for restore: only VIP
    row = jail.bot.db.prisoner(1, 7)
    assert row["roles"] == "10" and row["reason"] == "spamming"
    # Lockdown: prison role denied view everywhere except the prison channel (allowed + can write)
    assert jail.channels[200].overwrites_for(jail.prison_role).view_channel is False
    assert jail.channels[300].overwrites_for(jail.prison_role).view_channel is False
    pc = jail.channels[900].overwrites_for(jail.prison_role)
    assert pc.view_channel is True and pc.send_messages is True
    assert jail.channels[900].sent and "<@7>" in jail.channels[900].sent[0] and jail.dms       # DM + channel notice
    assert any("jailed" in a for a in jail.audits) and "in prison" in out.followups[-1].text

    # A new channel created while someone is jailed gets locked too
    new = jail.channels[202] = FakeChannel(202, "new")
    new.guild = jail.guild
    jail.guild.channels.append(new)
    run(jail.cog.on_guild_channel_create(new))
    assert new.overwrites_for(jail.prison_role).view_channel is False

    # Can't jail twice
    i2, out2 = interaction(jail, staff)
    run(jail.cog.jail.callback(jail.cog, i2, jail.member, None))
    assert "already in prison" in out2.replies[-1].text


def test_free_restores_roles(jail):
    staff = jail.Member(2, "Mod", [jail.roles[99]])
    run(jail.cog.jail.callback(jail.cog, interaction(jail, staff)[0], jail.member, None))
    i, out = interaction(jail, staff)
    run(jail.cog.free.callback(jail.cog, i, jail.member))
    assert jail.prison_role not in jail.member.roles and jail.vip in jail.member.roles   # VIP given back
    assert jail.bot.db.prisoner(1, 7) is None and "free" in out.followups[-1].text
    # Freeing someone not jailed
    i2, out2 = interaction(jail, staff)
    run(jail.cog.free.callback(jail.cog, i2, jail.member))
    assert "isn't in prison" in out2.replies[-1].text


def test_setup_errors_and_self_jail(jail):
    staff = jail.Member(2, "Mod", [jail.roles[99]])
    # No role configured
    jail.bot.config = replace(jail.bot.config, prison_role_id=None)
    i, out = interaction(jail, staff)
    run(jail.cog.jail.callback(jail.cog, i, jail.member, None))
    assert "No prison role" in out.replies[-1].text
    jail.bot.config = replace(jail.bot.config, prison_role_id=50, prison_channel_id=None)
    i, out = interaction(jail, staff)
    run(jail.cog.jail.callback(jail.cog, i, jail.member, None))
    assert "No prison channel" in out.replies[-1].text
    # Can't jail yourself
    jail.bot.config = replace(jail.bot.config, prison_channel_id=900)
    me = jail.Member(2, "Mod", [jail.roles[99]])
    i, out = interaction(jail, me)
    run(jail.cog.jail.callback(jail.cog, i, me, None))
    assert "can't jail yourself" in out.replies[-1].text


def test_ticket_stays_visible_mechanism():
    """A per-member allow overwrite beats the prison role's channel deny (Discord permission resolution)."""
    base = discord.Permissions(view_channel=True)
    role_deny = discord.PermissionOverwrite(view_channel=False)
    member_allow = discord.PermissionOverwrite(view_channel=True)
    allow, deny = role_deny.pair()
    perms = discord.Permissions(base.value)
    perms.value = (perms.value & ~deny.value) | allow.value          # role overwrite denies view
    assert perms.view_channel is False
    allow2, deny2 = member_allow.pair()
    perms.value = (perms.value & ~deny2.value) | allow2.value        # member overwrite (applied last) re-allows
    assert perms.view_channel is True


def test_storage_jail_free_roundtrip(tmp_path):
    db = Storage(tmp_path / "b.db")
    db.jail(1, 7, [10, 20, 30], "test", "Mod")
    assert db.prisoner(1, 7)["roles"] == "10,20,30"
    assert [r["user_id"] for r in db.prisoners(1)] == [7]
    assert db.free(1, 7) == [10, 20, 30] and db.prisoner(1, 7) is None and db.free(1, 7) == []
    db.close()


def test_storage_timed_jail_and_expiry(tmp_path):
    import time
    db = Storage(tmp_path / "b.db")
    db.jail(1, 7, [10], "timed", "Mod", expires_at=int(time.time()) - 5)        # already expired
    db.jail(1, 8, [20], "forever", "Mod")                                        # no timer
    db.jail(1, 9, [30], "future", "Mod", expires_at=int(time.time()) + 3600)     # still running
    assert db.prisoner(1, 7)["expires_at"] is not None and db.prisoner(1, 8)["expires_at"] is None
    expired = [r["user_id"] for r in db.expired_prisoners()]
    assert expired == [7]                                                        # only the past-due timed jail
    db.close()


def test_jail_with_timer_and_auto_release(jail):
    staff = jail.Member(2, "Mod", [jail.roles[99]])
    i, out = interaction(jail, staff)
    run(jail.cog.jail.callback(jail.cog, i, jail.member, "spamming", 30))         # 30-minute jail
    row = jail.bot.db.prisoner(1, 7)
    assert row["expires_at"] is not None and jail.prison_role in jail.member.roles
    assert "for 30 minutes" in out.followups[-1].text
    assert any("30 minutes" in d.description for d in jail.dms)                   # the DM says how long

    # Force the timer into the past and run the auto-release loop: the member is freed and told so
    jail.bot.db._db.execute("UPDATE prison SET expires_at = ? WHERE user_id = 7", (1,))
    jail.bot.db._db.commit()
    jail.bot.get_guild = lambda gid: jail.guild if gid == 1 else None
    jail.guild.get_member = lambda uid: jail.member if uid == 7 else None
    run(jail.cog.auto_release.coro(jail.cog))                                     # run the loop body once
    assert jail.bot.db.prisoner(1, 7) is None                                    # released
    assert jail.prison_role not in jail.member.roles and jail.vip in jail.member.roles
    assert any("time is up" in c for c in jail.channels[900].sent)


def test_ai_jail_helper(jail):
    """The shared jail_member/free_member the AI moderation uses."""
    run_err = run(jail.cog.jail_member(jail.guild, jail.member, "AI: slur", "AI moderation", minutes=60,
                                       announce=False))
    assert run_err is None and jail.bot.db.prisoner(1, 7)["expires_at"] is not None
    assert jail.prison_role in jail.member.roles
    assert not jail.channels[900].sent                                           # announce=False: no channel notice
    # Jailing again is refused with a short reason the AI can check for
    assert run(jail.cog.jail_member(jail.guild, jail.member, None, "AI moderation", minutes=60)) == "already in prison"
