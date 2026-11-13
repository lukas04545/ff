"""Dial-a-ride: an emote combo ("Let's go!" then e.g. "OK") spawns a minicopter / scrap heli / attack heli / horse
next to the player, helicopters with low grade fuel; per-ride roles; the combo emotes can't give kits."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from tests.test_plugins import pb  # noqa: F401


def _chat(player, raw):
    return NS(channel="GLOBAL", player=player, raw=raw, text="")


def test_emote_combo_spawns_the_ride_with_fuel(pb, monkeypatch):  # noqa: F811
    from support import emote_binds
    go, ok, no, thanks = (emote_binds.find(t)[0] for t in ("Let's go!", "OK", "No", "Thanks"))

    async def main():
        await pb.m.load_all()
        await pb.m.set_enabled("dial_a_ride", True)
        ride = pb.m.running("dial_a_ride")
        await pb.m.save_settings("dial_a_ride", {"cooldown": 30})
        assert ride.problems() == []
        pb.bot.online_players = ["Lukas"]
        monkeypatch.setattr(type(ride), "online_name", lambda self, p: "Lukas" if p.lower() == "lukas" else None)

        async def pos(self, player):
            return (100.0, 20.0, -50.0)
        monkeypatch.setattr(type(ride), "position", pos)
        spawns = lambda: [c for c in pb.said if c.startswith("entity.spawn")]   # noqa: E731
        # the ride's emote alone does nothing – it needs "Let's go!" first
        await ride.on_rce_chat(_chat("Lukas", ok))
        assert spawns() == []
        assert await ride.on_emote("Lukas", go, now=1000) is None
        assert await ride.on_emote("Lukas", ok, now=1030) is None                # too late (15 s)
        assert spawns() == []
        await ride.on_emote("Lukas", go, now=2000)
        assert await ride.on_emote("Lukas", ok, now=2005) == "minicopter"
        assert spawns() == ["entity.spawn minicopter.entity 104.0,21.0,-50.0"]
        assert 'inventory.giveto "Lukas" "lowgradefuel" 100' in pb.said            # fuel with the heli
        assert any("your Minicopter" in c and "+100 low grade fuel" in c for c in pb.said)
        await ride.on_emote("Lukas", go)
        await ride.on_emote("Lukas", thanks)                                       # cooldown (any vehicle)
        assert len(spawns()) == 1 and any("next ride in" in c for c in pb.said)
        assert await ride.call("Lukas", "horse", free=True)                        # admin gift: no cooldown
        assert pb.said.count('inventory.giveto "Lukas" "lowgradefuel" 100') == 1   # no fuel for a horse
        # a role per ride
        await pb.m.save_settings("dial_a_ride", {"cooldown": 0, "scrapheli_role": "555"})
        n = len(spawns())
        await ride.on_emote("Lukas", go)
        await ride.on_emote("Lukas", no)
        assert len(spawns()) == n and any("Link your account" in c for c in pb.said)
        # no start emote: the ride's emote alone calls it
        await pb.m.save_settings("dial_a_ride", {"start_emote": "", "scrapheli_role": "", "fuel": 0})
        await ride.on_rce_chat(_chat("Lukas", no))
        assert spawns()[-1].startswith("entity.spawn scraptransporthelicopter")
        await pb.m.save_settings("dial_a_ride", {"start_emote": "Let's go!", "fuel": 100})
        # the combo emotes can't give kits
        assert emote_binds.reserved_by(go) == "Dial-a-ride" and emote_binds.reserved_by(ok) == "Dial-a-ride"
        with pytest.raises(ValueError, match="Dial-a-ride"):
            emote_binds.bind(pb.bot.db, ok, "starter")
        assert emote_binds.reserved_by(emote_binds.find("Hello")[0]) is None
        await pb.m.set_enabled("dial_a_ride", False)
        assert emote_binds.reserved_by(go) is None                                 # off: free again
        await pb.bot.rcon.close()
    asyncio.run(main())


def test_kits_bound_before_dont_fire_on_combo_emotes(pb, monkeypatch):  # noqa: F811
    from cogs.kits import Kits
    from support import emote_binds
    ok = emote_binds.find("OK")[0]

    async def no_load(self):
        return None
    monkeypatch.setattr(Kits, "cog_load", no_load)
    tried = []

    async def try_claim(self, rule, player, source):
        tried.append(rule["kit"])
    monkeypatch.setattr(Kits, "_try_claim", try_claim)
    pb.bot.db.add_kit_rule("starter", "quickchat", ok, 0, 0, "test", "ingame", None)

    async def main():
        await pb.m.load_all()
        await pb.m.set_enabled("dial_a_ride", True)
        ride = pb.m.running("dial_a_ride")
        assert any("won't be given" in p for p in ride.problems())
        kits = Kits(pb.bot)
        await kits.on_rce_chat(NS(channel="GLOBAL", player="Lukas", raw=ok, text="OK"))
        assert tried == []                                                         # kept for Dial-a-ride
        await pb.m.set_enabled("dial_a_ride", False)
        await kits.on_rce_chat(NS(channel="GLOBAL", player="Lukas", raw=ok, text="OK"))
        assert tried == ["starter"]                                                # off: the kit works again
        await pb.bot.rcon.close()
    asyncio.run(main())


def test_each_ride_can_have_its_own_cooldown(pb, monkeypatch):  # noqa: F811
    import time as _time

    async def main():
        await pb.m.load_all()
        await pb.m.set_enabled("dial_a_ride", True)
        ride = pb.m.running("dial_a_ride")
        await pb.m.save_settings("dial_a_ride", {"cooldown": 0, "attackheli_cooldown": 120, "fuel": 0})
        monkeypatch.setattr(type(ride), "online_name", lambda self, p: "Lukas")

        async def pos(self, player):
            return (1.0, 2.0, 3.0)
        monkeypatch.setattr(type(ride), "position", pos)
        spawns = lambda: [c for c in pb.said if c.startswith("entity.spawn")]   # noqa: E731
        assert await ride.call("Lukas", "attackheli")
        assert not await ride.call("Lukas", "attackheli")                          # its own 2 h cooldown
        assert any("next Attack helicopter in" in c for c in pb.said)
        assert await ride.call("Lukas", "horse")                                   # other rides: no wait
        ride.data.set("used:lukas:attackheli", _time.time() - 121 * 60)
        assert await ride.call("Lukas", "attackheli")                              # 2 h later: again
        await pb.m.save_settings("dial_a_ride", {"cooldown": 30})
        assert not await ride.call("Lukas", "horse")                               # the cooldown for all rides
        assert len(spawns()) == 3
        await pb.m.set_enabled("dial_a_ride", False)
        await pb.bot.rcon.close()
    asyncio.run(main())


def test_spawn_tries_the_next_form_when_the_server_refuses(pb, monkeypatch):  # noqa: F811
    async def main():
        await pb.m.load_all()
        await pb.m.set_enabled("dial_a_ride", True)
        ride = pb.m.running("dial_a_ride")
        sent = []

        async def command(cmd, **kw):
            sent.append(cmd)
            if cmd.startswith("entity.spawn ") and "minicopter.entity" in cmd:
                return "Couldn't find prefab minicopter.entity"
            return "" if cmd.startswith("entity.spawn minicopter ") else "Unknown command"
        monkeypatch.setattr(pb.bot.rcon, "command", command)
        ok, _ = await ride.spawn("minicopter", 1, 2, 3, "Lukas")
        assert ok and sent[0] == "entity.spawn minicopter.entity 1,2,3" and sent[-1] == "entity.spawn minicopter 1,2,3"
        sent.clear()
        await ride.spawn("minicopter", 1, 2, 3, "Lukas")
        assert sent == ["entity.spawn minicopter 1,2,3"]                       # remembered
        assert "entity.spawn minicopter 1,2,3" in ride.last_spawn()
        await pb.m.save_settings("dial_a_ride", {"spawn_command": "spawn {prefab} {x} {y} {z}"})
        sent.clear()
        ok, why = await ride.spawn("horse", 1, 2, 3, "Lukas")
        assert not ok and sent == ["spawn testridablehorse 1 2 3"] and "Unknown command" in why
        await pb.m.set_enabled("dial_a_ride", False)
        await pb.bot.rcon.close()
    asyncio.run(main())


def test_tool_calls_written_as_text_are_run_not_shown():
    from support.deepseek import parse_text_tool_calls
    raw = ('<｜DSML｜function_calls>\n<｜DSML｜invoke name="test_emote_bind">\n'
           '<｜DSML｜parameter name="player" string="true">Jones_Ewan</｜DSML｜parameter>\n'
           '<｜DSML｜parameter name="count" string="false">2</｜DSML｜parameter>\n'
           '</｜DSML｜invoke>\n</｜DSML｜function_calls>')
    text, calls = parse_text_tool_calls("Let me check. " + raw)
    assert text == "Let me check." and calls[0]["function"]["name"] == "test_emote_bind"
    assert calls[0]["function"]["arguments"] == '{"player": "Jones_Ewan", "count": 2}'
    assert parse_text_tool_calls("normal answer about invoke")[1] == []
