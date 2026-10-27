"""Offline protection zones: leader-only placement with quick chat, red after the offline time, green on join.

The zone commands here are made-up TEST_* strings – the plugin ships with them empty (the admin enters the
documented commands)."""
import asyncio
import time

import pytest

from rce.log_parser import ChatEvent
from tests.test_plugins import pb  # noqa: F401

ASK, YES = "d11_quick_chat_questions_slot_1", "d11_quick_chat_responses_slot_0"
CONFIG = {"position_command": 'TEST_POS "{name}"', "create_command": "TEST_CREATE {zone} {x} {y} {z} {radius}",
          "remove_command": "TEST_REMOVE {zone}", "red_command": "TEST_RED {zone}",
          "green_command": "TEST_GREEN {zone}", "offline_minutes": 30, "radius": 25}


@pytest.fixture
def zp(pb, monkeypatch):  # noqa: F811
    async def command(cmd, **kw):
        pb.said.append(cmd)
        return "Position: (123.5, 20.0, -456.25)" if cmd.startswith("TEST_POS") else ""
    monkeypatch.setattr(pb.bot.rcon, "command", command)
    asyncio.run(pb.m.load_all())
    asyncio.run(pb.m.set_enabled("offline_protection", True))
    db = pb.db
    db.team_event("create", "Lead", 5)
    db.team_event("join", "Mate", 5, "Lead")
    pb.bot.online_players = {"Lead": {}, "Mate": {}, "Stranger": {}}
    pb.p = pb.m.running("offline_protection")
    return pb


def chat(p, player, raw):
    asyncio.run(p.on_rce_chat(ChatEvent("GLOBAL", player, raw, raw)))


def configure(zp, **extra):
    asyncio.run(zp.m.save_settings("offline_protection", {**CONFIG, **extra}))


def test_documented_commands_by_default(zp, monkeypatch):
    """Out of the box only commands from the guide "Custom zones" are sent."""
    async def command(cmd, **kw):
        zp.said.append(cmd)
        return "Lead (2, 7) position: (512.4, 31.0, -88.25)" if cmd.startswith("server.printpos") else ""
    monkeypatch.setattr(zp.bot.rcon, "command", command)
    assert zp.p.problems() == []
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    sent = [c for c in zp.said if c.startswith(("server.", "zones."))]
    assert sent == ['server.printpos "Lead"',
                    'zones.createcustomzone "team_5" (512.4,31.0,-88.25) 0 Sphere 30 1 1 0 1 1',
                    'zones.editcustomzone "team_5" showarea 1',
                    'zones.editcustomzone "team_5" color (0,255,0)',
                    'zones.editcustomzone "team_5" showchatmessage 1',
                    'zones.editcustomzone "team_5" allowbuildingdamage 1',
                    'zones.editcustomzone "team_5" color (0,255,0)',
                    'zones.editcustomzone "team_5" entermessage "This team is online – offline protection is off."']
    zp.said.clear()
    zp.bot.online_players = {}
    asyncio.run(zp.p.on_rce_player_leave("Lead"))
    asyncio.run(zp.p._update(5, now=zp.p.zone(5)["offline_since"] + 30 * 60))
    assert zp.said[:2] == ['zones.editcustomzone "team_5" allowbuildingdamage 0',
                           'zones.editcustomzone "team_5" color (255,0,0)']
    chat(zp.p, "Lead", ASK)                                       # (offline players can't chat – just the API)
    zp.said.clear()
    zp.bot.online_players = {"Lead": {}}
    chat(zp.p, "Lead", YES)                                       # moving: the old zone is deleted first
    assert 'zones.deletecustomzone "team_5"' in zp.said
    assert all(c.split(" ", 1)[0] in DOCUMENTED for c in zp.said if "." in c.split(" ", 1)[0])


DOCUMENTED = {"zones.createcustomzone", "zones.editcustomzone", "zones.deletecustomzone", "server.printpos", "say",
              "relationshipmanager.logteamactions"}


def test_empty_command_is_never_sent(zp):
    configure(zp, red_command="")
    assert "the command for status red" in zp.p.problems()[0]
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    zp.said.clear()
    zp.bot.online_players = {}
    asyncio.run(zp.p.on_rce_player_leave("Lead"))
    asyncio.run(zp.p._update(5, now=zp.p.zone(5)["offline_since"] + 3600))
    assert zp.p.zone(5)["state"] == "red" and not any(c.startswith("TEST_RED") for c in zp.said)


