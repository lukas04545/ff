"""The features from the Helios/KAOS comparison: TP home, bank & pay, market & trader, clans, challenges, auto events &
timed commands, zones & portals, live leaderboards, raid alerts."""
import asyncio
import time
from datetime import datetime
from types import SimpleNamespace as NS

from rce.kill_sources import KillerType
from rce.log_parser import ChatEvent, KillEvent
from tests.test_plugins import pb, plug  # noqa: F401

P = KillerType.PLAYER


def run(coro):
    return asyncio.run(coro)


def at_position(pb, monkeypatch, pos="(10.5, 20.0, -30.25)"):
    sent = pb.said

    async def command(cmd, **kw):
        sent.append(cmd)
        return pos if cmd.startswith("server.printpos") else ""
    monkeypatch.setattr(pb.bot.rcon, "command", command)


def test_tp_home(pb, monkeypatch):  # noqa: F811
    at_position(pb, monkeypatch)
    h = plug(pb, "tp_home", set_emote="We need beds!", go_emote="Let's go!", delay=0, cooldown=10,
             linked_only=False, role=None)
    pb.bot.online_players.update({"Bob": {}})
    set_raw, go_raw = h.emote_raw("We need beds!"), h.emote_raw("Let's go!")
    run(h.on_rce_chat(ChatEvent("GLOBAL", "Bob", go_raw, "")))
    assert any("no home yet" in c for c in pb.said)
    run(h.on_rce_chat(ChatEvent("GLOBAL", "Bob", set_raw, "")))
    assert h.home_of("bob")["x"] == 10.5
    pb.said.clear()
    run(h.on_rce_chat(ChatEvent("GLOBAL", "Bob", go_raw, "")))
    assert 'global.teleportpos (10.5,20.0,-30.25) "Bob"' in pb.said
    pb.said.clear()
    run(h.on_rce_chat(ChatEvent("GLOBAL", "Bob", go_raw, "")))                 # cooldown
    assert not any(c.startswith("global.teleportpos") for c in pb.said) and any("home again in" in c for c in pb.said)
    h2 = plug(pb, "tp_home", set_emote="We need beds!", go_emote="We need beds!", delay=0, cooldown=0,
              linked_only=True, role=None)
    assert any("different emotes" in p for p in h2.problems())
    assert run(h2.set_home("Bob")) is False                                    # not linked


def test_bank_and_pay(pb):  # noqa: F811
    b = plug(pb, "bank", pay_fee=10, pay_min=1, interest=10, interest_cap=50, beg_max=5, beg_cooldown=60,
             beg_chance=100)
    db = pb.db
    db.add_points(1, 1000, "test")
    assert b.pay(1, 1, 10)[0] is False and b.pay(1, 2, 5000)[0] is False
    assert b.pay(1, 2, 100) == (True, "Sent 90 points (fee 10 points).")
    assert db.wallet(1)["points"] == 900 and db.wallet(2)["points"] == 90
    assert b.deposit(1, 800)[0] and b.bank(1) == 800 and db.wallet(1)["points"] == 100
    assert b.withdraw(1, 900)[0] is False and b.withdraw(1, 300)[0] and b.bank(1) == 500
    assert b.pay_interest() == 5 and b.bank(1) == 505                          # 1 %
    b.data.set("bank:9", 100_000)
    b.pay_interest()
    assert b.bank(9) == 100_050                                                # capped at 50
    now = time.time()
    ok, _ = b.beg(3, now=now)
    assert ok and 1 <= db.wallet(3)["points"] <= 5 and b.beg(3, now=now + 100)[0] is False


