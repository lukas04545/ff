"""Weekly full backups (full_backup.py) and the emergency restore.

Every week the whole bot folder (code, every database, settings, plugins, uploads) is saved to data/full_backups/;
each is deleted after a week (the newest always stays). Main server only – it covers the extra servers' folders too.
  /emergency-restore backup:<…>   owner (ADMIN_USER_IDS, else admins): safety backup of now, then restart into the
                                  chosen backup. Also in the panel (Backups → 🚨 Emergency restore).
Bot dead? `python full_backup.py restore` in the bot folder.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

import full_backup
import servers
from utils import is_admin

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.full_backups")
LAST_KEY = "last_full_backup_ts"


def is_owner(bot, user) -> bool:
    if bot.config.admin_user_ids:
        return user.id in bot.config.admin_user_ids
    return is_admin(bot, user)


class RestoreConfirm(discord.ui.View):
    def __init__(self, cog: "FullBackups", user_id: int, name: str) -> None:
        super().__init__(timeout=120)
        self.cog, self.user_id, self.name = cog, user_id, name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Restore now", emoji="🚨", style=discord.ButtonStyle.danger)
    async def go(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=f"⏳ Saving the current state, then restoring "
                                                        f"**{self.name}** – the bot restarts …", view=None)
        try:
            safety = await self.cog.emergency_restore(self.name, by=str(interaction.user))
        except Exception as exc:
            await interaction.edit_original_response(content=f"⚠️ Restore failed: {exc}")
            return
        await interaction.edit_original_response(
            content=f"🚨 Restoring **{self.name}** – the bot is back in a moment.\nThe state before is saved as "
                    f"`{safety}` (restore that to undo).")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


class FullBackups(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self.last_error: str | None = None
        self.busy = asyncio.Lock()

    async def cog_load(self) -> None:
        if not servers.is_child():
            self.weekly_loop.start()

    async def cog_unload(self) -> None:
        self.weekly_loop.cancel()

    def enabled(self) -> bool:
        return bool(getattr(self.bot.config, "full_backup_enabled", True)) and not servers.is_child()

    def _audit(self, text: str) -> None:
        self.bot.audit(text)

    async def make_now(self, label: str = "manual") -> str:
        async with self.busy:
            path = await asyncio.to_thread(full_backup.make, label)
            removed = await asyncio.to_thread(full_backup.prune)
        self.bot.db.set_value(LAST_KEY, str(time.time()))
        self.last_error = None
        log.info("Full backup saved: %s (%s MB)%s", path.name, round(path.stat().st_size / 1e6, 1),
                 f" – deleted {len(removed)} older than a week" if removed else "")
        return path.name

    @tasks.loop(hours=1)
    async def weekly_loop(self) -> None:
        if not self.enabled():
            return
        if not full_backup.due(float(self.bot.db.get_value(LAST_KEY) or 0)):
            await asyncio.to_thread(full_backup.prune)      # a week old -> gone, even between backups
            return
        try:
            name = await self.make_now("weekly")
            self._audit(f"🧳 Weekly full backup saved: {name}")
        except Exception as exc:                          # disk full, permissions …
            self.last_error = str(exc)
            log.error("Full backup failed: %s", exc)
            self._audit(f"⚠️ Weekly full backup failed: {exc}")

    @weekly_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(600)                          # not right at start-up

    async def emergency_restore(self, name: str, *, by: str) -> str:
        """Safety backup of the current state, then restart into the chosen full backup. Returns the safety name."""
        if servers.is_child():
            raise ValueError("Restore on the main server – a full backup covers every server.")
        if full_backup.path_of(name) is None:
            raise ValueError("No such full backup.")
        safety = await self.make_now("pre-restore")
        full_backup.request_restore(name)
        self._audit(f"🚨 Emergency restore of {name} by {by} (state before saved as {safety}) – restarting")
        log.warning("Emergency restore of %s requested by %s – restarting …", name, by)
        self.bot.update_restart = True                    # main() starts bot.py again -> it restores first
        asyncio.get_running_loop().call_later(1.0, lambda: asyncio.ensure_future(self.bot.close()))
        return safety

    # ------------------------------------------------------------ Discord

    async def _backup_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        q = current.lower()
        return [app_commands.Choice(name=f"{b['name']} ({b['size_mb']} MB)"[:100], value=b["name"])
                for b in full_backup.list_backups() if q in b["name"].lower()][:25]

    @app_commands.command(name="emergency-restore",
                          description="Puts the whole bot back to a full backup (code, databases, settings).")
    @app_commands.describe(backup="Which full backup (newest first)")
    @app_commands.autocomplete(backup=_backup_ac)
    @app_commands.guild_only()
    async def emergency_cmd(self, interaction: discord.Interaction, backup: str) -> None:
        if not is_owner(self.bot, interaction.user):
            await interaction.response.send_message("Only the owner (ADMIN_USER_IDS) can restore backups.",
                                                    ephemeral=True)
            return
        if full_backup.path_of(backup) is None:
            await interaction.response.send_message("No such full backup – pick one from the list.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🚨 Put **everything** back to `{backup}`? Code, databases (all servers), settings and plugins go back to "
            "that point – everything since then is replaced. The current state is saved first (pre-restore), so you "
            "can undo it.", view=RestoreConfirm(self, interaction.user.id, backup), ephemeral=True)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(FullBackups(bot))
