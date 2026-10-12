"""AI chat moderation: offensive messages in watched channels get a warning, repeat offenders a timeout."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from cogs.automod import parse_flags
from tests.test_kit_requests import env  # noqa: F401  (fixture: bot + Tickets cog with staff role 42)


def test_parse_flags():
    raw = json.dumps({"flags": [{"id": "1", "severity": "HIGH", "reason": "racist slur"},
                                {"id": "99", "severity": "low", "reason": "x"}, "junk"]})
    assert parse_flags(raw, {"1", "2"}) == [{"id": "1", "severity": "high", "reason": "racist slur"}]
    assert parse_flags("nope", {"1"}) == [] and parse_flags('{"flags": []}', {"1"}) == []


@pytest.fixture
def mod(env):
    from cogs.automod import AutoMod
    env.bot.config = replace(env.bot.config, ai_mod_channel_ids=frozenset({500}), ai_mod_timeout_after=2,
                             ai_mod_timeout_minutes=10, ai_mod_delete=True)
    cog = AutoMod(env.bot)
    calls, verdicts = [], {}

    async def chat_json(messages, **kw):
        calls.append(messages[1]["content"])
        return json.dumps({"flags": [{"id": mid, **v} for mid, v in verdicts.items() if f"[id {mid}]" in calls[-1]]})
    env.tickets.ai = NS(enabled=True, chat_json=chat_json)
    out = NS(replies=[], sent=[], deleted=[], timeouts=[])
    channel = NS(id=500, name="general", mention="#general")

    async def send(text, **kw):
        out.sent.append(text)
    channel.send = send
    guild = NS(id=1)

    def message(mid, text, author):
        async def reply(t, **kw):
            out.replies.append(t)

        async def delete():
            out.deleted.append(mid)
        return NS(id=mid, content=text, author=author, guild=guild, channel=channel, reply=reply, delete=delete,
                  jump_url=f"https://discord.com/channels/1/500/{mid}")
    def Member(uid, roles=()):
        async def timeout(delta, reason=None):
            out.timeouts.append((uid, delta.total_seconds()))
        return NS(id=uid, bot=False, display_name=f"user{uid}", mention=f"<@{uid}>", roles=list(roles),
                  timeout=timeout, __str__=lambda: f"user{uid}")
    return NS(cog=cog, env=env, out=out, calls=calls, verdicts=verdicts, message=message, Member=Member)


def say(m, mid, text, author):
    asyncio.run(m.cog.on_message(m.message(mid, text, author)))


def test_flags_warn_delete_and_timeout(mod):
    troll, friendly = mod.Member(10), mod.Member(11)
    mod.verdicts.update({"1": {"severity": "low", "reason": "insulting another player"},
                         "3": {"severity": "high", "reason": "hate speech"}})
    say(mod, 1, "you are a worthless idiot", troll)
    say(mod, 2, "lol get raided ez", friendly)
    flags = asyncio.run(mod.cog.flush())
    assert [f["id"] for f in flags] == ["1"] and len(mod.calls) == 1        # one AI call for the whole batch
    assert "[id 1] user10: you are a worthless idiot" in mod.calls[0] and "[id 2]" in mod.calls[0]
    assert mod.out.replies == ["⚠️ <@10>, please keep it respectful – insulting another player. (warning 1/2)"]
    assert mod.out.deleted == [] and mod.out.timeouts == []                 # low severity: warn only

    mod.cog._last_warn.clear()
    say(mod, 3, "[slur]", troll)
    asyncio.run(mod.cog.flush())
    assert mod.out.deleted == [3]                                           # severe + AI_MOD_DELETE
    assert mod.out.timeouts == [(10, 600.0)]                                # 2nd warning in 24 h -> 10 min timeout
    assert "Timed out for 10 minutes" in mod.out.sent[-1]
    assert "CONTEXT (already checked)" in mod.calls[1] and "user11: lol get raided ez" in mod.calls[1]
    assert mod.env.db.count_ai_warnings(1, 10) == 2 and mod.env.db.count_ai_warnings(1, 11) == 0


def test_staff_bots_and_other_channels_are_skipped(mod):
    staff = mod.Member(12, roles=[NS(id=42)])                               # support role
    say(mod, 5, "you idiot", staff)
    bot_author = mod.Member(13)
    bot_author.bot = True
    say(mod, 6, "you idiot", bot_author)
    other = mod.message(7, "you idiot", mod.Member(14))
    other.channel = NS(id=999)
    asyncio.run(mod.cog.on_message(other))
    assert asyncio.run(mod.cog.flush()) == [] and mod.calls == []           # nothing sent to the AI
    mod.env.tickets.ai = NS(enabled=False)
    say(mod, 8, "you idiot", mod.Member(15))
    assert mod.cog._pending == []                                           # no AI key -> off


def test_staff_commands(mod):
    troll = mod.Member(10)
    mod.verdicts["1"] = {"severity": "low", "reason": "insult"}
    say(mod, 1, "idiot", troll)
    asyncio.run(mod.cog.flush())
    answers = []

    async def send_message(text, **kw):
        answers.append(text)
    inter = NS(guild_id=1, user="Mod", response=NS(send_message=send_message))
    asyncio.run(mod.cog.warnings_cmd.callback(mod.cog, inter, troll))
    assert "insult" in answers[-1] and "“idiot”" in answers[-1]
    asyncio.run(mod.cog.clear_cmd.callback(mod.cog, inter, troll))
    assert "1 warning(s)" in answers[-1] and mod.env.db.count_ai_warnings(1, 10) == 0