def test_only_the_leader_places_with_ask_then_yes(zp):
    configure(zp)
    assert zp.p.problems() == []
    for who in ("Mate", "Stranger"):
        chat(zp.p, who, ASK)
        chat(zp.p, who, YES)
    assert zp.p.zones() == {}
    assert any("only your team leader" in c for c in zp.said) and any("need to be in a team" in c for c in zp.said)
    chat(zp.p, "Lead", YES)                                                # "Yes" alone does nothing
    assert zp.p.zones() == {}
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    zone = zp.p.zone(5)
    assert zone["name"] == "team_5" and (zone["x"], zone["y"], zone["z"]) == (123.5, 20.0, -456.25)
    assert "TEST_CREATE team_5 123.5 20.0 -456.25 25" in zp.said and zone["state"] == "green"
    assert 'TEST_POS "Lead"' in zp.said
    chat(zp.p, "Lead", ASK)                                                # placing again moves it
    chat(zp.p, "Lead", YES)
    assert zp.said.count("TEST_REMOVE team_5") == 1 and zp.said.count("TEST_CREATE team_5 123.5 20.0 -456.25 25") == 2


def test_yes_too_late_is_ignored(zp, monkeypatch):
    configure(zp, confirm_seconds=10)
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    chat(zp.p, "Lead", ASK)
    clock[0] += 11
    chat(zp.p, "Lead", YES)
    assert zp.p.zones() == {}


def test_red_after_offline_time_green_on_first_join(zp):
    configure(zp)
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    zp.said.clear()
    zp.bot.online_players = {"Stranger": {}}
    asyncio.run(zp.p.on_rce_player_leave("Lead"))
    start = zp.p.zone(5)["offline_since"]
    assert start is not None and zp.p.zone(5)["state"] == "green"
    asyncio.run(zp.p._update(5, now=start + 29 * 60))
    assert zp.p.zone(5)["state"] == "green" and "TEST_RED team_5" not in zp.said
    asyncio.run(zp.p._update(5, now=start + 30 * 60))
    assert zp.p.zone(5)["state"] == "red" and zp.said.count("TEST_RED team_5") == 1
    asyncio.run(zp.p._update(5, now=start + 40 * 60))
    assert zp.said.count("TEST_RED team_5") == 1                           # sent once, not every check
    asyncio.run(zp.p.on_rce_player_join("Mate"))                           # any member back: green at once
    assert zp.p.zone(5)["state"] == "green" and zp.said[-1] == "TEST_GREEN team_5"
    assert zp.p.zone(5)["offline_since"] is None
    assert zp.p.status() == {"Zones": 1, "Red (team offline)": 0, "Green": 1, "Expired": 0}


def test_offline_timer_is_adjustable(zp):
    configure(zp, offline_minutes=5)
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    zp.bot.online_players = {}
    asyncio.run(zp.p.on_rce_player_leave("Mate"))
    start = zp.p.zone(5)["offline_since"]
    asyncio.run(zp.p._update(5, now=start + 5 * 60))
    assert zp.p.zone(5)["state"] == "red"


def test_new_leader_and_team_gone(zp):
    configure(zp)
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    zp.db.team_event("promote", "Lead", 5, "Mate")                         # Mate is the leader now
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    assert any("only your team leader" in c for c in zp.said)
    zp.db.team_event("leave", "Lead", 5, "Mate")
    zp.db.team_event("leave", "Mate", 5, "Mate")                           # team disbanded
    asyncio.run(zp.p._update(5))
    assert zp.p.zone(5) is None and "TEST_REMOVE team_5" in zp.said


def test_bad_position_pattern_is_reported(zp):
    configure(zp, position_pattern=r"pos (\d+)")
    assert "groups" in zp.p.problems()[0]
    configure(zp, position_pattern=r"Position: \((?P<x>[-\d.]+), (?P<y>[-\d.]+), (?P<z>[-\d.]+)\)")
    assert zp.p.problems() == []
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    assert zp.p.zone(5)["z"] == -456.25


def test_team_logs_switched_on_once(zp):
    asyncio.run(zp.p.clock())
    asyncio.run(zp.p.clock())
    assert zp.said.count("relationshipmanager.logteamactions 1") == 1


