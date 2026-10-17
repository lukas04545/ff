"""Admin abuse protection: raid items spawned without permission -> remove admin + global.killplayer."""
import asyncio
import time
from collections import deque
from types import SimpleNamespace

import pytest

from cogs.abuse import AbuseGuard, CaseView
from rce import log_parser as lp
from storage import Storage


@pytest.fixture
def guard(tmp_path):
    sent, alerts, audit = [], [], []
    cfg = SimpleNamespace(abuse_protection=True, abuse_exempt_role_ids=frozenset({500}),
                          abuse_exempt_players=frozenset({"ownergt"}),
                          abuse_items=("rocket", "c4", "timed explosive", "explosive.timed"),
                          abuse_remove_admin_commands=('removeowner "{name}"', 'removemoderator "{name}"'),
                          abuse_before_kill_commands=('global.god 0 "{name}"',),
                          abuse_teleport_command="", abuse_kill_delay=0,
                          abuse_kill_command='global.killplayer "{name}"',
                          abuse_after_kill_commands=(), abuse_approval_commands=('entity.deleteby "{name}"',),
                          abuse_on_respawn_commands=('global.sleep "{name}"',), guild_id=1, alert_channel_id=9,
                          admin_log_channel_id=None, alert_role_ids=frozenset({42}))

    async def command(cmd, **kw):
        rcon.recent_commands.append((time.monotonic(), cmd))
        sent.append(cmd)
        return "Removed" if cmd.startswith("remove") else ""
    rcon = SimpleNamespace(command=command, recent_commands=deque(maxlen=500))

    views = []

    class Channel:
        async def send(self, content=None, embed=None, **kw):
            alerts.append((content, embed))
            views.append(kw.get("view"))
            return SimpleNamespace(id=777, channel=SimpleNamespace(id=9))

        async def fetch_message(self, mid):
            async def edit(embed=None, view="unchanged"):
                alerts.append(("edited", embed))
                views.append(view)
            return SimpleNamespace(id=mid, edit=edit)
    import discord
    old = discord.abc.Messageable
    discord.abc.Messageable = Channel
    coowner = SimpleNamespace(id=8, roles=[SimpleNamespace(id=500)])
    import discord as _d

    async def fetch_member(uid):
        raise _d.NotFound(SimpleNamespace(status=404, reason="nf"), "Unknown Member")
    guild = SimpleNamespace(get_member={8: coowner}.get, get_role=lambda rid: SimpleNamespace(name="Co-Owner"),
                            fetch_member=fetch_member)
    db = Storage(tmp_path / "bot.db")
    db.link_player(8, "CoOwnerPSN")
    db.link_player(7, "RogueAdmin")
    bot = SimpleNamespace(config=cfg, rcon=rcon, db=db, audit=audit.append, get_channel=lambda cid: Channel(),
                          get_guild=lambda gid: guild, guilds=[guild])
    yield SimpleNamespace(cog=AbuseGuard(bot), bot=bot, sent=sent, alerts=alerts, audit=audit, cfg=cfg, views=views)
    discord.abc.Messageable = old
    db.close()


def spawn(guard, line):
    ev = lp.parse_line(line)
    assert isinstance(ev, lp.AdminActionEvent) and ev.kind == "item"
    asyncio.run(guard.cog.on_rce_admin_action(ev))


