"""Wipe manager, scheduled messages, reports + watchlist, teams and the population graph."""
import asyncio
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from cogs.wipe import next_occurrence, parse_when
from storage import Storage

H = {"X-Requested-With": "rce-web"}
BERLIN = ZoneInfo("Europe/Berlin")


def test_wipe_dates():
    ts = parse_when("2026-10-15 18:00", BERLIN)
    assert datetime.fromtimestamp(ts, BERLIN).strftime("%Y-%m-%d %H:%M") == "2026-10-15 18:00"
    assert parse_when("15.10.2026 18:00", BERLIN) == ts and parse_when("next thursday", BERLIN) is None
    # Weekly across the end of daylight saving time (25 Oct 2026): still 18:00 local time
    nxt = next_occurrence(parse_when("2026-10-22 18:00", BERLIN), "weekly", BERLIN, now=0)
    assert datetime.fromtimestamp(nxt, BERLIN).strftime("%Y-%m-%d %H:%M") == "2026-10-29 18:00"
    # Monthly = first same weekday of the next month (Thursday 1 Oct 2026 -> Thursday 5 Nov 2026)
    nxt = next_occurrence(parse_when("2026-10-01 20:00", BERLIN), "monthly", BERLIN, now=0)
    assert datetime.fromtimestamp(nxt, BERLIN).strftime("%a %Y-%m-%d %H:%M") == "Thu 2026-11-05 20:00"
    # Missed several weeks (bot was off): jumps to the first date in the future
    old = int(time.time()) - 30 * 86400
    assert time.time() < next_occurrence(old, "weekly", BERLIN) <= time.time() + 7 * 86400
    assert next_occurrence(old, "none", BERLIN) is None


@pytest.fixture
def bot(tmp_path):
    cfg = SimpleNamespace(wipe_timezone="Europe/Berlin", wipe_channel_id=5, events_channel_id=None, wipe_ping_role_id=9,
                          wipe_reset_kits=True, wipe_reset_stats=True, wipe_reset_points=False, backup_enabled=False,
                          rust_server_wipe="", report_channel_id=6, alert_channel_id=None, admin_log_channel_id=None,
                          support_role_ids=frozenset({42}), admin_user_ids=frozenset(), admin_role_ids=frozenset())
    sent, said, audit = [], [], []

    class Channel:
        id = 5

        async def send(self, content=None, embed=None, **kw):
            sent.append((content, embed.title if embed else None, embed))
            return SimpleNamespace(id=777, channel=SimpleNamespace(id=6))

    async def command(cmd, **kw):
        said.append(cmd)
        return ""
    import discord
    b = SimpleNamespace(config=cfg, db=Storage(tmp_path / "bot.db"), audit=audit.append, online_players={},
                        rcon=SimpleNamespace(command=command, connected=True), get_channel=lambda cid: Channel(),
                        get_cog=lambda name: None, add_view=lambda v: None, get_user=lambda uid: None)
    b.sent, b.said, b.audit_lines = sent, said, audit
    old = discord.abc.Messageable
    discord.abc.Messageable = Channel
    yield b
    discord.abc.Messageable = old
    b.db.close()


def test_wipe_countdown_and_resets(bot):
    from cogs.wipe import Wipe
    w = Wipe(bot)
    now = int(time.time())
    w.set_schedule(now + 30 * 60, "weekly")                      # set 30 min before the wipe
    asyncio.run(w.tick(now))
    assert [s[1] for s in bot.sent] == ["🧹 Wipe coming up"] and bot.sent[0][0] == "<@&9>"   # only the 1 h post
    assert bot.said and "Server wipe in 30 minutes" in bot.said[0]
    asyncio.run(w.tick(now + 60))
    assert len(bot.sent) == 1                                     # no repeat
    asyncio.run(w.tick(now + 25 * 60))                            # 10 min stage: in-game only
    assert len(bot.sent) == 1 and len(bot.said) == 2

    bot.db.record_kill("A", "B")
    bot.db.add_kit_rule("starter", "respawn", None, 0, 1, "admin")
    bot.db.record_kit_claim(1, "starter", "A", "autokit")
    asyncio.run(w.tick(now + 31 * 60))                            # wipe time
    assert bot.sent[-1][1] == "🧹 Server wiped!" and bot.db.player("A")["kills"] == 0
    assert bot.db.kit_claim_state(1, "A")[0] == 0
    nxt = w.schedule()["next"]
    assert nxt and abs(nxt - (now + 30 * 60 + 7 * 86400)) < 3700  # next week, same local time
    assert "Next wipe" in [f.name for f in bot.sent[-1][2].fields]


def test_scheduled_messages(bot):
    from cogs.schedule import Schedule
    s = Schedule(bot)
    with pytest.raises(ValueError):
        s.add("too often", 1, "game", None, "admin")
    with pytest.raises(ValueError):
        s.add("needs a channel", 30, "both", None, "admin")
    msg_id = s.add("Join our Discord!\nNow", 30, "both", 5, "admin")
    now = int(time.time())
    asyncio.run(s.tick(now + 60))
    assert bot.said == []                                         # first time after one interval
    asyncio.run(s.tick(now + 31 * 60))
    assert bot.said == ["say Join our Discord! Now"] and bot.sent[-1][0] == "Join our Discord! Now"
    bot.db.set_scheduled_enabled(msg_id, False)
    asyncio.run(s.tick(now + 120 * 60))
    assert len(bot.said) == 1


