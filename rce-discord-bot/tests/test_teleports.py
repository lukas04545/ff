"""Location teleports: Location emotes (North … Northwest) teleport to set coordinates and never give kits."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from rce import log_parser as lp
from support import emote_binds
from support import teleports as tp
from tests.test_kit_requests import env  # noqa: F401
from tests.test_web import H, bot, run  # noqa: F401

NORTH = "d11_quick_chat_location_slot_0"


def test_locations_helpers():
    assert tp.find("north") == NORTH and tp.find("NE") == tp.LOCATIONS[1] and tp.find("South-West") == tp.LOCATIONS[5]
    assert tp.find("thanks") is None and tp.find(NORTH) == NORTH
    assert tp.parse_position("Position: (12.5, 30.0, -400.25)") == (12.5, 30.0, -400.25)
    assert tp.parse_position("12 30 -4") == (12.0, 30.0, -4.0) and tp.parse_position("no player") is None
    with pytest.raises(ValueError):
        tp.check_coords(1, 2, 999999)
    # Location emotes are not kit emotes
    assert all(not tp.is_location(e["raw"]) for e in emote_binds.emotes())
    assert emote_binds.find("north")[0] is None
    with pytest.raises(ValueError):
        emote_binds.bind(None, NORTH, "Starter")


@pytest.fixture
def tele(env, monkeypatch):  # noqa: F811
    from cogs.teleports import Teleports
    bot = env.bot
    bot.config = replace(bot.config, kit_claim_announce=True)
    sent = []

    async def command(cmd, expect_response=True):
        sent.append(cmd)
        return "(101.5, 20.0, -33.3)" if cmd.startswith("server.printpos") else ""
    monkeypatch.setattr(bot.rcon, "command", command)
    bot.online_players.clear()
    bot.online_players.update({"AdminGuy": {}, "Bob": {}})
    cog = Teleports(bot)
    return NS(cog=cog, bot=bot, sent=sent)


def test_capture_set_and_teleport_in_game(tele, monkeypatch):
    who, pos = asyncio.run(tele.cog.capture("adminguy"))
    assert who == "AdminGuy" and pos == (101.5, 20.0, -33.3) and tele.sent == ['server.printpos "AdminGuy"']
    t = tele.cog.set(NORTH, *pos, name="Outpost", cooldown_minutes=10, by="t")
    assert t["set"] and t["name"] == "Outpost" and t["cooldown_minutes"] == 10
    tele.sent.clear()
    chat = lp.ChatEvent("GLOBAL", "Bob", NORTH, "North")
    asyncio.run(tele.cog.on_rce_chat(chat))
    assert tele.sent[0] == 'global.teleportpos (101.5,20.0,-33.3) "Bob"' and "teleported to" in tele.sent[1]
    tele.sent.clear()
    asyncio.run(tele.cog.on_rce_chat(chat))                                 # cooldown: no second teleport
    assert not any(c.startswith("global.teleportpos") for c in tele.sent) and "again in" in tele.sent[0]
    tele.bot.db.update_teleport(NORTH, enabled=False)
    tele.sent.clear()
    asyncio.run(tele.cog.on_rce_chat(lp.ChatEvent("GLOBAL", "Other", NORTH, "North")))
    assert tele.sent == []                                                  # switched off
    from cogs.teleports import TeleportError
    with pytest.raises(TeleportError):
        asyncio.run(tele.cog.capture("Nobody"))                             # not online
    with pytest.raises(TeleportError):
        tele.cog.set("d11_quick_chat_responses_slot_3", 1, 2, 3)            # only Location emotes


def test_old_kit_binds_on_locations_are_removed(tele):
    db = tele.bot.db
    db.add_kit_rule("Starter", "quickchat", NORTH, 0, 0, "old")
    keep = db.add_kit_rule("Starter", "quickchat", "d11_quick_chat_responses_slot_3", 0, 0, "old")
    asyncio.run(tele.cog.cog_load())
    assert [r["id"] for r in db.kit_rules()] == [keep]


def test_panel_teleports(bot, monkeypatch):  # noqa: F811
    async def scenario(c):
        from cogs.teleports import Teleports
        await bot.add_cog(Teleports(bot))

        async def command(cmd, expect_response=True):
            return "(5.0, 6.0, 7.0)"
        monkeypatch.setattr(bot.rcon, "command", command)
        bot.online_players["AdminGuy"] = {}
        await c.post("/api/login", json={"password": "secret123"}, headers=H)
        r = await (await c.get("/api/teleports")).json()
        assert len(r["teleports"]) == 8 and r["online"] == ["AdminGuy"] and not r["teleports"][0]["set"]
        r = await c.put("/api/teleports", json={"location": "north", "x": 1, "y": 2, "z": 3, "name": "Outpost",
                                                "cooldown": 5}, headers=H)
        t = (await r.json())["teleport"]
        assert t["x"] == 1 and t["name"] == "Outpost" and t["cooldown_minutes"] == 5
        r = await c.post("/api/teleports/capture", json={"player": "adminguy", "location": "North"}, headers=H)
        body = await r.json()
        assert body["x"] == 5.0 and body["teleport"]["z"] == 7.0 and body["teleport"]["name"] == "Outpost"
        assert (await c.post("/api/teleports/capture", json={"player": "Ghost"}, headers=H)).status == 400
        assert (await c.put("/api/teleports", json={"location": "thanks", "x": 1, "y": 1, "z": 1},
                            headers=H)).status == 400
        assert (await c.put("/api/teleports", json={"location": "north", "x": "a", "y": 1, "z": 1},
                            headers=H)).status == 400
        assert (await c.post("/api/autokits", json={"kit": "x", "trigger": "quickchat", "phrase": NORTH},
                             headers=H)).status == 400                    # no kits on Location emotes
        assert (await c.post("/api/teleports/clear", json={"location": "north"}, headers=H)).status == 200
        assert bot.db.teleport(NORTH) is None
    run(bot, scenario)
