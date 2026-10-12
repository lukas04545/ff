"""Leaderboards & player stats (collected from the killfeed and player list).

Note: counting only starts once the bot is running – earlier kills cannot be
retrieved via RCON.
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
    "deaths": "Deaths",
    "kd": "K/D",
    "playtime": "Playtime",
}
MEDALS = ["🥇", "🥈", "🥉"]


def _kd(kills: int, deaths: int) -> str:
    return f"{kills / max(deaths, 1):.2f}"


TEAM_LABELS = {"kills": "Kills", "kd": "K/D", "playtime": "Playtime", "members": "Members"}


class Stats(commands.Cog):
    teams = app_commands.Group(name="teams", description="In-game teams (from the team log)")

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_rce_team(self, ev) -> None:
        """Team membership for the team leaderboard (only known from the moment the bot runs)."""
        self.bot.db.team_event(ev.kind, ev.player, ev.team_id, ev.other)

    @teams.command(name="top", description="Team leaderboard (teams with at least 2 members).")
    @app_commands.describe(category="What to rank by")
    @app_commands.choices(category=[app_commands.Choice(name=v, value=k) for k, v in TEAM_LABELS.items()])
    async def teams_top(self, interaction: discord.Interaction,
                        category: app_commands.Choice[str] | None = None) -> None:
        key = category.value if category else "kills"
        rows = self.bot.db.team_leaderboard(key, limit=10)
        if not rows:
            await interaction.response.send_message(
                "No teams known yet – teams are picked up from the server log as players create/join them.",
                ephemeral=True)
            return
        lines = []
        for i, r in enumerate(rows):
            rank = MEDALS[i] if i < len(MEDALS) else f"`{i + 1}.`"
            value = (format_duration(r["playtime"]) if key == "playtime" else
                     f"{r['kills']} kills · K/D {_kd(r['kills'], r['deaths'])}")
            names = r["names"] if len(r["names"]) <= 80 else r["names"][:80] + " …"
            lines.append(f"{rank} **Team {safe_md(r['leader'] or '?')}** ({r['members']}) – {value}\n"
                         f"  ↳ {safe_md(names)}")
        embed = discord.Embed(title=f"👥 Team leaderboard – {TEAM_LABELS[key]}", description="\n".join(lines)[:4000],
                              colour=discord.Colour.gold())
        embed.set_footer(text="Teams are named after their leader · current members, their stats summed up")
        await interaction.response.send_message(embed=embed)

    @teams.command(name="of", description="Shows a player's team.")
    @app_commands.describe(player="In-game name")
    async def teams_of(self, interaction: discord.Interaction, player: str) -> None:
        found = self.bot.db.team_of(player.strip())
        if found is None:
            await interaction.response.send_message(f"**{safe_md(player)}** is in no known team.", ephemeral=True)
            return
        team, members = found
        lines = [("👑 " if m.lower() == (team["leader"] or "").lower() else "• ") + safe_md(m)
                 + (" 🟢" if m in self.bot.online_players else "") for m in members]
        embed = discord.Embed(title=f"👥 Team {team['leader'] or team['team_id']}", description="\n".join(lines),
                              colour=discord.Colour.blurple())
        embed.set_footer(text=f"Team ID {team['team_id']}")
        await interaction.response.send_message(embed=embed)

    @teams_of.autocomplete("player")
    async def _team_player_ac(self, interaction: discord.Interaction, current: str
                              ) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in self.bot.db.search_names(current)]

    @app_commands.command(name="leaderboard", description="Server leaderboard.")
    @app_commands.describe(category="What to rank by")
    @app_commands.choices(category=[app_commands.Choice(name=v, value=k) for k, v in CATEGORY_LABELS.items()])
    async def leaderboard(self, interaction: discord.Interaction,
                          category: app_commands.Choice[str] | None = None) -> None:
        category_key = category.value if category else "kills"
        rows = self.bot.db.leaderboard(category_key, limit=10)
        if not rows:
            await interaction.response.send_message("No data collected yet.", ephemeral=True)
            return

        lines = []
        for i, row in enumerate(rows):
            rank = MEDALS[i] if i < len(MEDALS) else f"`{i + 1}.`"
            if category_key == "playtime":
                value = format_duration(row["playtime_seconds"])
            else:
                value = f"{row['kills']} kills · {row['deaths']} deaths · K/D {_kd(row['kills'], row['deaths'])}"
            lines.append(f"{rank} **{safe_md(row['name'])}** – {value}")

        embed = discord.Embed(title=f"🏆 Leaderboard – {CATEGORY_LABELS[category_key]}",
                              description="\n".join(lines), colour=discord.Colour.gold())
        embed.set_footer(text="PvP kills from the killfeed · playtime from the player list")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="stats", description="Stats for a player.")
    @app_commands.describe(player="In-game name")
    async def stats(self, interaction: discord.Interaction, player: str) -> None:
        row = self.bot.db.player(player.strip())
        if row is None:
            await interaction.response.send_message(f"No data found for **{safe_md(player)}**.",
                                                    ephemeral=True)
            return
        online = row["name"] in self.bot.online_players
        embed = discord.Embed(title=f"📊 {row['name']}", colour=discord.Colour.blurple())
        embed.add_field(name="Kills", value=str(row["kills"]))
        embed.add_field(name="Deaths", value=str(row["deaths"]))
        embed.add_field(name="K/D", value=_kd(row["kills"], row["deaths"]))
        embed.add_field(name="Suicides", value=str(row["suicides"]))
        embed.add_field(name="Playtime", value=format_duration(row["playtime_seconds"]))
        embed.add_field(name="Platform", value=row["platform"] or "unknown")
        embed.add_field(name="Status", value="🟢 online" if online else f"⚪ last seen {row['last_seen']} UTC",
                        inline=False)
        await interaction.response.send_message(embed=embed)

    @stats.autocomplete("player")
    async def _stats_autocomplete(self, interaction: discord.Interaction, current: str
                                  ) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in self.bot.db.search_names(current)]


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Stats(bot))
