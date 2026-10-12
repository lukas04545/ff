"""Timed roles: Discord roles that are removed again automatically (e.g. "VIP for 7 days").

/temprole give    – give a member a role for a duration (again = extend)
/temprole remove  – take it away now
/temprole list    – running timed roles

Used by the ticket AI for giveaway prizes like "VIP 7 days" and by the shop. Expiry is checked every minute and
survives restarts (database); roles that ran out while the bot was offline are removed right after the start.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from permcheck import explain_forbidden
from utils import admin_only, format_duration, parse_duration, safe_md

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.timed_roles")


class TimedRoleError(Exception):
    """Role can't be given – message for the user/AI."""


class TimedRoles(commands.Cog):
    temprole = app_commands.Group(name="temprole", description="Roles for a limited time (e.g. VIP for 7 days)",
                                  guild_only=True)

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._warned: set[int] = set()

    async def cog_load(self) -> None:
        self.expire_loop.start()

    async def cog_unload(self) -> None:
        self.expire_loop.cancel()

    # ------------------------------------------------------------ API (also for tickets/shop)

    def check_role(self, guild: discord.Guild, role: discord.Role) -> None:
        if role.is_default() or role.managed:
            raise TimedRoleError(f"The role {role.name} can't be given (managed by Discord or an integration).")
        if role >= guild.me.top_role:
            raise TimedRoleError(f"The role {role.name} is above the bot's highest role – drag the bot role higher "
                                 "in Server Settings → Roles.")

    async def grant(self, member: discord.Member, role: discord.Role, minutes: int, *, reason: str,
                    by: str) -> int:
        """Gives the role for `minutes` (extends a running timer). Returns the new expiry (Unix time)."""
        if minutes <= 0:
            raise TimedRoleError("The duration must be at least 1 minute.")
        self.check_role(member.guild, role)
        existing = self.bot.db.timed_role(member.guild.id, member.id, role.id)
        start = max(int(time.time()), existing["expires_at"]) if existing else int(time.time())
        expires_at = start + minutes * 60
        if role not in member.roles:
            try:
                await member.add_roles(role, reason=f"Timed role ({format_duration(minutes * 60)}): {reason}"[:500])
            except discord.Forbidden as exc:
                raise TimedRoleError(explain_forbidden(exc, self.bot)) from exc
            except discord.HTTPException as exc:
                raise TimedRoleError(f"Discord error: {exc}") from exc
        self.bot.db.set_timed_role(member.guild.id, member.id, role.id, expires_at, reason[:300], by[:100])
        self.bot.audit(f"⏳ {member.mention} has **{safe_md(role.name)}** until <t:{expires_at}:f> "
                       f"({safe_md(reason)}, by {safe_md(by)})")
        return expires_at

    def permanent(self, member: discord.Member, role: discord.Role) -> bool:
        """Member already has the role without a timer (e.g. bought/given for good) – don't add an expiry."""
        return role in member.roles and self.bot.db.timed_role(member.guild.id, member.id, role.id) is None

    async def revoke(self, row) -> str:
        """Removes a timed role now (expired or by hand). Returns a short description."""
        guild = self.bot.get_guild(row["guild_id"])
        role = guild.get_role(row["role_id"]) if guild else None
        member = guild.get_member(row["user_id"]) if guild else None
        if guild is not None and member is None:
            try:
                member = await guild.fetch_member(row["user_id"])
            except discord.HTTPException:
                member = None
        if role is not None and member is not None and role in member.roles:
            await member.remove_roles(role, reason=f"Timed role ended ({row['reason'] or 'no reason'})"[:500])
        self.bot.db.delete_timed_role(row["id"])
        return f"<@{row['user_id']}> – {safe_md(role.name) if role else 'deleted role'}"

    # ------------------------------------------------------------ Expiry

    @tasks.loop(seconds=60)
    async def expire_loop(self) -> None:
        for row in self.bot.db.due_timed_roles(int(time.time())):
            try:
                text = await self.revoke(row)
            except discord.HTTPException as exc:
                if row["id"] not in self._warned:   # e.g. bot role now too low – retry every minute, warn once
                    self._warned.add(row["id"])
                    log.warning("Could not remove timed role %s from %s: %s", row["role_id"], row["user_id"], exc)
                continue
            self.bot.audit(f"⌛ Timed role ended: {text}")
            await self._notify(row)

    @expire_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _notify(self, row) -> None:
        guild = self.bot.get_guild(row["guild_id"])
        role = guild.get_role(row["role_id"]) if guild else None
        user = self.bot.get_user(row["user_id"])
        if user is None or role is None:
            return
        try:
            await user.send(f"⌛ Your role **{safe_md(role.name)}** on **{safe_md(guild.name)}** has ended.")
        except discord.HTTPException:
            pass  # DMs closed – that's fine

    # ------------------------------------------------------------ Commands

    @temprole.command(name="give", description="Gives a member a role for a limited time (again = extend).")
    @app_commands.describe(member="Member", role="Role", duration="e.g. 7d, 12h, 2w, 1d 12h",
                           reason="Why (shown in the admin log)")
    @admin_only()
    async def give(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role,
                   duration: str, reason: str | None = None) -> None:
        minutes = parse_duration(duration)
        if not minutes:
            await interaction.response.send_message("⚠️ Invalid duration – use e.g. `7d`, `12h`, `2w` or `1d 12h`.",
                                                    ephemeral=True)
            return
        was_permanent = self.permanent(member, role)
        try:
            expires_at = await self.grant(member, role, minutes, reason=reason or "given by staff",
                                          by=str(interaction.user))
        except TimedRoleError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        note = ("\nℹ️ They already had this role without a time limit – it will now be **removed** at the end."
                if was_permanent else "")
        await interaction.response.send_message(
            f"✅ {member.mention} has **{safe_md(role.name)}** until <t:{expires_at}:f> (<t:{expires_at}:R>).{note}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @temprole.command(name="remove", description="Removes a timed role now.")
    @app_commands.describe(member="Member", role="Role")
    @admin_only()
    async def remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role) -> None:
        row = self.bot.db.timed_role(member.guild.id, member.id, role.id)
        if row is None:
            await interaction.response.send_message("This member has no timer for that role.", ephemeral=True)
            return
        try:
            text = await self.revoke(row)
        except discord.Forbidden as exc:
            await interaction.response.send_message(explain_forbidden(exc, self.bot), ephemeral=True)
            return
        self.bot.audit(f"⌛ Timed role removed by {safe_md(str(interaction.user))}: {text}")
        await interaction.response.send_message(f"✅ Removed: {text}", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @temprole.command(name="list", description="Shows running timed roles.")
    @app_commands.describe(member="Only this member")
    @admin_only()
    async def list_cmd(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        rows = self.bot.db.timed_roles(user_id=member.id if member else None)
        if not rows:
            await interaction.response.send_message("No timed roles running.", ephemeral=True)
            return
        lines = [f"<@{r['user_id']}> – <@&{r['role_id']}> ends <t:{r['expires_at']}:R>"
                 + (f" · {safe_md(r['reason'])}" if r["reason"] else "") for r in rows[:40]]
        embed = discord.Embed(title=f"⏳ Timed roles ({len(rows)})", description="\n".join(lines)[:4000],
                              colour=discord.Colour.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(TimedRoles(bot))
