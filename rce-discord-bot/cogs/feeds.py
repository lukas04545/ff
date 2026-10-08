"""Feeds: Chat-Bridge, Killfeed, Join/Leave & Server-Events, Admin-Log, Konsolen-Log."""
from __future__ import annotations

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

# Befehle, die der Bot selbst regelmäßig sendet – nicht ins Log spammen
POLLING_COMMANDS = ("playerlist", "serverinfo")
CHAT_COOLDOWN_SECONDS = 3.0
CHANNEL_LABELS = {"LOCAL": "Lokal", "TEAM": "Team", "SERVER": "Server", "GLOBAL": "Global"}


class Feeds(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        cfg = bot.config
        self.chat = bot.feed(cfg.chat_channel_id)
        self.killfeed = bot.feed(cfg.killfeed_channel_id)
        self.events = bot.feed(cfg.events_channel_id)
        self.admin_log = bot.feed(cfg.admin_log_channel_id)
        self.console = bot.feed(cfg.console_log_channel_id, code_block=True)
        self._chat_cooldowns: dict[int, float] = {}

    # ------------------------------------------------------------ Chat-Bridge

    @commands.Cog.listener()
    async def on_rce_chat(self, ev: lp.ChatEvent) -> None:
        if not self.chat:
            return
        if ev.channel == "SERVER" and not self.bot.config.chat_show_server_messages:
            return  # u. a. die eigenen /say- und Bridge-Nachrichten -> kein Echo
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
            line = f"⚔️ **{safe_md(ev.killer)}** hat **{safe_md(ev.victim)}** getötet"
        elif victim_is_player:
            self.bot.db.record_kill(None, ev.victim)
            icon = {"natural": "☠️", "npc": "🐺", "entity": "🪤"}.get(ev.killer_type.value, "💀")
            line = f"{icon} **{safe_md(ev.victim)}** starb durch {safe_md(ev.killer)}"
        else:
            # Spieler tötet NPC/Scientist o. Ä. – nicht in der PvP-Statistik
            line = f"🎯 **{safe_md(ev.killer)}** hat {safe_md(ev.victim)} getötet"
        if self.killfeed:
            self.killfeed.add_line(line)

    @commands.Cog.listener()
    async def on_rce_suicide(self, ev: lp.SuicideEvent) -> None:
        self.bot.db.record_suicide(ev.player)
        if self.killfeed:
            self.killfeed.add_line(f"🪦 **{safe_md(ev.player)}** hat Selbstmord begangen")

    # ------------------------------------------------------------ Events / Joins

    @commands.Cog.listener()
    async def on_rce_player_join(self, name: str) -> None:
        if self.events:
            self.events.add_line(f"📥 **{safe_md(name)}** hat den Server betreten")

    @commands.Cog.listener()
    async def on_rce_player_leave(self, name: str) -> None:
        if self.events:
            self.events.add_line(f"📤 **{safe_md(name)}** hat den Server verlassen")

    @commands.Cog.listener()
    async def on_rce_respawn(self, ev: lp.RespawnEvent) -> None:
        self.bot.db.set_platform(ev.player, ev.platform)
        if self.events and self.bot.config.show_respawns:
            self.events.add_line(f"🔄 **{safe_md(ev.player)}** ist gespawnt ({ev.platform})")

    @commands.Cog.listener()
    async def on_rce_server_event(self, ev: lp.ServerEvent) -> None:
        if self.events:
            self.events.add_line(f"**{ev.name}** ist gestartet!")

    @commands.Cog.listener()
    async def on_rce_team(self, ev: lp.TeamEvent) -> None:
        if not self.events:
            return
        p, other = safe_md(ev.player), safe_md(ev.other or "?")
        text = {
            "create": f"🤝 **{p}** hat ein Team erstellt (ID {ev.team_id})",
            "join": f"🤝 **{p}** ist dem Team von **{other}** beigetreten",
            "leave": f"👋 **{p}** hat das Team von **{other}** verlassen",
            "promote": f"👑 **{p}** hat **{other}** zum Teamleiter gemacht",
        }.get(ev.kind)
        if text:
            self.events.add_line(text)

    # ------------------------------------------------------------ Admin-Log

    @commands.Cog.listener()
    async def on_rce_ban(self, ev: lp.BanEvent) -> None:
        if self.admin_log:
            verb = "🔨 gebannt" if ev.banned else "🕊️ entbannt"
            self.admin_log.add_line(f"**{safe_md(ev.player)}** wurde {verb} (von {safe_md(ev.admin)})")

    @commands.Cog.listener()
    async def on_rce_role(self, ev: lp.RoleEvent) -> None:
        if self.admin_log:
            action = "zur Gruppe hinzugefügt" if ev.added else "aus Gruppe entfernt"
            self.admin_log.add_line(
                f"🛡️ **{safe_md(ev.player)}** {action}: `{ev.role}` (von {safe_md(ev.admin)})")

    @commands.Cog.listener()
    async def on_rce_admin_action(self, ev: lp.AdminActionEvent) -> None:
        if not self.admin_log:
            return
        if ev.kind == "command":
            if ev.text.split(" ")[0].lower() in POLLING_COMMANDS:
                return
            self.admin_log.add_line(f"⌨️ Konsolenbefehl: `{ev.text}`")
        else:
            self.admin_log.add_line(f"🎁 {safe_md(ev.text)}")

    @commands.Cog.listener()
    async def on_rce_connection(self, connected: bool) -> None:
        if self.admin_log:
            self.admin_log.add_line("🟢 RCON-Verbindung hergestellt" if connected
                                    else "🔴 RCON-Verbindung verloren – automatischer Reconnect läuft")

    # ------------------------------------------------------------ Konsolen-Log

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