def test_rogue_admin_is_punished_once(guard):
    spawn(guard, "[ServerVar] giving RogueAdmin 3 x Rocket")
    assert guard.sent == ['removeowner "RogueAdmin"', 'removemoderator "RogueAdmin"', 'global.god 0 "RogueAdmin"',
                          'global.killplayer "RogueAdmin"']                 # deletion waits for staff
    case = guard.bot.db.abuse_cases()[0]
    assert case["status"] == "pending" and case["player"] == "RogueAdmin" and case["message_id"] == 777
    content, embed = guard.alerts[0]
    assert "Waiting for staff" in [f.name for f in embed.fields][-2] and isinstance(guard.views[0], CaseView)
    # Respawn of someone else: nothing; respawn of the punished player: global.sleep once
    asyncio.run(guard.cog.on_rce_respawn(lp.parse_line("Innocent [SCARLETT] has entered the game")))
    asyncio.run(guard.cog.on_rce_respawn(lp.parse_line("RogueAdmin [SCARLETT] has entered the game")))
    asyncio.run(guard.cog.on_rce_respawn(lp.parse_line("RogueAdmin [SCARLETT] has entered the game")))
    assert guard.sent[4:] == ['global.sleep "RogueAdmin"'] and "respawned" in guard.audit[-1]
    del guard.sent[4:]
    # Staff approves -> entity.deleteby runs once; a second click can't run it again
    ok, text = asyncio.run(guard.cog.decide(case["id"], True, "Boss"))
    assert ok and guard.sent[-1] == 'entity.deleteby "RogueAdmin"'
    assert not asyncio.run(guard.cog.decide(case["id"], False, "Other"))[0]
    assert guard.bot.db.abuse_case(case["id"])["status"] == "approved"
    edited = [e for c, e in guard.alerts if c == "edited"][0]
    assert edited.fields[-2].name == "✅ Approved by Boss" and guard.views[-1] is None   # buttons removed
    del guard.sent[4:]
    content, embed = guard.alerts[0]
    assert content == "<@&42>" and "3 × Rocket" in embed.description
    assert "Removed" in embed.fields[0].value and "<@7>" in embed.fields[-1].value
    spawn(guard, "[ServerVar] giving RogueAdmin 3 x Rocket")                       # second stack of the same spawn
    assert len(guard.sent) == 4
    spawn(guard, "[ServerVar] giving Someone 1 x Timed Explosive Charge")          # C4
    assert guard.sent[-1] == 'global.killplayer "Someone"'
    status = guard.cog.status()                                       # web interface status box
    assert status["active"] and status["players"] == ["ownergt"]
    assert status["recent"][0]["player"] == "Someone" and status["recent"][0]["result"].startswith("PUNISHED")
    assert any(e["result"].startswith("on respawn: global.sleep") for e in status["recent"])


def test_allowed_and_harmless(guard):
    spawn(guard, "[ServerVar] giving OwnerGT 10 x Rocket")                          # allowed name (any case)
    spawn(guard, "[ServerVar] giving CoOwnerPSN 5 x Timed Explosive Charge")        # linked to the co-owner role
    spawn(guard, "[ServerVar] giving RogueAdmin 1 x Rocket Launcher")              # launcher is not a rocket
    spawn(guard, "[ServerVar] giving RogueAdmin 1000 x Wood")
    assert guard.sent == [] and guard.alerts == []


def test_bot_gives_dont_count(guard):
    asyncio.run(guard.bot.rcon.command('inventory.giveto "RogueAdmin" "ammo.rocket.basic" 3'))  # e.g. the shop
    guard.sent.clear()
    spawn(guard, "[ServerVar] giving RogueAdmin 3 x Rocket")
    assert guard.sent == []


def test_only_admin_panel_spawns_count(guard):
    """RCON/console gives (the bot, /rcon, other tools) are logged differently and never punished."""
    for line in ("giving RogueAdmin 3 x Rocket", "[ServerVar] SERVER giving RogueAdmin 3 x Rocket"):
        ev = lp.parse_line(line)
        assert ev.kind == "item" and not ev.admin_panel
        asyncio.run(guard.cog.on_rce_admin_action(ev))
    assert guard.sent == []
    # As the server shows it in the console, also with a time in front
    ev = lp.parse_line("21:51:16[ServerVar] giving Articbricks0 3 x Rocket")
    assert ev.admin_panel and ev.player == "Articbricks0" and ev.item == "Rocket" and ev.amount == 3
    asyncio.run(guard.cog.on_rce_admin_action(ev))
    asyncio.run(guard.cog.on_rce_admin_action(lp.parse_line("[ServerVar] giving Jones_Ewan 3 x Rocket")))
    assert [c for c in guard.sent if c.startswith("global.killplayer")] == ['global.killplayer "Articbricks0"',
                                                                   'global.killplayer "Jones_Ewan"']


