"""Scheduled messages: posted automatically every N minutes in-game (say) and/or in a Discord channel.

/schedule add     – new message (e.g. "Join our Discord: discord.gg/…" every 30 min)
/schedule list    – all messages
/schedule toggle  – pause/resume
/schedule remove  – delete

Also in the web interface (Community → Scheduled messages).
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from rce.rcon_client import RconError
from utils import admin_only, format_minutes, parse_duration, safe_md

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.schedule")

TARGETS = {"game": "In-game", "discord": "Discord", "both": "In-game + Discord"}
MIN_INTERVAL = 5          # minutes – protects chat and Discord from spam
MAX_LENGTH = 400


def clean_text(text: str) -> str:
    """One line (the game chat has no line breaks); rich text like <color=red> stays allowed."""
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())[:MAX_LENGTH]


class Schedule(commands.Cog):
    schedule = app_commands.Group(name="schedule", description="Messages posted automatically every N minutes",
                                  guild_only=True)

    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.send_loop.start()

    async def cog_unload(self) -> None:
        self.send_loop.cancel()

    # ------------------------------------------------------------ API (also for the web interface)

    def add(self, text: str, minutes: int, target: str, channel_id: int | None, by: str) -> int:
        text = clean_text(text)
        if not text:
            raise ValueError("The message is empty.")
        if minutes < MIN_INTERVAL:
            raise ValueError(f"The interval must be at least {MIN_INTERVAL} minutes.")
        if target not in TARGETS:
            raise ValueError("Unknown target.")
        if target != "game" and not channel_id:
            raise ValueError("Choose a Discord channel for this message.")
        return self.bot.db.add_scheduled_message(text, minutes, target, channel_id if target != "game" else None,
                                                 by, int(time.time()))

    async def send(self, row) -> bool:
        ok = True
        if row["target"] in ("game", "both"):
            try:
                await self.bot.rcon.command(f"say {row['text']}", expect_response=False)
            except RconError as exc:
                log.debug("Scheduled message #%s not sent in-game: %s", row["id"], exc)
                ok = False
        if row["target"] in ("discord", "both") and row["channel_id"]:
            channel = self.bot.get_channel(row["channel_id"])
            if isinstance(channel, discord.abc.Messageable):
                try:
                    await channel.send(row["text"], allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException as exc:
                    log.warning("Scheduled message #%s not sent to Discord: %s", row["id"], exc)
        return ok

    # ------------------------------------------------------------ Loop

    @tasks.loop(seconds=30)
    async def send_loop(self) -> None:
        await self.tick(int(time.time()))

    @send_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def tick(self, now: int) -> None:
        for row in self.bot.db.scheduled_messages(enabled_only=True):
            if row["last_sent"] and now - row["last_sent"] < row["interval_minutes"] * 60:
                continue
            if row["target"] == "game" and not self.bot.rcon.connected:
                continue   # nobody would see it – try again once the server is back
            self.bot.db.mark_scheduled_sent(row["id"], now)
            await self.send(row)

    # ------------------------------------------------------------ Commands

    @schedule.command(name="add", description="Posts a message automatically every N minutes.")
    @app_commands.describe(text="Message (in-game: rich text like <color=red> allowed)",
                           every="e.g. 30m, 1h, 2h 30m (at least 5 minutes)", target="Where to post it",
                           channel="Discord channel (for Discord / both)")
    @app_commands.choices(target=[app_commands.Choice(name=v, value=k) for k, v in TARGETS.items()])
    @admin_only()
    async def add_cmd(self, interaction: discord.Interaction, text: str, every: str,
                      target: app_commands.Choice[str] | None = None,
                      channel: discord.TextChannel | None = None) -> None:
        minutes = parse_duration(every)
        if not minutes:
            await interaction.response.send_message("⚠️ Invalid interval – e.g. `30m`, `1h` or `2h 30m`.",
                                                    ephemeral=True)
            return
        tgt = target.value if target else "game"
        try:
            msg_id = self.add(text, minutes, tgt, channel.id if channel else None, str(interaction.user))
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        self.bot.audit(f"🔁 {safe_md(str(interaction.user))} added scheduled message #{msg_id} "
                       f"({TARGETS[tgt]}, every {format_minutes(minutes)})")
        await interaction.response.send_message(
            f"✅ Message #{msg_id} is posted every **{format_minutes(minutes)}** ({TARGETS[tgt]}). "
            "First time after one interval.", ephemeral=True)

    @schedule.command(name="list", description="Shows all scheduled messages.")
    @admin_only()
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        rows = self.bot.db.scheduled_messages()
        lines = []
        for r in rows:
            where = TARGETS[r["target"]] + (f" <#{r['channel_id']}>" if r["channel_id"] else "")
            state = "▶️" if r["enabled"] else "⏸️"
            lines.append(f"`#{r['id']}` {state} every {format_minutes(r['interval_minutes'])} · {where}\n"
                         f"  {safe_md(r['text'][:150])}")
        await interaction.response.send_message(
            embed=discord.Embed(title="🔁 Scheduled messages", description="\n".join(lines)[:4000] or "None yet.",
                                colour=discord.Colour.blurple()), ephemeral=True)

    @schedule.command(name="toggle", description="Pauses or resumes a scheduled message.")
    @app_commands.describe(message="Number from /schedule list")
    @admin_only()
    async def toggle(self, interaction: discord.Interaction, message: int) -> None:
        row = self.bot.db.scheduled_message(message)
        if row is None:
            await interaction.response.send_message("No message with this number.", ephemeral=True)
            return
        self.bot.db.set_scheduled_enabled(message, not row["enabled"])
        await interaction.response.send_message(f"✅ #{message} {'paused' if row['enabled'] else 'resumed'}.",
                                                ephemeral=True)

    @schedule.command(name="remove", description="Deletes a scheduled message.")
    @app_commands.describe(message="Number from /schedule list")
    @admin_only()
    async def remove(self, interaction: discord.Interaction, message: int) -> None:
        if not self.bot.db.delete_scheduled_message(message):
            await interaction.response.send_message("No message with this number.", ephemeral=True)
            return
        self.bot.audit(f"🔁 {safe_md(str(interaction.user))} deleted scheduled message #{message}")
        await interaction.response.send_message(f"✅ #{message} deleted.", ephemeral=True)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Schedule(bot))
