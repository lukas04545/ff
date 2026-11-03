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
        spawns = lambda: [c for c in pb.said if c.startswith("entity.spawnat")]   # noqa: E731
        # the ride's emote alone does nothing – it needs "Let's go!" first
        await ride.on_rce_chat(_chat("Lukas", ok))
        assert spawns() == []
        assert await ride.on_emote("Lukas", go, now=1000) is None
        assert await ride.on_emote("Lukas", ok, now=1030) is None                # too late (15 s)
        assert spawns() == []
        await ride.on_emote("Lukas", go, now=2000)
        assert await ride.on_emote("Lukas", ok, now=2005) == "minicopter"
        assert spawns() == ["entity.spawnat minicopter.entity (104.0,21.0,-50.0)"]
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
        assert spawns()[-1].startswith("entity.spawnat scraptransporthelicopter")
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