# Output of relationshipmanager.teaminfoall as the server prints it (from a real console)
TEAMINFOALL = """Team 15 member list:
x120hz-LOGAN [DISCONNECTED] (LEADER)
Dom1234_2018 [DISCONNECTED]
Team 3 member list:
Beamedbe Ak [DISCONNECTED] (LEADER)
Hades_2Faze [DISCONNECTED]
Team 4 member list:
oreo_pheonix562 [DISCONNECTED] (LEADER)
Team 5 member list:
mikeyblaine [DISCONNECTED]
EminBezgin2013 [DISCONNECTED] (LEADER)
Team 6 member list:
RyanSpeijdel [883255] (LEADER)
secret_Liam123 [DISCONNECTED]
"""


def test_parse_team_list():
    from rce.teams import parse_team_list
    teams = parse_team_list(TEAMINFOALL)
    assert set(teams) == {15, 3, 4, 5, 6}
    assert teams[3] == {"leader": "Beamedbe Ak", "members": ["Beamedbe Ak", "Hades_2Faze"], "online": []}
    assert teams[5]["leader"] == "EminBezgin2013" and teams[5]["members"][0] == "mikeyblaine"
    assert teams[6]["online"] == ["RyanSpeijdel"]
    assert parse_team_list("some error") == {} and parse_team_list("") == {}


def test_teams_read_from_the_server_and_found_when_placing(zp, monkeypatch):
    replies = {"relationshipmanager.teaminfoall": TEAMINFOALL,
               'relationshipmanager.findplayerteam "Newbie"': "Team 44 member list:\nNewbie [12345] (LEADER)\n"
                                                             "Pal [DISCONNECTED]\n"}

    async def command(cmd, **kw):
        zp.said.append(cmd)
        if cmd.startswith("TEST_POS"):
            return "(1, 2, 3)"
        return replies.get(cmd, "")
    monkeypatch.setattr(zp.bot.rcon, "command", command)
    configure(zp)
    asyncio.run(zp.p.clock())                                              # first run: all teams read
    row, members = zp.db.team_of("Hades_2Faze")
    assert row["team_id"] == 3 and row["leader"] == "Beamedbe Ak" and members == ["Beamedbe Ak", "Hades_2Faze"]
    assert zp.db.team_of("Lead") is None                                   # replaced by the server's list
    asyncio.run(zp.p.clock())
    assert zp.said.count("relationshipmanager.teaminfoall") == 1           # not again until the interval
    zp.bot.online_players = {"Newbie": {}}
    chat(zp.p, "Newbie", ASK)                                              # team unknown -> asked the server
    chat(zp.p, "Newbie", YES)
    assert 'relationshipmanager.findplayerteam "Newbie"' in zp.said and zp.p.zone(44) is not None
    replies["relationshipmanager.teaminfoall"] = "Unknown command"           # unreadable: keep what we know
    asyncio.run(zp.p.sync_teams())
    assert zp.db.team_of("Hades_2Faze") is not None


def test_zone_expires_when_nobody_of_the_team_comes_online(zp):
    configure(zp, expire_hours=48)
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    assert zp.p.zone_list()[0]["expires_at"] is None                      # the team is online
    zp.bot.online_players = {}
    asyncio.run(zp.p.on_rce_player_leave("Mate"))
    start = zp.p.zone(5)["offline_since"]
    assert zp.p.zone_list()[0]["expires_at"] == start + 48 * 3600
    asyncio.run(zp.p._update(5, now=start + 47 * 3600))
    assert zp.p.zone(5)["state"] == "red" and "TEST_REMOVE team_5" not in zp.said
    asyncio.run(zp.p._update(5, now=start + 48 * 3600))
    assert zp.p.zone(5) is None and zp.said.count("TEST_REMOVE team_5") == 1
    assert zp.p.status()["Expired"] == 1 and zp.p.zone_list() == []


def test_coming_back_resets_the_expiry_and_0_means_never(zp):
    configure(zp, expire_hours=10)
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    zp.bot.online_players = {}
    asyncio.run(zp.p.on_rce_player_leave("Mate"))
    start = zp.p.zone(5)["offline_since"]
    asyncio.run(zp.p._update(5, now=start + 9 * 3600))
    zp.bot.online_players = {"Mate": {}}
    asyncio.run(zp.p.on_rce_player_join("Mate"))                           # back in time: timer starts over
    zp.bot.online_players = {}
    asyncio.run(zp.p.on_rce_player_leave("Mate"))
    again = zp.p.zone(5)["offline_since"]
    asyncio.run(zp.p._update(5, now=again + 9 * 3600))
    assert zp.p.zone(5) is not None
    configure(zp, expire_hours=0)
    asyncio.run(zp.p._update(5, now=again + 1000 * 3600))
    assert zp.p.zone(5) is not None and zp.p.zone_list()[0]["expires_at"] is None


