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
                                             "shrink_minutes": 3, "warn_seconds": 30, "equip_seconds": 0}))
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
    rad = [c for c in zones if '"br_rad0_' in c]
    assert rad and all(" Box (" in c and ",600," in c and c.endswith(" 1 1 50 1 1") for c in rad)   # tall boxes
    assert any('"br_border0"' in c and " 1 1 0 1 1" in c for c in zones)
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
    created = pb.said.index(next(c for c in pb.said if c.startswith('zones.createcustomzone "br_rad1_0"')))
    deleted = pb.said.index('zones.deletecustomzone "br_rad0_0"')
    assert created < deleted                                   # the new zone first, the old one after
    assert p.game["zones"][0] == "br_rad1_0" and p.data.get("zones") == p.game["zones"]
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
    assert any(c.startswith('zones.deletecustomzone "br_border1"') for c in pb.said)
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


def _in_box(px, pz, x, z, rot, width, depth):
    """Yaw rot: the box's local forward (sin, cos) points out from the centre – depth along it, width across."""
    t = math.radians(rot)
    fx, fz, rx, rz = math.sin(t), math.cos(t), math.cos(t), -math.sin(t)
    dx, dz = px - x, pz - z
    return abs(dx * fx + dz * fz) <= depth / 2 + 0.01 and abs(dx * rx + dz * rz) <= width / 2 + 0.01


def test_radiation_boxes_fill_everything_outside_the_zone_up_to_the_map_edge():
    for cx, cz, r in ((30, -20, 1500), (900, -600, 200), (-1500, 1400, 40), (0, 0, 3246)):
        w, depth, boxes = ring_boxes(cx, cz, r, 120, 40, 4500)
        assert len(boxes) == 40
        for x, z, rot in boxes:                                # inner face on the circle
            assert abs(math.hypot(x - cx, z - cz) - (r + depth / 2)) < 1
        for i in range(720):                                   # outside the circle, to beyond the map: radiation
            a = math.radians(i / 2)
            for d in (0.005 * r + 1, 5, 50, 300, 1000, 2500, 4000):
                px, pz = cx + (r + d) * math.cos(a), cz + (r + d) * math.sin(a)
                if max(abs(px), abs(pz)) > 2250 + 100:         # beyond the map + margin: not needed
                    continue
                assert any(_in_box(px, pz, *b, w, depth) for b in boxes), (cx, cz, r, i, d)
        for i in range(360):                                   # inside the circle: never
            a = math.radians(i)
            px, pz = cx + 0.99 * r * math.cos(a), cz + 0.99 * r * math.sin(a)
            assert not any(_in_box(px, pz, *b, w, depth) for b in boxes)
    _, _, normal = ring_boxes(0, 0, 500, 120, 40, 4500)
    _, _, turned = ring_boxes(0, 0, 500, 120, 40, 4500, reverse=True)
    assert [b[2] for b in normal] != [b[2] for b in turned] and normal[0][:2] == turned[0][:2]


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
    assert any(" 0 Sphere " in c and c.endswith(" 1 1 80 1 1") for c in pb.said if '"br_rad' in c)
    run(p.end_round())


def test_kit_first_then_15_seconds_to_put_the_parachute_on(pb, monkeypatch):  # noqa: F811
    import plugins.battle_royale as br
    slept = []
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)
    monkeypatch.setattr(br.asyncio, "sleep", fast_sleep)

    async def main():
        await pb.m.load_all()
        await pb.m.set_enabled("battle_royale", True)
        await pb.m.save_settings("battle_royale", {"kit": "BR start"})
        p = pb.m.running("battle_royale")
        pb.bot.online_players = {"Ann": {}, "Bob": {}}
        p.open_lobby(30)
        p.join("Ann")
        p.join("Bob")
        await p.start_round()
        pb.said.clear()
        await p.on_rce_respawn(RespawnEvent("Ann", "Xbox"))
        assert pb.said[0] == 'kit givetoplayer "BR start" "Ann"'                # the kit comes first
        assert any("put your parachute on" in c and "15 seconds" in c for c in pb.said)
        assert not any(c.startswith("global.teleportpos") for c in pb.said)     # not yet
        assert not p.game["players"]["ann"]["dropped"]
        await p._waiting["ann"]
        assert slept == [10, 5] and any("5 seconds" in c for c in pb.said)
        assert sum(c.startswith('global.teleportpos') and c.endswith('"Ann"') for c in pb.said) == 1
        assert p.game["players"]["ann"]["dropped"] and "ann" not in p._waiting
        # respawning again while waiting starts over (new kit), only one teleport in the end
        pb.said.clear()
        slept.clear()
        await p.on_rce_respawn(RespawnEvent("Bob", "Xbox"))
        first = p._waiting["bob"]
        await p.on_rce_respawn(RespawnEvent("Bob", "Xbox"))
        assert first.cancelled() or first.cancelling()
        await p._waiting["bob"]
        assert pb.said.count('kit givetoplayer "BR start" "Bob"') == 2
        assert sum(c.startswith('global.teleportpos') and c.endswith('"Bob"') for c in pb.said) == 1
        # the round ends while someone waits: no teleport afterwards
        await p.end_round()
        assert p._waiting == {}
    run(main())


