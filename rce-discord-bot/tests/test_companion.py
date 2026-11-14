"""RCE Companion (players' app): pairing, the alert feed, and the RF raid alarm (explosion sensor + RF broadcaster)."""
import asyncio
import json
from types import SimpleNamespace as NS

from tests.test_plugins import pb, plug  # noqa: F401
from tests.test_web import bot, run as run_web  # noqa: F401


def run(c):
    return asyncio.run(c)


def test_pairing_feed_and_limits(pb):  # noqa: F811
    c = plug(pb, "companion", max_devices=2)
    assert c.push(7, "x", "y") is None                          # nobody paired: nothing queued
    code = c.new_code(7)
    assert len(code) == 8 and c.exchange("nope") is None
    token, uid = c.exchange(code.lower(), "Pixel")              # any case
    assert uid == 7 and c.user_of(token) == 7
    assert c.exchange(code) is None                              # one-time
    n = c.push(7, "🚨 Raid alert", "Bob: boom", kind="raid")
    feed = c.feed(7, 0)
    assert [a["id"] for a in feed["alerts"]] == [n] and c.feed(7, n)["alerts"] == []
    t2, _ = c.exchange(c.new_code(7))
    t3, _ = c.exchange(c.new_code(7))
    assert c.devices(7) == 2 and c.user_of(token) is None        # the oldest phone beyond the limit is unpaired
    expired = c.new_code(7)
    row = c.data.get(f"code:{expired}")
    row["exp"] = 0
    c.data.set(f"code:{expired}", row)
    assert c.exchange(expired) is None
    assert c.unpair(t2) and c.user_of(t2) is None


def test_rf_frequencies_are_read_from_the_broadcaster_list():
    from plugins.raid_alerts import RaidAlerts
    raw = ("Broadcaster at (120.5, 3.0, -44.2) frequency 1234 range 250\n"
           "freq: 777 pos (1,2,3)\n4321 (10.0, 5.0, 2.0)\n12.5 not a frequency\n")
    assert RaidAlerts.parse_frequencies(raw) == {1234, 777, 4321}
    assert RaidAlerts.parse_frequencies("") == set()


def test_rf_raid_alarm_alerts_owner_and_team_once(pb, monkeypatch):  # noqa: F811
    p = plug(pb, "raid_alerts", rf=True, cooldown=5)
    comp = plug(pb, "companion")
    pb.db.link_player(7, "Bob")
    pb.db.link_player(8, "Ann")
    monkeypatch.setattr(pb.db, "team_of", lambda player: (NS(team_id=1), ["Bob", "Ann"]))
    sent = []

    async def alert(uid, player, line):
        sent.append((uid, player, line))
        return 1
    monkeypatch.setattr(p, "_alert", alert)
    assert "taken" not in p.claim_rf(7, 1234)
    assert "taken" in p.claim_rf(8, 1234)
    answers = iter(["", "Broadcaster frequency 1234 range 250", "Broadcaster frequency 1234 range 250", "",
                    "Broadcaster frequency 1234 range 250"])

    async def command(cmd, **kw):
        assert cmd == "rf.listboardcaster"
        return next(answers)
    monkeypatch.setattr(pb.bot.rcon, "command", command)
    assert run(p.rf_check()) == []                              # nothing on
    assert run(p.rf_check()) == [1234]                          # the sensor powered the broadcaster
    assert sorted(u for u, _, _ in sent) == [7, 8] and "1234" in sent[0][2]
    assert run(p.rf_check()) == []                              # still on: no second alert
    run(p.rf_check())                                           # off again
    assert run(p.rf_check()) == []                              # on again within the cooldown: quiet
    assert p.raid_stats(7)["total"] == 1
    assert comp is not None


def test_companion_web_endpoints(bot):  # noqa: F811
    from plugin_api import Plugin
    from plugin_manager import PluginManager

    async def scenario(c):
        m = bot.plugins
        comp = m.running("companion")
        code = comp.new_code(7)
        r = await c.post("/p/companion/exchange", data=json.dumps({"code": code, "device": "Pixel"}),
                         headers={"Content-Type": "application/json"})
        token = (await r.json())["token"]
        comp.push(7, "🚨 Raid alert", "Bob: boom")
        r = await c.get("/p/companion/feed?since=0", headers={"Authorization": f"Bearer {token}"})
        data = await r.json()
        assert r.status == 200 and data["alerts"][0]["title"] == "🚨 Raid alert" and "server" in data
        assert (await c.get("/p/companion/feed")).status == 401
        assert (await c.post("/p/companion/exchange", data=json.dumps({"code": "WRONG"}),
                             headers={"Content-Type": "application/json"})).status == 400
        page = await (await c.get(f"/p/companion/pair?code={code}")).text()
        assert "rce-companion.apk" in page
        r = await c.get("/app/rce-companion.apk")
        assert r.status == 200 and (await r.read())[:2] == b"PK"
        assert (await c.get("/app/other.apk")).status == 404

    async def setup():
        bot.plugins = PluginManager(bot)
        await bot.plugins.load_all()
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(Plugin, "_loops", lambda self: [])
    asyncio.run(setup())
    try:
        run_web(bot, scenario)
    finally:
        mp.undo()


