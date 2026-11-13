"""Warnings & notes, staff applications, role menus, anti-raid, event sign-ups, trivia, /connect, staff activity."""
import asyncio
import datetime
import time
from types import SimpleNamespace as NS

from tests.test_plugins import pb, plug  # noqa: F401


def run(c):
    return asyncio.run(c)


def test_warnings_kick_and_ban_after_n(pb):  # noqa: F811
    p = plug(pb, "player_notes", kick_after=2, ban_after=3, tell_ingame=True, dm=False)
    pb.bot.online_players = {"Ryvex": {}}
    run(p.warn_player("ryvex", "spawn killing", "Mod"))
    assert any("was warned" in c for c in pb.said)
    _, count, action = run(p.warn_player("Ryvex", "again", "Mod"))
    assert count == 2 and action.startswith("kicked")
    _, count, action = run(p.warn_player("Ryvex", "third", "Mod"))
    assert count == 3 and action.startswith("banned")
    assert any(c.startswith(pb.bot.config.ban_template.split("{")[0]) for c in pb.said)
    p.add("n", "Ryvex", "alt of Bob?", "Mod")
    assert len(p.entries("n", "RYVEX")) == 1 and len(p.entries("w")) == 3
    assert p.remove("w", p.entries("w")[0]["id"]) and p.clear("w", "ryvex") == 2


def test_role_menu_refuses_roles_with_permissions():
    from plugins.role_menus import role_problem
    me = NS(top_role=10)

    class Role(int):
        def __new__(cls, pos, perms=0, managed=False, default=False):
            r = super().__new__(cls, pos)
            r.permissions, r.managed, r.name, r._default = NS(value=perms), managed, f"r{pos}", default
            return r

        def is_default(self):
            return self._default
    guild = NS(me=me)
    assert role_problem(guild, Role(5)) is None
    assert "permissions" in role_problem(guild, Role(5, perms=8))           # e.g. administrator
    assert "above" in role_problem(guild, Role(12))
    assert "managed" in role_problem(guild, Role(3, managed=True))


def test_anti_raid_join_wave_and_young_accounts(pb):  # noqa: F811
    p = plug(pb, "anti_raid", joins=3, seconds=30, raid_action="kick", always_action="alert", min_age_days=7)
    acted = []

    def member(days_old):
        async def kick(reason=None):
            acted.append(("kick", days_old))

        async def timeout(delta, reason=None):
            acted.append(("timeout", days_old))
        return NS(bot=False, created_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_old),
                  kick=kick, timeout=timeout, mention="@x")
    run(p.on_member_join(member(1)))                     # young, no raid: only flagged
    assert acted == []
    for _ in range(3):
        run(p.on_member_join(member(400)))               # old accounts: never touched
    run(p.on_member_join(member(2)))                     # 5th join in 30 s = raid; young → kicked
    assert acted == [("kick", 2)] and p.data.get("raids") == 1


def test_event_signups_teams_winners_and_rewards(pb, monkeypatch):  # noqa: F811
    p = plug(pb, "event_signups", points=100)
    monkeypatch.setattr(type(p), "points_enabled", property(lambda self: True))
    paid = []
    monkeypatch.setattr(p, "give_points", lambda uid, amount, why: paid.append((uid, amount)) or 1)
    eid = p.create_event("Raid night", p.now() + 600, team_size=2)
    u = lambda i, n: NS(id=i, display_name=n)                               # noqa: E731
    assert "with team" in run(p.join(eid, u(1, "A"), "Wolves"))
    assert "Wolves" in run(p.join(eid, u(2, "B"), "wolves"))               # same team, any case
    assert "full" in run(p.join(eid, u(3, "C"), "Wolves"))
    assert "already" in run(p.join(eid, u(1, "A"), "Bears"))
    run(p.join(eid, u(3, "C"), "Bears"))
    assert run(p.leave(eid, 3)) == "You left the event."
    assert "**Wolves** – A, B" in p.embed(eid).fields[0].value
    run(p.set_winners(eid, ["Wolves"]))
    assert sorted(paid) == [(1, 100), (2, 100)] and p.leaderboard() == [("Wolves", 1)]
    late = p.create_event("Later", p.now() - 1)
    assert "closed" in run(p.join(late, u(5, "E")))
    assert run(p.start_due()) == 1 and p.event(late)["closed"]
    assert p.find("raid night") == eid and p.find(f"#{eid}") == eid


