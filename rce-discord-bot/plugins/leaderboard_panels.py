"""Live leaderboard panels: a leaderboard message that keeps itself up to date (edited every few minutes).

/leaderboard-panel <category> [channel]   posts one (kills, K/D, playtime, deaths, points, clans) – delete the
                                          message to stop it.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from utils import admin_only, format_duration, safe_md

MEDALS = ("🥇", "🥈", "🥉")
CATEGORIES = {"kills": "Kills", "kd": "K/D", "playtime": "Playtime", "deaths": "Deaths", "points": "Points",
              "clans": "Clans"}


class LeaderboardPanels(Plugin):
    NAME = "Live leaderboards"
    ICON = "🏆"
    DESCRIPTION = "Leaderboard messages that update themselves – kills, K/D, playtime, deaths, points or clans."
    SETTINGS = (
        Setting("minutes", "Update every … minutes", "int", 5, min=1, max=120),
        Setting("size", "Places shown", "int", 10, min=3, max=25),
    )

    def panels(self) -> list[dict]:
        return list(self.data.get("panels", []))

    def embed(self, category: str) -> discord.Embed:
        size = self.setting("size")
        lines = []
        if category == "points":
            rows = self.bot.db.top_wallets(size)
            lines = [f"<@{r['user_id']}> – {self.fmt_points(r['points'])}" for r in rows]
        elif category == "clans":
            clans = self.plugin("clans")
            if clans is not None:
                scored = sorted(((c, clans.score(c)) for c in clans.clans().values()), key=lambda r: -r[1]["kills"])
                lines = [f"**[{c['tag']}] {safe_md(c['name'])}** – {s['kills']} kills · K/D {s['kd']}"
                         for c, s in scored[:size]]
        else:
            for row in self.bot.db.leaderboard(category, limit=size):
                if category == "playtime":
                    value = format_duration(row["playtime_seconds"])
                else:
                    value = f"{row['kills']} kills · {row['deaths']} deaths · K/D " \
                            f"{round(row['kills'] / max(row['deaths'], 1), 2)}"
                lines.append(f"**{safe_md(row['name'])}** – {value}")
        lines = [f"{MEDALS[i] if i < 3 else f'`{i + 1}.`'} {line}" for i, line in enumerate(lines)]
        embed = discord.Embed(title=f"🏆 Leaderboard – {CATEGORIES[category]}", colour=discord.Colour.gold(),
                              description="\n".join(lines) or "No data yet.")
        embed.set_footer(text=f"Updates every {self.setting('minutes')} min · last update")
        embed.timestamp = discord.utils.utcnow()
        return embed

    async def refresh(self) -> int:
        keep, edited = [], 0
        for p in self.panels():
            channel = self.bot.get_channel(p["channel_id"])
            if channel is None:
                keep.append(p)
                continue
            try:
                await channel.get_partial_message(p["message_id"]).edit(embed=self.embed(p["category"]),
                                                                        allowed_mentions=discord.AllowedMentions.none())
                keep.append(p)
                edited += 1
            except discord.NotFound:
                pass                                       # deleted: forget it
            except discord.HTTPException as exc:
                self.log.warning("Leaderboard panel: %s", exc)
                keep.append(p)
        if len(keep) != len(self.panels()):
            self.data.set("panels", keep)
        return edited

    @tasks.loop(minutes=1)
    async def refresh_loop(self) -> None:
        tick = int(self.data.get("tick", 0)) + 1
        self.data.set("tick", tick)
        if tick % self.setting("minutes") == 0 and self.panels():
            await self.refresh()

    @app_commands.command(name="leaderboard-panel", description="Posts a leaderboard that updates itself (admins).")
    @app_commands.choices(category=[app_commands.Choice(name=v, value=k) for k, v in CATEGORIES.items()])
    @app_commands.guild_only()
    @admin_only()
    async def panel_cmd(self, interaction: discord.Interaction, category: app_commands.Choice[str],
                        channel: discord.TextChannel | None = None) -> None:
        target = channel or interaction.channel
        await interaction.response.defer(ephemeral=True)
        try:
            msg = await target.send(embed=self.embed(category.value), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            await interaction.followup.send(f"⚠️ Couldn't post there: {exc}", ephemeral=True)
            return
        self.data.set("panels", self.panels() + [{"channel_id": target.id, "message_id": msg.id,
                                                  "category": category.value}])
        await interaction.followup.send(f"✅ Live leaderboard posted: {msg.jump_url} – delete the message to stop it.",
                                        ephemeral=True)

    def status(self) -> dict:
        return {"Panels": len(self.panels())}