def test_account_overview_and_kit_claim(pb, monkeypatch):  # noqa: F811
    from cogs.custom_kits import ClaimOutcome
    c = plug(pb, "companion")
    raid = plug(pb, "raid_alerts", rf=True)
    pb.db.link_player(7, "Bob")
    raid.claim_rf(7, 4242)
    member = NS(id=7, roles=[])

    async def get_member(uid):
        return member if uid == 7 else None
    monkeypatch.setattr(c, "member", get_member)
    claimed = []

    class Custom:
        def kit_overview(self, m):
            return {"claimable_kits": [
                {"kit": "Starter", "can_claim_now": True, "ready_at": None, "cooldown": "1 day", "last_claimed": "never"},
                {"kit": "VIP", "can_claim_now": False, "ready_at": 2_000_000_000, "cooldown": "12 h",
                 "last_claimed": "2 h ago"}]}

        async def claim_for(self, m, kit, via):
            claimed.append((m.id, kit, via))
            return ClaimOutcome(True, "claimed", f"✅ **{kit}** sent to `Bob`.", kit)
    real_get_cog = pb.bot.get_cog
    monkeypatch.setattr(pb.bot, "get_cog", lambda name: Custom() if name == "CustomKits" else real_get_cog(name))
    acc = run(c.account(7))
    assert acc["player"] == "Bob" and [k["kit"] for k in acc["kits"]] == ["Starter", "VIP"]
    assert acc["kits"][1]["ready"] is False and acc["kits"][1]["ready_at"] == 2_000_000_000
    assert acc["raid"]["rf"] == 4242 and acc["raid"]["dm"] is False
    out = run(c.claim(7, "Starter"))
    assert out["ok"] and out["message"] == "✅ Starter sent to Bob." and claimed == [(7, "Starter", "the Companion app")]
    assert run(c.claim(8, "Starter"))["ok"] is False                 # not on the Discord: nothing claimed
    assert len(claimed) == 1


def test_app_switches_alerts_and_rf(bot):  # noqa: F811
    from plugin_api import Plugin
    from plugin_manager import PluginManager

    async def scenario(c):
        m = bot.plugins
        await m.set_enabled("raid_alerts", True)
        comp, raid = m.running("companion"), m.running("raid_alerts")
        bot.db.link_player(7, "Bob")
        token, _ = comp.exchange(comp.new_code(7))
        auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        r = await c.post("/p/companion/alerts", data=json.dumps({"on": True}), headers=auth)
        assert (await r.json())["ok"] and raid.data.get("sub:7") == 7
        r = await c.post("/p/companion/rf", data=json.dumps({"frequency": "555"}), headers=auth)
        assert (await r.json())["ok"] and raid.rf_of(7) == 555
        r = await c.post("/p/companion/rf", data=json.dumps({"release": True}), headers=auth)
        assert raid.rf_of(7) is None
        acc = await (await c.get("/p/companion/account", headers=auth)).json()
        assert acc["player"] == "Bob" and acc["raid"]["dm"] is True
        assert (await c.get("/p/companion/account")).status == 401
        assert (await c.post("/p/companion/claim", data=json.dumps({"kit": "x"}),
                             headers={"Content-Type": "application/json"})).status == 401

    async def setup():
        bot.plugins = PluginManager(bot)
        await bot.plugins.load_all()
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(Plugin, "_loops", lambda self: [])
    asyncio.run(setup())
    try:
        run_web(bot, scenario)
    finally:
        mp.undo()


def test_companion_api_has_its_own_limits(bot):  # noqa: F811
    from plugin_api import Plugin
    from plugin_manager import PluginManager

    async def scenario(c):
        comp = bot.plugins.running("companion")
        token, _ = comp.exchange(comp.new_code(7))
        auth = {"Authorization": f"Bearer {token}"}
        codes = [(await c.get("/p/companion/feed", headers=auth)).status for _ in range(45)]
        assert codes[:40] == [200] * 40 and codes[-1] == 429            # per phone, not the website's shared limit
        bad = [(await c.post("/p/companion/exchange", data=json.dumps({"code": "WRONG123"}),
                             headers={"Content-Type": "application/json"})).status for _ in range(12)]
        assert bad[0] == 400 and bad[-1] == 429                          # wrong pairing codes are limited

    async def setup():
        bot.plugins = PluginManager(bot)
        await bot.plugins.load_all()
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(Plugin, "_loops", lambda self: [])
    asyncio.run(setup())
    try:
        run_web(bot, scenario)
    finally:
        mp.undo()