def test_reports_and_watchlist(bot):
    from cogs.reports import Reports
    r = Reports(bot)
    member = SimpleNamespace(id=7, roles=[])
    ok, text = asyncio.run(r.create_report(member, "Cheater123", "flying over my base at outpost"))
    assert ok and "report #1" in text and bot.sent[-1][0] == "<@&42>"
    assert bot.db.report(1)["message_id"] == 777
    assert not asyncio.run(r.create_report(member, "Cheater123", "again!"))[0]   # cooldown
    assert r.is_staff(SimpleNamespace(id=1, roles=[SimpleNamespace(id=42)])) and not r.is_staff(member)

    bot.db.watch("BanEvader", "alt of banned Cheater123", "admin")
    asyncio.run(r.on_rce_player_join("banevader"))                # case doesn't matter
    assert bot.sent[-1][1] == "👀 Watched player joined"
    n = len(bot.sent)
    asyncio.run(r.on_rce_player_join("BanEvader"))                # re-join right away: no second ping
    asyncio.run(r.on_rce_player_join("SomeoneElse"))
    assert len(bot.sent) == n


def test_teams_and_population(tmp_path):
    db = Storage(tmp_path / "bot.db")
    for killer in ("Ann", "Ann", "Bob", "Cid"):
        db.record_kill(killer, "Zed")
    db.team_event("create", "Ann", 11)
    db.team_event("join", "Bob", 11, "Ann")
    db.team_event("create", "Cid", 12)                            # solo team -> not ranked
    rows = db.team_leaderboard("kills")
    assert [(r["leader"], r["members"], r["kills"]) for r in rows] == [("Ann", 2, 3)]
    db.team_event("promote", "Ann", 11, "Bob")
    db.team_event("leave", "Ann", 11, "Bob")
    team, members = db.team_of("Bob")
    assert team["leader"] == "Bob" and members == ["Bob"] and db.team_of("Ann") is None

    now = int(time.time())
    db.add_population_sample(now - 50 * 86400, 99, 50, 0)         # too old -> deleted by the next sample
    for i in range(12):
        db.add_population_sample(now - 3600 + i * 300, i, 50, 0)
    assert len(db.population(now - 86400)) == 12
    hourly = db.population(now - 86400, 3600)
    assert sum(p[1] for p in hourly) / len(hourly) <= 11 and all(p[2] == 50 for p in hourly)
    assert db.population(0)[0][1] != 99
    db.close()


def test_web_endpoints(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    for k, v in dict(DISCORD_TOKEN="x", RCON_HOST="127.0.0.1", RCON_PORT="1", RCON_PASSWORD="pw",
                     ADMIN_ROLE_IDS="1", WEB_PASSWORD="secret123", DATABASE_PATH=str(tmp_path / "bot.db"),
                     SUPPORT_KNOWLEDGE_FILE=str(tmp_path / "k.md")).items():
        monkeypatch.setenv(k, v)
    from bot import RceBot
    from cogs.reports import Reports
    from cogs.schedule import Schedule
    from cogs.wipe import Wipe
    from config import load_config
    from web.server import WebInterface

    async def no_loop(self):
        return None
    for cls in (Wipe, Schedule):
        monkeypatch.setattr(cls, "cog_load", no_loop)

    async def scenario():
        bot = RceBot(load_config("/nonexistent"))
        for cls in (Wipe, Schedule, Reports):
            await bot.add_cog(cls(bot))
        bot.db.add_report(7, "Cheater123", "aimbot")
        bot.db.add_population_sample(int(time.time()) - 600, 12, 50, 0)
        web = WebInterface(bot, env_path=tmp_path / ".env", secret_path=tmp_path / "k")
        c = TestClient(TestServer(web.app))
        await c.start_server()
        try:
            await c.post("/api/login", json={"password": "secret123"}, headers=H)
            r = await c.put("/api/wipe", json={"when": "2099-01-05T18:00", "repeat": "weekly"}, headers=H)
            assert r.status == 200
            w = await (await c.get("/api/wipe")).json()
            assert w["local"] == "2099-01-05T18:00" and w["repeat"] == "weekly" and w["repeat_text"] == "every week"
            assert (await c.put("/api/wipe", json={"when": "2000-01-01T10:00"}, headers=H)).status == 400
            bad = await c.post("/api/schedules", json={"text": "hi", "minutes": 30, "target": "discord"}, headers=H)
            assert bad.status == 400
            ok = await c.post("/api/schedules", json={"text": "Vote for us!", "minutes": 60, "target": "game"}, headers=H)
            assert ok.status == 200
            rows = (await (await c.get("/api/schedules")).json())["rows"]
            assert rows[0]["text"] == "Vote for us!" and rows[0]["channel_id"] is None
            reports = await (await c.get("/api/reports?status=open")).json()
            assert reports["reports"][0]["player"] == "Cheater123"
            assert (await c.post("/api/reports/1/handled", headers=H)).status == 200
            assert (await (await c.get("/api/reports?status=open")).json())["reports"] == []
            await c.post("/api/watchlist", json={"player": "BanEvader", "reason": "alt"}, headers=H)
            assert (await (await c.get("/api/reports")).json())["watchlist"][0]["player"] == "BanEvader"
            assert (await c.delete("/api/watchlist/BanEvader", headers=H)).status == 200
            pop = await (await c.get("/api/population?days=1")).json()
            assert pop["points"][0][1] == 12
            teams = await (await c.get("/api/leaderboard?teams=1&category=kills")).json()
            assert teams["rows"] == []
        finally:
            await c.close()
            await bot.rcon.close()
            bot.db.close()
    asyncio.run(scenario())
