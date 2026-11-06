"""Feeds: chat bridge, killfeed, join/leave & server events, admin log, console log."""
from __future__ import annotations

import asyncio

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from rce import log_parser as lp
from rce.kill_sources import KillerType
from rce.rcon_client import RconError, RconMessage
from utils import safe_md, sanitize_ingame_text

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.feeds")

# Commands the bot sends itself on a schedule – don't spam them into the log
POLLING_COMMANDS = ("playerlist", "serverinfo")
CHAT_COOLDOWN_SECONDS = 3.0
CHANNEL_LABELS = {"LOCAL": "Local", "TEAM": "Team", "SERVER": "Server", "GLOBAL": "Global"}


class Feeds(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._chat_cooldowns: dict[int, float] = {}

    # Looked up on every use, so channels changed in the settings apply right away
    @property
    def chat(self):
        return self.bot.feed(self.bot.config.chat_channel_id)

    @property
    def killfeed(self):
        return self.bot.feed(self.bot.config.killfeed_channel_id)

    @property
    def events(self):
        return self.bot.feed(self.bot.config.events_channel_id)

    @property
    def admin_log(self):
        return self.bot.feed(self.bot.config.admin_log_channel_id)

    @property
    def console(self):
        return self.bot.feed(self.bot.config.console_log_channel_id, code_block=True)

    # ------------------------------------------------------------ Chat bridge

    @commands.Cog.listener()
    async def on_rce_chat(self, ev: lp.ChatEvent) -> None:
        if not self.chat:
            return
        if ev.channel == "SERVER" and not self.bot.config.chat_show_server_messages:
            return  # e.g. our own /say and bridge messages -> no echo
        label = CHANNEL_LABELS.get(ev.channel, ev.channel)
        icon = "👥" if ev.channel == "TEAM" else "💬"
        self.chat.add_line(f"{icon} `[{label}]` **{safe_md(ev.player)}**: {safe_md(ev.text)}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        cfg = self.bot.config
        if (
            not cfg.chat_bridge_to_game
            or message.channel.id != cfg.chat_channel_id
            or message.author.bot
            or not message.content
        ):
            return

        now = time.monotonic()
        if now - self._chat_cooldowns.get(message.author.id, 0) < CHAT_COOLDOWN_SECONDS:
            await message.add_reaction("⏳")
            return
        self._chat_cooldowns[message.author.id] = now

        text = sanitize_ingame_text(message.clean_content)
        name = sanitize_ingame_text(message.author.display_name, limit=32) or "Discord"
        if not text:
            return
        try:
            await self.bot.rcon.command(f"say {cfg.say_prefix} {name}: {text}", expect_response=False)
            await message.add_reaction("✅")
        except RconError:
            await message.add_reaction("⚠️")

    # ------------------------------------------------------------ Killfeed

    @commands.Cog.listener()
    async def on_rce_kill(self, ev: lp.KillEvent) -> None:
        victim_is_player = ev.victim_type is KillerType.PLAYER
        if ev.is_pvp:
            self.bot.db.record_kill(ev.killer, ev.victim)
            line = f"⚔️ **{safe_md(ev.killer)}** killed **{safe_md(ev.victim)}**"
        elif victim_is_player:
            self.bot.db.record_kill(None, ev.victim)
            icon = {"natural": "☠️", "npc": "🐺", "entity": "🪤"}.get(ev.killer_type.value, "💀")
            line = f"{icon} **{safe_md(ev.victim)}** was killed by {safe_md(ev.killer)}"
        else:
            # Player kills an NPC/scientist etc. – not counted in PvP stats
            line = f"🎯 **{safe_md(ev.killer)}** killed {safe_md(ev.victim)}"
        if self.killfeed:
            self.killfeed.add_line(line)

    @commands.Cog.listener()
    async def on_rce_suicide(self, ev: lp.SuicideEvent) -> None:
        self.bot.db.record_suicide(ev.player)
        if self.killfeed:
            self.killfeed.add_line(f"🪦 **{safe_md(ev.player)}** committed suicide")

    # ------------------------------------------------------------ Events / Joins

    @commands.Cog.listener()
    async def on_rce_player_join(self, name: str) -> None:
        if self.events:
            self.events.add_line(f"📥 **{safe_md(name)}** joined the server")

    @commands.Cog.listener()
    async def on_rce_player_leave(self, name: str) -> None:
        if self.events:
            self.events.add_line(f"📤 **{safe_md(name)}** left the server")

    @commands.Cog.listener()
    async def on_rce_respawn(self, ev: lp.RespawnEvent) -> None:
        self.bot.db.set_platform(ev.player, ev.platform)
        if self.events and self.bot.config.show_respawns:
            self.events.add_line(f"🔄 **{safe_md(ev.player)}** spawned ({ev.platform})")

    @commands.Cog.listener()
    async def on_rce_server_event(self, ev: lp.ServerEvent) -> None:
        if self.events:
            self.events.add_line(f"**{ev.name}** has started!")

    @commands.Cog.listener()
    async def on_rce_team(self, ev: lp.TeamEvent) -> None:
        if not self.events:
            return
        p, other = safe_md(ev.player), safe_md(ev.other or "?")
        text = {
            "create": f"🤝 **{p}** created a team (ID {ev.team_id})",
            "join": f"🤝 **{p}** joined **{other}**'s team",
            "leave": f"👋 **{p}** left **{other}**'s team",
            "promote": f"👑 **{p}** made **{other}** team leader",
        }.get(ev.kind)
        if text:
            self.events.add_line(text)

    # ------------------------------------------------------------ Admin log

    @commands.Cog.listener()
    async def on_rce_ban(self, ev: lp.BanEvent) -> None:
        if self.admin_log:
            verb = "🔨 banned" if ev.banned else "🕊️ unbanned"
            self.admin_log.add_line(f"**{safe_md(ev.player)}** was {verb} (by {safe_md(ev.admin)})")

    @commands.Cog.listener()
    async def on_rce_role(self, ev: lp.RoleEvent) -> None:
        if self.admin_log:
            action = "added to group" if ev.added else "removed from group"
            self.admin_log.add_line(
                f"🛡️ **{safe_md(ev.player)}** {action}: `{ev.role}` (by {safe_md(ev.admin)})")

    @commands.Cog.listener()
    async def on_rce_admin_action(self, ev: lp.AdminActionEvent) -> None:
        if not self.admin_log:
            return
        if ev.kind == "command":
            if ev.text.split(" ")[0].lower() in POLLING_COMMANDS:
                return
            self.admin_log.add_line(f"⌨️ Console command: `{ev.text}`")
        else:
            self.admin_log.add_line(f"🎁 {safe_md(ev.text)}")

    # A drop that's back within HICCUP_SECONDS is one 🟡 line (with the reason), not a 🔴/🟢 pair; several of
    # them in a row are counted on that line instead of one line each.
    HICCUP_SECONDS = 60

    @commands.Cog.listener()
    async def on_rce_connection(self, connected: bool) -> None:
        import time as _time
        if not self.admin_log:
            return
        rcon = self.bot.rcon
        if not connected:
            self._lost_at = _time.monotonic()
            self._lost_reason = getattr(rcon, "last_drop", "") or ""
            pending = getattr(self, "_lost_task", None)
            if pending is not None and not pending.done():
                pending.cancel()

            async def announce_if_still_down():
                await asyncio.sleep(self.HICCUP_SECONDS)
                if not rcon.connected:
                    self._announced_down = True
                    self.admin_log.add_line("🔴 RCON connection lost – reconnecting automatically"
                                            + (f" ({self._lost_reason})" if self._lost_reason else ""))
            self._lost_task = asyncio.create_task(announce_if_still_down())
            return
        pending = getattr(self, "_lost_task", None)
        if pending is not None and not pending.done():
            pending.cancel()
        if getattr(self, "_announced_down", False):
            self._announced_down = False
            self.admin_log.add_line("🟢 RCON connection established")
            return
        if getattr(self, "_lost_at", None) is None:
            self.admin_log.add_line("🟢 RCON connection established")      # the first connection after a start
            self._lost_at = 0.0
            return
        seconds = max(1, round(_time.monotonic() - self._lost_at))
        now = _time.monotonic()
        streak = getattr(self, "_hiccups", (0.0, 0))
        count = streak[1] + 1 if now - streak[0] < 1800 else 1
        self._hiccups = (now, count)
        self.admin_log.add_line(f"🟡 RCON hiccup – back after {seconds}s"
                                + (f" ({self._lost_reason})" if self._lost_reason else "")
                                + (f" · {count}× in the last 30 min" if count > 1 else ""))

    # ------------------------------------------------------------ Console log

    @commands.Cog.listener()
    async def on_rce_console(self, msg: RconMessage) -> None:
        if not self.console:
            return
        for line in msg.text.splitlines():
            line = line.strip()
            if not line:
                continue
            if any(f"command '{cmd}" in line for cmd in POLLING_COMMANDS):
                continue
            self.console.add_line(line)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Feeds(bot))