def test_inactive_without_exemptions(guard):
    guard.cfg.abuse_exempt_role_ids, guard.cfg.abuse_exempt_players = frozenset(), frozenset()
    for _ in range(4):                                                  # one spawn = several lines
        spawn(guard, "[ServerVar] giving RogueAdmin 3 x Rocket")
    assert guard.sent == [] and len(guard.audit) == 1 and "waiting for setup" in guard.audit[0]
    # Set up later (settings apply without restart) -> the next spawn is punished right away
    guard.cfg.abuse_exempt_players = frozenset({"ownergt"})
    guard.cog._noted.clear()
    spawn(guard, "[ServerVar] giving RogueAdmin 3 x Rocket")
    assert guard.sent[-1] == 'global.killplayer "RogueAdmin"'


def test_allowed_spawn_is_logged(guard):
    spawn(guard, "[ServerVar] giving OwnerGT 3 x Rocket")
    spawn(guard, "[ServerVar] giving OwnerGT 3 x Rocket")
    assert guard.sent == [] and len(guard.audit) == 1 and "allowed (allowed in-game name)" in guard.audit[0]


def test_commands_go_out_at_once_in_order(guard):
    """The server often doesn't answer: the kill must not wait for the replies to the commands before it."""
    async def slow(cmd, **kw):
        guard.sent.append(cmd)
        await asyncio.sleep(1)          # no reply for a while
        return ""
    guard.bot.rcon.command = slow
    guard.cfg.abuse_on_respawn_commands = ()
    import time as t
    start = t.monotonic()
    spawn(guard, "[ServerVar] giving RogueAdmin 3 x Rocket")
    assert t.monotonic() - start < 2.5                                 # not 4 × 1 s one after another
    assert guard.sent == ['removeowner "RogueAdmin"', 'removemoderator "RogueAdmin"', 'global.god 0 "RogueAdmin"',
                          'global.killplayer "RogueAdmin"']


def test_teleport_then_delay_then_kill(guard, monkeypatch):
    """With a teleport command + kill delay: remove admin, god off and teleport run first, then (after the wait)
    the kill, then after-kill commands."""
    guard.cfg.abuse_teleport_command = 'global.teleportpos (999,999,999) "{name}"'
    guard.cfg.abuse_kill_delay = 10
    guard.cfg.abuse_after_kill_commands = ()
    guard.cfg.abuse_on_respawn_commands = ()
    waits = []

    async def fake_sleep(sec):
        waits.append(sec)
    monkeypatch.setattr("cogs.abuse.asyncio.sleep", fake_sleep)

    spawn(guard, "[ServerVar] giving RogueAdmin 3 x Rocket")
    # Teleport is sent before the kill; the kill comes after a 10 s wait
    assert guard.sent == ['removeowner "RogueAdmin"', 'removemoderator "RogueAdmin"', 'global.god 0 "RogueAdmin"',
                          'global.teleportpos (999,999,999) "RogueAdmin"', 'global.killplayer "RogueAdmin"']
    tp, kill = guard.sent.index('global.teleportpos (999,999,999) "RogueAdmin"'), \
        guard.sent.index('global.killplayer "RogueAdmin"')
    assert tp < kill and 10 in waits                                     # teleport first, then the 10 s wait
    content, embed = guard.alerts[0]
    assert "teleportpos" in embed.fields[0].value                        # the teleport is in the "done" results
    assert any("teleported away" in a and "after 10s" in a for a in guard.audit)   # summary in the admin log


def test_commands_are_retried_when_rcon_was_down(guard, monkeypatch):
    """A command that fails because RCON is reconnecting is sent again once it is back – the abuser must not escape
    just because the connection blinked."""
    from rce.rcon_client import RconNotConnected
    fail = {'global.killplayer "RogueAdmin"': 2, 'removeowner "RogueAdmin"': 1}      # failures left per command
    real = guard.bot.rcon.command
    waits = []

    async def flaky(cmd, **kw):
        if fail.get(cmd, 0) > 0:
            fail[cmd] -= 1
            raise RconNotConnected("No RCON connection to the Rust server (connecting...).")
        return await real(cmd, **kw)

    async def wait_until_connected(timeout=None):
        waits.append(timeout)
        return True

    async def fast_sleep(seconds):
        return None
    guard.bot.rcon.command = flaky
    guard.bot.rcon.wait_until_connected = wait_until_connected
    guard.cfg.abuse_on_respawn_commands = ()
    monkeypatch.setattr("cogs.abuse.asyncio.sleep", fast_sleep)
    spawn(guard, "[ServerVar] giving RogueAdmin 3 x Rocket")
    assert sorted(guard.sent) == sorted(['removeowner "RogueAdmin"', 'removemoderator "RogueAdmin"',
                                         'global.god 0 "RogueAdmin"', 'global.killplayer "RogueAdmin"'])
    assert waits                                                              # it waited for the connection
    case = guard.bot.db.abuse_cases()[0]
    assert "retry" in case["done"] and "failed:" not in case["done"]          # all went through in the end
    assert not any("FAILED" in a for a in guard.audit)