def test_own_tab_in_the_control_panel(zp):
    from aiohttp.test_utils import TestClient, TestServer
    configure(zp)
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    H = {"X-Requested-With": "rce-web"}

    async def main():
        from web.server import WebInterface
        web = WebInterface(zp.bot, env_path=zp.tmp / ".env", secret_path=zp.tmp / "web_secret.key")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            assert (await c.get("/api/protection")).status == 401
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            r = await (await c.get("/api/protection")).json()
            assert r["plugin"]["running"]
            assert [z["name"] for z in r["zones"]] == ["team_5"] and r["zones"][0]["members"] == ["Lead", "Mate"]
            assert r["plugin"]["core"] is True
            plugins = (await (await c.get("/api/plugins")).json())["plugins"]
            assert "offline_protection" not in {p["id"] for p in plugins}             # not on the Plugins page
            assert (await (await c.get("/api/plugins/menu")).json())["menu"] == []   # nor in the plugin menu
            r = await c.post("/api/plugins/upload", json={"filename": "offline_protection.py", "code": "x = 1"},
                             headers=H)
            assert r.status == 400 and "main feature" in (await r.json())["error"]  # can't be replaced
            r = await c.post("/api/plugins/offline_protection/toggle", json={"enabled": True}, headers=H)
            assert r.status == 200 and (await r.json())["plugin"]["running"]          # its own on/off still works
            assert (await c.delete("/api/protection/zones/9", headers=H)).status == 404
            assert (await c.delete("/api/protection/zones/5", headers=H)).status == 200
            assert zp.p.zone(5) is None and "TEST_REMOVE team_5" in zp.said
            index = await (await c.get("/admin")).text()
            assert 'href="#protection" data-view="protection" data-owner' in index     # fixed: Server group
        finally:
            await c.close()
            await zp.bot.rcon.close()
    asyncio.run(main())


def test_ticket_ai_places_the_zone_for_a_player_who_struggles(zp):
    """Support AI tools: only the ticket owner's own team, only the leader, only while online (his position)."""
    import json
    from types import SimpleNamespace as NS

    from cogs.custom_kits import CustomKits
    from cogs.tickets import PROTECTION_TOOLS, Tickets
    configure(zp)
    db = zp.db
    db.link_player(7, "Lead")
    db.link_player(8, "Mate")
    members = {7: NS(id=7, roles=[]), 8: NS(id=8, roles=[])}
    posted = []

    async def send(content=None, **kw):
        posted.append(content)
    channel = NS(id=100, guild=NS(id=1, get_member=members.get), send=send)

    async def add():
        await zp.bot.add_cog(CustomKits(zp.bot))
        t = Tickets(zp.bot)
        await zp.bot.add_cog(t)
        return t
    tickets = asyncio.run(add())
    assert all(t in tickets._ai_tools() for t in PROTECTION_TOOLS)

    def call(name, user_id):
        tid = db.create_ticket(user_id, "I can't place my offline protection zone", None)
        call = {"id": "1", "function": {"name": name, "arguments": "{}"}}
        return asyncio.run(tickets._run_tool(call, db.ticket(tid), channel))

    status = call("get_my_protection_zone", 7)
    assert status["is_leader"] and status["online"] and status["zone"] is None and status["members"] == ["Lead", "Mate"]
    assert "Can I build" in status["how_to_place_in_game"]
    r = call("place_protection_zone", 8)                                     # a member, not the leader
    assert not r["ok"] and r["result"] == "not_leader" and zp.p.zone(5) is None
    zp.bot.online_players = {"Mate": {}}
    r = call("place_protection_zone", 7)                                     # leader offline: can't read position
    assert not r["ok"] and r["result"] == "offline"
    zp.bot.online_players = {"Lead": {}, "Mate": {}}
    zp.said.clear()
    r = call("place_protection_zone", 7)
    assert r["ok"] and r["result"] == "placed" and zp.p.zone(5)["x"] == 123.5
    assert 'TEST_POS "Lead"' in zp.said and any(c.startswith("TEST_CREATE team_5") for c in zp.said)
    assert "placed at their current position" in posted[-1]                   # visible in the ticket
    assert call("place_protection_zone", 7)["result"] == "moved"
    assert call("get_my_protection_zone", 8)["zone"]["name"] == "team_5"
    tickets_text = json.dumps(PROTECTION_TOOLS)
    assert "CURRENT in-game position" in tickets_text
    from support.prompt import build_system_prompt
    prompt = build_system_prompt(server_name="S", topic="zone", ingame_name=None, status="", knowledge="",
                                 kit_tools=True, protection_tools=True)
    assert "place_protection_zone" in prompt and "standing in the middle of their base" in prompt
    assert "place_protection_zone" not in build_system_prompt(server_name="S", topic="x", ingame_name=None,
                                                              status="", knowledge="", kit_tools=True)


