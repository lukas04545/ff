"""Discord moderation: /mod timeout|untimeout|kick|ban|unban|purge.

Applies to Discord members (not in-game players – use /kick and /ban for those).
Permissions work like in Discord itself: timing out requires "Timeout Members",
kicking requires "Kick Members", and so on. The role hierarchy is checked as well.
All actions are posted to the Discord log channel.
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

MAX_TIMEOUT_MINUTES = 28 * 24 * 60  # Discord limit: 28 days


class Moderation(commands.Cog):
    mod = app_commands.Group(
        name="mod", description="Discord moderation (members of this Discord server)", guild_only=True,
        default_permissions=discord.Permissions(moderate_members=True),
    )

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot

    # ------------------------------------------------------------ Helpers

    @staticmethod
    def _hierarchy_problem(interaction: discord.Interaction, target: discord.Member) -> str | None:
        guild = interaction.guild
        actor = interaction.user
        if target.id == actor.id:
            return "You can't moderate yourself."
        if target.id == guild.owner_id:
            return "The server owner can't be moderated."
        if target.id == guild.me.id:
            return "You can't moderate the bot itself this way."
        if actor.id != guild.owner_id and isinstance(actor, discord.Member) and target.top_role >= actor.top_role:
            return "This member has an equal or higher role than you."
        if target.top_role >= guild.me.top_role:
            return "The member's role is above (or equal to) the bot role – drag the bot role higher."
        return None

    def _log(self, title: str, interaction: discord.Interaction, target: str, reason: str | None,
             colour: discord.Colour, extra: str | None = None) -> None:
        embed = discord.Embed(title=title, colour=colour)
        embed.add_field(name="Member", value=target)
        embed.add_field(name="Moderator", value=f"{interaction.user.mention} ({safe_md(str(interaction.user))})")
        if extra:
            embed.add_field(name="Details", value=extra)
        embed.add_field(name="Reason", value=safe_md(reason or "No reason given")[:1000], inline=False)
        self.bot.discord_log(embed)

    @staticmethod
    async def _notify(member: discord.Member, text: str) -> None:
        try:
            await member.send(text)
        except discord.HTTPException:
            pass  # DMs disabled

    async def _deny(self, interaction: discord.Interaction, problem: str) -> None:
        await interaction.response.send_message(f"⛔ {problem}", ephemeral=True)

    # ------------------------------------------------------------ Commands

    @mod.command(name="timeout", description="Mutes a member for a while.")
    @app_commands.describe(member="Who?", minutes="Duration in minutes (max. 40320 = 28 days)", reason="Reason")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def timeout(self, interaction: discord.Interaction, member: discord.Member,
                      minutes: app_commands.Range[int, 1, MAX_TIMEOUT_MINUTES], reason: str | None = None) -> None:
        if problem := self._hierarchy_problem(interaction, member):
            return await self._deny(interaction, problem)
        await member.timeout(datetime.timedelta(minutes=minutes), reason=f"{reason or '-'} | by {interaction.user}")
        await self._notify(member, f"You have been muted on **{safe_md(interaction.guild.name)}** for {minutes} "
                                   f"minute(s). Reason: {reason or 'no reason given'}")
        self._log("🔇 Timeout", interaction, f"{member.mention} ({safe_md(str(member))})", reason,
                  discord.Colour.orange(), f"{minutes} minute(s)")
        await interaction.response.send_message(
            f"🔇 {member.mention} has been muted for {minutes} minute(s). Reason: {reason or '–'}",
            allowed_mentions=discord.AllowedMentions.none())

    @mod.command(name="untimeout", description="Removes a timeout.")
    @app_commands.describe(member="Who?")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if problem := self._hierarchy_problem(interaction, member):
            return await self._deny(interaction, problem)
        await member.timeout(None, reason=f"Timeout removed by {interaction.user}")
        self._log("🔊 Timeout removed", interaction, f"{member.mention}", None, discord.Colour.green())
        await interaction.response.send_message(f"🔊 Timeout of {member.mention} removed.",
                                                allowed_mentions=discord.AllowedMentions.none())

    @mod.command(name="kick", description="Kicks a member from the Discord server.")
    @app_commands.describe(member="Who?", reason="Reason")
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None) -> None:
        if problem := self._hierarchy_problem(interaction, member):
            return await self._deny(interaction, problem)
        await self._notify(member, f"You have been kicked from **{safe_md(interaction.guild.name)}**. "
                                   f"Reason: {reason or 'no reason given'}")
        await member.kick(reason=f"{reason or '-'} | by {interaction.user}")
        self._log("👢 Discord kick", interaction, f"{safe_md(str(member))} (ID {member.id})", reason,
                  discord.Colour.orange())
        await interaction.response.send_message(f"👢 {safe_md(str(member))} has been kicked. Reason: {reason or '–'}")

    @mod.command(name="ban", description="Bans a member from the Discord server.")
    @app_commands.describe(member="Who?", reason="Reason", delete_days="Delete their messages from the last X days")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None,
                  delete_days: app_commands.Range[int, 0, 7] = 0) -> None:
        if problem := self._hierarchy_problem(interaction, member):
            return await self._deny(interaction, problem)
        await self._notify(member, f"You have been banned from **{safe_md(interaction.guild.name)}**. "
                                   f"Reason: {reason or 'no reason given'}")
        await member.ban(reason=f"{reason or '-'} | by {interaction.user}",
                         delete_message_seconds=delete_days * 86400)
        self._log("🔨 Discord ban", interaction, f"{safe_md(str(member))} (ID {member.id})", reason,
                  discord.Colour.red(), f"Messages deleted: {delete_days} day(s)")
        await interaction.response.send_message(f"🔨 {safe_md(str(member))} has been banned. Reason: {reason or '–'}")

    @mod.command(name="unban", description="Lifts a Discord ban (by user ID).")
    @app_commands.describe(user_id="User ID of the banned user", reason="Reason")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str | None = None) -> None:
        if not user_id.strip().isdigit():
            return await self._deny(interaction, "Please enter a valid user ID (Developer Mode → Copy User ID).")
        try:
            await interaction.guild.unban(discord.Object(id=int(user_id)), reason=f"{reason or '-'} | by {interaction.user}")
        except discord.NotFound:
            return await self._deny(interaction, "This user is not banned.")
        self._log("🕊️ Discord unban", interaction, f"<@{user_id}> (ID {user_id})", reason, discord.Colour.green())
        await interaction.response.send_message(f"🕊️ Ban of <@{user_id}> lifted.",
                                                allowed_mentions=discord.AllowedMentions.none())

    @mod.command(name="purge", description="Deletes the latest messages in this channel.")
    @app_commands.describe(count="1–100 messages", user="Only delete messages from this user")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 100],
                    user: discord.Member | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        check = (lambda m: m.author.id == user.id) if user else (lambda m: True)
        deleted = await interaction.channel.purge(limit=count, check=check, bulk=True,
                                                  reason=f"/mod purge by {interaction.user}")
        self._log("🧹 Messages deleted", interaction, user.mention if user else "all", None,
                  discord.Colour.dark_grey(), f"{len(deleted)} in {interaction.channel.mention}")
        await interaction.followup.send(f"🧹 {len(deleted)} message(s) deleted.", ephemeral=True)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Moderation(bot))
