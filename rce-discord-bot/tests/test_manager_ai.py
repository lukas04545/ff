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


def test_ai_sets_location_teleports(mgr):
    from cogs.teleports import Teleports
    asyncio.run(mgr.bot.add_cog(Teleports(mgr.bot)))
    msg = mgr.message("x")
    t = mgr.cog.run_tool
    r = asyncio.run(t("set_teleport", {"location": "north", "x": 10, "y": 20, "z": 30, "name": "Outpost"}, msg))
    assert r["ok"] and r["teleport"]["name"] == "Outpost" and mgr.bot.db.teleport(
        "d11_quick_chat_location_slot_0")["x"] == 10
    r = asyncio.run(t("set_teleport", {"location": "north", "cooldown_minutes": 15}, msg))     # keeps x/y/z
    assert r["teleport"]["x"] == 10 and r["teleport"]["cooldown_minutes"] == 15
    r = asyncio.run(t("set_teleport", {"location": "south", "capture_from_player": "Nobody"}, msg))
    assert r["ok"] is False and "not online" in r["error"]
    assert asyncio.run(t("bind_kit_to_emote", {"emote": "North", "kit": "Raid"}, msg))["ok"] is False
    assert len(asyncio.run(t("list_teleports", {}, msg))["teleports"]) == 8
    assert asyncio.run(t("clear_teleport", {"location": "N"}, msg))["ok"]


def test_cut_off_answer_is_retried_and_bulk_binds(mgr):
    from support.deepseek import DeepSeekError
    calls = []

    async def ai_chat(messages, **kw):
        calls.append(kw.get("max_tokens"))
        if len(calls) == 1:
            raise DeepSeekError("Empty response (finish_reason=length).")         # like the real DeepSeek error
        if len(calls) == 2:
            assert "smaller steps" in messages[-1]["content"]
            return {"content": "", "finish_reason": "tool_calls", "tool_calls": [_call(
                "bind_kits_to_emotes", binds=[{"emote": "thanks", "kit": "Starter"}, {"emote": "hello", "kit": "Raid"},
                                              {"emote": "north", "kit": "Raid"}])]}
        return {"content": "Bound 2 kits.", "tool_calls": []}
    mgr.bot.get_cog("Tickets").ai = NS(enabled=True, chat=ai_chat)
    asyncio.run(mgr.cog.on_message(mgr.message("bind every in-game kit")))
    assert calls[:2] == [4000, 8000]
    assert sorted(r["kit"] for r in mgr.bot.db.kit_rules()) == ["Raid", "Starter"]          # North: a teleport
    text, _ = mgr.replies[-1]
    assert "Bound 2 kits." in text and "🎭 2 emote bind(s)" in text


def test_failed_request_is_remembered_for_again(mgr):
    from support.deepseek import DeepSeekError

    async def broken(messages, **kw):
        raise DeepSeekError("HTTP 503: DeepSeek is overloaded.")
    mgr.bot.get_cog("Tickets").ai = NS(enabled=True, chat=broken)
    asyncio.run(mgr.cog.on_message(mgr.message("bind all kits")))
    assert "Say **again** to retry" in mgr.replies[-1][0]
    mgr.bot.get_cog("Tickets").ai = NS(enabled=True, chat=lambda messages, **kw: _done(
        {"content": "Retrying.", "tool_calls": []} if any("bind all kits" in m["content"] for m in messages
                                                          if m["role"] == "user") else {"content": "?", "tool_calls": []}))
    asyncio.run(mgr.cog.on_message(mgr.message("again")))
    assert mgr.replies[-1][0].startswith("Retrying.")


# ------------------------------------------------------------ The management AI on the admin website

def test_admin_website_guard_rails():
    from web.server import WebInterface
    assert WebInterface.ai_blocked("GET", "/api/shop") is None
    assert WebInterface.ai_blocked("POST", "/api/shop/items") is None
    for method, path in (("POST", "/api/users"), ("GET", "/api/users"), ("POST", "/api/restart"),
                         ("GET", "/api/backups/x"), ("POST", "/api/2fa/disable"), ("POST", "/api/command"),
                         ("POST", "/api/servers"), ("POST", "/api/webshop/giftcards"), ("DELETE", "/api/plugins/x"),
                         ("POST", "/api/plugins/x/file/logo"), ("GET", "/admin"), ("POST", "/api/login")):
        assert WebInterface.ai_blocked(method, path), (method, path)
    assert WebInterface.ai_blocked("GET", "/api/plugins/x/file/logo") is None              # reading is fine
    kept, dropped = WebInterface.ai_clean_settings({"values": {"DISCORD_TOKEN": "x", "WEB_PASSWORD": "p",
                                                               "ADMIN_ROLE_IDS": "1", "SERVER_DISPLAY_NAME": "Kryvon"}})
    assert kept == {"SERVER_DISPLAY_NAME": "Kryvon"} and set(dropped) == {"DISCORD_TOKEN", "WEB_PASSWORD",
                                                                          "ADMIN_ROLE_IDS"}
    red = WebInterface._ai_redact({"api_key": "abc", "items": [{"paypal_secret": "s", "name": "n"}], "token": ""})
    assert red == {"api_key": "[hidden]", "items": [{"paypal_secret": "[hidden]", "name": "n"}], "token": ""}


