"""/countdown: one message that edits itself down, pings a role at most once, only in its own channel."""
import asyncio
from types import SimpleNamespace as NS

import discord
import pytest

from cogs.countdown import MAX_TICKS, Countdown


class FakeRole:
    def __init__(self, rid=5, default=False, managed=False):
        self.id, self.mention, self.managed = rid, f"<@&{rid}>", managed
        self._default = default

    def is_default(self):
        return self._default


@pytest.fixture
def cd(monkeypatch):
    _real_sleep = asyncio.sleep
    monkeypatch.setattr("cogs.countdown.asyncio.sleep", lambda *_a, **_k: _real_sleep(0))
    bot = NS(audit=lambda *_a: None)
    cog = Countdown(bot)
    sent, edits = [], []

    class Message:
        async def edit(self, embed=None, allowed_mentions=None):
            edits.append((embed, allowed_mentions))

    class Channel(discord.abc.Messageable):
        id = 100

        async def _get_channel(self):
            return self

        async def send(self, content=None, embed=None, allowed_mentions=None):
            sent.append(NS(content=content, embed=embed, allowed_mentions=allowed_mentions))
            return Message()
    return NS(cog=cog, bot=bot, channel=Channel(), sent=sent, edits=edits)


def interaction(cd):
    replies = []

    async def send_message(text=None, ephemeral=False):
        replies.append(NS(text=text, ephemeral=ephemeral))
    return NS(channel=cd.channel, user=NS(id=1, __str__=lambda: "Staff"),
              response=NS(send_message=send_message)), replies


def run(cd, **kw):
    i, replies = interaction(cd)
    kw.setdefault("step", 10)
    kw.setdefault("interval", 10)
    kw.setdefault("title", None)
    kw.setdefault("role", None)
    kw.setdefault("done", None)
    asyncio.run(cd.cog.countdown.callback(cd.cog, i, **kw))
    return replies


def test_counts_down_in_place_with_one_ping(cd):
    role = FakeRole()
    replies = run(cd, start=100, step=10, interval=10, title="Wipe", role=role, done="Wiped!")
    assert replies[0].ephemeral and "Countdown started" in replies[0].text
    # One message posted in this channel, with the single role ping
    assert len(cd.sent) == 1 and cd.sent[0].content == "<@&5>"
    assert cd.sent[0].allowed_mentions.roles == [role] and cd.sent[0].embed.description == "# 100"
    # Then only edits – 10 of them (90, 80, …, 0), and none of them ping
    assert len(cd.edits) == 10
    assert all(not am.roles and am.everyone is False and am.users is False for _, am in cd.edits)
    assert [e.description for e, _ in cd.edits][:2] == ["# 90", "# 80"]
    assert cd.edits[-1][0].description == "# Wiped!" and cd.edits[-1][0].colour == discord.Colour.green()


def test_no_role_means_no_ping(cd):
    run(cd, start=20, step=10)
    assert cd.sent[0].content is None and cd.sent[0].allowed_mentions.roles == []
    assert [e.description for e, _ in cd.edits] == ["# 10", "# Go!"]


def test_limits(cd):
    assert "too many steps" in run(cd, start=100000, step=1, interval=5)[0].text.lower()  # > MAX_TICKS
    assert cd.sent == []
    assert "not @everyone" in run(cd, start=10, role=FakeRole(default=True))[0].text
    assert "not @everyone" in run(cd, start=10, role=FakeRole(managed=True))[0].text
    # Too many at once in the same channel
    cd.cog._running[cd.channel.id] = 3
    assert "Too many countdowns" in run(cd, start=10)[0].text
    assert MAX_TICKS == 200
