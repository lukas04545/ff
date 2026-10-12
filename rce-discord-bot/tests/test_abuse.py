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
                          abuse_kill_command='global.killplayer "{name}"', guild_id=1, alert_channel_id=9,
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
    spawn(guard, "giving RogueAdmin 3 x Rocket")
    assert guard.sent == ['removeowner "RogueAdmin"', 'removemoderator "RogueAdmin"', 'global.killplayer "RogueAdmin"']
    content, embed = guard.alerts[0]
    assert content == "<@&42>" and "3 × Rocket" in embed.description
    assert "Removed" in embed.fields[0].value and "<@7>" in embed.fields[1].value
    spawn(guard, "giving RogueAdmin 3 x Rocket")                       # second stack of the same spawn
    assert len(guard.sent) == 3
    spawn(guard, "giving Someone 1 x Timed Explosive Charge")          # C4
    assert guard.sent[-1] == 'global.killplayer "Someone"'


def test_allowed_and_harmless(guard):
    spawn(guard, "giving OwnerGT 10 x Rocket")                          # allowed name (any case)
    spawn(guard, "giving CoOwnerPSN 5 x Timed Explosive Charge")        # linked to the co-owner role
    spawn(guard, "giving RogueAdmin 1 x Rocket Launcher")              # launcher is not a rocket
    spawn(guard, "giving RogueAdmin 1000 x Wood")
    assert guard.sent == [] and guard.alerts == []


def test_bot_gives_dont_count(guard):
    asyncio.run(guard.bot.rcon.command('inventory.giveto "RogueAdmin" "ammo.rocket.basic" 3'))  # e.g. the shop
    guard.sent.clear()
    spawn(guard, "giving RogueAdmin 3 x Rocket")
    assert guard.sent == []


def test_inactive_without_exemptions(guard, caplog):
    guard.cfg.abuse_exempt_role_ids, guard.cfg.abuse_exempt_players = frozenset(), frozenset()
    spawn(guard, "giving RogueAdmin 3 x Rocket")
    assert guard.sent == [] and "not active" in caplog.text
