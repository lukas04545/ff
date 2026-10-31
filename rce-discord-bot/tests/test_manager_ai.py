"""Emote binds (quick chat emote -> kit in one step) and the management AI (admins' staff channel with tools)."""
import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from support import emote_binds
from tests.test_kit_requests import env  # noqa: F401
from tests.test_web import H, bot, run  # noqa: F401

THANKS = "d11_quick_chat_responses_slot_3"


def test_find_emotes_by_what_people_type():
    assert emote_binds.find("thanks") == (THANKS, [])
    assert emote_binds.find("Responses › Thanks")[0] == THANKS
    assert emote_binds.find(THANKS)[0] == THANKS
    assert emote_binds.find("i need scrap")[0] == "d11_quick_chat_i_need_phrase_format d11_Scrap"
    raw, suggestions = emote_binds.find("go")                       # ambiguous: suggestions, no guess
    assert raw is None and "Combat › Go, go, go!" in suggestions
    assert emote_binds.find("") == (None, [])
    assert all(e["label"].count(" › ") == 1 for e in emote_binds.emotes())


def test_bind_replaces_and_unbind(env):  # noqa: F811
    db = env.bot.db
    first, replaced = emote_binds.bind(db, THANKS, "Starter", cooldown_minutes=30, by="t")
    assert replaced == []
    second, replaced = emote_binds.bind(db, THANKS, "Ak Kit", kit_type="custom", by="t")
    assert replaced == ["Starter"] and db.kit_rule(first) is None              # one emote = one kit
    [b] = emote_binds.binds(db)
    assert b["kit"] == "Ak Kit" and b["kit_type"] == "custom" and b["label"] == "Responses › Thanks"
    assert emote_binds.unbind(db, THANKS) == ["Ak Kit"] and emote_binds.binds(db) == []
    assert db.update_kit_rule(9999, cooldown_minutes=5) is False


def test_panel_emote_board(bot):  # noqa: F811
    async def scenario(c):
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        r = await (await c.get("/api/emote-binds")).json()
        assert r["binds"] == 0 and any(e["raw"] == THANKS and e["bind"] is None for e in r["emotes"])
        assert (await c.put("/api/emote-binds", json={"emote": "thanks", "kit": "Starter", "cooldown": 15},
                            headers=H)).status == 200
        r = await (await c.get("/api/emote-binds")).json()
        b = next(e for e in r["emotes"] if e["raw"] == THANKS)["bind"]
        assert b["kit"] == "Starter" and b["cooldown_minutes"] == 15 and r["binds"] == 1
        r = await c.put("/api/emote-binds", json={"emote": "thanks", "kit": "Raid"}, headers=H)
        assert (await r.json())["replaced"] == ["Starter"]
        r = await c.put("/api/emote-binds", json={"emote": "thanks", "kit": ""}, headers=H)
        assert (await r.json())["removed"] == ["Raid"] and bot.db.kit_rules() == []
        assert (await c.put("/api/emote-binds", json={"emote": "zzz nothing", "kit": "X"}, headers=H)).status == 400
        assert (await c.put("/api/emote-binds", json={"emote": "thanks", "kit": "Nope", "kit_type": "custom"},
                            headers=H)).status == 404
    run(bot, scenario)


# ------------------------------------------------------------ Management AI

