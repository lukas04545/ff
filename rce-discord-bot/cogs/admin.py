"""Admin-Befehle: Kick, Ban, Unban, Say, beliebiger RCON-Befehl.

Alle Befehle sind auf ADMIN_ROLE_IDS / ADMIN_USER_IDS beschränkt und werden im
Admin-Log-Channel protokolliert (wer hat was ausgeführt).
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

    # ------------------------------------------------------------ Hilfen

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
            return "(Server hat keine Ausgabe zurückgegeben – bei RCE für viele Befehle normal.)"
        return f"```\n{response[:1800].replace('```', 'ˋˋˋ')}\n```"

    # ------------------------------------------------------------ Befehle

    @app_commands.command(name="kick", description="Kickt einen Spieler vom Server.")
    @app_commands.describe(spieler="Ingame-Name (Gamertag/PSN)", grund="Optionaler Grund (nur fürs Log)")
    @app_commands.autocomplete(spieler=_player_autocomplete)
    @app_commands.guild_only()
    @admin_only()
    async def kick(self, interaction: discord.Interaction, spieler: str, grund: str | None = None) -> None:
        name = sanitize_player_name(spieler)
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(self.bot.config.kick_template.format(name=name),
                                               expect_response=False)
        self._audit(interaction, f"**Kick** `{name}`" + (f" – Grund: {safe_md(grund)}" if grund else ""))
        await interaction.followup.send(f"👢 Kick für **{safe_md(name)}** gesendet.\n"
                                        f"{self._format_response(response)}", ephemeral=True)

    @app_commands.command(name="ban", description="Bannt einen Spieler dauerhaft.")
    @app_commands.describe(spieler="Ingame-Name (Gamertag/PSN)", grund="Optionaler Grund (nur fürs Log)")
    @app_commands.autocomplete(spieler=_player_autocomplete)
    @app_commands.guild_only()
    @admin_only()
    async def ban(self, interaction: discord.Interaction, spieler: str, grund: str | None = None) -> None:
        name = sanitize_player_name(spieler)
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(self.bot.config.ban_template.format(name=name),
                                               expect_response=False)
        self._audit(interaction, f"**Ban** `{name}`" + (f" – Grund: {safe_md(grund)}" if grund else ""))
        await interaction.followup.send(f"🔨 Ban für **{safe_md(name)}** gesendet.\n"
                                        f"{self._format_response(response)}", ephemeral=True)

    @app_commands.command(name="unban", description="Hebt einen Bann auf.")
    @app_commands.describe(spieler="Ingame-Name (Gamertag/PSN)")
    @app_commands.autocomplete(spieler=_player_autocomplete)
    @app_commands.guild_only()
    @admin_only()
    async def unban(self, interaction: discord.Interaction, spieler: str) -> None:
        name = sanitize_player_name(spieler)
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(self.bot.config.unban_template.format(name=name),
                                               expect_response=False)
        self._audit(interaction, f"**Unban** `{name}`")
        await interaction.followup.send(f"🕊️ Unban für **{safe_md(name)}** gesendet.\n"
                                        f"{self._format_response(response)}", ephemeral=True)

    @app_commands.command(name="say", description="Sendet eine Nachricht an alle Spieler im Spiel.")
    @app_commands.describe(nachricht="Text, der ingame als SERVER-Nachricht erscheint")
    @app_commands.guild_only()
    @admin_only()
    async def say(self, interaction: discord.Interaction, nachricht: str) -> None:
        # Admins dürfen Rich-Text (<color=...>, <b>) bewusst nutzen -> nur Zeilenumbrüche entfernen
        text = " ".join(nachricht.splitlines()).strip()[:400]
        await interaction.response.defer(ephemeral=True)
        await self.bot.rcon.command(f"say {text}", expect_response=False)
        self._audit(interaction, f"**Say**: {safe_md(sanitize_ingame_text(text, 300))}")
        await interaction.followup.send("📢 Nachricht gesendet.", ephemeral=True)

    @app_commands.command(name="rcon", description="Führt einen beliebigen Konsolenbefehl aus.")
    @app_commands.describe(befehl="z. B. serverinfo, playerlist, env.time 12")
    @app_commands.guild_only()
    @admin_only()
    async def rcon(self, interaction: discord.Interaction, befehl: str) -> None:
        await interaction.response.defer(ephemeral=True)
        response = await self.bot.rcon.command(befehl.strip(), timeout=10, expect_response=False)
        self._audit(interaction, f"**RCON** `{befehl[:300]}`")
        if len(response) > 1800:
            file = discord.File(io.BytesIO(response.encode()), filename="rcon-ausgabe.txt")
            await interaction.followup.send(f"Ausgabe von `{befehl[:100]}`:", file=file, ephemeral=True)
        else:
            await interaction.followup.send(f"Ausgabe von `{befehl[:100]}`:\n{self._format_response(response)}",
                                            ephemeral=True)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Admin(bot))
    if not bot.config.allow_raw_rcon:
        bot.tree.remove_command("rcon")  # ALLOW_RAW_RCON=false -> /rcon gar nicht registrieren
