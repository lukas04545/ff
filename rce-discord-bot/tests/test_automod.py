"""AI chat moderation: offensive messages in watched channels get a warning, repeat offenders a timeout."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from cogs.automod import parse_flags, system_prompt
from tests.test_kit_requests import env  # noqa: F401  (fixture: bot + Tickets cog with staff role 42)


def test_parse_flags():
    raw = json.dumps({"flags": [{"id": "1", "severity": "HIGH", "reason": "racist slur"},
                                {"id": "99", "severity": "low", "reason": "x"}, "junk"]})
    assert parse_flags(raw, {"1", "2"}) == [{"id": "1", "severity": "high", "reason": "racist slur", "warning": None,
                                             "mute_minutes": None, "jail_minutes": None, "jail_permanent": False}]
    clamp = lambda m: parse_flags(json.dumps({"flags": [{"id": "1", "mute_minutes": m}]}), {"1"})[0]["mute_minutes"]  # noqa
    assert (clamp(1), clamp(45), clamp("90"), clamp(500), clamp("x")) == (5, 45, 90, 120, None)
    # jail_minutes only when jailing is on (jail_max > 0), clamped to 1..jail_max; 0/blank/invalid -> None
    jail = lambda m, mx=60: parse_flags(json.dumps({"flags": [{"id": "1", "jail_minutes": m}]}), {"1"}, mx)[0]["jail_minutes"]  # noqa
    assert (jail(30), jail(500), jail(0), jail("x"), jail(30, 0)) == (30, 60, None, None, None)
    # jail_permanent is only honoured while jailing is on (jail_max > 0)
    perm = lambda v, mx=60: parse_flags(json.dumps({"flags": [{"id": "1", "jail_permanent": v}]}), {"1"}, mx)[0]["jail_permanent"]  # noqa
    assert (perm(True), perm("true"), perm(False), perm(True, 0)) == (True, True, False, False)
    w = parse_flags(json.dumps({"flags": [{"id": "1", "warning": "Hey, das war beleidigend @everyone <@123> – "
                                                                "bitte bleib respektvoll. https://x.y"}]}), {"1"})
    assert w[0]["warning"] == "Hey, das war beleidigend – bitte bleib respektvoll."
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
    mod.verdicts.update({"1": {"severity": "low", "reason": "insulting another player",
                               "warning": "Calling other players worthless isn't okay here – please keep it respectful."},
                         "3": {"severity": "high", "reason": "hate speech", "mute_minutes": 90}})
    say(mod, 1, "you are a worthless idiot", troll)
    say(mod, 2, "lol get raided ez", friendly)
    flags = asyncio.run(mod.cog.flush())
    assert [f["id"] for f in flags] == ["1"] and len(mod.calls) == 1        # one AI call for the whole batch
    assert "[id 1] user10 (warnings today: 0): you are a worthless idiot" in mod.calls[0] and "[id 2]" in mod.calls[0]
    assert mod.out.replies == ["⚠️ <@10> Calling other players worthless isn't okay here – please keep it "
                               "respectful. `1/2`"]
    assert mod.out.deleted == [] and mod.out.timeouts == []                 # low severity: warn only

    mod.cog._last_warn.clear()
    say(mod, 3, "[slur]", troll)
    asyncio.run(mod.cog.flush())
    assert mod.out.deleted == [3]                                           # severe + AI_MOD_DELETE
    assert mod.out.timeouts == [(10, 5400.0)]                               # 2nd warning: the AI chose 90 minutes
    assert "Timed out for 1 hour 30 minutes" in mod.out.sent[-1]
    assert "(warnings today: 1)" in mod.calls[1]                            # the AI sees earlier warnings
    assert mod.out.sent[-1].startswith("⚠️ <@10> please keep it respectful – hate speech. `2/2`")  # no AI text
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


def test_ai_jails_serious_offender(mod):
    """With AI_MOD_JAIL on and the prison set up, a jail_minutes verdict puts the member in prison (no timeout)."""
    mod.env.bot.config = replace(mod.env.bot.config, ai_mod_jail=True, ai_mod_jail_minutes=60,
                                 prison_role_id=50, prison_channel_id=900)
    jailed = []

    async def jail_member(guild, member, reason, by, *, minutes=None, announce=True):
        jailed.append((member.id, reason, by, minutes, announce))
        return None
    real_get_cog = mod.env.bot.__class__.get_cog.__get__(mod.env.bot)
    mod.env.bot.get_cog = lambda name: NS(jail_member=jail_member) if name == "Prison" else real_get_cog(name)

    troll = mod.Member(10)
    mod.verdicts["1"] = {"severity": "high", "reason": "racist slur", "jail_minutes": 120}
    say(mod, 1, "[slur]", troll)
    asyncio.run(mod.cog.flush())
    # The AI asked for 120 but the cap is 60; parse clamps it, and the member is jailed, not timed out
    assert jailed == [(10, "AI moderation: racist slur", "AI moderation", 60, False)]
    assert mod.out.timeouts == [] and "🔒 Sent to prison for 1 hour" in mod.out.sent[-1]


def test_ai_permanent_jail(mod):
    """AI_MOD_JAIL_PERMANENT lets a jail_permanent verdict jail with no timer (minutes=None)."""
    jailed = []

    async def jail_member(guild, member, reason, by, *, minutes="unset", announce=True):
        jailed.append(minutes)
        return None
    real_get_cog = mod.env.bot.__class__.get_cog.__get__(mod.env.bot)
    mod.env.bot.get_cog = lambda name: NS(jail_member=jail_member) if name == "Prison" else real_get_cog(name)

    # Permanent allowed: jail_permanent wins over jail_minutes and jails with no timer
    mod.env.bot.config = replace(mod.env.bot.config, ai_mod_jail=True, ai_mod_jail_minutes=60,
                                 ai_mod_jail_permanent=True, prison_role_id=50, prison_channel_id=900)
    assert "PERMANENT PRISON is allowed" in system_prompt("", jail_max=60, allow_permanent=True)
    mod.verdicts["1"] = {"severity": "high", "reason": "threats", "jail_minutes": 30, "jail_permanent": True}
    say(mod, 1, "[threat]", mod.Member(10))
    asyncio.run(mod.cog.flush())
    assert jailed == [None] and "🔒 Sent to prison until staff free you." in mod.out.sent[-1]

    # Permanent NOT allowed: the same verdict falls back to a timed jail
    jailed.clear(); mod.cog._last_warn.clear(); mod.cog._context.clear()
    mod.env.bot.config = replace(mod.env.bot.config, ai_mod_jail_permanent=False)
    mod.verdicts["2"] = {"severity": "high", "reason": "threats", "jail_minutes": 30, "jail_permanent": True}
    say(mod, 2, "[threat again]", mod.Member(11))
    asyncio.run(mod.cog.flush())
    assert jailed == [30] and "🔒 Sent to prison for 30 minutes" in mod.out.sent[-1]


def test_ai_jail_off_without_setup(mod):
    """No prison role/channel (or AI_MOD_JAIL off): jail_minutes is ignored, the AI falls back to timeouts."""
    mod.env.bot.config = replace(mod.env.bot.config, ai_mod_jail=True, prison_role_id=None, prison_channel_id=None)
    assert mod.cog._jail_max() == 0                                   # not set up -> AI isn't told it may jail
    troll = mod.Member(10)
    mod.verdicts["1"] = {"severity": "high", "reason": "slur", "jail_minutes": 30}
    say(mod, 1, "[slur]", troll)
    asyncio.run(mod.cog.flush())
    assert not any("prison" in a.lower() for a in mod.out.sent)       # jail_minutes dropped (jail_max 0)


def test_owner_instructions_reach_the_ai(mod):
    from cogs.automod import SYSTEM, system_prompt
    assert system_prompt("").startswith(SYSTEM) and "PRISON is OFF" in system_prompt("")
    p = system_prompt("Be strict: warn for 'stfu'.\nNever warn for 'noob'.")
    assert p.startswith(SYSTEM) and "RULES FROM THE SERVER OWNER" in p and "Never warn for 'noob'." in p
    # When jailing is enabled the prompt tells the AI it may set jail_minutes within the allowed range
    jp = system_prompt("", jail_max=120)
    assert "PRISON is enabled" in jp and "between 1 and 120" in jp
    mod.env.bot.config = replace(mod.env.bot.config, ai_mod_instructions="Warn for 'stfu'.")
    seen = []

    async def chat_json(messages, **kw):
        seen.append(messages[0]["content"])
        return '{"flags": []}'
    mod.env.tickets.ai = NS(enabled=True, chat_json=chat_json)
    say(mod, 1, "stfu", mod.Member(10))
    asyncio.run(mod.cog.flush())
    assert "Warn for 'stfu'." in seen[0]


def test_check_interval(mod):
    from cogs.automod import MAX_BATCH
    user = mod.Member(20)
    # 0 = every message is checked right away
    mod.env.bot.config = replace(mod.env.bot.config, ai_mod_interval=0)
    say(mod, 1, "hello", user)
    assert len(mod.calls) == 1 and "[id 1]" in mod.calls[0]

    # 1 second: messages are collected and checked together after the interval
    mod.env.bot.config = replace(mod.env.bot.config, ai_mod_interval=1)
    mod.calls.clear()

    async def scenario():
        await mod.cog.on_message(mod.message(2, "one", user))
        await mod.cog.on_message(mod.message(3, "two", user))
        assert mod.calls == []                                            # still waiting for the interval
        await asyncio.sleep(1.3)
        assert len(mod.calls) == 1 and "[id 2]" in mod.calls[0] and "[id 3]" in mod.calls[0]
        # More than a full batch: checked at once, the rest after the next interval
        for i in range(MAX_BATCH + 2):
            await mod.cog.on_message(mod.message(100 + i, f"msg {i}", user))
        assert len(mod.calls) == 2 and "[id 129]" in mod.calls[1] and "[id 130]" not in mod.calls[1]
        await asyncio.sleep(1.3)
        await asyncio.sleep(1.3)
        assert len(mod.calls) == 3 and "[id 130]" in mod.calls[2] and "[id 131]" in mod.calls[2]
        assert not mod.cog._pending
    asyncio.run(scenario())
    from settings_schema import validate
    assert validate("AI_MOD_INTERVAL", "60") is None and validate("AI_MOD_INTERVAL", "fast")


def test_warn_cooldown_setting(mod):
    troll = mod.Member(30)
    mod.verdicts.update({str(i): {"severity": "low", "reason": "insult", "warning": "Keep it respectful."}
                         for i in (1, 2, 3)})
    mod.env.bot.config = replace(mod.env.bot.config, ai_mod_interval=0, ai_mod_timeout_after=0)
    say(mod, 1, "idiot", troll)
    say(mod, 2, "idiot again", troll)
    assert len(mod.out.replies) == 1                                       # default: one warning per minute
    mod.env.bot.config = replace(mod.env.bot.config, ai_mod_warn_cooldown=0)
    say(mod, 3, "still an idiot", troll)
    assert len(mod.out.replies) == 2                                       # 0: warned again right away
    assert mod.env.bot.db.count_ai_warnings(1, 30) == 3                    # every message counted
