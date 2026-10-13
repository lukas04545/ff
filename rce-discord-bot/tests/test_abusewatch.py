"""AI admin abuse watch: collects admin-related log lines, the AI flags patterns, staff get an alert."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from cogs.abusewatch import SYSTEM, AbuseWatch, parse_flags, system_prompt
from tests.test_kit_requests import env  # noqa: F401


def test_parse_flags_and_prompt():
    raw = json.dumps({"flags": [{"player": "Bob", "severity": "HIGH", "reason": "spawned rockets", "lines": [2, 9, "x"]},
                                {"player": "", "reason": "x"}]})
    assert parse_flags(raw, 3) == [{"player": "Bob", "severity": "high", "reason": "spawned rockets", "lines": [2]}]
    assert parse_flags("nope", 3) == []
    assert system_prompt("") == SYSTEM and "Build events are fine." in system_prompt("Build events are fine.")


@pytest.fixture
def watch(env):
    env.bot.config = replace(env.bot.config, ai_abuse_watch=True, ai_abuse_channel_id=900, alert_role_ids=frozenset({5}),
                             abuse_exempt_players=frozenset({"ownerguy"}), ai_abuse_instructions="No AKs for admins.")
    w = AbuseWatch(env.bot)
    sent, prompts, verdict = [], [], {"flags": []}

    async def chat_json(messages, **kw):
        prompts.append(messages)
        return json.dumps(verdict)
    env.tickets.ai = NS(enabled=True, chat_json=chat_json)

    async def send(content=None, embed=None, **kw):
        sent.append((content, embed))
    env.bot.get_channel = lambda cid: NS(send=send) if cid == 900 else None
    return NS(w=w, env=env, sent=sent, prompts=prompts, verdict=verdict)


def feed(w, *lines):
    for line in lines:
        w.feed(line)


def test_collects_only_admin_lines_and_their_kills(watch):
    w = watch.w
    feed(w, "Random was killed by Stranger",                         # nobody active yet -> ignored
         "[CHAT GLOBAL] Bob : hi",                                    # chat -> ignored
         "[ServerVar] giving Bob 10 x Assault Rifle",                 # admin panel spawn
         "giving Alice 5 x Wood",                                     # console/bot give -> tagged (bot)
         "[ServerVar] SERVER giving Alice kit vip",                   # bot kit -> (bot)
         "[ServerVar] Bob giving Carl kit raid",                      # in-game admin kit -> not bot
         "Victim was killed by Bob",                                  # kill of an active admin -> kept
         "[Bob] Added [Victim] to [Banned]")
    lines = [line for _, line in w.lines]
    assert lines == ["[ServerVar] giving Bob 10 x Assault Rifle", "giving Alice 5 x Wood (bot)",
                     "[ServerVar] SERVER giving Alice kit vip (bot)", "[ServerVar] Bob giving Carl kit raid",
                     "Victim was killed by Bob", "[Bob] Added [Victim] to [Banned]"]


def test_flag_alert_trusted_and_dedupe(watch):
    w = watch.w
    feed(w, "[ServerVar] giving Bob 10 x Assault Rifle", "Victim was killed by Bob", "[ServerVar] giving OwnerGuy 5 x Rocket")
    watch.verdict["flags"] = [{"player": "Bob", "severity": "high", "reason": "Spawned 10 AKs and killed a player.",
                               "lines": [1, 2]},
                              {"player": "OwnerGuy", "severity": "high", "reason": "rockets", "lines": [3]}]
    flags = asyncio.run(w.check())
    assert [f["player"] for f in flags] == ["Bob"]                    # trusted owner never flagged
    user_msg = watch.prompts[-1][1]["content"]
    assert "TRUSTED (owners, never flag): ownerguy" in user_msg and "1. [" in user_msg
    assert "No AKs for admins." in watch.prompts[-1][0]["content"]  # owner's own rules
    content, embed = watch.sent[-1]
    assert content == "<@&5>" and "Bob" in embed.title and "Victim was killed by Bob" in embed.fields[0].value
    assert "Nothing was done automatically" in embed.footer.text
    feed(w, "[ServerVar] giving Bob 10 x Assault Rifle")
    asyncio.run(w.check())
    assert len(watch.sent) == 1                                       # same suspicion not re-sent within 30 min


def test_nothing_to_check(watch):
    w = watch.w
    assert asyncio.run(w.check()) == [] and watch.prompts == []        # empty -> no AI call
    w.actors["bob"] = 10 ** 10
    feed(w, "Victim was killed by Bob")
    assert asyncio.run(w.check()) == [] and watch.prompts == []        # only kills -> no AI call
    watch.env.bot.config = replace(watch.env.bot.config, ai_abuse_watch=False)
    assert not w.enabled
