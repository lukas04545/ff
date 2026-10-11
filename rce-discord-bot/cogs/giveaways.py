"""Giveaways with an entry button – stored in the database, so they survive bot restarts.

/giveaway start   – start a giveaway in this channel (Manage Server)
/giveaway end     – draw early
/giveaway reroll  – draw new winners
/giveaway list    – running and finished giveaways

Optionally, winners receive the GIVEAWAY_WINNER_ROLE_ID role.
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import safe_md

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.giveaways")

COLOUR = discord.Colour.gold()
CHECK_SECONDS = 15


def draw_winners(entries: list[int], count: int, exclude: set[int] | None = None) -> list[int]:
    """Draws up to count random, distinct winners (cryptographically secure randomness)."""
    pool = [e for e in dict.fromkeys(entries) if e not in (exclude or set())]
    rng = secrets.SystemRandom()
    return rng.sample(pool, min(count, len(pool)))


class GiveawayView(discord.ui.View):
    """Persistent entry button – one custom_id for all giveaways, matched via the message."""

    def __init__(self, cog: "Giveaways") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Enter", emoji="🎉", style=discord.ButtonStyle.success,
                       custom_id="rce:giveaway:enter")
    async def enter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handle_entry(interaction)


class LeaveView(discord.ui.View):
    def __init__(self, cog: "Giveaways", giveaway_id: int) -> None:
        super().__init__(timeout=60)
        self.cog, self.giveaway_id = cog, giveaway_id

    @discord.ui.button(label="Withdraw entry", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.cog.bot.db.remove_giveaway_entry(self.giveaway_id, interaction.user.id)
        await interaction.response.edit_message(content="You are no longer entered.", view=None)


class Giveaways(commands.Cog):
    giveaway = app_commands.Group(name="giveaway", description="Giveaways", guild_only=True,
                                  default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(GiveawayView(self))
        self.check_due.start()

    async def cog_unload(self) -> None:
        self.check_due.cancel()

    # ------------------------------------------------------------ Display

    def _embed(self, row, *, entries: int | None = None, winners: list[int] | None = None) -> discord.Embed:
        ended = bool(row["ended"]) or winners is not None
        embed = discord.Embed(title=f"🎉 Giveaway: {row['prize']}"[:256], colour=COLOUR if not ended else discord.Colour.dark_grey())
        lines = [f"**Prize:** {safe_md(row['prize'])}", f"**Hosted by:** <@{row['host_id']}>",
                 f"**Winners:** {row['winners']}"]
        if ended:
            names = ", ".join(f"<@{w}>" for w in winners or []) or "no valid entries"
            lines.append(f"**Ended** – winners: {names}")
        else:
            lines.append(f"**Ends:** <t:{row['ends_at']}:R> (<t:{row['ends_at']}:f>)")
            lines.append("\nClick **🎉 Enter** to join!")
        if entries is not None:
            lines.append(f"**Entries:** {entries}")
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Giveaway #{row['id']}")
        return embed

    # ------------------------------------------------------------ Flow

    async def handle_entry(self, interaction: discord.Interaction) -> None:
        row = self.bot.db.giveaway_by_message(interaction.message.id) if interaction.message else None
        if row is None or row["ended"] or row["ends_at"] <= time.time():
            await interaction.response.send_message("This giveaway has already ended.", ephemeral=True)
            return
        if interaction.user.bot:
            return
        if self.bot.db.add_giveaway_entry(row["id"], interaction.user.id):
            await interaction.response.send_message(f"🎉 You are entered in the **{safe_md(row['prize'])}** giveaway. Good luck!",
                                                    ephemeral=True)
        else:
            await interaction.response.send_message("You are already entered.", view=LeaveView(self, row["id"]),
                                                    ephemeral=True)

    @tasks.loop(seconds=CHECK_SECONDS)
    async def check_due(self) -> None:
        for row in self.bot.db.due_giveaways(int(time.time())):
            try:
                await self.finish(row)
            except Exception:  # a broken giveaway must not stop the loop
                log.exception("Could not finish giveaway #%s", row["id"])
                self.bot.db.finish_giveaway(row["id"], [])

    @check_due.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def finish(self, row, *, reroll: bool = False) -> list[int]:
        """Draws winners, updates the message and announces the result."""
        entries = self.bot.db.giveaway_entries(row["id"])
        previous = {int(w) for w in row["winner_ids"].split(",") if w} if reroll else set()
        winners = draw_winners(entries, row["winners"], exclude=previous)
        if reroll and not winners:
            # Nobody left: keep the previous winners instead of overwriting them
            channel = self.bot.get_channel(row["channel_id"])
            if isinstance(channel, discord.abc.Messageable):
                await channel.send(f"Can't reroll **{safe_md(row['prize'])}** – no other entrants left.")
            return []
        self.bot.db.finish_giveaway(row["id"], winners)
        row = self.bot.db.giveaway(row["id"])

        channel = self.bot.get_channel(row["channel_id"])
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("Channel of giveaway #%s not found.", row["id"])
            return winners
        try:
            message = await channel.fetch_message(row["message_id"])
            await message.edit(embed=self._embed(row, entries=len(entries), winners=winners), view=None)
        except discord.HTTPException:
            message = None

        if winners:
            mentions = ", ".join(f"<@{w}>" for w in winners)
            text = (f"🎉 {'Rerolled' if reroll else 'Congratulations'}: {mentions} – "
                    f"{'wins' if len(winners) == 1 else 'win'} **{safe_md(row['prize'])}**!")
        else:
            text = f"Giveaway **{safe_md(row['prize'])}** ended – no (further) valid entries."
        await channel.send(text, reference=message, mention_author=False,
                           allowed_mentions=discord.AllowedMentions(users=[discord.Object(w) for w in winners]))
        await self._give_winner_role(channel, winners)

        embed = discord.Embed(title="🎉 Giveaway " + ("rerolled" if reroll else "ended"), colour=COLOUR)
        embed.add_field(name="Prize", value=safe_md(row["prize"])[:1000])
        embed.add_field(name="Entries", value=str(len(entries)))
        embed.add_field(name="Winners", value=", ".join(f"<@{w}>" for w in winners) or "–", inline=False)
        self.bot.discord_log(embed)
        return winners

    async def _give_winner_role(self, channel, winners: list[int]) -> None:
        role_id = self.bot.config.giveaway_winner_role_id
        guild = getattr(channel, "guild", None)
        if not role_id or guild is None or not winners:
            return
        role = guild.get_role(role_id)
        if role is None or role >= guild.me.top_role:
            log.warning("Winner role %s is missing or above the bot role.", role_id)
            return
        for winner_id in winners:
            member = guild.get_member(winner_id)
            if member is None:
                try:
                    member = await guild.fetch_member(winner_id)
                except discord.HTTPException:
                    continue
            try:
                await member.add_roles(role, reason="Won a giveaway")
            except discord.HTTPException as exc:
                log.warning("Winner role for %s failed: %s", winner_id, exc)

    # ------------------------------------------------------------ Commands

    async def _ac_active(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        return [app_commands.Choice(name=f"#{r['id']} {r['prize']}"[:100], value=int(r["id"]))
                for r in self.bot.db.giveaways(active_only=True) if current.lower() in r["prize"].lower()][:25]

    async def _ac_ended(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        return [app_commands.Choice(name=f"#{r['id']} {r['prize']}"[:100], value=int(r["id"]))
                for r in self.bot.db.giveaways() if r["ended"] and current.lower() in r["prize"].lower()][:25]

    @giveaway.command(name="start", description="Starts a giveaway in this channel.")
    @app_commands.describe(prize="What can be won?", minutes="Duration in minutes (max. 10080 = 7 days)",
                           winners="Number of winners")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def start(self, interaction: discord.Interaction, prize: app_commands.Range[str, 1, 200],
                    minutes: app_commands.Range[int, 1, 10080], winners: app_commands.Range[int, 1, 20] = 1) -> None:
        ends_at = int(time.time()) + minutes * 60
        gid = self.bot.db.create_giveaway(interaction.guild_id, interaction.channel_id, prize, winners,
                                          interaction.user.id, ends_at)
        row = self.bot.db.giveaway(gid)
        try:
            message = await interaction.channel.send(embed=self._embed(row), view=GiveawayView(self))
        except discord.HTTPException:
            self.bot.db.delete_giveaway(gid)
            raise
        self.bot.db.set_giveaway_message(gid, message.id)
        embed = discord.Embed(title="🎉 Giveaway started", colour=COLOUR,
                              description=f"**{safe_md(prize)}** in {interaction.channel.mention}, ends <t:{ends_at}:R>")
        embed.add_field(name="By", value=interaction.user.mention)
        self.bot.discord_log(embed)
        await interaction.response.send_message(f"✅ Giveaway #{gid} started: {message.jump_url}", ephemeral=True)

    @giveaway.command(name="end", description="Ends a running giveaway right now and draws the winners.")
    @app_commands.describe(giveaway="Running giveaway")
    @app_commands.autocomplete(giveaway=_ac_active)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def end(self, interaction: discord.Interaction, giveaway: int) -> None:
        row = self.bot.db.giveaway(giveaway)
        if row is None or row["ended"]:
            await interaction.response.send_message("No running giveaway with this number.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        winners = await self.finish(row)
        await interaction.followup.send(f"Giveaway #{giveaway} ended ({len(winners)} winner(s)).", ephemeral=True)

    @giveaway.command(name="reroll", description="Draws new winners for a finished giveaway.")
    @app_commands.describe(giveaway="Finished giveaway")
    @app_commands.autocomplete(giveaway=_ac_ended)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reroll(self, interaction: discord.Interaction, giveaway: int) -> None:
        row = self.bot.db.giveaway(giveaway)
        if row is None or not row["ended"]:
            await interaction.response.send_message("Only finished giveaways can be rerolled.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        winners = await self.finish(row, reroll=True)
        await interaction.followup.send(f"Rerolled: {len(winners)} winner(s).", ephemeral=True)

    @giveaway.command(name="list", description="Shows running and finished giveaways.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        rows = self.bot.db.giveaways(limit=15)
        if not rows:
            await interaction.response.send_message("No giveaways yet.", ephemeral=True)
            return
        lines = []
        for r in rows:
            state = (f"🏁 ended – {', '.join(f'<@{w}>' for w in r['winner_ids'].split(',') if w) or 'no winners'}"
                     if r["ended"] else f"⏳ ends <t:{r['ends_at']}:R>")
            lines.append(f"`#{r['id']}` **{safe_md(r['prize'])}** · {r['entries']} entries · {state}")
        await interaction.response.send_message(
            embed=discord.Embed(title="🎉 Giveaways", description="\n".join(lines)[:4000], colour=COLOUR),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Giveaways(bot))
