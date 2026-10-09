"""Discord-Moderation: /mod timeout|untimeout|kick|ban|unban|purge.

Betrifft Discord-Mitglieder (nicht Ingame-Spieler – dafür gibt es /kick und /ban).
Rechte wie bei Discord selbst: Wer timeouten will, braucht „Mitglieder timeouten“,
für Kick „Mitglieder kicken“ usw. Zusätzlich wird die Rollen-Hierarchie geprüft.
Alle Aktionen landen im Discord-Log-Channel.
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from utils import safe_md

if TYPE_CHECKING:
    from bot import RceBot

MAX_TIMEOUT_MINUTES = 28 * 24 * 60  # Discord-Limit: 28 Tage


class Moderation(commands.Cog):
    mod = app_commands.Group(
        name="mod", description="Discord-Moderation (Mitglieder dieses Discord-Servers)", guild_only=True,
        default_permissions=discord.Permissions(moderate_members=True),
    )

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot

    # ------------------------------------------------------------ Hilfen

    @staticmethod
    def _hierarchy_problem(interaction: discord.Interaction, target: discord.Member) -> str | None:
        guild = interaction.guild
        actor = interaction.user
        if target.id == actor.id:
            return "Du kannst dich nicht selbst moderieren."
        if target.id == guild.owner_id:
            return "Der Server-Owner kann nicht moderiert werden."
        if target.id == guild.me.id:
            return "Den Bot selbst kannst du so nicht moderieren."
        if actor.id != guild.owner_id and isinstance(actor, discord.Member) and target.top_role >= actor.top_role:
            return "Das Mitglied hat eine gleich hohe oder höhere Rolle als du."
        if target.top_role >= guild.me.top_role:
            return "Die Rolle des Mitglieds steht über (oder gleich) der Bot-Rolle – Bot-Rolle nach oben ziehen."
        return None

    def _log(self, title: str, interaction: discord.Interaction, target: str, reason: str | None,
             colour: discord.Colour, extra: str | None = None) -> None:
        embed = discord.Embed(title=title, colour=colour)
        embed.add_field(name="Mitglied", value=target)
        embed.add_field(name="Moderator", value=f"{interaction.user.mention} ({safe_md(str(interaction.user))})")
        if extra:
            embed.add_field(name="Details", value=extra)
        embed.add_field(name="Grund", value=safe_md(reason or "Kein Grund angegeben")[:1000], inline=False)
        self.bot.discord_log(embed)

    @staticmethod
    async def _notify(member: discord.Member, text: str) -> None:
        try:
            await member.send(text)
        except discord.HTTPException:
            pass  # DMs deaktiviert

    async def _deny(self, interaction: discord.Interaction, problem: str) -> None:
        await interaction.response.send_message(f"⛔ {problem}", ephemeral=True)

    # ------------------------------------------------------------ Befehle

    @mod.command(name="timeout", description="Schaltet ein Mitglied für eine Zeit stumm.")
    @app_commands.describe(mitglied="Wen?", minuten="Dauer in Minuten (max. 40320 = 28 Tage)", grund="Grund")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def timeout(self, interaction: discord.Interaction, mitglied: discord.Member,
                      minuten: app_commands.Range[int, 1, MAX_TIMEOUT_MINUTES], grund: str | None = None) -> None:
        if problem := self._hierarchy_problem(interaction, mitglied):
            return await self._deny(interaction, problem)
        await mitglied.timeout(datetime.timedelta(minutes=minuten), reason=f"{grund or '-'} | von {interaction.user}")
        await self._notify(mitglied, f"Du wurdest auf **{safe_md(interaction.guild.name)}** für {minuten} Minute(n) "
                                     f"stummgeschaltet. Grund: {grund or 'kein Grund angegeben'}")
        self._log("🔇 Timeout", interaction, f"{mitglied.mention} ({safe_md(str(mitglied))})", grund,
                  discord.Colour.orange(), f"{minuten} Minute(n)")
        await interaction.response.send_message(
            f"🔇 {mitglied.mention} ist für {minuten} Minute(n) stummgeschaltet. Grund: {grund or '–'}",
            allowed_mentions=discord.AllowedMentions.none())

    @mod.command(name="untimeout", description="Hebt einen Timeout auf.")
    @app_commands.describe(mitglied="Wen?")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, mitglied: discord.Member) -> None:
        if problem := self._hierarchy_problem(interaction, mitglied):
            return await self._deny(interaction, problem)
        await mitglied.timeout(None, reason=f"Timeout aufgehoben von {interaction.user}")
        self._log("🔊 Timeout aufgehoben", interaction, f"{mitglied.mention}", None, discord.Colour.green())
        await interaction.response.send_message(f"🔊 Timeout von {mitglied.mention} aufgehoben.",
                                                allowed_mentions=discord.AllowedMentions.none())

    @mod.command(name="kick", description="Wirft ein Mitglied vom Discord-Server.")
    @app_commands.describe(mitglied="Wen?", grund="Grund")
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, mitglied: discord.Member, grund: str | None = None) -> None:
        if problem := self._hierarchy_problem(interaction, mitglied):
            return await self._deny(interaction, problem)
        await self._notify(mitglied, f"Du wurdest von **{safe_md(interaction.guild.name)}** gekickt. "
                                     f"Grund: {grund or 'kein Grund angegeben'}")
        await mitglied.kick(reason=f"{grund or '-'} | von {interaction.user}")
        self._log("👢 Discord-Kick", interaction, f"{safe_md(str(mitglied))} (ID {mitglied.id})", grund,
                  discord.Colour.orange())
        await interaction.response.send_message(f"👢 {safe_md(str(mitglied))} wurde gekickt. Grund: {grund or '–'}")

    @mod.command(name="ban", description="Bannt ein Mitglied vom Discord-Server.")
    @app_commands.describe(mitglied="Wen?", grund="Grund", nachrichten_loeschen="Nachrichten der letzten X Tage löschen")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, mitglied: discord.Member, grund: str | None = None,
                  nachrichten_loeschen: app_commands.Range[int, 0, 7] = 0) -> None:
        if problem := self._hierarchy_problem(interaction, mitglied):
            return await self._deny(interaction, problem)
        await self._notify(mitglied, f"Du wurdest von **{safe_md(interaction.guild.name)}** gebannt. "
                                     f"Grund: {grund or 'kein Grund angegeben'}")
        await mitglied.ban(reason=f"{grund or '-'} | von {interaction.user}",
                           delete_message_seconds=nachrichten_loeschen * 86400)
        self._log("🔨 Discord-Ban", interaction, f"{safe_md(str(mitglied))} (ID {mitglied.id})", grund,
                  discord.Colour.red(), f"Nachrichten gelöscht: {nachrichten_loeschen} Tag(e)")
        await interaction.response.send_message(f"🔨 {safe_md(str(mitglied))} wurde gebannt. Grund: {grund or '–'}")

    @mod.command(name="unban", description="Hebt einen Discord-Bann auf (per User-ID).")
    @app_commands.describe(user_id="User-ID des gebannten Nutzers", grund="Grund")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, grund: str | None = None) -> None:
        if not user_id.strip().isdigit():
            return await self._deny(interaction, "Bitte eine gültige User-ID angeben (Entwicklermodus → ID kopieren).")
        try:
            await interaction.guild.unban(discord.Object(id=int(user_id)), reason=f"{grund or '-'} | von {interaction.user}")
        except discord.NotFound:
            return await self._deny(interaction, "Dieser Nutzer ist nicht gebannt.")
        self._log("🕊️ Discord-Unban", interaction, f"<@{user_id}> (ID {user_id})", grund, discord.Colour.green())
        await interaction.response.send_message(f"🕊️ Bann von <@{user_id}> aufgehoben.",
                                                allowed_mentions=discord.AllowedMentions.none())

    @mod.command(name="purge", description="Löscht die letzten Nachrichten in diesem Channel.")
    @app_commands.describe(anzahl="1–100 Nachrichten", nutzer="Nur Nachrichten dieses Nutzers löschen")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(self, interaction: discord.Interaction, anzahl: app_commands.Range[int, 1, 100],
                    nutzer: discord.Member | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        check = (lambda m: m.author.id == nutzer.id) if nutzer else (lambda m: True)
        deleted = await interaction.channel.purge(limit=anzahl, check=check, bulk=True,
                                                  reason=f"/mod purge von {interaction.user}")
        self._log("🧹 Nachrichten gelöscht", interaction, nutzer.mention if nutzer else "alle", None,
                  discord.Colour.dark_grey(), f"{len(deleted)} in {interaction.channel.mention}")
        await interaction.followup.send(f"🧹 {len(deleted)} Nachricht(en) gelöscht.", ephemeral=True)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Moderation(bot))
