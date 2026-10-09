"""Community-Funktionen: /ping, /rust, /regeln, Willkommensnachricht, Auto-Rolle, Discord-Log.

Discord-Log (DISCORD_LOG_CHANNEL_ID): Beitritte/Austritte, gelöschte und bearbeitete
Nachrichten. Hinweis: Discord meldet gelöschte/bearbeitete Nachrichten nur mit Inhalt,
wenn der Bot sie seit seinem Start gesehen hat (Nachrichten-Cache).
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
    """Regeln aus der Datei: eine Regel pro Zeile, '#' = Kommentar, Nummerierung wird ignoriert."""
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

    # ------------------------------------------------------------ Hilfen

    def _server_name(self, guild: discord.Guild | None = None) -> str:
        info = self.bot.server_info or {}
        return (self.bot.config.server_display_name or strip_rich_text(str(info.get("Hostname", "")))
                or (guild.name if guild else "") or "Rust Console Server")

    def discord_log(self, embed: discord.Embed) -> None:
        self.bot.discord_log(embed)

    # ------------------------------------------------------------ Befehle

    @app_commands.command(name="ping", description="Prüft, ob der Bot und die Server-Verbindung laufen.")
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
            lines.append("🛢️ RCON: 🔴 nicht verbunden")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="rust", description="Infos zum Rust-Server (Plattform, Wipe, Status).")
    async def rust(self, interaction: discord.Interaction) -> None:
        cfg = self.bot.config
        info = self.bot.server_info
        embed = discord.Embed(title=self._server_name(interaction.guild)[:256], colour=discord.Colour(0xCE422B))
        embed.add_field(name="Plattform", value=cfg.rust_server_platform[:1024])
        embed.add_field(name="Wipe", value=cfg.rust_server_wipe[:1024])
        if self.bot.rcon.connected and info:
            status = f"🟢 Online – {info.get('Players', '?')}/{info.get('MaxPlayers', '?')} Spieler"
            if info.get("Queued"):
                status += f" (+{info['Queued']} in der Warteschlange)"
        else:
            status = "🔴 Offline / keine Verbindung"
        embed.add_field(name="Status", value=status, inline=False)
        rules = f"<#{cfg.rules_channel_id}>" if cfg.rules_channel_id else "`/regeln`"
        embed.add_field(name="Regeln", value=rules)
        embed.add_field(name="Hilfe", value="`/ticket oeffnen`")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="regeln", description="Zeigt die Serverregeln.")
    async def rules(self, interaction: discord.Interaction) -> None:
        rules = load_rules(self.bot.config.rules_file)
        if not rules:
            await interaction.response.send_message(
                "Es sind noch keine Regeln hinterlegt (Datei `rules.md` bzw. Webinterface → Regeln).", ephemeral=True)
            return
        text = "\n".join(f"**{i}.** {r}" for i, r in enumerate(rules, 1))
        embed = discord.Embed(title=f"📜 Regeln – {self._server_name(interaction.guild)}"[:256],
                              description=text[:4000], colour=discord.Colour(0xCE422B))
        if self.bot.config.rules_channel_id:
            embed.set_footer(text="Ausführlich im Regel-Channel")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------ Beitritt / Austritt

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
                    log.warning("Willkommensnachricht fehlgeschlagen: %s", exc)

        embed = discord.Embed(title="📥 Mitglied beigetreten", colour=COLOR_JOIN,
                              description=f"{member.mention} ({safe_md(str(member))}, ID {member.id})")
        embed.add_field(name="Account erstellt", value=discord.utils.format_dt(member.created_at, "R"))
        embed.set_thumbnail(url=member.display_avatar.url)
        self.discord_log(embed)

    async def _give_auto_role(self, member: discord.Member) -> None:
        role = member.guild.get_role(self.bot.config.auto_role_id)
        if role is None:
            if not self._role_warned:
                log.error("AUTO_ROLE_ID %s gibt es auf diesem Server nicht.", self.bot.config.auto_role_id)
                self._role_warned = True
            return
        if role >= member.guild.me.top_role:
            if not self._role_warned:
                log.error("Auto-Rolle '%s' steht über der höchsten Bot-Rolle – Bot-Rolle in den Servereinstellungen "
                          "nach oben ziehen.", role.name)
                self._role_warned = True
            return
        try:
            await member.add_roles(role, reason="Auto-Rolle für neue Mitglieder")
        except discord.HTTPException as exc:
            log.warning("Auto-Rolle für %s fehlgeschlagen: %s", member, exc)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        embed = discord.Embed(title="📤 Mitglied hat den Server verlassen", colour=COLOR_LEAVE,
                              description=f"{safe_md(str(member))} (ID {member.id})")
        if member.joined_at:
            embed.add_field(name="Beigetreten", value=discord.utils.format_dt(member.joined_at, "R"))
        self.discord_log(embed)

    # ------------------------------------------------------------ Nachrichten-Log

    def _skip_message(self, message: discord.Message) -> bool:
        return (message.guild is None or message.author.bot
                or message.channel.id == self.bot.config.discord_log_channel_id)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if self._skip_message(message) or not self.bot.config.discord_log_channel_id:
            return
        content = message.content or "*(kein Text)*"
        if message.attachments:
            content += "\n" + "\n".join(f"📎 {a.filename}" for a in message.attachments)
        embed = discord.Embed(title="🗑️ Nachricht gelöscht", colour=COLOR_MESSAGE, description=content[:3500])
        embed.add_field(name="Autor", value=f"{message.author.mention} ({safe_md(str(message.author))})")
        embed.add_field(name="Channel", value=message.channel.mention)
        self.discord_log(embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if self._skip_message(after) or before.content == after.content or not self.bot.config.discord_log_channel_id:
            return
        embed = discord.Embed(title="✏️ Nachricht bearbeitet", colour=COLOR_MESSAGE, url=after.jump_url)
        embed.add_field(name="Vorher", value=(before.content or "*(leer)*")[:1000], inline=False)
        embed.add_field(name="Nachher", value=(after.content or "*(leer)*")[:1000], inline=False)
        embed.add_field(name="Autor", value=f"{after.author.mention} ({safe_md(str(after.author))})")
        embed.add_field(name="Channel", value=after.channel.mention)
        self.discord_log(embed)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Community(bot))