def test_trivia_first_right_answer_wins_once(pb, monkeypatch):  # noqa: F811
    from plugins.trivia import TriviaRound
    p = plug(pb, "trivia", points=0, builtin=True)

    async def go():
        game = TriviaRound(p, ("2 + 2?", "4", "3", "5", "6"), 1, 60)
        out = []

        def it(uid):
            async def send_message(text=None, ephemeral=False, **kw):
                out.append((uid, text))

            async def edit_message(embed=None, view=None):
                out.append((uid, "EDIT", embed.fields[0].name))
            return NS(user=NS(id=uid), response=NS(send_message=send_message, edit_message=edit_message),
                      followup=NS(send=lambda *a, **k: asyncio.sleep(0)))
        wrong = next(i for i in range(4) if i != game.right)
        await game.answer(it(1), wrong)
        await game.answer(it(1), game.right)                  # one try each
        await game.answer(it(2), game.right)
        await game.answer(it(3), game.right)
        return game, out
    game, out = run(go())
    assert out[0] == (1, "❌ Wrong!") and out[1] == (1, "You had your try.")
    assert out[2] == (2, "EDIT", "✅ Solved") and "Already solved" in out[3][1]
    assert game.winner == 2 and p.data.get("wins") == {"2": 1}
    assert len(p.questions()) >= 10
    p2 = plug(pb, "trivia", builtin=False, questions="Best gun? | AK | Bow | Rock | Spear\nbroken line")
    assert p2.questions() == [("Best gun?", "AK", "Bow", "Rock", "Spear")]


def test_connect_and_staff_activity(pb):  # noqa: F811
    c = plug(pb, "connect_info", how_to_join="Search Kryvon EU")
    assert "Search Kryvon EU" in c.embed().description
    s = plug(pb, "staff_activity")
    s.record("Mod#1", "commands", "/kick")
    s.record("Mod#1", "commands", "/ban")
    s.record("panel:owner", "panel", "Settings changed")
    rows = dict(s.summary(7))
    assert rows["Mod#1"]["commands"] == 2 and rows["panel:owner"]["panel"] == 1


def test_staff_application_accept_gives_the_role(pb, monkeypatch):  # noqa: F811
    p = plug(pb, "staff_applications", channel=100, role=55, wait_days=7)
    monkeypatch.setattr("plugins.staff_applications.is_admin", lambda bot, user: user.id == 9)
    replies, roles = [], []

    def it(uid, guild=None):
        async def send_message(text=None, ephemeral=False, **kw):
            replies.append(text)
        return NS(user=NS(id=uid, mention=f"<@{uid}>", __str__=lambda s: "u"), guild=guild,
                  response=NS(send_message=send_message))

    async def add_roles(role, reason=None):
        roles.append(role)
    member = NS(id=4, add_roles=add_roles, send=lambda *a, **k: asyncio.sleep(0))
    guild = NS(get_member=lambda uid: member, get_role=lambda rid: NS(id=rid, mention="@Staff"))
    run(p.submit(it(4), [("Why?", "I love Rust")]))
    assert "sent to the staff" in replies[-1] and pb.posted[-1].embed.fields[0].value == "I love Rust"
    assert "open application" in p.blocked(4)
    run(p.decide(it(5, guild), 1, True))
    assert "Only staff" in replies[-1]
    run(p.decide(it(9, guild), 1, True))
    assert roles and "Accepted" in replies[-1] and p.data.get("app:1")["state"] == "accepted"
    run(p.decide(it(9, guild), 1, False))
    assert "already decided" in replies[-1]


def test_auto_nuke_warns_launches_and_reschedules(pb, monkeypatch):  # noqa: F811
    p = plug(pb, "auto_nuke", every_hours=2, warn_minutes=10, points=50, min_players=1, announce_ingame=True)
    monkeypatch.setattr(type(p), "points_enabled", property(lambda self: True))
    dropped = []

    class Eco:
        enabled = True

        async def drop_nuke(self, buyer, title, points, max_claims, channel):
            dropped.append((title, points, max_claims))
    real = pb.bot.get_cog
    monkeypatch.setattr(pb.bot, "get_cog", lambda n: Eco() if n == "Economy" else real(n))
    t0 = p.now()
    at = p.next_at()
    assert abs(at - (t0 + 7200)) < 5 and run(p.tick(t0)) == ""
    assert run(p.tick(at - 300)) == "warned" and "drops in **5 min**" in pb.posted[-1].content
    assert any("NUKE" in c for c in pb.said) and run(p.tick(at - 200)) == ""          # warned once
    pb.bot.online_players = {}
    assert run(p.tick(at + 1)) == "waiting" and dropped == []                         # too few players
    pb.bot.online_players = {"Bob": {}}
    assert run(p.tick(at + 60)) == "launched" and dropped == [("Auto nuke", 50, 0)]
    assert p.next_at() == at + 60 + 7200 and p.data.get("warned") is False
    p.data.set("paused", True)
    assert run(p.tick(p.next_at() + 10)) == "" and len(dropped) == 1                  # paused: nothing