def test_red_and_green_are_sent_again_when_rcon_was_down(zp, monkeypatch):
    """RCON down the moment the zone should turn red (or green): the bot must not think it's done – it tries
    again on the next check, otherwise the base stays raidable in-game."""
    from rce.rcon_client import RconError
    configure(zp)
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    zp.bot.online_players = {"Stranger": {}}
    asyncio.run(zp.p.on_rce_player_leave("Lead"))
    start = zp.p.zone(5)["offline_since"]
    down = {"on": True}

    async def command(cmd, **kw):
        if down["on"]:
            raise RconError("not connected")
        zp.said.append(cmd)
        return ""
    monkeypatch.setattr(zp.bot.rcon, "command", command)
    zp.said.clear()
    asyncio.run(zp.p._update(5, now=start + 31 * 60))
    assert zp.p.zone(5)["state"] != "red" and zp.said == []                  # not sent – not marked red
    down["on"] = False
    asyncio.run(zp.p._update(5, now=start + 32 * 60))
    assert zp.p.zone(5)["state"] == "red" and zp.said.count("TEST_RED team_5") == 1
    down["on"] = True
    asyncio.run(zp.p.on_rce_player_join("Mate"))
    assert zp.p.zone(5)["state"] == "red"                                      # green failed: still red
    down["on"] = False
    zp.bot.online_players = {"Stranger": {}, "Mate": {}}
    asyncio.run(zp.p._update(5))                                                # the next check
    assert zp.p.zone(5)["state"] == "green" and zp.said[-1] == "TEST_GREEN team_5"


def test_status_is_sent_again_after_reconnect_and_regularly(zp, monkeypatch):
    """A zone stuck green in-game (server restart reset it, or a command got lost) is fixed: the bot sends the
    current status again after RCON reconnects and every reapply_minutes."""
    configure(zp, team_sync_minutes=0, reapply_minutes=10)
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    zp.bot.online_players = {"Stranger": {}}
    asyncio.run(zp.p.on_rce_player_leave("Lead"))
    start = zp.p.zone(5)["offline_since"]
    asyncio.run(zp.p._update(5, now=start + 31 * 60))
    assert zp.p.zone(5)["state"] == "red" and zp.said.count("TEST_RED team_5") == 1
    asyncio.run(zp.p.clock())
    assert zp.said.count("TEST_RED team_5") == 1                       # just sent: not again yet
    asyncio.run(zp.p.on_rce_connection(True))                          # e.g. the server restarted
    asyncio.run(zp.p.clock())
    assert zp.said.count("TEST_RED team_5") == 2 and zp.p.zone(5)["state"] == "red"
    zone = zp.p.zone(5)
    zone["applied"] -= 11 * 60                                          # 11 minutes later
    zp.p._save(5, zone)
    asyncio.run(zp.p.clock())
    assert zp.said.count("TEST_RED team_5") == 3
    old = zp.p.zone(5)                                                  # zones from before this update
    old.pop("applied")
    zp.p._save(5, old)
    asyncio.run(zp.p.clock())
    assert zp.said.count("TEST_RED team_5") == 4
    asyncio.run(zp.m.save_settings("offline_protection", {"reapply_minutes": 0}))
    asyncio.run(zp.p.on_rce_connection(True))
    asyncio.run(zp.p.clock())
    assert zp.said.count("TEST_RED team_5") == 4                       # 0 = only on changes


