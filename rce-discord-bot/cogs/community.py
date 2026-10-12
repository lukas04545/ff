"""Community features: /ping, /rust, /rules, welcome message, auto role, Discord log.

Discord log (DISCORD_LOG_CHANNEL_ID): joins/leaves, deleted and edited messages.
Note: Discord only reports deleted/edited messages with their content if the bot
has seen them since it started (message cache).
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from rce.rcon_client import RconError
from utils import safe_md, strip_rich_text

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.community")

COLOR_JOIN = discord.Colour.green()
COLOR_LEAVE = discord.Colour.dark_grey()
COLOR_MESSAGE = discord.Colour.orange()


def load_rules(path: Path) -> list[str]:
    """Rules from the file: one rule per line, '#' = comment, numbering is ignored."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("<!--"):
            continue
        rules.append(re.sub(r"^(\d+[.)]|[-*•])\s*", "", line))
    return rules


def render_welcome(template: str, *, member_mention: str, server: str, count: int, rules_channel_id: int | None) -> str:
    rules = f" in <#{rules_channel_id}>" if rules_channel_id else ""
    return (template.replace("{member}", member_mention).replace("{server}", server)
            .replace("{count}", str(count)).replace("{rules}", rules))


class Community(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._role_warned = False

    # ------------------------------------------------------------ Helpers

    def _server_name(self, guild: discord.Guild | None = None) -> str:
        info = self.bot.server_info or {}
        return (self.bot.config.server_display_name or strip_rich_text(str(info.get("Hostname", "")))
                or (guild.name if guild else "") or "Rust Console Server")

    def discord_log(self, embed: discord.Embed) -> None:
        self.bot.discord_log(embed)

    # ------------------------------------------------------------ Commands

    @app_commands.command(name="ping", description="Checks whether the bot and the server connection are working.")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        lines = [f"🏓 Discord: **{round(self.bot.latency * 1000)} ms**"]
        if self.bot.rcon.connected:
            start = time.perf_counter()
            try:
                await self.bot.rcon.command("serverinfo")
                lines.append(f"🛢️ RCON: **{round((time.perf_counter() - start) * 1000)} ms**")
            except RconError as exc:
                lines.append(f"🛢️ RCON: ⚠️ {exc}")
        else:
            lines.append("🛢️ RCON: 🔴 not connected")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="rust", description="Info about the Rust server (platform, wipe, status).")
    async def rust(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.config
        info = self.bot.server_info
        embed = discord.Embed(title=self._server_name(interaction.guild)[:256], colour=discord.Colour(0xCE422B))
        embed.add_field(name="Platform", value=cfg.rust_server_platform[:1024])
        wipe = self.bot.get_cog("Wipe")
        embed.add_field(name="Wipe", value=(wipe.next_text() if wipe else None) or cfg.rust_server_wipe[:1024])
        if self.bot.rcon.connected and info:
            status = f"🟢 Online – {info.get('Players', '?')}/{info.get('MaxPlayers', '?')} players"
            if info.get("Queued"):
                status += f" (+{info['Queued']} in queue)"
        else:
            status = "🔴 Offline / no connection"
        embed.add_field(name="Status", value=status, inline=False)
        rules = f"<#{cfg.rules_channel_id}>" if cfg.rules_channel_id else "`/rules`"
        embed.add_field(name="Rules", value=rules)
        embed.add_field(name="Help", value="`/ticket open`")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="rules", description="Shows the server rules.")
    async def rules(self, interaction: discord.Interaction) -> None:
        rules = load_rules(self.bot.config.rules_file)
        if not rules:
            await interaction.response.send_message(
                "No rules have been set yet (file `rules.md` or web interface → Rules).", ephemeral=True)
            return
        text = "\n".join(f"**{i}.** {r}" for i, r in enumerate(rules, 1))
        embed = discord.Embed(title=f"📜 Rules – {self._server_name(interaction.guild)}"[:256],
                              description=text[:4000], colour=discord.Colour(0xCE422B))
        if self.bot.config.rules_channel_id:
            embed.set_footer(text="Full details in the rules channel")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------ Join / leave

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        cfg = self.bot.config
        if cfg.auto_role_id and not member.bot:
            await self._give_auto_role(member)

        if cfg.welcome_channel_id:
            channel = member.guild.get_channel(cfg.welcome_channel_id)
            if isinstance(channel, discord.abc.Messageable):
                text = render_welcome(cfg.welcome_message, member_mention=member.mention,
                                      server=self._server_name(member.guild), count=member.guild.member_count or 0,
                                      rules_channel_id=cfg.rules_channel_id)
                try:
                    await channel.send(text, allowed_mentions=discord.AllowedMentions(users=[member]))
                except discord.HTTPException as exc:
                    log.warning("Welcome message failed: %s", exc)

        embed = discord.Embed(title="📥 Member joined", colour=COLOR_JOIN,
                              description=f"{member.mention} ({safe_md(str(member))}, ID {member.id})")
        embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
        embed.set_thumbnail(url=member.display_avatar.url)
        self.discord_log(embed)

    async def _give_auto_role(self, member: discord.Member) -> None:
        role = member.guild.get_role(self.bot.config.auto_role_id)
        if role is None:
            if not self._role_warned:
                log.error("AUTO_ROLE_ID %s does not exist on this server.", self.bot.config.auto_role_id)
                self._role_warned = True
            return
        if role >= member.guild.me.top_role:
            if not self._role_warned:
                log.error("Auto role '%s' is above the bot's highest role – drag the bot role higher in the server "
                          "settings.", role.name)
                self._role_warned = True
            return
        try:
            await member.add_roles(role, reason="Auto role for new members")
        except discord.HTTPException as exc:
            log.warning("Auto role for %s failed: %s", member, exc)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        embed = discord.Embed(title="📤 Member left the server", colour=COLOR_LEAVE,
                              description=f"{safe_md(str(member))} (ID {member.id})")
        if member.joined_at:
            embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"))
        self.discord_log(embed)

    # ------------------------------------------------------------ Message log

    def _skip_message(self, message: discord.Message) -> bool:
        return (message.guild is None or message.author.bot
                or message.channel.id == self.bot.config.discord_log_channel_id)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if self._skip_message(message) or not self.bot.config.discord_log_channel_id:
            return
        content = message.content or "*(no text)*"
        if message.attachments:
            content += "\n" + "\n".join(f"📎 {a.filename}" for a in message.attachments)
        embed = discord.Embed(title="🗑️ Message deleted", colour=COLOR_MESSAGE, description=content[:3500])
        embed.add_field(name="Author", value=f"{message.author.mention} ({safe_md(str(message.author))})")
        embed.add_field(name="Channel", value=message.channel.mention)
        self.discord_log(embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if self._skip_message(after) or before.content == after.content or not self.bot.config.discord_log_channel_id:
            return
        embed = discord.Embed(title="✏️ Message edited", colour=COLOR_MESSAGE, url=after.jump_url)
        embed.add_field(name="Before", value=(before.content or "*(empty)*")[:1000], inline=False)
        embed.add_field(name="After", value=(after.content or "*(empty)*")[:1000], inline=False)
        embed.add_field(name="Author", value=f"{after.author.mention} ({safe_md(str(after.author))})")
        embed.add_field(name="Channel", value=after.channel.mention)
        self.discord_log(embed)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Community(bot))
