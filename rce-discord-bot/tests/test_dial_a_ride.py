"""Dial-a-ride: an emote spawns a minicopter / scrap heli / attack heli / horse next to the player."""
import asyncio
from types import SimpleNamespace as NS

from tests.test_plugins import pb  # noqa: F401


def _chat(player, raw):
    return NS(channel="GLOBAL", player=player, raw=raw, text="")


def test_emote_spawns_the_ride_next_to_the_player(pb, monkeypatch):  # noqa: F811
    from support import emote_binds

    async def main():
        await pb.m.load_all()
        await pb.m.set_enabled("dial_a_ride", True)
        ride = pb.m.running("dial_a_ride")
        assert any("No ride has an emote" in p for p in ride.problems())
        heli_emote = emote_binds.emotes()[0]["text"]
        horse_emote = emote_binds.emotes()[1]["text"]
        await pb.m.save_settings("dial_a_ride", {"minicopter_emote": heli_emote, "horse_emote": horse_emote,
                                                 "cooldown": 30})
        assert ride.problems() == []
        pb.bot.online_players = ["Lukas"]
        monkeypatch.setattr(type(ride), "online_name", lambda self, p: "Lukas" if p.lower() == "lukas" else None)

        async def pos(self, player):
            return (100.0, 20.0, -50.0)
        monkeypatch.setattr(type(ride), "position", pos)
        raw = emote_binds.find(heli_emote)[0]
        await ride.on_rce_chat(_chat("Lukas", raw))
        spawn = [c for c in pb.said if c.startswith("entity.spawnat")]
        assert spawn == ["entity.spawnat minicopter.entity (104.0,21.0,-50.0)"]
        assert any("your Minicopter" in c for c in pb.said)
        await ride.on_rce_chat(_chat("Lukas", emote_binds.find(horse_emote)[0]))   # cooldown (any vehicle)
        assert len([c for c in pb.said if c.startswith("entity.spawnat")]) == 1
        assert any("next ride in" in c for c in pb.said)
        assert await ride.call("Lukas", "attackheli", free=True)                  # admin gift: no cooldown
        assert pb.said[-2].startswith("entity.spawnat attackhelicopter.entity ") or any(
            c.startswith("entity.spawnat attackhelicopter.entity") for c in pb.said)
        assert ride.status()["Minicopter"] == 1 and ride.status()["Attack helicopter"] == 1
        # a price needs a linked account
        await pb.m.save_settings("dial_a_ride", {"cooldown": 0, "horse_price": 500})
        n = len([c for c in pb.said if c.startswith("entity.spawnat")])
        await ride.on_rce_chat(_chat("Lukas", emote_binds.find(horse_emote)[0]))
        assert len([c for c in pb.said if c.startswith("entity.spawnat")]) == n and any("costs 500" in c for c in pb.said)
        # the spawn command is a setting
        await pb.m.save_settings("dial_a_ride", {"spawn_command": "spawn {prefab} {x} {y} {z}", "horse_price": 0})
        await ride.on_rce_chat(_chat("Lukas", emote_binds.find(horse_emote)[0]))
        assert "spawn testridablehorse 104.0 21.0 -50.0" in pb.said
        # same emote twice is a problem
        await pb.m.save_settings("dial_a_ride", {"scrapheli_emote": heli_emote})
        assert any("same emote" in p for p in ride.problems())
        await pb.bot.rcon.close()
    asyncio.run(main())
