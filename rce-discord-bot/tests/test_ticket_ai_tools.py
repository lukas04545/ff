"""Ticket AI with kit tools: looks up the owner's kits, claims one, respects roles and cooldowns."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from support.deepseek import DeepSeekClient
from support.prompt import build_system_prompt, parse_answer


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for k, v in dict(DISCORD_TOKEN="x", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw",
                     ADMIN_ROLE_IDS="1", DATABASE_PATH=str(tmp_path / "bot.db"),
                     SUPPORT_KNOWLEDGE_FILE=str(tmp_path / "k.md"), TICKET_CATEGORY_ID="5").items():
        monkeypatch.setenv(k, v)
    from bot import RceBot
    from cogs.custom_kits import CustomKits
    from cogs.kits import Kits
    from cogs.tickets import Tickets
    from config import load_config
    bot = RceBot(load_config("/nonexistent"))
    sent = []

    async def command(cmd, **kwargs):
        sent.append(cmd)
        return ""
    monkeypatch.setattr(bot.rcon, "command", command)
    bot.online_players = {"GamerTag42": {}}
    db = bot.db
    db.create_custom_kit("VIP", None, "admin")
    db.add_custom_kit_item("VIP", "rifle.ak", 1)
    db.set_custom_kit_roles("VIP", [55])
    db.set_custom_kit_cooldown("VIP", 60)
    db.create_custom_kit("Staff", None, "admin")          # owner doesn't have role 99
    db.add_custom_kit_item("Staff", "rocket.launcher", 1)
    db.set_custom_kit_roles("Staff", [99])
    db.add_kit_rule("vipspawn", "respawn", None, 0, 0, "admin", required_role_id=55)
    db.link_player(7, "GamerTag42")
    tid = db.create_ticket(7, "Can I get my VIP kit?", "GamerTag42")
    db.update_ticket(tid, channel_id=100)

    member = SimpleNamespace(id=7, roles=[SimpleNamespace(id=55)], mention="<@7>", __str__=lambda: "Bob")
    guild = SimpleNamespace(get_member=lambda uid: member if uid == 7 else None,
                            get_role=lambda rid: SimpleNamespace(name="VIP") if rid == 55 else None)
    posted = []

    async def send(content=None, **kwargs):
        posted.append(content)
    channel = SimpleNamespace(id=100, guild=guild, send=send)

    async def add():
        await bot.add_cog(CustomKits(bot))
        await bot.add_cog(Kits(bot))
        tickets = Tickets(bot)
        await bot.add_cog(tickets)
        return tickets
    tickets = asyncio.run(add())
    yield SimpleNamespace(bot=bot, tickets=tickets, channel=channel, ticket=db.ticket_by_channel(100),
                          sent=sent, posted=posted)
    bot.db.close()


def _call(name, args=None, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}}


def test_ai_looks_up_and_claims_kit(setup):
    s = setup
    script = [
        {"content": "", "tool_calls": [_call("get_my_kits")]},
        {"content": "", "tool_calls": [_call("claim_kit", {"kit": "vip"}, "c2")]},
        {"content": '{"reply": "Done – your VIP kit is in your inventory!", "escalate": false}', "tool_calls": []},
    ]
    seen_messages = []

    async def fake_chat(messages, *, tools=None, json_mode=True, **kw):
        seen_messages.append([dict(m) for m in messages])
        assert tools and {"get_my_kits", "claim_kit"} <= {t["function"]["name"] for t in tools}
        return script.pop(0)
    s.tickets.ai.chat = fake_chat

    raw = asyncio.run(s.tickets._chat_with_tools([{"role": "system", "content": "x"}], s.ticket, s.channel))
    assert parse_answer(raw).reply.startswith("Done")
    overview = json.loads(seen_messages[1][-1]["content"])
    assert overview["linked_player"] == "GamerTag42" and overview["player_online"]
    assert [k["kit"] for k in overview["claimable_kits"]] == ["VIP"]            # not "Staff"
    assert overview["claimable_kits"][0]["can_claim_now"] and overview["claimable_kits"][0]["last_claimed"] == "never"
    assert overview["role_locked_autokits"] == [{"kit": "vipspawn", "trigger": "respawn", "required_role": "VIP",
                                                 "has_role": True}]
    claim = json.loads(seen_messages[2][-1]["content"])
    assert claim["ok"] and claim["player"] == "GamerTag42"
    assert s.sent == ['inventory.giveto "GamerTag42" "rifle.ak" 1']
    assert s.posted and s.posted[-1].startswith("✅ 🎁 **VIP** sent to **GamerTag42**")


def test_ai_tools_respect_rules(setup):
    s = setup
    run = lambda name, args=None: asyncio.run(s.tickets._run_tool(_call(name, args), s.ticket, s.channel))
    assert run("claim_kit", {"kit": "Staff"})["result"] == "no_access"         # role missing
    assert run("claim_kit", {"kit": "VIP"})["ok"]
    again = run("claim_kit", {"kit": "VIP"})
    assert again["result"] == "cooldown" and again["can_claim_again_discord"].startswith("<t:")
    kits = run("get_my_kits")["claimable_kits"]
    assert not kits[0]["can_claim_now"] and kits[0]["last_claimed"].endswith("ago")
    s.bot.online_players = {}
    s.bot.db.reset_custom_claims("VIP")
    assert run("claim_kit", {"kit": "VIP"})["result"] == "offline"
    s.bot.db.unlink_player(7)
    assert run("claim_kit", {"kit": "VIP"})["result"] == "not_linked"
    assert "customkit link" in run("get_my_kits")["note"]
    assert len(s.sent) == 1                                                      # only the one real claim


def test_prompt_and_client_with_tools():
    assert "claim_kit" in build_system_prompt(server_name="S", topic="t", ingame_name=None, status="",
                                              knowledge="", kit_tools=True)
    assert "CANNOT perform any actions" in build_system_prompt(server_name="S", topic="t", ingame_name=None,
                                                               status="", knowledge="")
    assert parse_answer('Sure!\n```json\n{"reply": "Hi", "escalate": false}\n```').reply == "Hi"
    seen = {}

    async def api(request):
        seen["body"] = await request.json()
        return web.json_response({"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [_call("get_my_kits")]}}]})

    async def scenario():
        app = web.Application()
        app.router.add_post("/chat/completions", api)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        client = DeepSeekClient("sk", model="deepseek-flash", base_url=url)
        msg = await client.chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
        assert msg["tool_calls"][0]["function"]["name"] == "get_my_kits" and msg["content"] == ""
        assert seen["body"]["tools"] and "response_format" not in seen["body"]
        await client.close()
        await runner.cleanup()
    asyncio.run(scenario())


# ------------------------------------------------------------ Giveaway wins (Helios)

def _helios(mid, *, winner_id, winner_name, prize, author_bot=True):
    from datetime import datetime, timezone
    field = SimpleNamespace(name="Prize", value=prize)
    embed = SimpleNamespace(title="Giveaway Winner", description=f"Winner: `{winner_name}`", fields=[field])
    return SimpleNamespace(id=mid, content="<@%d>" % winner_id, embeds=[embed],
                           mentions=[SimpleNamespace(id=winner_id)], author=SimpleNamespace(bot=author_bot),
                           created_at=datetime(2026, 10, 12, 17, 15, tzinfo=timezone.utc),
                           jump_url=f"https://discord.com/channels/1/2/{mid}")


def test_parse_helios_winner_message():
    from support.giveaway_wins import match_kit, parse_win
    win = parse_win(_helios(1, winner_id=7, winner_name="zimisgood.", prize="Store credit"))
    assert win.prize == "Store credit" and win.winner_ids == [7] and win.winner_names == ["zimisgood."]
    assert not win.lifetime and win.won_by(99, {"zimisgood."}) and not win.won_by(99, {"bob"})
    plain = SimpleNamespace(id=2, content="", mentions=[], created_at=None, jump_url="", embeds=[SimpleNamespace(
        title="Giveaway Winner", fields=[], description="Winner: `bob`\nPrize\nVIP Kit (Lifetime)")])
    win2 = parse_win(plain)
    assert win2.prize == "VIP Kit (Lifetime)" and win2.lifetime and win2.winner_names == ["bob"]
    assert parse_win(SimpleNamespace(id=3, content="hello", embeds=[], mentions=[])) is None
    assert match_kit("VIP Kit (Lifetime)", ["VIP", "VIP Plus", "Starter"]) == "VIP"
    assert match_kit("VIP Plus kit – permanent", ["VIP", "VIP Plus"]) == "VIP Plus"
    assert match_kit("Store credit", ["VIP"]) is None


def test_ai_redeems_lifetime_kit_win(setup):
    s = setup
    vip_role = SimpleNamespace(id=55, name="VIP")
    granted = []
    member = s.channel.guild.get_member(7)
    member.roles = []                      # owner doesn't have the VIP role yet
    member.name, member.global_name, member.display_name = "zimisgood.", "zim", "zim-is-good"

    async def add_roles(role, reason=None):
        granted.append((role.name, reason))
        member.roles.append(role)
    member.add_roles = add_roles
    history = [
        _helios(501, winner_id=7, winner_name="zimisgood.", prize="VIP Kit (Lifetime)"),
        _helios(502, winner_id=8, winner_name="someoneelse", prize="VIP Kit (Lifetime)"),
        _helios(503, winner_id=7, winner_name="zimisgood.", prize="Store credit"),
        _helios(504, winner_id=7, winner_name="zimisgood.", prize="VIP Kit 7 days"),
        _helios(505, winner_id=7, winner_name="zimisgood.", prize="VIP Kit (Lifetime)", author_bot=False),  # fake
    ]

    class Channel:
        async def history(self, limit=None):
            for m in history:
                yield m
    s.channel.guild.get_channel = lambda cid: Channel() if cid == 1558525305004757032 else None
    s.channel.guild.get_role = lambda rid: vip_role if rid == 55 else None
    run = lambda name, args=None: asyncio.run(s.tickets._run_tool(_call(name, args), s.ticket, s.channel))

    wins = run("get_my_giveaway_wins")["wins"]
    assert [(w["message_id"], w["prize"], w["lifetime"], w["kit"], w["kit_role"]) for w in wins] == [
        ("501", "VIP Kit (Lifetime)", True, "VIP", "VIP"),
        ("503", "Store credit", False, None, None),
        ("504", "VIP Kit 7 days", False, "VIP", "VIP")]                       # not 502 (other user), not 505 (fake)

    assert run("redeem_giveaway_win", {"message_id": "502"})["result"] == "not_your_win"
    assert run("redeem_giveaway_win", {"message_id": "503"})["result"] == "needs_staff"   # store credit
    assert run("redeem_giveaway_win", {"message_id": "504"})["reason"] == "Kit prize is not lifetime"
    r = run("redeem_giveaway_win", {"message_id": "501"})
    assert r["ok"] and r["action"] == "role VIP given" and granted[0][0] == "VIP"
    assert "redeemed" in s.posted[-1] and "VIP" in s.posted[-1]
    s.tickets._win_cache = None
    assert run("redeem_giveaway_win", {"message_id": "501"})["result"] == "already_redeemed"  # only once
    assert len(granted) == 1
    assert run("get_my_kits")["claimable_kits"][0]["kit"] == "VIP"             # role now unlocks the kit
