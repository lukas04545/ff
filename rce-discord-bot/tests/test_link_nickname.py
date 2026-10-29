"""On link, the member's Discord nickname is set to their in-game name; a button does it for everyone already linked."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import discord
import pytest


class FakeMember:
    def __init__(self, uid, nick=None, *, fail=None):
        self.id = uid
        self.nick = nick
        self.fail = fail          # None / "forbidden" / "http"
        self.edits = []

    async def edit(self, *, nick=None, reason=None):
        if self.fail == "forbidden":
            raise discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")
        if self.fail == "http":
            raise discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
        self.nick = nick
        self.edits.append(nick)


class FakeGuild:
    def __init__(self, members):
        self._members = {m.id: m for m in members}

    def get_member(self, uid):
        return self._members.get(uid)

    async def fetch_member(self, uid):
        if uid in self._members:
            return self._members[uid]
        raise discord.HTTPException(SimpleNamespace(status=404, reason="x"), "not found")


@pytest.fixture
def cog(tmp_path, monkeypatch):
    for k, v in dict(DISCORD_TOKEN="x", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw",
                     ADMIN_ROLE_IDS="1", DATABASE_PATH=str(tmp_path / "bot.db")).items():
        monkeypatch.setenv(k, v)
    from bot import RceBot
    from cogs.custom_kits import CustomKits
    from config import load_config
    bot = RceBot(load_config("/nonexistent"))
    c = CustomKits(bot)
    yield c
    bot.db.close()


def run(coro):
    return asyncio.run(coro)


def test_set_member_nickname_statuses(cog):
    guild = FakeGuild([FakeMember(1), FakeMember(2, nick="Bob"),
                       FakeMember(3, fail="forbidden"), FakeMember(4, fail="http")])
    monkeypatch_guild(cog, guild)
    assert run(cog.set_member_nickname(1, "Alice")) == "changed" and guild.get_member(1).nick == "Alice"
    assert run(cog.set_member_nickname(2, "Bob")) == "already"                # already matches
    assert run(cog.set_member_nickname(3, "Cara")) == "no-permission"         # owner / higher role
    assert run(cog.set_member_nickname(4, "Dan")) == "error"
    assert run(cog.set_member_nickname(999, "Ghost")) == "not-in-server"      # not in the guild
    # long in-game names are cut to Discord's 32-character limit
    long = "x" * 40
    assert run(cog.set_member_nickname(1, long)) == "changed" and guild.get_member(1).nick == "x" * 32


def test_nickname_respects_the_setting(cog):
    guild = FakeGuild([FakeMember(1)])
    monkeypatch_guild(cog, guild)
    cog.bot.config = replace(cog.bot.config, link_set_nickname=False)
    assert run(cog.set_member_nickname(1, "Alice")) == "off" and guild.get_member(1).nick is None
    assert run(cog.set_member_nickname(1, "Alice", respect_setting=False)) == "changed"   # the button ignores it


def test_sync_all_nicknames_button(cog):
    db = cog.bot.db
    db.link_player(1, "Alice")
    db.link_player(2, "Bob")
    db.link_player(3, "Cara")
    db.link_player(9, "Ghost")                       # not in the guild
    guild = FakeGuild([FakeMember(1), FakeMember(2, nick="Bob"), FakeMember(3, fail="forbidden")])
    monkeypatch_guild(cog, guild)
    cog.bot.config = replace(cog.bot.config, link_set_nickname=False)     # the button works even with it off
    counts = run(cog.sync_all_nicknames())
    assert counts["total"] == 4 and counts["changed"] == 1 and counts["already"] == 1
    assert counts["no-permission"] == 1 and counts["not-in-server"] == 1
    assert guild.get_member(1).nick == "Alice"


def monkeypatch_guild(cog, guild):
    cog._guild = lambda: guild
