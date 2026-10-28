"""Battle royale: lobby, kill + respawn → drop with the kit, radiation ring that shrinks inside the old circle,
eliminations from the killfeed, far-outside kill, winner with points, cleanup (also after a restart)."""
import asyncio
import math
import random
import time

from plugins.battle_royale import ring_boxes, drop_spot, next_circle, parse_items, ring_spheres, whole_map_radius
from rce.log_parser import KillEvent, KillerType, RespawnEvent
from tests.test_plugins import pb  # noqa: F401


def run(coro):
    return asyncio.run(coro)


def test_ring_covers_the_circle_edge_and_is_capped():
    r, centres = ring_spheres(100, -50, 1500, 120, 40)
    assert len(centres) <= 40
    for x, z in centres:                                       # inner edge of every sphere on the circle
        assert abs(math.hypot(x - 100, z + 50) - (1500 + r)) < 1
    # any point just outside the circle (5 % of a sphere) is inside some sphere
    for i in range(360):
        a = math.radians(i)
        px, pz = 100 + (1500 + 0.08 * r) * math.cos(a), -50 + (1500 + 0.08 * r) * math.sin(a)
        assert any(math.hypot(px - x, pz - z) <= r for x, z in centres)
    small_r, small = ring_spheres(0, 0, 50, 120, 40)
    assert small_r == 120 and 6 <= len(small) <= 40


def test_next_circle_is_inside_and_stops_at_the_minimum():
    rng = random.Random(7)
    cx, cz, r = 0.0, 0.0, 1500.0
    for _ in range(20):
        nx, nz, nr = next_circle(cx, cz, r, 60, 40, rng)
        assert nr <= r and math.hypot(nx - cx, nz - cz) + nr <= r + 0.5
        cx, cz, r = nx, nz, nr
    assert r == 40
    assert parse_items("parachute 1\npistol.semiauto\nammo.pistol 40\nbad item!\nx") == [
        ("parachute", 1), ("pistol.semiauto", 1), ("ammo.pistol", 40)]


def test_a_whole_round(pb, monkeypatch):  # noqa: F811
    run(pb.m.load_all())
    run(pb.m.set_enabled("battle_royale", True))
    run(pb.m.save_settings("battle_royale", {"win_points": 500, "kill_points": 50, "far_kill_seconds": 15,
                                             "shrink_minutes": 3, "warn_seconds": 30}))
    p = pb.m.running("battle_royale")
    p.rng = random.Random(3)
    pb.bot.online_players = {"Ann": {}, "Bob": {}, "Cid": {}, "Visitor": {}}
    for uid, name in ((1, "Ann"), (2, "Bob"), (3, "Cid")):
        pb.db.link_player(uid, name)
    ok, _ = p.join("Ann")
    assert not ok                                              # no lobby yet
    assert p.open_lobby(60)[0]
    assert p.join("ann")[0] and p.join("Bob")[0] and p.join("Cid")[0]
    assert not p.join("Ann")[0] and not p.join("Offline guy")[0]
    pb.said.clear()
    run(p.start_round())
    zones = [c for c in pb.said if c.startswith("zones.createcustomzone")]
    rad = [c for c in zones if '"br_rad_' in c]
    assert rad and all(" Box (" in c and ",600," in c and c.endswith(" 1 1 50 1 1") for c in rad)   # tall boxes
    assert any('"br_border"' in c and " 1 1 0 1 1" in c for c in zones)
    assert [c for c in pb.said if c.startswith("global.killplayer")] == [
        'global.killplayer "Ann"', 'global.killplayer "Bob"', 'global.killplayer "Cid"']
    # the start kill doesn't count – nobody has dropped yet
    run(p.on_rce_kill(KillEvent("Ann", KillerType.PLAYER, "Ann", KillerType.PLAYER)))
    assert p.game["players"]["ann"]["alive"]
    pb.said.clear()
    for name in ("Ann", "Bob", "Cid"):
        run(p.on_rce_respawn(RespawnEvent(name, "Xbox")))
    tps = [c for c in pb.said if c.startswith("global.teleportpos")]
    assert len(tps) == 3 and all(",250," in c for c in tps)    # drop height
    assert pb.said.count('inventory.giveto "Ann" "parachute" 1') == 1
    assert 'inventory.giveto "Bob" "pistol.semiauto" 1' in pb.said
    run(p.on_rce_respawn(RespawnEvent("Ann", "Xbox")))         # a second respawn: no second drop
    assert len([c for c in pb.said if c.startswith("global.teleportpos")]) == 3
    # shrink: the ring is deleted and built again, smaller
    old_radius = p.game["radius"]
    pb.said.clear()
    run(p.shrink())
    assert p.game["radius"] < old_radius
    assert any(c.startswith('zones.deletecustomzone "br_rad_0"') for c in pb.said)
    assert any(c.startswith('zones.createcustomzone "br_rad_0"') for c in pb.said)
    # far outside: warned, then killed and out
    async def far_away(name):
        return (99999.0, 50.0, 99999.0) if name == "Cid" else (p.game["cx"], 50.0, p.game["cz"])
    monkeypatch.setattr(p, "_position", far_away)
    t = time.time() + 120                                       # landed a while ago
    run(p._check_positions(t))
    assert p.game["players"]["cid"]["far_since"] == t and p.game["players"]["cid"]["alive"]
    assert p.game["players"]["ann"]["far_since"] is None
    run(p._check_positions(t + 16))
    assert not p.game["players"]["cid"]["alive"] and 'global.killplayer "Cid"' in pb.said
    # PvP kill: Ann kills Bob – Ann wins, gets kill + win points, zones deleted
    pb.said.clear()
    run(p.on_rce_kill(KillEvent("Bob", KillerType.PLAYER, "Ann", KillerType.PLAYER)))
    assert p.game["state"] == "idle"
    assert pb.db.wallet(1)["points"] == 550 and pb.db.wallet(2)["points"] == 0
    assert any(c.startswith('zones.deletecustomzone "br_border"') for c in pb.said)
    assert any("Ann wins the battle royale" in c for c in pb.said)
    assert p.data.get("zones") is None and p.status()["Rounds played"] == 1