def test_alert_says_when_commands_never_went_through(guard, monkeypatch):
    from rce.rcon_client import RconNotConnected

    async def dead(cmd, **kw):
        raise RconNotConnected("No RCON connection to the Rust server (connecting...).")

    async def wait_until_connected(timeout=None):
        return False

    async def fast_sleep(seconds):
        return None
    guard.bot.rcon.command = dead
    guard.bot.rcon.wait_until_connected = wait_until_connected
    guard.cfg.abuse_on_respawn_commands = ()
    monkeypatch.setattr("cogs.abuse.asyncio.sleep", fast_sleep)
    spawn(guard, "[ServerVar] giving RogueAdmin 3 x Rocket")
    assert any("COMMAND(S) FAILED" in a and "run them by hand" in a for a in guard.audit)


def test_unrelated_bot_gives_dont_hide_a_spawn(guard):
    """Only a bot give that contains a raid item counts; a shop kit with wood must not silence the protection."""
    asyncio.run(guard.bot.rcon.command('inventory.giveto "RogueAdmin" "wood" 1000'))
    asyncio.run(guard.bot.rcon.command('inventory.giveto "RogueAdmin" "metal.fragments" 500'))
    guard.sent.clear()
    spawn(guard, "[ServerVar] giving RogueAdmin 3 x Rocket")
    assert 'global.killplayer "RogueAdmin"' in guard.sent
    # ...but a give of rockets by the bot (a kit) is still recognised as the bot's
    guard.sent.clear()
    guard.cog._punished.clear()
    asyncio.run(guard.bot.rcon.command('inventory.giveto "RogueAdmin" "explosive.timed" 2'))
    guard.sent.clear()
    spawn(guard, "[ServerVar] giving RogueAdmin 2 x Timed Explosive Charge")
    assert guard.sent == []


def test_connection_gap_is_reported(guard):
    asyncio.run(guard.cog.on_rce_connection(False))
    guard.cog._lost_at = time.monotonic() - 12                 # RCON was down for 12 s
    asyncio.run(guard.cog.on_rce_connection(True))
    assert any("blind" in a and "12 s" in a for a in guard.audit)
    assert guard.cog.status()["recent"][0]["what"] == "connection"
    guard.audit.clear()
    asyncio.run(guard.cog.on_rce_connection(False))            # a blink of less than 3 s isn't worth a message
    guard.cog._lost_at = time.monotonic() - 1
    asyncio.run(guard.cog.on_rce_connection(True))
    assert guard.audit == []


def test_punished_again_when_they_do_it_again(guard):
    """Admin given back, rockets spawned again right away -> punished again (the bot's own removeowner/killplayer/
    sleep commands for this player don't count as "the bot gave items")."""
    spawn(guard, "[ServerVar] giving RogueAdmin 3 x Rocket")
    asyncio.run(guard.cog.on_rce_respawn(lp.parse_line("RogueAdmin [SCARLETT] has entered the game")))  # sleep
    asyncio.run(guard.bot.rcon.command('ownerid "RogueAdmin"'))       # staff gives admin back via /rcon
    guard.sent.clear()
    guard.cog._punished.clear()                                         # > 10 s later (one spawn = one punishment)
    spawn(guard, "[ServerVar] giving RogueAdmin 3 x Rocket")
    assert 'global.killplayer "RogueAdmin"' in guard.sent
    assert len(guard.bot.db.abuse_cases()) == 2                         # second case for the deletion
