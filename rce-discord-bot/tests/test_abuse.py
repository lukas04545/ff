"""Admin abuse protection: raid items spawned without permission -> remove admin + global.killplayer."""
import asyncio
import time
from collections import deque
from types import SimpleNamespace

import pytest

from cogs.abuse import AbuseGuard
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
                          abuse_kill_command='global.killplayer "{name}"',
                          abuse_after_kill_commands=('entity.deleteby "{name}"',),
                          abuse_on_respawn_commands=('global.sleep "{name}"',), guild_id=1, alert_channel_id=9,
                          admin_log_channel_id=None, alert_role_ids=frozenset({42}))

    async def command(cmd, **kw):
        rcon.recent_commands.append((time.monotonic(), cmd))
        sent.append(cmd)
        return "Removed" if cmd.startswith("remove") else ""
    rcon = SimpleNamespace(command=command, recent_commands=deque(maxlen=500))

    class Channel:
        async def send(self, content=None, embed=None, **kw):
            alerts.append((content, embed))
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
    yield SimpleNamespace(cog=AbuseGuard(bot), bot=bot, sent=sent, alerts=alerts, audit=audit, cfg=cfg)
    discord.abc.Messageable = old
    db.close()


def spawn(guard, line):
    ev = lp.parse_line(line)
    assert isinstance(ev, lp.AdminActionEvent) and ev.kind == "item"
    asyncio.run(guard.cog.on_rce_admin_action(ev))


def test_rogue_admin_is_punished_once(guard):
    spawn(guard, "[ServerVar] giving RogueAdmin 3 x Rocket")
    assert guard.sent == ['removeowner "RogueAdmin"', 'removemoderator "RogueAdmin"', 'global.god 0 "RogueAdmin"',
                          'global.killplayer "RogueAdmin"', 'entity.deleteby "RogueAdmin"']
    # Respawn of someone else: nothing; respawn of the punished player: global.sleep once
    asyncio.run(guard.cog.on_rce_respawn(lp.parse_line("Innocent [SCARLETT] has entered the game")))
    asyncio.run(guard.cog.on_rce_respawn(lp.parse_line("RogueAdmin [SCARLETT] has entered the game")))
    asyncio.run(guard.cog.on_rce_respawn(lp.parse_line("RogueAdmin [SCARLETT] has entered the game")))
    assert guard.sent[5:] == ['global.sleep "RogueAdmin"'] and "respawned" in guard.audit[-1]
    del guard.sent[5:]
    content, embed = guard.alerts[0]
    assert content == "<@&42>" and "3 × Rocket" in embed.description
    assert "Removed" in embed.fields[0].value and "<@7>" in embed.fields[1].value
    spawn(guard, "[ServerVar] giving RogueAdmin 3 x Rocket")                       # second stack of the same spawn
    assert len(guard.sent) == 5
    spawn(guard, "[ServerVar] giving Someone 1 x Timed Explosive Charge")          # C4
    assert guard.sent[-2:] == ['global.killplayer "Someone"', 'entity.deleteby "Someone"']
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
    assert guard.sent[-2:] == ['global.killplayer "RogueAdmin"', 'entity.deleteby "RogueAdmin"']


def test_allowed_spawn_is_logged(guard):
    spawn(guard, "[ServerVar] giving OwnerGT 3 x Rocket")
    spawn(guard, "[ServerVar] giving OwnerGT 3 x Rocket")
    assert guard.sent == [] and len(guard.audit) == 1 and "allowed (allowed in-game name)" in guard.audit[0]