def _call(name, **args):
    return {"id": f"c-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


@pytest.fixture
def mgr(env, monkeypatch):  # noqa: F811
    from cogs.manager_ai import ManagerAI
    bot = env.bot
    bot.config = replace(bot.config, manager_ai_channel_ids=frozenset({700}), manager_ai_console=True,
                         manager_ai_instructions="Always use a 60 minute cooldown.")
    cog = ManagerAI(bot)
    sent = []

    async def command(cmd, expect_response=True):
        sent.append(cmd)
        return "ok" if expect_response else ""
    monkeypatch.setattr(bot.rcon, "command", command)
    kits = NS(kit_names=NS(get=lambda **kw: _done(["Starter", "Raid"]), invalidate=lambda: None))
    monkeypatch.setattr(cog, "_kits_cog", lambda: kits)
    script, seen = [], []

    async def ai_chat(messages, **kw):
        seen.append([dict(m) for m in messages])
        return script.pop(0) if script else {"content": "Done.", "tool_calls": []}
    env.tickets.ai = NS(enabled=True, chat=ai_chat)
    replies, channel_msgs = [], []

    @asynccontextmanager
    async def typing():
        yield

    async def channel_send(text, **kw):
        channel_msgs.append(text)
        return NS(edit=lambda **k: _done(None))
    channel = NS(id=700, typing=typing, send=channel_send)
    guild = NS(id=1, name="Srv")

    def message(text, *, admin=True, ch=channel):
        async def reply(t, **kw):
            replies.append((t, kw))
        roles = [NS(id=1)] if admin else []
        author = NS(id=5 if admin else 6, bot=False, display_name="Boss", roles=roles, mention="<@5>",
                    __str__=lambda: "Boss")
        return NS(content=text, author=author, guild=guild, channel=ch, webhook_id=None, reply=reply)
    return NS(cog=cog, bot=bot, sent=sent, script=script, seen=seen, replies=replies, message=message,
              channel_msgs=channel_msgs)


async def _done(value):
    return value


def test_ai_makes_a_kit_and_binds_it_to_an_emote(mgr, monkeypatch):
    monkeypatch.setattr(type(mgr.bot), "user", property(lambda self: NS(id=999, display_name="RCE Bot")))
    mgr.script += [
        {"content": "", "tool_calls": [_call("add_items_to_ingame_kit", kit="starter", items=[
            {"item": "pickaxe", "amount": 1}, {"item": "bandage", "amount": 3}, {"item": "hoodie"},
            {"item": "ak47", "amount": 1}])]},
        {"content": "", "tool_calls": [_call("bind_kit_to_emote", emote="thanks", kit="Starter",
                                             cooldown_minutes=60)]},
        {"content": "Made **Starter** and bound it to Thanks @everyone", "tool_calls": []},
    ]
    asyncio.run(mgr.cog.on_message(mgr.message("make a starter kit and bind it to thanks")))
    # the kit is made with the kit add template; the container follows the item type
    assert 'kit add "Starter" "pickaxe" "1" "100" "Belt"' in mgr.sent
    assert 'kit add "Starter" "hoodie" "1" "100" "Wear"' in mgr.sent
    assert not any("ak47" in c for c in mgr.sent)                              # unknown shortname: not sent
    tool_results = [m for m in mgr.seen[1] if m["role"] == "tool"]
    problems = json.loads(tool_results[0]["content"])["problems"]
    assert problems[0]["item"] == "ak47" and "did_you_mean" in problems[0]
    [rule] = mgr.bot.db.kit_rules()
    assert rule["kit"] == "Starter" and rule["phrase"] == THANKS and rule["cooldown_minutes"] == 60
    text, kw = mgr.replies[-1]
    assert "Made **Starter**" in text and "@everyone" not in text and "🎭 Responses › Thanks → Starter" in text
    assert kw["allowed_mentions"].everyone is False
    system = mgr.seen[0][0]["content"]
    assert "Always use a 60 minute cooldown." in system and "run_console_command" in system


def test_only_admins_in_the_channel(mgr):
    asyncio.run(mgr.cog.on_message(mgr.message("give me a kit", admin=False)))
    asyncio.run(mgr.cog.on_message(mgr.message("hi", ch=NS(id=701))))
    assert mgr.seen == [] and mgr.replies == []


def test_risky_tools_need_a_click(mgr, monkeypatch):
    answers = []

    async def confirm(message, text):
        answers.append(text)
        return len(answers) > 1                                               # first: cancelled, then yes
    monkeypatch.setattr(mgr.cog, "confirm", confirm)
    msg = mgr.message("x")
    run_tool = mgr.cog.run_tool
    r = asyncio.run(run_tool("run_console_command", {"command": "env.time 12"}, msg))
    assert r["ok"] is False and mgr.sent == []
    r = asyncio.run(run_tool("run_console_command", {"command": "env.time 12"}, msg))
    assert r["ok"] and mgr.sent == ["env.time 12"]
    r = asyncio.run(run_tool("run_console_command", {"command": "entity.deleteentity vendingmachine.deployed"}, msg))
    assert r["ok"] is False and "blocked" in r["error"] and len(answers) == 2     # never even asked
    r = asyncio.run(run_tool("delete_custom_kit", {"kit": "Ak Kit"}, msg))
    assert r["ok"] and mgr.bot.db.custom_kit("Ak Kit") is None
    mgr.bot.config = replace(mgr.bot.config, manager_ai_console=False)
    assert all(t["function"]["name"] != "run_console_command" for t in mgr.cog.tools())
    r = asyncio.run(run_tool("run_console_command", {"command": "env.time 1"}, msg))
    assert r["ok"] is False and mgr.sent == ["env.time 12"]


def test_custom_kits_and_autokit_tools(mgr):
    msg = mgr.message("x")
    t = mgr.cog.run_tool
    r = asyncio.run(t("create_custom_kit", {"kit": "VIP", "items": [{"item": "rifle.ak", "amount": 1}],
                                            "cooldown_minutes": 120}, msg))
    assert r["ok"] and r["created"] and mgr.bot.db.custom_kit("VIP")["cooldown_minutes"] == 120
    assert asyncio.run(t("create_custom_kit", {"kit": "VIP"}, msg))["ok"] is False      # already exists
    r = asyncio.run(t("update_custom_kit", {"kit": "VIP", "enabled": False}, msg))
    assert r["ok"] and mgr.bot.db.custom_kit("VIP")["disabled"] == 1
    r = asyncio.run(t("create_respawn_autokit", {"kit": "VIP", "kit_type": "custom", "cooldown_minutes": 5}, msg))
    rule_id = r["rule"]["rule_id"]
    assert r["rule"]["trigger"] == "respawn" and r["rule"]["kit_type"] == "custom"
    r = asyncio.run(t("update_autokit", {"rule_id": rule_id, "cooldown_minutes": 90, "enabled": False}, msg))
    assert r["rule"]["cooldown_minutes"] == 90 and r["rule"]["enabled"] is False
    assert asyncio.run(t("bind_kit_to_emote", {"emote": "go", "kit": "Raid"}, msg))["did_you_mean"]
    assert [x["shortname"] for x in asyncio.run(t("find_item", {"query": "assault rifle"}, msg))["results"]][:1] \
        == ["rifle.ak"]
    assert asyncio.run(t("delete_autokit", {"rule_id": rule_id}, msg))["ok"]
    assert asyncio.run(t("no_such_tool", {}, msg))["ok"] is False