def test_admin_website_end_to_end(bot, tmp_path, monkeypatch):  # noqa: F811
    import socket
    from aiohttp import web as aioweb
    from web.server import WebInterface
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    async def main():
        tmp = bot.config.database_path.parent
        (tmp / ".env").write_text("DISCORD_TOKEN=x\nSERVER_DISPLAY_NAME=Old\n")
        web_ui = WebInterface(bot, env_path=tmp / ".env", secret_path=tmp / "web_secret.key")
        web_ui.ws = replace(web_ui.ws, host="127.0.0.1", port=port)
        runner = aioweb.AppRunner(web_ui.app)
        await runner.setup()
        await aioweb.TCPSite(runner, "127.0.0.1", port).start()
        try:
            r = await web_ui.ai_request("GET", "/api/me")
            assert r["ok"] and r["result"]["user"] == "Management AI" and r["result"]["role"] == "owner"
            r = await web_ui.ai_request("GET", "/api/emote-binds")
            assert r["ok"] and r["result"]["binds"] == 0
            r = await web_ui.ai_request("PUT", "/api/emote-binds", {"emote": "thanks", "kit": "Starter"})
            assert r["ok"] and bot.db.kit_rules()[0]["kit"] == "Starter"
            r = await web_ui.ai_request("PUT", "/api/settings", {"values": {"SERVER_DISPLAY_NAME": "Kryvon",
                                                                           "DISCORD_TOKEN": "stolen"}})
            env = (tmp / ".env").read_text()
            assert r["ok"] and r["not_changed"] == ["DISCORD_TOKEN"] and "SERVER_DISPLAY_NAME=Kryvon" in env \
                and "stolen" not in env
            r = await web_ui.ai_request("GET", "/api/users")
            assert r["ok"] is False and r["status"] == 403
            assert any(a["path"] == "/api/shop" for a in web_ui.ai_endpoints("shop"))
            assert not any(a["path"].startswith("/api/users") for a in web_ui.ai_endpoints())
            # nobody can use the AI's session from outside: an owner-looking cookie without the key fails
            assert (await web_ui.ai_request("GET", "/api/me"))["ok"]
        finally:
            await runner.cleanup()
            await bot.rcon.close()
    asyncio.run(main())


def test_manager_uses_the_website_with_a_click_for_changes(mgr, monkeypatch):
    asked, calls = [], []

    async def confirm(message, text):
        asked.append(text)
        return True
    monkeypatch.setattr(mgr.cog, "confirm", confirm)

    class FakeWeb:
        ai_blocked = staticmethod(__import__("web.server", fromlist=["x"]).WebInterface.ai_blocked)

        def ai_endpoints(self, q=""):
            return [{"method": "GET", "path": "/api/shop"}]

        async def ai_request(self, method, path, body=None):
            calls.append((method, path, body))
            return {"ok": True, "status": 200, "result": {}}
    mgr.bot.web = FakeWeb()
    names = [t["function"]["name"] for t in mgr.cog.tools()]
    assert "admin_website" in names and "admin website" in mgr.cog._prompt()
    t, msg = mgr.cog.run_tool, mgr.message("x")
    assert asyncio.run(t("admin_website", {"method": "GET", "path": "/api/shop"}, msg))["ok"] and asked == []
    r = asyncio.run(t("admin_website", {"method": "POST", "path": "/api/shop/items", "body": {"name": "VIP"}}, msg))
    assert r["ok"] and len(asked) == 1 and "/api/shop/items" in asked[0] and r["did"].startswith("🌐 POST")
    r = asyncio.run(t("admin_website", {"method": "POST", "path": "/api/users", "body": {}}, msg))
    assert r["ok"] is False and len(calls) == 2                                          # blocked, never sent
    mgr.bot.config = replace(mgr.bot.config, manager_ai_web=False)
    assert "admin_website" not in [t["function"]["name"] for t in mgr.cog.tools()]


