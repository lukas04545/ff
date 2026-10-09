"""Giveaways mit Teilnahme-Button – gespeichert in der Datenbank, überleben also Bot-Neustarts.

/giveaway start  – Gewinnspiel in diesem Channel starten (Server verwalten)
/giveaway ende   – vorzeitig auslosen
/giveaway neu    – Gewinner neu auslosen (Reroll)
/giveaway liste  – laufende und beendete Giveaways

Optional bekommen Gewinner die Rolle GIVEAWAY_WINNER_ROLE_ID.
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
    """Zieht bis zu count zufällige, verschiedene Gewinner (kryptografisch sicherer Zufall)."""
    pool = [e for e in dict.fromkeys(entries) if e not in (exclude or set())]
    rng = secrets.SystemRandom()
    return rng.sample(pool, min(count, len(pool)))


class GiveawayView(discord.ui.View):
    """Dauerhafter Teilnahme-Button – ein custom_id für alle Giveaways, Zuordnung über die Nachricht."""

    def __init__(self, cog: "Giveaways") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Teilnehmen", emoji="🎉", style=discord.ButtonStyle.success,
                       custom_id="rce:giveaway:enter")
    async def enter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handle_entry(interaction)


class LeaveView(discord.ui.View):
    def __init__(self, cog: "Giveaways", giveaway_id: int) -> None:
        super().__init__(timeout=60)
        self.cog, self.giveaway_id = cog, giveaway_id

    @discord.ui.button(label="Teilnahme zurückziehen", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.cog.bot.db.remove_giveaway_entry(self.giveaway_id, interaction.user.id)
        await interaction.response.edit_message(content="Du nimmst nicht mehr teil.", view=None)


class Giveaways(commands.Cog):
    giveaway = app_commands.Group(name="giveaway", description="Gewinnspiele", guild_only=True,
                                  default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(GiveawayView(self))
        self.check_due.start()

    async def cog_unload(self) -> None:
        self.check_due.cancel()

    # ------------------------------------------------------------ Darstellung

    def _embed(self, row, *, entries: int | None = None, winners: list[int] | None = None) -> discord.Embed:
        ended = bool(row["ended"]) or winners is not None
        embed = discord.Embed(title=f"🎉 Giveaway: {row['prize']}"[:256], colour=COLOUR if not ended else discord.Colour.dark_grey())
        lines = [f"**Preis:** {safe_md(row['prize'])}", f"**Veranstaltet von:** <@{row['host_id']}>",
                 f"**Gewinner:** {row['winners']}"]
        if ended:
            names = ", ".join(f"<@{w}>" for w in winners or []) or "keine gültigen Teilnahmen"
            lines.append(f"**Beendet** – Gewinner: {names}")
        else:
            lines.append(f"**Endet:** <t:{row['ends_at']}:R> (<t:{row['ends_at']}:f>)")
            lines.append("\nKlicke auf **🎉 Teilnehmen**!")
        if entries is not None:
            lines.append(f"**Teilnehmer:** {entries}")
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Giveaway #{row['id']}")
        return embed

    # ------------------------------------------------------------ Ablauf

    async def handle_entry(self, interaction: discord.Interaction) -> None:
        row = self.bot.db.giveaway_by_message(interaction.message.id) if interaction.message else None
        if row is None or row["ended"] or row["ends_at"] <= time.time():
            await interaction.response.send_message("Dieses Giveaway ist bereits beendet.", ephemeral=True)
            return
        if interaction.user.bot:
            return
        if self.bot.db.add_giveaway_entry(row["id"], interaction.user.id):
            await interaction.response.send_message(f"🎉 Du nimmst am Giveaway **{safe_md(row['prize'])}** teil. Viel Glück!",
                                                    ephemeral=True)
        else:
            await interaction.response.send_message("Du nimmst bereits teil.", view=LeaveView(self, row["id"]),
                                                    ephemeral=True)

    @tasks.loop(seconds=CHECK_SECONDS)
    async def check_due(self) -> None:
        for row in self.bot.db.due_giveaways(int(time.time())):
            try:
                await self.finish(row)
            except Exception:  # ein kaputtes Giveaway darf die Schleife nicht stoppen
                log.exception("Giveaway #%s konnte nicht beendet werden", row["id"])
                self.bot.db.finish_giveaway(row["id"], [])

    @check_due.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def finish(self, row, *, reroll: bool = False) -> list[int]:
        """Lost Gewinner aus, aktualisiert die Nachricht und verkündet das Ergebnis."""
        entries = self.bot.db.giveaway_entries(row["id"])
        previous = {int(w) for w in row["winner_ids"].split(",") if w} if reroll else set()
        winners = draw_winners(entries, row["winners"], exclude=previous)
        if reroll and not winners:
            # Niemand übrig: bisherige Gewinner behalten statt sie zu überschreiben
            channel = self.bot.get_channel(row["channel_id"])
            if isinstance(channel, discord.abc.Messageable):
                await channel.send(f"Kein Reroll für **{safe_md(row['prize'])}** möglich – keine weiteren Teilnehmer.")
            return []
        self.bot.db.finish_giveaway(row["id"], winners)
        row = self.bot.db.giveaway(row["id"])

        channel = self.bot.get_channel(row["channel_id"])
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("Channel von Giveaway #%s nicht gefunden.", row["id"])
            return winners
        try:
            message = await channel.fetch_message(row["message_id"])
            await message.edit(embed=self._embed(row, entries=len(entries), winners=winners), view=None)
        except discord.HTTPException:
            message = None

        if winners:
            mentions = ", ".join(f"<@{w}>" for w in winners)
            text = (f"🎉 {'Neu ausgelost' if reroll else 'Glückwunsch'}: {mentions} – "
                    f"{'gewinnt' if len(winners) == 1 else 'gewinnen'} **{safe_md(row['prize'])}**!")
        else:
            text = f"Giveaway **{safe_md(row['prize'])}** beendet – keine (weiteren) gültigen Teilnahmen."
        await channel.send(text, reference=message, mention_author=False,
                           allowed_mentions=discord.AllowedMentions(users=[discord.Object(w) for w in winners]))
        await self._give_winner_role(channel, winners)

        embed = discord.Embed(title="🎉 Giveaway " + ("neu ausgelost" if reroll else "beendet"), colour=COLOUR)
        embed.add_field(name="Preis", value=safe_md(row["prize"])[:1000])
        embed.add_field(name="Teilnehmer", value=str(len(entries)))
        embed.add_field(name="Gewinner", value=", ".join(f"<@{w}>" for w in winners) or "–", inline=False)
        self.bot.discord_log(embed)
        return winners

    async def _give_winner_role(self, channel, winners: list[int]) -> None:
        role_id = self.bot.config.giveaway_winner_role_id
        guild = getattr(channel, "guild", None)
        if not role_id or guild is None or not winners:
            return
        role = guild.get_role(role_id)
        if role is None or role >= guild.me.top_role:
            log.warning("Gewinner-Rolle %s fehlt oder steht über der Bot-Rolle.", role_id)
            return
        for winner_id in winners:
            member = guild.get_member(winner_id)
            if member is None:
                try:
                    member = await guild.fetch_member(winner_id)
                except discord.HTTPException:
                    continue
            try:
                await member.add_roles(role, reason="Giveaway gewonnen")
            except discord.HTTPException as exc:
                log.warning("Gewinner-Rolle für %s fehlgeschlagen: %s", winner_id, exc)

    # ------------------------------------------------------------ Befehle

    async def _ac_active(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        return [app_commands.Choice(name=f"#{r['id']} {r['prize']}"[:100], value=int(r["id"]))
                for r in self.bot.db.giveaways(active_only=True) if current.lower() in r["prize"].lower()][:25]

    async def _ac_ended(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        return [app_commands.Choice(name=f"#{r['id']} {r['prize']}"[:100], value=int(r["id"]))
                for r in self.bot.db.giveaways() if r["ended"] and current.lower() in r["prize"].lower()][:25]

    @giveaway.command(name="start", description="Startet ein Giveaway in diesem Channel.")
    @app_commands.describe(preis="Was gibt es zu gewinnen?", minuten="Dauer in Minuten (max. 10080 = 7 Tage)",
                           gewinner="Anzahl Gewinner")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def start(self, interaction: discord.Interaction, preis: app_commands.Range[str, 1, 200],
                    minuten: app_commands.Range[int, 1, 10080], gewinner: app_commands.Range[int, 1, 20] = 1) -> None:
        ends_at = int(time.time()) + minuten * 60
        gid = self.bot.db.create_giveaway(interaction.guild_id, interaction.channel_id, preis, gewinner,
                                          interaction.user.id, ends_at)
        row = self.bot.db.giveaway(gid)
        try:
            message = await interaction.channel.send(embed=self._embed(row), view=GiveawayView(self))
        except discord.HTTPException:
            self.bot.db.delete_giveaway(gid)
            raise
        self.bot.db.set_giveaway_message(gid, message.id)
        embed = discord.Embed(title="🎉 Giveaway gestartet", colour=COLOUR,
                              description=f"**{safe_md(preis)}** in {interaction.channel.mention}, endet <t:{ends_at}:R>")
        embed.add_field(name="Von", value=interaction.user.mention)
        self.bot.discord_log(embed)
        await interaction.response.send_message(f"✅ Giveaway #{gid} gestartet: {message.jump_url}", ephemeral=True)

    @giveaway.command(name="ende", description="Beendet ein laufendes Giveaway sofort und lost aus.")
    @app_commands.describe(giveaway="Laufendes Giveaway")
    @app_commands.autocomplete(giveaway=_ac_active)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def end(self, interaction: discord.Interaction, giveaway: int) -> None:
        row = self.bot.db.giveaway(giveaway)
        if row is None or row["ended"]:
            await interaction.response.send_message("Kein laufendes Giveaway mit dieser Nummer.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        winners = await self.finish(row)
        await interaction.followup.send(f"Giveaway #{giveaway} beendet ({len(winners)} Gewinner).", ephemeral=True)

    @giveaway.command(name="neu", description="Lost für ein beendetes Giveaway neue Gewinner aus.")
    @app_commands.describe(giveaway="Beendetes Giveaway")
    @app_commands.autocomplete(giveaway=_ac_ended)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reroll(self, interaction: discord.Interaction, giveaway: int) -> None:
        row = self.bot.db.giveaway(giveaway)
        if row is None or not row["ended"]:
            await interaction.response.send_message("Nur beendete Giveaways können neu ausgelost werden.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        winners = await self.finish(row, reroll=True)
        await interaction.followup.send(f"Neu ausgelost: {len(winners)} Gewinner.", ephemeral=True)

    @giveaway.command(name="liste", description="Zeigt laufende und beendete Giveaways.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        rows = self.bot.db.giveaways(limit=15)
        if not rows:
            await interaction.response.send_message("Noch keine Giveaways.", ephemeral=True)
            return
        lines = []
        for r in rows:
            state = (f"🏁 beendet – {', '.join(f'<@{w}>' for w in r['winner_ids'].split(',') if w) or 'keine Gewinner'}"
                     if r["ended"] else f"⏳ endet <t:{r['ends_at']}:R>")
            lines.append(f"`#{r['id']}` **{safe_md(r['prize'])}** · {r['entries']} Teilnehmer · {state}")
        await interaction.response.send_message(
            embed=discord.Embed(title="🎉 Giveaways", description="\n".join(lines)[:4000], colour=COLOUR),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Giveaways(bot))
