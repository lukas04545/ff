"""Staff alerts and backups.

* Server-down alert: when there has been no RCON connection for SERVER_DOWN_ALERT_MINUTES, staff is pinged once
  (ALERT_CHANNEL_ID / ALERT_ROLE_IDS) – and told again when the server is back, with the downtime.
* Daily backup (BACKUP_ENABLED) of database, web logins, rules and knowledge base to data/backups/, see backups.py.
"""
from __future__ import annotations

import asyncio
import sqlite3
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
        self.live_last: float | None = None          # last live-backup.zip written
        self.live_error: str | None = None
        self.copy_error: str | None = None
        self._live_marker: tuple | None = None

    async def cog_load(self) -> None:
        self.watch_loop.start()
        self.live_loop.start()
        if self.bot.config.backup_enabled:
            self.backup_loop.start()

    async def cog_unload(self) -> None:
        self.watch_loop.cancel()
        self.backup_loop.cancel()
        self.live_loop.cancel()

    async def on_config_changed(self, old, new) -> None:
        if new.backup_enabled and not self.backup_loop.is_running():
            self.backup_loop.start()
        elif not new.backup_enabled and self.backup_loop.is_running():
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
        cfg = self.bot.config
        try:
            path = await asyncio.to_thread(backups.make_backup, cfg.database_path, label=label,
                                           include_env=cfg.backup_include_env)
            await asyncio.to_thread(backups.prune_backups, cfg.backup_keep_days)
        except Exception as exc:  # disk full, permissions …
            log.error("Backup failed: %s", exc)
            self.bot.audit(f"⚠️ Backup failed: {safe_md(str(exc))}")
            return None
        self.bot.db.set_value(BACKUP_KEY, str(time.time()))
        await self.copy_out(path)
        return path.name

    async def copy_out(self, path) -> None:
        """Copies a backup ZIP into BACKUP_COPY_DIR (e.g. Google Drive) and removes old copies there."""
        folder = self.bot.config.backup_copy_dir
        if folder is None:
            return
        try:
            await asyncio.to_thread(backups.copy_to_folder, path, folder)
            await asyncio.to_thread(backups.prune_folder, folder, self.bot.config.backup_keep_days)
            self.copy_error = None
        except OSError as exc:
            if self.copy_error != str(exc):
                self.bot.audit(f"⚠️ Backup could not be copied to {safe_md(str(folder))}: {safe_md(str(exc))}")
            self.copy_error = str(exc)
            log.warning("Backup copy to %s failed: %s", folder, exc)

    # ------------------------------------------------------------ Live backup (copy folder)

    @tasks.loop(minutes=1)
    async def live_loop(self) -> None:
        cfg = self.bot.config
        if cfg.backup_copy_dir is None or not cfg.backup_live_minutes:
            return
        if self.live_last and time.time() - self.live_last < cfg.backup_live_minutes * 60 - 5:
            return
        await self.live_backup()

    @live_loop.before_loop
    async def _before_live(self) -> None:
        await self.bot.wait_until_ready()

    async def live_backup(self, *, force: bool = False) -> bool:
        """Refreshes live-backup.zip in the copy folder if anything changed since the last one."""
        cfg = self.bot.config
        if cfg.backup_copy_dir is None:
            return False
        marker = await asyncio.to_thread(backups.change_marker, cfg.database_path)
        if not force and marker == self._live_marker:
            return False
        try:
            await asyncio.to_thread(backups.write_live, cfg.database_path, cfg.backup_copy_dir,
                                    include_env=cfg.backup_include_env)
        except (OSError, sqlite3.Error) as exc:
            if self.live_error != str(exc):
                self.bot.audit(f"⚠️ Live backup to {safe_md(str(cfg.backup_copy_dir))} failed: {safe_md(str(exc))}")
            self.live_error = str(exc)
            log.warning("Live backup failed: %s", exc)
            return False
        self._live_marker, self.live_last, self.live_error = marker, time.time(), None
        return True

    def backup_status(self) -> dict:
        cfg = self.bot.config
        return {"copy_dir": str(cfg.backup_copy_dir) if cfg.backup_copy_dir else None,
                "live_minutes": cfg.backup_live_minutes, "live_last": self.live_last,
                "live_error": self.live_error, "copy_error": self.copy_error, "include_env": cfg.backup_include_env}


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Monitor(bot))
