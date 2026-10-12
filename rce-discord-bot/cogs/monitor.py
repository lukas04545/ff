"""Staff alerts and backups.

* Server-down alert: when there has been no RCON connection for SERVER_DOWN_ALERT_MINUTES, staff is pinged once
  (ALERT_CHANNEL_ID / ALERT_ROLE_IDS) – and told again when the server is back, with the downtime.
* Daily backup (BACKUP_ENABLED) of database, web logins, rules and knowledge base to data/backups/, see backups.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

import backups
from utils import format_duration, safe_md

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.monitor")

BACKUP_KEY = "last_backup_ts"
BACKUP_EVERY = 24 * 3600


class Monitor(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self.down_since: float | None = time.time()   # until the first connection
        self.alerted = False
        self.last_error: str | None = None

    async def cog_load(self) -> None:
        self.watch_loop.start()
        if self.bot.config.backup_enabled:
            self.backup_loop.start()

    async def cog_unload(self) -> None:
        self.watch_loop.cancel()
        self.backup_loop.cancel()

    # ------------------------------------------------------------ Server-down alert

    @commands.Cog.listener()
    async def on_rce_connection(self, connected: bool) -> None:
        if connected:
            await self._back_online()
        elif self.down_since is None:
            self.down_since = time.time()

    @tasks.loop(seconds=30)
    async def watch_loop(self) -> None:
        minutes = self.bot.config.server_down_alert_minutes
        if self.bot.rcon.connected:
            await self._back_online()
            return
        if self.down_since is None:
            self.down_since = time.time()
        if minutes and not self.alerted and time.time() - self.down_since >= minutes * 60:
            self.alerted = True
            await self._alert_down()

    @watch_loop.before_loop
    async def _before_watch(self) -> None:
        await self.bot.wait_until_ready()

    def _channel(self) -> discord.abc.Messageable | None:
        channel_id = self.bot.config.alert_channel_id or self.bot.config.admin_log_channel_id
        channel = self.bot.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.abc.Messageable) else None

    async def _send(self, embed: discord.Embed, *, ping: bool) -> None:
        channel = self._channel()
        if channel is None:
            log.warning("%s (no alert channel set – ALERT_CHANNEL_ID or ADMIN_LOG_CHANNEL_ID)", embed.title)
            return
        roles = [discord.Object(r) for r in self.bot.config.alert_role_ids] if ping else []
        content = " ".join(f"<@&{r.id}>" for r in roles) or None
        try:
            await channel.send(content=content, embed=embed, allowed_mentions=discord.AllowedMentions(roles=roles))
        except discord.HTTPException as exc:
            log.warning("Could not send alert: %s", exc)

    async def _alert_down(self) -> None:
        since = int(self.down_since or time.time())
        embed = discord.Embed(title="🔴 Server offline", colour=discord.Colour.red(),
                              description=f"No RCON connection since <t:{since}:R> (<t:{since}:t>).")
        error = getattr(self.bot.rcon, "last_error", None)
        if error:
            embed.add_field(name="Last error", value=safe_md(str(error))[:1000], inline=False)
        diagnosis = self.bot.rcon_diagnosis
        if diagnosis and not diagnosis.get("ok"):
            hints = [s["text"] for s in diagnosis.get("steps", []) if s.get("level") in ("fail", "warn")]
            if hints:
                embed.add_field(name="Diagnosis", value=safe_md("\n".join(hints))[:1000], inline=False)
        embed.set_footer(text="Usually a server restart or G-Portal maintenance. You'll get a message when it's back.")
        log.warning("Server offline for %s – staff alerted.", format_duration(time.time() - since))
        await self._send(embed, ping=True)

    async def _back_online(self) -> None:
        if self.down_since is None:
            return
        downtime = time.time() - self.down_since
        self.down_since = None
        if self.alerted:
            self.alerted = False
            embed = discord.Embed(title="🟢 Server back online", colour=discord.Colour.green(),
                                  description=f"Offline for **{format_duration(downtime)}**.")
            await self._send(embed, ping=False)

    # ------------------------------------------------------------ Daily backup

    @tasks.loop(hours=1)
    async def backup_loop(self) -> None:
        last = float(self.bot.db.get_value(BACKUP_KEY) or 0)
        if time.time() - last >= BACKUP_EVERY - 600:
            await self.backup_now("daily")

    @backup_loop.before_loop
    async def _before_backup(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(300)   # not right at start-up

    async def backup_now(self, label: str = "manual") -> str | None:
        """Creates a backup (in a thread) and prunes old ones. Returns the file name or None on error."""
        try:
            path = await asyncio.to_thread(backups.make_backup, self.bot.config.database_path, label=label)
            await asyncio.to_thread(backups.prune_backups, self.bot.config.backup_keep_days)
        except Exception as exc:  # disk full, permissions …
            log.error("Backup failed: %s", exc)
            self.bot.audit(f"⚠️ Backup failed: {safe_md(str(exc))}")
            return None
        self.bot.db.set_value(BACKUP_KEY, str(time.time()))
        return path.name


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Monitor(bot))
