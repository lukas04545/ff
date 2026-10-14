"""Prison (jail): /prison jail locks a member down so they can only see the prison channel and their own ticket.

How it works
  * The member gets the prison role; the bot removes their other roles (saved, restored when freed).
  * The prison role is denied "View Channel" on every category and channel, except the prison channel (allowed).
    New channels are locked down too (on_guild_channel_create).
  * Their own ticket stays visible: a ticket channel grants its owner a per-member View overwrite, and per-member
    overwrites beat the role deny – but they still can't see anyone else's ticket.
  * A jail can be timed: give `/prison jail` a `minutes` value and the member is released automatically when the
    time is up (checked once a minute). The AI moderation can jail for a duration too (see cogs/automod.py).
  * /prison free removes the prison role and gives their old roles back.

Set the role and channel in Settings → 🎉 Community (PRISON_ROLE_ID, PRISON_CHANNEL_ID).
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from permcheck import explain_forbidden
from utils import admin_only, safe_md

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.prison")

MAX_JAIL_MINUTES = 40320          # 28 days – Discord's own timeout cap, a sane ceiling for a jail too


def human_duration(minutes: int) -> str:
    days, rem = divmod(int(minutes), 1440)
    hours, mins = divmod(rem, 60)
    parts = [f"{days} day{'s' if days != 1 else ''}" if days else "",
             f"{hours} hour{'s' if hours != 1 else ''}" if hours else "",
             f"{mins} minute{'s' if mins != 1 else ''}" if mins else ""]
    return " ".join(p for p in parts if p) or "0 minutes"


class Prison(commands.Cog):
    prison = app_commands.Group(name="prison", description="Jail a member (sees only the prison channel + own ticket)",
                                guild_only=True, default_permissions=discord.Permissions(moderate_members=True))

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.auto_release.start()

    def cog_unload(self) -> None:
        self.auto_release.cancel()

    # ------------------------------------------------------------ Helpers

    def role(self, guild: discord.Guild) -> discord.Role | None:
        rid = self.bot.config.prison_role_id
        return guild.get_role(rid) if rid else None

    def channel(self, guild: discord.Guild):
        cid = self.bot.config.prison_channel_id
        ch = guild.get_channel(cid) if cid else None
        return ch if ch is not None and hasattr(ch, "set_permissions") else None

    def _setup_error(self, guild: discord.Guild) -> str | None:
        role, channel = self.role(guild), self.channel(guild)
        if role is None:
            return "No prison role set – choose one in Settings → 🎉 Community (Prison role)."
        if channel is None:
            return "No prison channel set – choose one in Settings → 🎉 Community (Prison channel)."
        if role >= guild.me.top_role:
            return f"The role {role.name} is above the bot's role – drag the bot's role higher so it can give it."
        return None

    def _removable(self, member: discord.Member, prison_role: discord.Role) -> list[discord.Role]:
        """Roles the bot takes away while jailed (and gives back later): not @everyone, the prison role, managed
        roles (bots/boosts), or roles above the bot."""
        top = member.guild.me.top_role
        return [r for r in member.roles
                if not r.is_default() and r != prison_role and not r.managed and r < top]

    async def lockdown(self, guild: discord.Guild, *, only=None) -> int:
        """Denies the prison role View Channel everywhere except the prison channel. `only`: a single channel (new
        channel). Returns how many channels were changed."""
        role, prison_channel = self.role(guild), self.channel(guild)
        if role is None:
            return 0
        targets = [only] if only is not None else list(guild.channels)
        changed = 0
        for ch in targets:
            allow = prison_channel is not None and ch.id == prison_channel.id
            current = ch.overwrites_for(role)
            want = True if allow else False
            if current.view_channel is want and (not allow or current.send_messages):
                continue
            try:
                if allow:
                    await ch.set_permissions(role, view_channel=True, send_messages=True, read_message_history=True,
                                             reason="Prison channel")
                else:
                    await ch.set_permissions(role, view_channel=False, reason="Prison lockdown")
                changed += 1
            except discord.HTTPException as exc:
                log.warning("Prison lockdown of #%s failed: %s", ch, exc)
        return changed

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        if self.role(channel.guild) is not None and self.bot.db.prisoners(channel.guild.id):
            await self.lockdown(channel.guild, only=channel)

    # ------------------------------------------------------------ Jail / free (shared by commands and the AI)

    async def jail_member(self, guild: discord.Guild, member: discord.Member, reason: str | None, by: str, *,
                          minutes: int | None = None, announce: bool = True) -> str | None:
        """Puts a member in prison. `minutes` (if given) makes it a timed jail (auto-released later).
        Returns None on success or a short error string. Raises nothing – callers get the message."""
        error = self._setup_error(guild)
        if error:
            return error
        role = self.role(guild)
        if self.bot.db.prisoner(guild.id, member.id) or role in member.roles:
            return "already in prison"
        if member.top_role >= guild.me.top_role:
            return f"{member}'s highest role is above the bot's – I can't change their roles."
        minutes = max(1, min(int(minutes), MAX_JAIL_MINUTES)) if minutes else None
        expires_at = int(time.time()) + minutes * 60 if minutes else None
        removed = self._removable(member, role)
        self.bot.db.jail(guild.id, member.id, [r.id for r in removed], (reason or "").strip()[:400] or None, by,
                         expires_at)
        try:
            await member.add_roles(role, reason=f"Jailed by {by}: {reason or '-'}"[:500])
            if removed:
                await member.remove_roles(*removed, reason=f"Jailed by {by}"[:500])
        except discord.Forbidden as exc:
            self.bot.db.free(guild.id, member.id)
            return explain_forbidden(exc, self.bot)
        await self.lockdown(guild)
        channel = self.channel(guild)
        until = f" for **{human_duration(minutes)}**" if minutes else ""
        self.bot.audit(f"🔒 {safe_md(by)} jailed {member.mention}{until} "
                       f"({safe_md(reason or 'no reason')}) – {len(removed)} role(s) removed")
        try:
            await member.send(embed=discord.Embed(
                title="🔒 You've been put in prison", colour=discord.Colour.red(),
                description=(f"On **{safe_md(guild.name)}** you can now only see {channel.mention} and your own "
                            "ticket." + (f"\nYou'll be let out automatically in **{human_duration(minutes)}**."
                                         if minutes else "") + (f"\n**Reason:** {safe_md(reason)}" if reason else ""))))
        except discord.HTTPException:
            pass
        if announce and hasattr(channel, "send"):
            await channel.send(f"🔒 {member.mention}, you're in prison{until}."
                               + (f" Reason: {safe_md(reason)}." if reason else "")
                               + " Staff will talk to you here.",
                               allowed_mentions=discord.AllowedMentions(users=[member]))
        return None

    async def free_member(self, guild: discord.Guild, member: discord.Member, by: str, *,
                          auto: bool = False) -> str | None:
        """Lets a member out and restores their roles. Returns None on success or a short error string."""
        role = self.role(guild)
        if self.bot.db.prisoner(guild.id, member.id) is None and (role is None or role not in member.roles):
            return "isn't in prison"
        role_ids = self.bot.db.free(guild.id, member.id)
        restored = [r for rid in role_ids if (r := guild.get_role(rid)) is not None
                    and r < guild.me.top_role and not r.managed]
        try:
            if role is not None and role in member.roles:
                await member.remove_roles(role, reason=f"Freed by {by}")
            if restored:
                await member.add_roles(*restored, reason=f"Freed by {by} – roles restored")
        except discord.Forbidden as exc:
            return explain_forbidden(exc, self.bot)
        self.bot.audit(f"🔓 {safe_md(by)} freed {member.mention} ({len(restored)} role(s) restored)")
        try:
            await member.send(embed=discord.Embed(
                title="🔓 You're out of prison", colour=discord.Colour.green(),
                description=f"Your time is up – you can use **{safe_md(guild.name)}** normally again." if auto
                else f"You can use **{safe_md(guild.name)}** normally again."))
        except discord.HTTPException:
            pass
        return None

    # ------------------------------------------------------------ Auto-release of timed jails

    @tasks.loop(seconds=60)
    async def auto_release(self) -> None:
        try:
            rows = self.bot.db.expired_prisoners()
        except Exception as exc:                                 # never let the loop die
            log.warning("Prison auto-release query failed: %s", exc)
            return
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                continue
            member = guild.get_member(row["user_id"])
            if member is None:
                self.bot.db.free(guild.id, row["user_id"])       # left the server – just clear the record
                continue
            error = await self.free_member(guild, member, "Prison timer", auto=True)
            if error is None:
                channel = self.channel(guild)
                if hasattr(channel, "send"):
                    try:
                        await channel.send(f"🔓 {member.mention}, your time is up – you're free to go.",
                                           allowed_mentions=discord.AllowedMentions(users=[member]))
                    except discord.HTTPException:
                        pass
            else:
                log.warning("Prison auto-release of %s failed: %s", member, error)

    @auto_release.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ Commands

    @prison.command(name="jail", description="Lock a member down: only the prison channel and their own ticket.")
    @app_commands.describe(member="Who to jail", reason="Why (shown to them and in the log)",
                           minutes="Optional: auto-release after this many minutes (leave empty for no timer)")
    @admin_only()
    async def jail(self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None,
                   minutes: app_commands.Range[int, 1, MAX_JAIL_MINUTES] | None = None) -> None:
        guild = interaction.guild
        if member.id == interaction.user.id:
            await interaction.response.send_message("You can't jail yourself.", ephemeral=True)
            return
        # Surface setup / state errors before deferring so the message is a reply, matching the old behaviour.
        pre = self._setup_error(guild)
        if pre:
            await interaction.response.send_message(f"⚠️ {pre}", ephemeral=True)
            return
        if self.bot.db.prisoner(guild.id, member.id) or self.role(guild) in member.roles:
            await interaction.response.send_message(f"{member.mention} is already in prison.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        error = await self.jail_member(guild, member, reason, str(interaction.user), minutes=minutes)
        if error:
            await interaction.followup.send(f"⚠️ {error}", ephemeral=True)
            return
        until = f" for {human_duration(minutes)}" if minutes else ""
        await interaction.followup.send(f"🔒 {member.mention} is in prison{until}. Freed with `/prison free`.",
                                        ephemeral=True)

    @prison.command(name="free", description="Let a member out of prison and give their roles back.")
    @app_commands.describe(member="Who to free")
    @admin_only()
    async def free(self, interaction: discord.Interaction, member: discord.Member) -> None:
        guild = interaction.guild
        role = self.role(guild)
        if self.bot.db.prisoner(guild.id, member.id) is None and (role is None or role not in member.roles):
            await interaction.response.send_message(f"{member.mention} isn't in prison.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        error = await self.free_member(guild, member, str(interaction.user))
        if error:
            await interaction.followup.send(f"⚠️ {error}", ephemeral=True)
            return
        await interaction.followup.send(f"🔓 {member.mention} is free.", ephemeral=True)

    @prison.command(name="list", description="Who is in prison.")
    @admin_only()
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        rows = self.bot.db.prisoners(interaction.guild.id)
        lines = [f"🔒 <@{r['user_id']}> – <t:{r['jailed_at']}:R> by {safe_md(r['jailed_by'] or '?')}"
                 + (f" · until <t:{r['expires_at']}:R>" if r["expires_at"] else "")
                 + (f" · {safe_md(r['reason'])}" if r["reason"] else "") for r in rows]
        embed = discord.Embed(title="🔒 Prison", colour=discord.Colour.red(),
                              description="\n".join(lines)[:4000] or "Nobody is in prison.")
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Prison(bot))
