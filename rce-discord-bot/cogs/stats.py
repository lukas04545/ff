"""Leaderboards & Spielerstatistiken (aus Killfeed und Spielerliste gesammelt).

Hinweis: Gezählt wird erst ab dem Zeitpunkt, an dem der Bot läuft – frühere
Kills kann man über RCON nicht abrufen.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from utils import format_duration, safe_md

if TYPE_CHECKING:
    from bot import RceBot

CATEGORY_LABELS = {
    "kills": "Kills",
    "deaths": "Tode",
    "kd": "K/D",
    "playtime": "Spielzeit",
}
MEDALS = ["🥇", "🥈", "🥉"]


def _kd(kills: int, deaths: int) -> str:
    return f"{kills / max(deaths, 1):.2f}"


class Stats(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot

    @app_commands.command(name="leaderboard", description="Bestenliste des Servers.")
    @app_commands.describe(kategorie="Wonach sortiert wird")
    @app_commands.choices(kategorie=[app_commands.Choice(name=v, value=k) for k, v in CATEGORY_LABELS.items()])
    async def leaderboard(self, interaction: discord.Interaction,
                          kategorie: app_commands.Choice[str] | None = None) -> None:
        category = kategorie.value if kategorie else "kills"
        rows = self.bot.db.leaderboard(category, limit=10)
        if not rows:
            await interaction.response.send_message("Noch keine Daten gesammelt.", ephemeral=True)
            return

        lines = []
        for i, row in enumerate(rows):
            rank = MEDALS[i] if i < len(MEDALS) else f"`{i + 1}.`"
            if category == "playtime":
                value = format_duration(row["playtime_seconds"])
            else:
                value = f"{row['kills']} Kills · {row['deaths']} Tode · K/D {_kd(row['kills'], row['deaths'])}"
            lines.append(f"{rank} **{safe_md(row['name'])}** – {value}")

        embed = discord.Embed(title=f"🏆 Leaderboard – {CATEGORY_LABELS[category]}",
                              description="\n".join(lines), colour=discord.Colour.gold())
        embed.set_footer(text="PvP-Kills aus dem Killfeed · Spielzeit aus der Spielerliste")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="stats", description="Statistiken eines Spielers.")
    @app_commands.describe(spieler="Ingame-Name")
    async def stats(self, interaction: discord.Interaction, spieler: str) -> None:
        row = self.bot.db.player(spieler.strip())
        if row is None:
            await interaction.response.send_message(f"Keine Daten zu **{safe_md(spieler)}** gefunden.",
                                                    ephemeral=True)
            return
        online = row["name"] in self.bot.online_players
        embed = discord.Embed(title=f"📊 {row['name']}", colour=discord.Colour.blurple())
        embed.add_field(name="Kills", value=str(row["kills"]))
        embed.add_field(name="Tode", value=str(row["deaths"]))
        embed.add_field(name="K/D", value=_kd(row["kills"], row["deaths"]))
        embed.add_field(name="Selbstmorde", value=str(row["suicides"]))
        embed.add_field(name="Spielzeit", value=format_duration(row["playtime_seconds"]))
        embed.add_field(name="Plattform", value=row["platform"] or "unbekannt")
        embed.add_field(name="Status", value="🟢 online" if online else f"⚪ zuletzt gesehen {row['last_seen']} UTC",
                        inline=False)
        await interaction.response.send_message(embed=embed)

    @stats.autocomplete("spieler")
    async def _stats_autocomplete(self, interaction: discord.Interaction, current: str
                                  ) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in self.bot.db.search_names(current)]


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Stats(bot))