def test_too_few_players_and_cleanup_after_restart(pb):  # noqa: F811
    run(pb.m.load_all())
    run(pb.m.set_enabled("battle_royale", True))
    p = pb.m.running("battle_royale")
    pb.bot.online_players = {"Ann": {}}
    p.open_lobby(30)
    p.join("Ann")
    assert "Not enough players" in run(p.start_round()) and p.game["state"] == "idle"
    p.data.set("zones", ["br_rad_0", "br_border"])            # a round cut off by a restart
    pb.said.clear()
    run(p.on_load())
    assert pb.said == ['zones.deletecustomzone "br_rad_0"', 'zones.deletecustomzone "br_border"']
    assert p.data.get("zones") is None


def test_start_zone_is_the_whole_island_and_drops_stay_on_the_map():
    for cx, cz in ((0, 0), (400, -900)):
        r = whole_map_radius(cx, cz, 3500)
        assert all(math.hypot(x - cx, z - cz) < r for x in (-1750, 1750) for z in (-1750, 1750))   # every corner
    rng = random.Random(5)
    for _ in range(500):
        x, z = drop_spot(0, 0, 2500, 3500, 12, rng)
        assert abs(x) <= 1540 and abs(z) <= 1540


def test_round_starts_with_the_whole_map_safe(pb):  # noqa: F811
    run(pb.m.load_all())
    run(pb.m.set_enabled("battle_royale", True))
    p = pb.m.running("battle_royale")
    pb.bot.online_players = {"Ann": {}, "Bob": {}}
    p.open_lobby(30)
    p.join("Ann")
    p.join("Bob")
    run(p.start_round())
    assert p.game["radius"] == whole_map_radius(0, 0, 4500)
    run(p.end_round())


def test_radiation_boxes_ring_the_circle_tall_and_without_gaps():
    w, boxes = ring_boxes(30, -20, 1500, 120, 40)
    assert len(boxes) <= 40

    def inside(px, pz, x, z, rot):                             # yaw rot: local forward (sin, cos) faces the centre
        t = math.radians(rot)
        fx, fz, rx, rz = math.sin(t), math.cos(t), math.cos(t), -math.sin(t)
        dx, dz = px - x, pz - z
        return abs(dx * fx + dz * fz) <= w / 2 + 0.01 and abs(dx * rx + dz * rz) <= w / 2 + 0.01
    for x, z, rot in boxes:                                    # inner face on the circle
        assert abs(math.hypot(x - 30, z + 20) - (1500 + w / 2)) < 1
    for i in range(720):                                       # just outside the circle: always radiation
        a = math.radians(i / 2)
        for d in (0.02, 0.5, 0.95):
            px, pz = 30 + (1500 + d * w) * math.cos(a), -20 + (1500 + d * w) * math.sin(a)
            assert any(inside(px, pz, *b) for b in boxes), (i, d)
    for i in range(360):                                       # inside the circle: never
        a = math.radians(i)
        px, pz = 30 + 1490 * math.cos(a), -20 + 1490 * math.sin(a)
        assert not any(inside(px, pz, *b) for b in boxes)


def test_sphere_shape_and_old_saved_command_still_work(pb):  # noqa: F811
    from plugins.battle_royale import OLD_RING_DEFAULT
    run(pb.m.load_all())
    run(pb.m.set_enabled("battle_royale", True))
    run(pb.m.save_settings("battle_royale", {"ring_command": OLD_RING_DEFAULT, "radiation": 80}))
    p = pb.m.running("battle_royale")
    pb.bot.online_players = {"Ann": {}, "Bob": {}}
    p.open_lobby(30)
    p.join("Ann")
    p.join("Bob")
    pb.said.clear()
    run(p.start_round())
    assert any(" Box (" in c and c.endswith(" 1 1 80 1 1") for c in pb.said)   # old default -> boxes now
    run(p.end_round())
    run(pb.m.save_settings("battle_royale", {"ring_shape": "Sphere"}))
    p.open_lobby(30)
    p.join("Ann")
    p.join("Bob")
    pb.said.clear()
    run(p.start_round())
    assert any(" 0 Sphere " in c and c.endswith(" 1 1 80 1 1") for c in pb.said if '"br_rad_' in c)
    run(p.end_round())