def test_panel_shows_who_keeps_a_zone_green(zp):
    configure(zp)
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    zp.bot.online_players = {"mate": {}}                                # names compared without case
    asyncio.run(zp.p._update(5))
    z = zp.p.zone_list()[0]
    assert z["state"] == "green" and z["online"] == ["Mate"] and z["red_at"] is None
    zp.bot.online_players = {}
    asyncio.run(zp.p.on_rce_player_leave("Mate"))
    z = zp.p.zone_list()[0]
    assert z["online"] == [] and z["red_at"] == zp.p.zone(5)["offline_since"] + 30 * 60


def test_zone_size_by_team_size_and_vip(zp, monkeypatch):
    """Size from the team size table, ×1.5 for VIP teams; a team that grows gets a bigger zone (deleted and created
    again at the same spot, then its status again)."""
    import types
    configure(zp, size_by_team="1=20\n2=25\n3+=40", vip_players="Lead", team_sync_minutes=0, reapply_minutes=0)
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    assert "TEST_CREATE team_5 123.5 20.0 -456.25 38" in zp.said            # 2 members = 25, VIP leader ×1.5
    assert zp.p.zone_list()[0]["radius"] == 38 and zp.p.zone_list()[0]["vip"] is True
    zp.said.clear()
    asyncio.run(zp.p.clock())
    assert not any(c.startswith(("TEST_CREATE", "TEST_REMOVE")) for c in zp.said)   # same size: nothing sent
    zp.db.team_event("join", "Third", 5, "Lead")                             # the team grows to 3
    asyncio.run(zp.p.clock())
    sent = [c for c in zp.said if c.startswith(("TEST_CREATE", "TEST_REMOVE", "TEST_GREEN"))]
    assert sent == ["TEST_REMOVE team_5", "TEST_CREATE team_5 123.5 20.0 -456.25 60", "TEST_GREEN team_5"]
    assert zp.p.zone(5)["radius"] == 60 and (zp.p.zone(5)["x"], zp.p.zone(5)["z"]) == (123.5, -456.25)
    # VIP counts for the leader only (default) – a VIP member doesn't, unless "any member is VIP"
    configure(zp, size_by_team="1=20\n2=25\n3+=40", vip_players="Mate", team_sync_minutes=0, reapply_minutes=0)
    asyncio.run(zp.p.clock())
    assert zp.p.zone(5)["radius"] == 40
    configure(zp, size_by_team="1=20\n2=25\n3+=40", vip_players="Mate", vip_who="any member is VIP",
              team_sync_minutes=0, reapply_minutes=0)
    asyncio.run(zp.p.clock())
    assert zp.p.zone(5)["radius"] == 60
    # VIP by Discord role (linked account)
    configure(zp, size_by_team="1=20\n2=25\n3+=40", vip_role=777, vip_percent=200, vip_players="", team_sync_minutes=0,
              reapply_minutes=0)
    zp.db.link_player(42, "Lead")
    member = types.SimpleNamespace(roles=[types.SimpleNamespace(id=777)])
    guild = types.SimpleNamespace(get_member=lambda uid: member if uid == 42 else None)
    monkeypatch.setattr(type(zp.p), "guild", property(lambda self: guild))
    asyncio.run(zp.p.clock())
    assert zp.p.zone(5)["radius"] == 80 and zp.p.zone_list()[0]["vip"] is True
    member.roles = []                                                        # VIP role gone: back to normal
    asyncio.run(zp.p.clock())
    assert zp.p.zone(5)["radius"] == 40


def test_size_table_mistakes_are_reported_and_old_zones_keep_their_size(zp):
    from plugins.offline_protection import parse_size_table
    assert parse_size_table("1=20\n 3 - 4 : 30 \n5+=40\n") == ([(1, 1, 20), (3, 4, 30), (5, None, 40)], [])
    assert parse_size_table("two=25\n2=0")[1] == ["two=25", "2=0"]
    configure(zp, size_by_team="two=25")
    assert "Zone size by team size" in zp.p.problems()[0]
    configure(zp, team_sync_minutes=0, reapply_minutes=0)
    chat(zp.p, "Lead", ASK)
    chat(zp.p, "Lead", YES)
    old = zp.p.zone(5)
    old.pop("radius")                                                        # a zone from before sizes existed
    zp.p._save(5, old)
    zp.said.clear()
    asyncio.run(zp.p.clock())
    assert zp.p.zone(5)["radius"] == 25 and not any(c.startswith("TEST_REMOVE") for c in zp.said)
