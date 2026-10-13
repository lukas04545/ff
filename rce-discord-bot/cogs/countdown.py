"""/countdown – one message that counts down in place (no spam, no repeated pings).

Staff run /countdown in a channel; the bot posts ONE message there, pings a role once at the start (optional), and
then edits that same message as the number ticks down to zero, where it shows a final message. It only ever writes
in the channel the command was used in, and it never pings more than once.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from utils import admin_only, safe_md

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.countdown")

MIN_INTERVAL = 5           # seconds between edits – Discord rate limits, and it stops rapid-fire edits
MAX_TICKS = 200            # a countdown can update at most this many times (keeps it from running forever)
COLOUR = discord.Colour(0xE67E22)
MAX_CONCURRENT = 3         # running countdowns per channel


class Countdown(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._running: dict[int, int] = {}      # channel id -> how many countdowns are running there

    def cog_unload(self) -> None:
        self._running.clear()

    @app_commands.command(name="countdown",
                          description="Post one message that counts down in place (staff).")
    @app_commands.describe(start="Number to count down from (e.g. 100)",
                           step="How much to subtract each time (default 10)",
                           interval="Seconds between updates (min 5, default 10)",
                           title="What the countdown is for (e.g. Wipe, Event start)",
                           role="Role to ping once at the start (optional)",
                           done="Message shown when it reaches zero (default: \"Go!\")")
    @app_commands.guild_only()
    @admin_only()
    async def countdown(self, interaction: discord.Interaction,
                        start: app_commands.Range[int, 1, 100_000],
                        step: app_commands.Range[int, 1, 100_000] = 10,
                        interval: app_commands.Range[int, MIN_INTERVAL, 3600] = 10,
                        title: str | None = None, role: discord.Role | None = None,
                        done: str | None = None) -> None:
        channel = interaction.channel
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message("Run this in a text channel.", ephemeral=True)
            return
        ticks = (start + step - 1) // step
        if ticks > MAX_TICKS:
            await interaction.response.send_message(
                f"That's too many steps ({ticks}). Use a bigger step or a smaller start so it updates at most "
                f"{MAX_TICKS} times (about {MAX_TICKS * interval // 60} minutes at this interval).", ephemeral=True)
            return
        if self._running.get(channel.id, 0) >= MAX_CONCURRENT:
            await interaction.response.send_message("Too many countdowns are already running in this channel – "
                                                    "wait for one to finish.", ephemeral=True)
            return
        if role is not None and (role.is_default() or role.managed):
            await interaction.response.send_message("Pick a normal role to ping (not @everyone or a bot role).",
                                                    ephemeral=True)
            return

        title = safe_md((title or "Countdown").strip())[:200]
        done = safe_md((done or "Go!").strip())[:200]
        await interaction.response.send_message(f"⏳ Countdown started here ({start} → 0, every {interval}s).",
                                                ephemeral=True)
        self._running[channel.id] = self._running.get(channel.id, 0) + 1
        try:
            await self._run(channel, start, step, interval, title, done, role, interaction.user)
        finally:
            self._running[channel.id] = max(0, self._running.get(channel.id, 1) - 1)

    def _embed(self, title: str, value: int, *, final: str | None = None) -> discord.Embed:
        embed = discord.Embed(title=f"⏳ {title}", colour=discord.Colour.green() if final else COLOUR,
                              description=f"# {final}" if final else f"# {value:,}".replace(",", " "))
        return embed

    async def _run(self, channel, start: int, step: int, interval: int, title: str, done: str,
                   role: discord.Role | None, author) -> None:
        # The only ping: the role, once, with the first message.
        mentions = discord.AllowedMentions(roles=[role] if role else [], users=False, everyone=False)
        try:
            message = await channel.send(content=role.mention if role else None, embed=self._embed(title, start),
                                         allowed_mentions=mentions)
        except discord.HTTPException as exc:
            log.warning("Countdown could not start: %s", exc)
            return
        self.bot.audit(f"⏳ Countdown '{safe_md(title)}' ({start}→0) started by {safe_md(str(author))} "
                       f"in <#{channel.id}>")
        value = start
        none = discord.AllowedMentions.none()
        try:
            while value > 0:
                await asyncio.sleep(interval)
                value = max(0, value - step)
                embed = self._embed(title, value, final=done if value == 0 else None)
                try:
                    await message.edit(embed=embed, allowed_mentions=none)   # edits never ping
                except discord.NotFound:
                    return                                                   # message was deleted – stop quietly
                except discord.HTTPException as exc:
                    log.warning("Countdown edit failed: %s", exc)
                    return
        except asyncio.CancelledError:
            raise


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Countdown(bot))