def _fake_server(pb, monkeypatch, *, with_id=True):
    counter = [100000]

    async def command(cmd, **kw):
        pb.said.append(cmd)
        if cmd.startswith("entity.spawn"):
            counter[0] += 1
            return f"Spawned vendingmachine.deployed at (12.5, 200.0, -1234.5) [{counter[0]}]" if with_id else "Spawned"
        return ""
    monkeypatch.setattr(pb.bot.rcon, "command", command)


def test_test_round_markers_shrink_and_stop(pb, monkeypatch):  # noqa: F811
    _fake_server(pb, monkeypatch)

    async def main():
        await pb.m.load_all()
        await pb.m.set_enabled("battle_royale", True)
        await pb.m.save_settings("battle_royale", {"marker_count": 8})
        p = pb.m.running("battle_royale")
        ok, msg = await p.start_test()
        assert ok and p.game["test"] and p.game["players"] == {}
        spawns = [c for c in pb.said if c.startswith("entity.spawn vendingmachine.deployed")]
        assert len(spawns) == 8 and all(c.split()[-1].split(",")[1] == "200" for c in spawns)   # in the sky
        first = list(p.game["markers"])
        assert len(first) == 8 and p.data.get("markers") == first
        assert not (await p.start_test())[0]                   # one round at a time
        # no players: the round doesn't end by itself and never shrinks on its own
        await p._check_winner()
        p.game["started"] -= 99999
        await p.clock()
        assert p.game["state"] == "running" and p.game["radius"] == whole_map_radius(0, 0, 4500)
        assert not any("[Battle royale]" in c for c in pb.said)            # no announcements in a test round
        pb.said.clear()
        ok, _ = await p.shrink_now()
        assert ok and p.game["radius"] < whole_map_radius(0, 0, 4500)
        assert [c for c in pb.said if c.startswith("entity.deleteentity")] == [f"entity.deleteentity {i}" for i in first]
        assert len([c for c in pb.said if c.startswith("entity.spawn")]) == 8 and p.game["markers"] != first
        second = list(p.game["markers"])
        pb.said.clear()
        await p.end_round()
        assert [c for c in pb.said if c.startswith("entity.deleteentity")] == [f"entity.deleteentity {i}" for i in second]
        assert any(c.startswith('zones.deletecustomzone "br_rad1_0"') for c in pb.said)
        assert p.data.get("markers") is None and p.game["state"] == "idle"
        assert not (await p.shrink_now())[0]
    run(main())


def test_markers_stop_without_an_id_and_are_cleaned_after_a_restart(pb, monkeypatch):  # noqa: F811
    _fake_server(pb, monkeypatch, with_id=False)

    async def main():
        await pb.m.load_all()
        await pb.m.set_enabled("battle_royale", True)
        p = pb.m.running("battle_royale")
        await p.start_test()
        assert len([c for c in pb.said if c.startswith("entity.spawn")]) == 1     # stopped after the first
        assert p.game["markers_broken"] and p.game["markers"] == []
        pb.said.clear()
        await p.shrink_now()
        assert not any(c.startswith("entity.spawn") for c in pb.said)            # not again this round
        await p.end_round()
        p.data.set("markers", ["555555", "666666"])           # left by a round cut off by a restart
        pb.said.clear()
        await p.on_load()
        assert pb.said == ["entity.deleteentity 555555", "entity.deleteentity 666666"]
    run(main())


def test_marker_test_button_shows_the_replies(pb, monkeypatch):  # noqa: F811
    _fake_server(pb, monkeypatch)

    async def main():
        await pb.m.load_all()
        await pb.m.set_enabled("battle_royale", True)
        p = pb.m.running("battle_royale")
        r = await p.test_marker()
        assert r["spawn_command"] == "entity.spawn vendingmachine.deployed 0.0,200,0.0"
        assert r["id"] == "100001" and r["delete_command"] == "entity.deleteentity 100001"   # not a coordinate
        assert p.panel_state()["marker_test"] == r
    run(main())