def test_market_trader_and_stash(pb, monkeypatch):  # noqa: F811
    m = plug(pb, "market", catalogue="wood = 2 / 1\nrifle.ak = 3000 / 1200\nscrap = 10 / 0", fee=10,
             max_listings=2, channel=None)
    db = pb.db
    db.add_points(1, 10_000, "t")
    db.add_points(2, 5_000, "t")
    assert m.buy(1, "nope", 1)[0] is False and m.buy(1, "rifle.ak", 5)[0] is False      # unknown / too expensive
    assert m.buy(1, "rifle.ak", 2)[0] and m.stash_of(1) == {"rifle.ak": 2} and db.wallet(1)["points"] == 4000
    assert m.sell(1, "rifle.ak", 1)[0] and db.wallet(1)["points"] == 5200
    assert m.buy(1, "scrap", 10)[0] and m.sell(1, "scrap", 1)[0] is False                # can't be sold back
    ok, text = m.list_item(1, "rifle.ak", 1, 2500)
    assert ok and m.stash_of(1).get("rifle.ak") is None
    lid = m.listings()[0]["id"]
    assert m.buy_listing(1, lid)[0] is False                                             # own listing
    assert m.buy_listing(2, lid)[0] and m.stash_of(2) == {"rifle.ak": 1}
    assert db.wallet(2)["points"] == 2500 and db.wallet(1)["points"] == 5100 + 2250
    m.list_item(2, "rifle.ak", 1, 100)
    assert m.cancel(1, m.listings()[0]["id"])[0] is False and m.cancel(2, m.listings()[0]["id"])[0]
    assert m.stash_of(2) == {"rifle.ak": 1}
    ok, text = run(m.claim(2, "rifle.ak", 1))
    assert ok is False and "online" in text                                             # not linked/online
    db.link_player(2, "Bob")
    pb.bot.online_players.update({"Bob": {}})
    ok, text = run(m.claim(2, "rifle.ak", 1))
    assert ok and m.stash_of(2) == {} and any("rifle.ak" in c and "Bob" in c for c in pb.said)
    m2 = plug(pb, "market", catalogue="notanitem = 1 / 1", fee=0, max_listings=1, channel=None)
    assert any("Unknown shortnames" in p for p in m2.problems())


def test_clans(pb):  # noqa: F811
    c = plug(pb, "clans", max_members=2, roles=False, cost=0)
    db = pb.db
    assert c.create(1, "Kryvon Raiders", "KRV")[0]
    assert c.create(2, "Other", "KRV")[0] is False and c.create(1, "Again", "AGN")[0] is False
    assert c.invite(2, 3)[0] is False and c.invite(1, 2)[0]
    assert c.accept(3, "KRV")[0] is False and c.accept(2, "krv")[0]
    assert c.invite(1, 3)[0] is False                                                    # full (2)
    db.link_player(1, "Alice")
    db.link_player(2, "Bob")
    for _ in range(3):
        db.record_kill("Alice", "X")
    db.record_kill("Bob", "Y")
    s = c.score(c.clan_of(1)[1])
    assert s["kills"] == 4
    assert c.kick(2, 1)[0] is False and c.kick(1, 2)[0] and c.clan_of(2) is None
    assert c.leave(1)[0] and c.clans() == {}                                            # last member: disbanded


def test_challenges(pb):  # noqa: F811
    ch = plug(pb, "challenges", daily_count=5, weekly_count=5, daily_reward=100, weekly_reward=500, ingame=True,
              channel=None)
    assert {c["type"] for c in ch.active("daily")} == {"kills", "playtime", "streak", "pve", "join"}
    assert ch.active("daily") == ch.active("daily")                                      # same for everyone
    pb.db.link_player(7, "Hunter")
    for victim in ("A", "B", "C", "D", "E"):
        run(ch.on_rce_kill(KillEvent(victim, P, "Hunter", P)))
    kills = next(c for c in ch.active("daily") if c["type"] == "kills")
    assert ch.progress("Hunter", kills["id"]) == 5
    assert pb.db.wallet(7)["points"] >= 100 + 100                                        # kills + streak (3) done
    run(ch.on_rce_kill(KillEvent("Lone", P, "Unlinked", P)))
    run(ch.on_rce_kill(KillEvent("Unlinked", P, "Hunter", P)))                           # streak reset for victim
    assert ch.data.get("streak:unlinked") == 0
    run(ch.on_rce_player_join("Unlinked"))
    assert ch.data.get("pending:unlinked")                                              # reward waits for the link
    pb.db.link_player(8, "Unlinked")
    assert ch.collect_pending(8) >= 100 and ch.collect_pending(8) == 0


