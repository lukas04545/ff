"""Filling-up ping: pings an opt-in role when the server reaches a player count – people come when it's fun.

Members take the role themselves with /pingme on (and drop it with /pingme off). Each threshold pings at most once
per "Cooldown" hours.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting

COLOUR = discord.Colour(0xE67E22)


def parse_thresholds(text: str) -> list[int]:
    out = set()
    for part in str(text or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= 1000:
            out.add(int(part))
    return sorted(out)


class FillingPing(Plugin):
    NAME = "Server is filling up ping"
    ICON = "🔥"
    DESCRIPTION = ("Members opt in with /pingme on; when the server reaches e.g. 10 or 20 players, that role is "
                   "pinged so people join while it's lively.")
    SETTINGS = (
        Setting("role", "Role to ping", "role", help="Create a role like \"Server alerts\" (below the bot's role)."),
        Setting("channel", "Channel", "channel", help="Empty = events channel."),
        Setting("thresholds", "Ping at … players", "text", "10, 20", help="Comma-separated, e.g. 10, 20, 30."),
        Setting("cooldown", "Cooldown per threshold (hours)", "int", 12, min=1, max=168),
        Setting("message", "Message", "text", "🔥 **{server}** is filling up – **{players}** players online! Jump in.",
                help="{server}, {players} and {max} are replaced."),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.previous: int | None = None

    async def tick(self, count: int) -> int | None:
        """Checks a new player count; returns the threshold that was pinged."""
        before, self.previous = self.previous, count
        if before is None or count <= before:
            return None
        crossed = [t for t in parse_thresholds(self.setting("thresholds")) if before < t <= count]
        if not crossed or not self.setting("role"):
            return None
        threshold, now = crossed[-1], self.now()
        last = self.data.get(f"last:{threshold}", 0)
        if now - last < self.setting("cooldown") * 3600:
            return None
        self.data.set(f"last:{threshold}", now)
        info = self.bot.server_info or {}
        text = (self.setting("message").replace("{server}", self.server_name).replace("{players}", str(count))
                .replace("{max}", str(info.get("MaxPlayers") or "?")))
        message = await self.post(self.channel(self.setting("channel"), self.bot.config.events_channel_id),
                                  embed=discord.Embed(colour=COLOUR, description=text), ping_role=self.setting("role"))
        if message is not None:
            self.data.set("pings", int(self.data.get("pings", 0)) + 1)
        return threshold

    @tasks.loop(seconds=60)
    async def watch_loop(self) -> None:
        if self.bot.rcon.connected:
            await self.tick(len(self.bot.online_players))
        else:
            self.previous = None

    @app_commands.command(name="pingme", description="Get pinged when the server is filling up (on/off).")
    @app_commands.describe(enabled="on = get pinged, off = stop")
    @app_commands.guild_only()
    async def pingme(self, interaction: discord.Interaction, enabled: bool) -> None:
        role = interaction.guild.get_role(self.setting("role") or 0)
        if role is None:
            await interaction.response.send_message("This isn't set up yet – ask an admin.", ephemeral=True)
            return
        try:
            if enabled:
                await interaction.user.add_roles(role, reason="/pingme on")
                text = f"🔥 You'll be pinged ({role.mention}) when the server fills up. `/pingme off` to stop."
            else:
                await interaction.user.remove_roles(role, reason="/pingme off")
                text = "Okay, no more filling-up pings."
        except discord.Forbidden:
            text = "⚠️ The bot can't give that role (it must be below the bot's own role)."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    def problems(self) -> list[str]:
        out = []
        if not self.setting("role"):
            out.append("Choose the role to ping.")
        if not parse_thresholds(self.setting("thresholds")):
            out.append("Enter at least one player count, e.g. 10, 20.")
        return out

    def status(self) -> dict:
        return {"Pings sent": int(self.data.get("pings", 0))}