def test_battle_royale_panel_api(pb, monkeypatch):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer
    _fake_server(pb, monkeypatch)
    H = {"X-Requested-With": "rce-web"}

    async def main():
        from web.server import WebInterface
        await pb.m.load_all()
        w = WebInterface(pb.bot, env_path=pb.tmp / ".env", secret_path=pb.tmp / "web_secret.key")
        c = TestClient(TestServer(w.app))
        await c.start_server()
        try:
            assert (await c.get("/api/br")).status == 401
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            d = await (await c.get("/api/br")).json()
            assert d["game"] is None and d["plugin"]["id"] == "battle_royale"
            assert (await c.post("/api/br/test", json={}, headers=H)).status == 400       # switched off
            await pb.m.set_enabled("battle_royale", True)
            r = await c.post("/api/br/test", json={}, headers=H)
            assert r.status == 200 and (await r.json())["game"]["test"]
            r = await c.post("/api/br/shrink", json={}, headers=H)
            assert r.status == 200 and "shrank" in (await r.json())["message"]
            r = await c.post("/api/br/marker-test", json={}, headers=H)
            assert (await r.json())["marker_test"]["id"]
            r = await c.post("/api/br/stop", json={}, headers=H)
            assert r.status == 200 and (await (await c.get("/api/br")).json())["game"]["state"] == "idle"
            assert (await c.post("/api/br/stop", json={}, headers=H)).status == 400
        finally:
            await c.close()
            await pb.bot.rcon.close()
    run(main())


def test_clear_vending_machines_button(pb, monkeypatch):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer
    _fake_server(pb, monkeypatch)
    H = {"X-Requested-With": "rce-web"}

    async def main():
        from web.server import WebInterface
        await pb.m.load_all()
        await pb.m.set_enabled("battle_royale", True)
        await pb.m.save_settings("battle_royale", {"marker_count": 4})
        p = pb.m.running("battle_royale")
        await p.start_test()
        await p.shrink_now()
        await p.end_round()                                    # deleted (maybe) – but remembered
        assert len(p.known_markers()) == 8
        w = WebInterface(pb.bot, env_path=pb.tmp / ".env", secret_path=pb.tmp / "web_secret.key")
        c = TestClient(TestServer(w.app))
        await c.start_server()
        try:
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            assert (await (await c.get("/api/br")).json())["game"]["known_markers"] == 8
            pb.said.clear()
            r = await (await c.post("/api/br/clear-markers", json={}, headers=H)).json()
            assert r["marker_clear"]["count"] == 8 and len(r["marker_clear"]["replies"]) == 5
            assert [x for x in pb.said if x.startswith("entity.deleteentity")] == \
                [f"entity.deleteentity {i}" for i in range(100001, 100009)]
            assert (await (await c.get("/api/br")).json())["game"]["marker_clear"]["count"] == 8
            r = await (await c.post("/api/br/forget-markers", json={}, headers=H)).json()
            assert "8" in r["message"] and p.known_markers() == []
        finally:
            await c.close()
            await pb.bot.rcon.close()
    run(main())


def test_delete_all_zones_button(pb, monkeypatch):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer
    _fake_server(pb, monkeypatch)
    H = {"X-Requested-With": "rce-web"}

    async def main():
        from web.server import WebInterface
        await pb.m.load_all()
        await pb.m.set_enabled("battle_royale", True)
        p = pb.m.running("battle_royale")
        await p.start_test()
        w = WebInterface(pb.bot, env_path=pb.tmp / ".env", secret_path=pb.tmp / "web_secret.key")
        c = TestClient(TestServer(w.app))
        await c.start_server()
        try:
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            pb.said.clear()
            r = await (await c.post("/api/br/delete-zones", json={}, headers=H)).json()
            assert r["ok"] and r["game"]["state"] == "idle"              # the running round was stopped
            deleted = {x for x in pb.said if x.startswith("zones.deletecustomzone")}
            for name in ("br_rad_0", "br_rad_119", "br_rad0_39", "br_rad1_7", "br_border", "br_border0", "br_border1"):
                assert f'zones.deletecustomzone "{name}"' in deleted   # old names and leftovers too
            assert p.data.get("zones") is None
        finally:
            await c.close()
            await pb.bot.rcon.close()
    run(main())
