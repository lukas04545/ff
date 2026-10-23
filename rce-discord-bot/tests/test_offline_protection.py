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


DOCUMENTED = {"zones.createcustomzone", "zones.editcustomzone", "zones.deletecustomzone", "server.printpos", "say"}


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
    assert zp.p.status() == {"Zones": 1, "Red (team offline)": 0, "Green": 1}


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
