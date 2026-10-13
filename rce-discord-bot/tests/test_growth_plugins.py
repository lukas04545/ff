"""The built-in growth plugins: bounties, achievements, invites, starter kit, low-pop bonus, filling ping,
early bird, comeback DMs, wipe poll, weekly recap."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import discord

from rce.kill_sources import KillerType
from rce.log_parser import KillEvent
from tests.test_plugins import pb, plug  # noqa: F401

P = KillerType.PLAYER


def kill(killer, victim, killer_type=P):
    return KillEvent(victim, P, killer, killer_type)


def run(coro):
    return asyncio.run(coro)


def test_bounties_place_claim_and_refund(pb):
    db = pb.db
    b = plug(pb, "bounties", min_amount=10, days=1)
    for name in ("Target", "Hunter", "Buddy"):
        db.record_kill(None, name)
    db.link_player(1, "Placer")
    db.link_player(2, "Hunter")
    db.link_player(3, "Target")
    db.add_points(1, 500, "test")
    assert b.place(1, "Nobody", 50)[0] is False                         # never seen on the server
    assert b.place(3, "target", 50) == (False, "You can't put a bounty on yourself.")
    assert b.place(1, "Target", 5)[0] is False                          # below the minimum
    assert b.place(1, "Target", 1000)[0] is False                       # not enough points
    assert b.place(1, "target", 100) == (True, "Target") and b.place(1, "Target", 50)[0]
    assert db.wallet(1)["points"] == 350 and b.total_on("TARGET") == 150
    # The killer must be linked
    run(b.on_rce_kill(kill("Stranger", "Target")))
    assert b.total_on("Target") == 150 and "link your account" in pb.said[-1]
    # Teammates can't collect
    db.team_event("create", "Target", 9)
    db.team_event("join", "Hunter", 9)
    assert b.claim("Hunter", "Target")[2] == "teammate"
    db.team_event("leave", "Hunter", 9)
    run(b.on_rce_kill(kill("Hunter", "Target")))
    assert db.wallet(2)["points"] == 150 and b.total_on("Target") == 0 and b.claimed_count(2) == 1
    assert "collected" in pb.posted[-1].embed.description and b.profile_fields(NS(id=2)) == [("🎯 Bounties collected", "1")]
    # Unclaimed bounties are refunded
    b.place(1, "Buddy", 20)
    assert b.expire(b.now() + 2 * 86400)[0]["target"] == "Buddy" and db.wallet(1)["points"] == 350


def test_achievements_quiet_start_then_announced(pb):
    db = pb.db
    db.link_player(5, "Killer")
    for _ in range(12):
        db.record_kill("Killer", "Someone")
    plug(pb, "bounties")
    a = plug(pb, "achievements", points=25)
    run(a.check_loop.coro(a))                                            # first run: quiet
    assert a.unlocked(5) == ["kills10"] and not pb.posted and db.wallet(5)["points"] == 25
    db.add_playtime(["Killer"], 11 * 3600)
    run(a.check_loop.coro(a))
    assert "hours10" in a.unlocked(5) and "Settler" in pb.posted[-1].embed.description
    assert any("unlocked the badge Settler" in s for s in pb.said) and db.wallet(5)["points"] == 50
    embed = a.overview(5)
    assert "🔒 Hunter – 12/100 kills" in embed.description and "Recruiter" not in embed.description   # invites off
    run(a.on_rce_wiped(None))
    pb.bot.online_players = {"Killer": {}}
    run(a.wipe_loop.coro(a))
    assert a.metrics(5)["wipes"] == 1 and a.profile_fields(NS(id=5))[0][0] == "🏅 Badges"


def test_invites_reward_after_playtime(pb):
    db = pb.db
    inv = plug(pb, "invites", hours=1, inviter_points=200, invitee_points=50, min_account_days=7)
    now = inv.now()
    old_account = now - 30 * 86400
    inv.data.set("known", [99])
    assert inv.used_invite({"a": 1, "b": 4}, {"a": 2, "b": 4}) == "a"
    assert inv.used_invite({"a": 1, "once": 0}, {"a": 1}) == "once"          # one-use invite vanished
    assert inv.used_invite({"a": 1, "b": 1}, {"a": 2, "b": 2}) is None       # unclear
    assert inv.record_join(99, 1, old_account)["status"] == "invalid"       # was on the Discord before
    assert inv.record_join(10, 10, old_account)["reason"] == "invited themselves"
    assert inv.record_join(11, 1, now - 3600)["reason"] == "Discord account too new"
    assert inv.record_join(12, 1, old_account)["status"] == "pending"
    assert inv.record_join(12, 2, old_account) is None                     # no second record
    db.link_player(12, "NewGuy")
    pb.bot.online_players = {"NewGuy": {}}
    run(inv.tick(1800))
    assert inv.pending_for(1) == [12] and db.wallet(1)["points"] == 0
    run(inv.tick(1800))
    assert db.wallet(1)["points"] == 200 and db.wallet(12)["points"] == 50 and inv.rewarded_count(1) == 1
    assert inv.data.get("ref:12")["status"] == "rewarded" and inv.pending_for(1) == []
    run(inv.tick(9999))
    assert db.wallet(1)["points"] == 200                                    # paid once


def test_starter_kit_only_for_new_players(pb):
    db = pb.db
    db.create_custom_kit("Starter", None, "admin")
    db.add_custom_kit_item("Starter", "rifle.ak", 1)
    db.record_kill("Veteran", None)
    s = plug(pb, "starter_kit", kit="Starter", delay=5)
    from cogs.custom_kits import CustomKits
    run(pb.bot.add_cog(CustomKits(pb.bot)))
    assert not s.is_new("veteran") and s.is_new("Rookie")
    pb.bot.online_players = {}
    s.data.set("seen:rookie", 0)
    assert run(s.welcome("Rookie")) is False and s.is_new("Rookie")         # left before the kit: still new
    pb.bot.online_players = {"Rookie": {}}
    assert run(s.welcome("rookie")) is True
    assert any("rifle.ak" in c for c in pb.said) and "Welcome to" in pb.said[0] + pb.said[-1]
    assert not s.is_new("Rookie") and s.status()["Starter kits given"] == 1


def test_lowpop_bonus_pays_extra_and_announces(pb):
    db = pb.db
    db.link_player(1, "Seeder")
    lp = plug(pb, "lowpop_bonus", below=3, multiplier=3)
    pb.bot.online_players = {"Seeder": {}, "Other": {}}
    assert run(lp.tick(60)) == 2                         # 60 points/h x (3-1) = 120/h -> 2 per minute
    assert run(lp.tick(3600)) == 120
    assert db.wallet(1)["points"] == 122 and "Low-pop bonus active" in pb.said[-1]
    pb.bot.online_players = {f"P{i}": {} for i in range(3)} | {"Seeder": {}}
    assert run(lp.tick(60)) == 0 and not lp.active


def test_filling_ping_thresholds_and_cooldown(pb):
    fp = plug(pb, "filling_ping", role=55, thresholds="5, 10", cooldown=12)
    assert run(fp.tick(3)) is None                       # first value only sets the baseline
    assert run(fp.tick(11)) == 10                         # jumps over both: the highest one pings
    msg = pb.posted[-1]
    assert msg.content == "<@&55>" and "11" in msg.embed.description
    assert msg.kw["allowed_mentions"].roles[0].id == 55
    run(fp.tick(4))
    assert run(fp.tick(6)) == 5 and run(fp.tick(4)) is None and run(fp.tick(11)) is None   # 10: cooldown
    assert fp.problems() == []


def test_early_bird_first_linked_players_once(pb):
    db = pb.db
    for uid, name in ((1, "A"), (2, "B"), (3, "C")):
        db.link_player(uid, name)
    db.link_player(1, "A")
    eb = plug(pb, "early_bird", players=2, points=40)
    pb.bot.online_players = {"A": {}, "B": {}, "C": {}, "Unlinked": {}}
    assert run(eb.tick()) == []                           # no wipe yet
    run(eb.on_rce_wiped(None))
    assert run(eb.tick()) == ["A", "B"] and run(eb.tick()) == []
    assert db.wallet(1)["points"] == 40 and db.wallet(3)["points"] == 0
    eb.data.set("wiped_at", eb.now() - 2 * 3600)
    assert not eb.window_open()


def test_comeback_dm_recipients_and_once_per_wipe(pb, monkeypatch):
    db = pb.db
    for uid, name in ((1, "Gone"), (2, "Recent"), (3, "Online"), (4, "OptOut")):
        db.link_player(uid, name)
        db.record_kill(None, name)
    db._db.execute("UPDATE players SET last_seen = '2020-01-01 00:00:00' WHERE name != 'Recent'")
    db._db.commit()
    cb = plug(pb, "comeback", hours=24, inactive_days=3)
    plug(pb, "early_bird", players=7)
    cb.data.set("optout:4", True)
    pb.bot.online_players = {"Online": {}}
    assert cb.recipients() == [(1, "Gone")]
    wipe_at = cb.now() + 3600
    monkeypatch.setattr(cb, "next_wipe", lambda: wipe_at)
    assert cb.due() == wipe_at
    sent = []

    async def send(embed=None):
        sent.append(embed)
    monkeypatch.setattr(pb.bot, "get_user", lambda uid: NS(id=uid, display_name="Gone", send=send))
    assert run(cb.send_all(wipe_at, pause=0)) == 1
    assert f"<t:{wipe_at}:R>" in sent[0].description and "first 7 linked players" in sent[0].description
    cb.data.set("sent_for", wipe_at)
    assert cb.due() is None


def test_wipe_poll(pb):
    from plugins.wipe_poll import parse_questions
    assert parse_questions("Q? | a | b\nonly one | x\n\nQ2 | 1 | 2 | 3") == [("Q?", ["a", "b"]), ("Q2", ["1", "2", "3"])]
    wp = plug(pb, "wipe_poll", delay=0)
    run(wp.on_rce_wiped(None))
    run(wp.poll_loop.coro(wp))
    polls = [m.kw["poll"] for m in pb.posted]
    assert len(polls) == 3 and isinstance(polls[0], discord.Poll) and polls[0].question == "How was this wipe?"
    assert len(polls[0].answers) == 4 and wp.data.get("due") is None


def test_weekly_recap_counts_and_posts(pb):
    from plugins.weekly_recap import last_due
    db = pb.db
    db.link_player(1, "Ace")
    wr = plug(pb, "weekly_recap", reward=30, min_kills=1)
    for _ in range(3):
        run(wr.on_rce_kill(kill("Ace", "Bob")))
    run(wr.on_rce_kill(kill("Wolf", "Bob", KillerType.NPC)))
    run(wr.on_rce_kill(kill("Fall", "Bob", KillerType.NATURAL)))
    run(wr.on_rce_kill(kill("Fall", "Ace", KillerType.NATURAL)))
    wr.add_playtime(["Ace", "Bob"], 3600)
    wr.add_playtime(["Ace"], 600)
    embed = wr.build()
    fields = {f.name: f.value for f in embed.fields}
    assert "**Ace** – 3 kills" in fields["⚔️ Top killers"] and "**Fall** – 2 victims" in fields["🤡 Most embarrassing killer"]
    assert "**Bob** – 5 deaths" in fields["💀 Died the most"] and "**2** players" in fields["📊 This week"]
    assert run(wr.post_recap()) is True and db.wallet(1)["points"] == 30          # top killer = top player: once
    assert wr._week()["kills"] == {} and run(wr.post_recap()) is False             # empty week: no post
    utc = timezone.utc
    sunday_18 = int(datetime(2026, 10, 11, 18, tzinfo=utc).timestamp())
    assert last_due(sunday_18 + 60, 6, 18, utc) == sunday_18
    assert last_due(sunday_18 - 60, 6, 18, utc) == sunday_18 - 7 * 86400
    wr.data.delete("last_slot")
    assert wr.due() is None and wr.due() is None                                   # first start: wait for the next slot
    wr.data.set("last_slot", 0)
    assert wr.due() is not None