def test_binds_get_their_kits_own_role(mgr, monkeypatch):
    import cogs.kits as kits_mod
    from cogs.kits import Kits

    class Role(NS):
        def is_default(self):
            return self.id == 1000
    roles = [Role(id=1000, name="@everyone"), Role(id=11, name="M4 Spawn"), Role(id=12, name="Ak PvP"),
             Role(id=13, name="Bot", managed=True)]
    guild = NS(roles=roles, get_role=lambda rid: next((r for r in roles if r.id == rid), None))
    monkeypatch.setattr(kits_mod, "main_guild", lambda bot: guild)
    db = mgr.bot.db
    db.set_ingame_kit_role("ak pvp", 12)                                     # linked (other spelling of the case)
    emote_binds.bind(db, THANKS, "AK PVP")
    emote_binds.bind(db, "d11_quick_chat_responses_slot_5", "m4 spawn kit")    # role found by its name
    emote_binds.bind(db, "d11_quick_chat_responses_slot_6", "mystery kit")    # no role
    kits = Kits(mgr.bot)
    asyncio.run(mgr.bot.add_cog(kits))
    r = asyncio.run(mgr.cog.run_tool("assign_kit_roles_to_binds", {}, mgr.message("assign them to their role")))
    assert r["ok"] and {a["kit"]: a["role"] for a in r["assigned"]} == {"AK PVP": "Ak PvP", "m4 spawn kit": "M4 Spawn"}
    assert r["no_role_found"] == ["mystery kit"] and r["did"] == "🎖️ 2 bind(s) → kit roles"
    by_kit = {b["kit"]: b["required_role_id"] for b in emote_binds.binds(db)}
    assert by_kit == {"AK PVP": 12, "m4 spawn kit": 11, "mystery kit": None}
    again = kits.roles_to_binds()
    assert again["assigned"] == [] and len(again["already_set"]) == 2


# ------------------------------------------------------------ Live binds panel, mentions, long answers

class _Msg(NS):
    async def edit(self, **kw):
        if self.gone:
            import discord
            raise discord.NotFound(NS(status=404, reason="gone"), "Unknown Message")
        self.edits.append(kw["embed"])

    async def delete(self):
        self.gone = True


class _Chan(NS):
    async def send(self, content=None, embed=None, **kw):
        msg = _Msg(id=500 + len(self.sent), gone=False, edits=[], embed=embed, jump_url="https://discord/x")
        self.sent.append(msg)
        return msg

    def get_partial_message(self, mid):
        return next(m for m in self.sent if m.id == mid)

    async def fetch_message(self, mid):
        return self.get_partial_message(mid)


def test_live_binds_panel_updates_when_binds_change(mgr, monkeypatch):
    from cogs.binds_panel import BindsPanel
    panel = BindsPanel(mgr.bot)
    asyncio.run(mgr.bot.add_cog(panel))
    chan = _Chan(id=900, name="kits", sent=[], mention="<#900>")
    monkeypatch.setattr(mgr.bot, "get_channel", lambda cid: chan if cid == 900 else None)
    emote_binds.bind(mgr.bot.db, THANKS, "Starter", cooldown_minutes=30)
    r = asyncio.run(mgr.cog.run_tool("post_binds_panel", {}, mgr.message("post a live panel", ch=chan)))
    assert r["ok"] and "Starter" in chan.sent[0].embed.fields[0].value
    assert asyncio.run(panel.refresh()) == 0                                       # nothing changed
    emote_binds.bind(mgr.bot.db, "d11_quick_chat_responses_slot_5", "Raid")
    assert asyncio.run(panel.refresh()) == 1
    assert "Raid" in chan.sent[0].edits[-1].fields[0].value
    asyncio.run(panel.post(chan))                                                  # again: replaces the old one
    assert chan.sent[0].gone and len(panel.panels()) == 1
    chan.sent[1].gone = True                                                        # deleted by someone
    emote_binds.unbind(mgr.bot.db, THANKS)
    asyncio.run(panel.refresh())
    assert panel.panels() == []


def test_split_long_answers():
    from cogs.manager_ai import split_reply
    parts = split_reply("\n".join(f"line {i} " + "x" * 80 for i in range(60)))
    assert 2 <= len(parts) <= 4 and all(len(p) <= 1900 for p in parts)
    assert split_reply("short") == ["short"]
    assert all(len(p) <= 1900 for p in split_reply("y" * 5000))                    # one giant line
    assert "ask for the rest" in split_reply("z\n" * 10000)[-1]


def test_it_may_mention_people_but_never_everyone_or_roles(mgr):
    members = {77: NS(id=77, name="ryvex", display_name="Ryvex | Owner", global_name=None)}
    mgr.bot.db.link_player(77, "RyvexTTV") if hasattr(mgr.bot.db, "link_player") else None
    guild = NS(id=1, name="Srv", members=list(members.values()), get_member=members.get)
    msg = mgr.message("tell ryvex")
    msg.guild = guild
    r = asyncio.run(mgr.cog.run_tool("find_member", {"name": "ryvex"}, msg))
    assert r["ok"] and r["members"][0]["mention"] == "<@77>"
    mgr.script.append({"content": "<@77> the binds are done. @everyone <@&5> <@999>", "tool_calls": []})
    asyncio.run(mgr.cog.handle(msg, "tell ryvex"))
    text, kw = mgr.replies[-1]
    allowed = kw["allowed_mentions"]
    assert allowed.everyone is False and allowed.roles is False and [m.id for m in allowed.users] == [77]
    assert "<@&5>" not in text and "@everyone" not in text.replace("@​everyone", "")
