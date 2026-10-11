"""Admin commands: kick, ban, unban, say, arbitrary RCON command.

All commands are restricted to ADMIN_ROLE_IDS / ADMIN_USER_IDS and are logged in
the admin log channel (who ran what).
"""
from __future__ import annotations

import io
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from utils import admin_only, player_choices, safe_md, sanitize_ingame_text, sanitize_player_name

if TYPE_CHECKING:
    from bot import RceBot


class Admin(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot

    # ------------------------------------------------------------ Helpers

    def _audit(self, interaction: discord.Interaction, action: str) -> None:
        feed = self.bot.feed(self.bot.config.admin_log_channel_id)
        if feed:
            feed.add_line(f"🛠️ {interaction.user.mention} ({safe_md(str(interaction.user))}): {action}")

    async def _player_autocomplete(self, interaction: discord.Interaction, current: str
                                   ) -> list[app_commands.Choice[str]]:
        return player_choices(self.bot, current)

    @staticmethod
    def _format_response(response: str) -> str:
        response = response.strip()
        if not response:
            return "(Server returned no output – normal for many commands on RCE.)"
        return f"```\n{response[:1800].replace('```', 'ˋˋˋ')}\n```"

    # ------------------------------------------------------------ Commands

    @app_commands.command(name="kick", description="Kicks a player from the server.")
    @app_commands.describe(player="In-game name (Gamertag/PSN)", reason="Optional reason (log only)")
    @app_commands.autocomplete(player=_player_autocomplete)
    @app_commands.guild_only()
    @admin_only()
    async def kick(self, interaction: discord.Interaction, player: str, reason: str | None = None) -> None:
        name = sanitize_player_name(player)
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(self.bot.config.kick_template.format(name=name),
                                               expect_response=False)
        self._audit(interaction, f"**Kick** `{name}`" + (f" – Reason: {safe_md(reason)}" if reason else ""))
        await interaction.followup.send(f"👢 Kick sent for **{safe_md(name)}**.\n"
                                        f"{self._format_response(response)}", ephemeral=True)

    @app_commands.command(name="ban", description="Permanently bans a player.")
    @app_commands.describe(player="In-game name (Gamertag/PSN)", reason="Optional reason (log only)")
    @app_commands.autocomplete(player=_player_autocomplete)
    @app_commands.guild_only()
    @admin_only()
    async def ban(self, interaction: discord.Interaction, player: str, reason: str | None = None) -> None:
        name = sanitize_player_name(player)
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(self.bot.config.ban_template.format(name=name),
                                               expect_response=False)
        self._audit(interaction, f"**Ban** `{name}`" + (f" – Reason: {safe_md(reason)}" if reason else ""))
        await interaction.followup.send(f"🔨 Ban sent for **{safe_md(name)}**.\n"
                                        f"{self._format_response(response)}", ephemeral=True)

    @app_commands.command(name="unban", description="Lifts a ban.")
    @app_commands.describe(player="In-game name (Gamertag/PSN)")
    @app_commands.autocomplete(player=_player_autocomplete)
    @app_commands.guild_only()
    @admin_only()
    async def unban(self, interaction: discord.Interaction, player: str) -> None:
        name = sanitize_player_name(player)
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(self.bot.config.unban_template.format(name=name),
                                               expect_response=False)
        self._audit(interaction, f"**Unban** `{name}`")
        await interaction.followup.send(f"🕊️ Unban sent for **{safe_md(name)}**.\n"
                                        f"{self._format_response(response)}", ephemeral=True)

    @app_commands.command(name="say", description="Sends a message to all players in game.")
    @app_commands.describe(message="Text shown in game as a SERVER message")
    @app_commands.guild_only()
    @admin_only()
    async def say(self, interaction: discord.Interaction, message: str) -> None:
        # Admins may deliberately use rich text (<color=...>, <b>) -> only strip line breaks
        text = " ".join(message.splitlines()).strip()[:400]
        await interaction.response.defer(ephemeral=True)
        await self.bot.rcon.command(f"say {text}", expect_response=False)
        self._audit(interaction, f"**Say**: {safe_md(sanitize_ingame_text(text, 300))}")
        await interaction.followup.send("📢 Message sent.", ephemeral=True)

    @app_commands.command(name="rcon", description="Runs any console command.")
    @app_commands.describe(command="e.g. serverinfo, playerlist, env.time 12")
    @app_commands.guild_only()
    @admin_only()
    async def rcon(self, interaction: discord.Interaction, command: str) -> None:
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(command.strip(), timeout=10, expect_response=False)
        self._audit(interaction, f"**RCON** `{command[:300]}`")
        if len(response) > 1800:
            file = discord.File(io.BytesIO(response.encode()), filename="rcon-output.txt")
            await interaction.followup.send(f"Output of `{command[:100]}`:", file=file, ephemeral=True)
        else:
            await interaction.followup.send(f"Output of `{command[:100]}`:\n{self._format_response(response)}",
                                            ephemeral=True)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Admin(bot))
    if not bot.config.allow_raw_rcon:
        bot.tree.remove_command("rcon")  # ALLOW_RAW_RCON=false -> don't register /rcon at all