def test_auto_events(pb):  # noqa: F811
    from plugins.auto_events import parse
    entries, errors = parse("every 30m | Save | server.save\n20:00 | Heli | heli.call ;; say hi | 🚁 Heli!\n"
                            "bad line\nevery 1m | Spam | x\n25:00 | Bad | x")
    assert [e["name"] for e in entries] == ["Save", "Heli"] and len(errors) == 3
    assert entries[1]["commands"] == ["heli.call", "say hi"]
    a = plug(pb, "auto_events", entries="every 30m | Save | server.save\n20:00 | Heli | heli.call | 🚁 Heli!",
             warn_minutes=5, channel=None)
    t0 = time.time()
    assert run(a.tick(t0)) == []                                                          # first start: no fire
    assert run(a.tick(t0 + 1801)) == ["Save"] and "server.save" in pb.said
    assert run(a.tick(t0 + 1830)) == []                                                   # not twice
    heli = a.entries()[1]
    due = a.next_run(heli, t0)
    pb.said.clear()
    run(a.tick(due - 290))                                                                # the warning
    assert any("Heli in 5 minutes" in c for c in pb.said)
    assert "Heli" in run(a.tick(due + 1)) and "heli.call" in pb.said
    assert datetime.fromtimestamp(due, a.tz).strftime("%H:%M") == "20:00"


def test_zones_and_portals(pb, monkeypatch):  # noqa: F811
    z = plug(pb, "zones", create_command="zones.createcustomzone \"{zone}\" ({x},{y},{z}) 0 Sphere {radius} {pvp} 1 0 "
                                         "{building} 1", delete_command='zones.deletecustomzone "{zone}"',
             portal_emote="Go, go, go!", portal_cooldown=0)
    assert run(z.create_zone("Outpost", (1.0, 2.0, 3.0), 60, pvp=False, building=False, message="Safe")) == []
    assert 'zones.createcustomzone "zone_Outpost" (1.0,2.0,3.0) 0 Sphere 60 0 1 0 0 1' in pb.said
    z.data.set("portals", {"a": {"name": "A", "entry": [10.0, 20.0, -30.0], "radius": 5, "exit": [500, 30, 500]}})
    at_position(pb, monkeypatch, "(12.0, 20.0, -31.0)")
    pb.bot.online_players.update({"Bob": {}})
    assert run(z.use_portal("Bob")) == "A" and 'global.teleportpos (500,30,500) "Bob"' in pb.said
    at_position(pb, monkeypatch, "(100.0, 20.0, 100.0)")
    assert run(z.use_portal("Bob")) is None                                              # not at an entry
    assert z.problems() == []


def test_leaderboards_and_raid_alerts(pb, monkeypatch):  # noqa: F811
    lb = plug(pb, "leaderboard_panels", minutes=5, size=5)
    pb.db.record_kill("Alice", "Bob")
    pb.db.add_points(5, 300, "t")
    assert "Alice" in lb.embed("kills").description and "<@5>" in lb.embed("points").description
    r = plug(pb, "raid_alerts", patterns=r"(?P<player>\w+)'s wall was destroyed", watch="destroyed",
             cooldown=5, channel=None)
    pb.db.link_player(9, "Bob")
    r.data.set("sub:9", 9)
    dms = []

    class User:
        async def send(self, text):
            dms.append(text)
    monkeypatch.setattr(pb.bot, "get_user", lambda uid: User() if uid == 9 else None)
    run(r.on_rce_console(NS(text="Bob's wall was destroyed by C4")))
    assert len(dms) == 1 and "Raid alert" in dms[0] and r.seen
    run(r.on_rce_console(NS(text="Bob's wall was destroyed by C4")))
    assert len(dms) == 1                                                                 # cooldown
    run(r.on_rce_console(NS(text="Alice's wall was destroyed")))
    assert len(dms) == 1                                                                 # not subscribed
    bad = plug(pb, "raid_alerts", patterns="(unclosed", watch="", cooldown=5, channel=None)
    assert any("Pattern" in p for p in bad.problems())
