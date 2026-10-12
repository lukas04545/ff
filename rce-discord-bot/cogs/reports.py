"""Player reports and the staff watchlist.

/report <player> <reason>   – everyone: reports an in-game player to staff (REPORT_CHANNEL_ID, support roles
                              are pinged). Staff closes it with the "✅ Handled" button.
/watch add|remove|list      – admins: players to watch; staff is pinged when one of them joins the server
                              (e.g. a ban evader with a known name).
Both also in the web interface (🚩 Reports).
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from utils import admin_only, is_admin, player_choices, safe_md

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.reports")

REPORT_COOLDOWN = 120        # seconds between two reports of the same member
WATCH_ALERT_EVERY = 1800     # a watched player re-joining doesn't ping again within 30 minutes
COLOUR_OPEN, COLOUR_DONE = discord.Colour.red(), discord.Colour.dark_grey()


class ReportView(discord.ui.View):
    """Persistent "Handled" button – one custom_id for all reports, matched via the message."""

    def __init__(self, cog: "Reports") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Handled", emoji="✅", style=discord.ButtonStyle.success, custom_id="rce:report:handled")
    async def handled(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.cog.is_staff(interaction.user):
            await interaction.response.send_message("Only staff can close reports.", ephemeral=True)
            return
        row = self.cog.bot.db.report_by_message(interaction.message.id) if interaction.message else None
        if row is None:
            await interaction.response.send_message("Report not found.", ephemeral=True)
            return
        self.cog.bot.db.handle_report(row["id"], str(interaction.user))
        row = self.cog.bot.db.report(row["id"])
        await interaction.response.edit_message(embed=self.cog.report_embed(row), view=None)


class Reports(commands.Cog):
    watch = app_commands.Group(name="watch", description="Watchlist: get pinged when these players join",
                               guild_only=True)

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._last_report: dict[int, float] = {}

    async def cog_load(self) -> None:
        self.bot.add_view(ReportView(self))

    # ------------------------------------------------------------ Helpers

    def is_staff(self, user: discord.abc.User) -> bool:
        if is_admin(self.bot, user):
            return True
        roles = {r.id for r in getattr(user, "roles", [])}
        return bool(roles & set(self.bot.config.support_role_ids))

    def _channel(self) -> discord.abc.Messageable | None:
        cfg = self.bot.config
        channel_id = cfg.report_channel_id or cfg.alert_channel_id or cfg.admin_log_channel_id
        channel = self.bot.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.abc.Messageable) else None

    def _ping(self) -> tuple[str | None, discord.AllowedMentions]:
        roles = [discord.Object(r) for r in self.bot.config.support_role_ids]
        return (" ".join(f"<@&{r.id}>" for r in roles) or None), discord.AllowedMentions(roles=roles)

    def report_embed(self, row) -> discord.Embed:
        handled = row["status"] == "handled"
        embed = discord.Embed(title=f"🚩 Report #{row['id']}" + (" – handled" if handled else ""),
                              colour=COLOUR_DONE if handled else COLOUR_OPEN, description=safe_md(row["reason"]))
        online = " 🟢 online" if row["player"] in self.bot.online_players else ""
        embed.add_field(name="Player", value=f"**{safe_md(row['player'])}**{online}")
        embed.add_field(name="Reported by", value=f"<@{row['reporter_id']}>")
        count = self.bot.db.reports_by_player(row["player"])
        if count > 1:
            embed.add_field(name="Reports about this player", value=str(count))
        if handled:
            embed.add_field(name="Handled by", value=safe_md(row["handled_by"] or "?"), inline=False)
        embed.set_footer(text=f"Reported {row['created_at']} UTC")
        return embed

    # ------------------------------------------------------------ Reports

    async def create_report(self, member: discord.abc.User, player: str, reason: str) -> tuple[bool, str]:
        now = time.monotonic()
        if now - self._last_report.get(member.id, -REPORT_COOLDOWN) < REPORT_COOLDOWN:
            return False, "⏳ Please wait a moment before sending another report."
        channel = self._channel()
        if channel is None:
            return False, "Reports aren't set up yet – please open a ticket instead."
        self._last_report[member.id] = now
        report_id = self.bot.db.add_report(member.id, player, reason)
        content, mentions = self._ping()
        try:
            msg = await channel.send(content=content, embed=self.report_embed(self.bot.db.report(report_id)),
                                     view=ReportView(self), allowed_mentions=mentions)
            self.bot.db.set_report_message(report_id, msg.channel.id, msg.id)
        except discord.HTTPException as exc:
            log.warning("Report #%s could not be posted: %s", report_id, exc)
        return True, f"✅ Thanks – report #{report_id} about **{safe_md(player)}** was sent to the staff."

    async def mark_handled(self, report_id: int, by: str) -> bool:
        """Closes a report (web interface) and updates its Discord message."""
        if not self.bot.db.handle_report(report_id, by):
            return False
        row = self.bot.db.report(report_id)
        channel = self.bot.get_channel(row["channel_id"]) if row["channel_id"] else None
        if channel is not None and row["message_id"]:
            try:
                msg = await channel.fetch_message(row["message_id"])
                await msg.edit(embed=self.report_embed(row), view=None)
            except discord.HTTPException:
                pass
        return True

    @app_commands.command(name="report", description="Report an in-game player to the staff.")
    @app_commands.describe(player="In-game name", reason="What happened? (where, when, what)")
    @app_commands.guild_only()
    async def report(self, interaction: discord.Interaction, player: app_commands.Range[str, 1, 64],
                     reason: app_commands.Range[str, 5, 500]) -> None:
        ok, text = await self.create_report(interaction.user, player.strip(), reason.strip())
        await interaction.response.send_message(text, ephemeral=True)

    @report.autocomplete("player")
    async def _player_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return player_choices(self.bot, current)

    # ------------------------------------------------------------ Watchlist

    @commands.Cog.listener()
    async def on_rce_player_join(self, name: str) -> None:
        row = self.bot.db.watched(name)
        now = int(time.time())
        if row is None or (row["last_alert"] and now - row["last_alert"] < WATCH_ALERT_EVERY):
            return
        self.bot.db.mark_watch_alert(row["player"], now)
        channel = self._channel()
        if channel is None:
            return
        embed = discord.Embed(title="👀 Watched player joined", colour=discord.Colour.orange(),
                              description=f"**{safe_md(name)}** is on the server now.")
        if row["reason"]:
            embed.add_field(name="Why watched", value=safe_md(row["reason"])[:1000], inline=False)
        embed.set_footer(text=f"On the watchlist since {row['created_at']} UTC (added by {row['added_by']})")
        content, mentions = self._ping()
        try:
            await channel.send(content=content, embed=embed, allowed_mentions=mentions)
        except discord.HTTPException as exc:
            log.warning("Watchlist alert failed: %s", exc)

    @watch.command(name="add", description="Get pinged when this player joins the server.")
    @app_commands.describe(player="In-game name (exact, upper/lower case doesn't matter)", reason="Why")
    @admin_only()
    async def watch_add(self, interaction: discord.Interaction, player: app_commands.Range[str, 1, 64],
                        reason: str | None = None) -> None:
        self.bot.db.watch(player.strip(), reason, str(interaction.user))
        self.bot.audit(f"👀 {safe_md(str(interaction.user))} put **{safe_md(player)}** on the watchlist")
        await interaction.response.send_message(f"✅ **{safe_md(player)}** is on the watchlist.", ephemeral=True)

    @watch.command(name="remove", description="Removes a player from the watchlist.")
    @app_commands.describe(player="In-game name")
    @admin_only()
    async def watch_remove(self, interaction: discord.Interaction, player: str) -> None:
        ok = self.bot.db.unwatch(player.strip())
        await interaction.response.send_message(
            f"✅ **{safe_md(player)}** removed." if ok else "That player isn't on the watchlist.", ephemeral=True)

    @watch.command(name="list", description="Shows the watchlist.")
    @admin_only()
    async def watch_list(self, interaction: discord.Interaction) -> None:
        rows = self.bot.db.watchlist()
        lines = [f"• **{safe_md(r['player'])}**{' 🟢' if r['player'] in self.bot.online_players else ''}"
                 + (f" – {safe_md(r['reason'])}" if r["reason"] else "") for r in rows]
        await interaction.response.send_message(
            embed=discord.Embed(title="👀 Watchlist", description="\n".join(lines)[:4000] or "Empty.",
                                colour=discord.Colour.orange()), ephemeral=True)

    @watch_add.autocomplete("player")
    async def _watch_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return player_choices(self.bot, current)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Reports(bot))
