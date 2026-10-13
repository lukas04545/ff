"""Post-wipe feedback poll: some time after each wipe, Discord polls ask players what they think.

One poll per line in "Questions": the question, then the answers, separated by "|". Results are shown by Discord.
The bot needs the "Send Polls" permission in the channel.
"""
from __future__ import annotations

import datetime as dt

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from utils import admin_only

DEFAULT_QUESTIONS = """How was this wipe? | 🔥 Amazing | 🙂 Good | 😐 Okay | 👎 Bad
How often should we wipe? | Keep it like this | More often | Less often
Kits & loot are … | Too strong | Just right | Too weak"""


def parse_questions(text: str) -> list[tuple[str, list[str]]]:
    out = []
    for line in str(text or "").splitlines():
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 3:
            out.append((parts[0][:300], [a[:55] for a in parts[1:11]]))
    return out[:5]


class WipePoll(Plugin):
    NAME = "Wipe feedback poll"
    ICON = "🗳️"
    DESCRIPTION = ("A day after each wipe, short Discord polls ask players about the wipe, the schedule and kit "
                   "balance. Players stay when they feel heard.")
    DEFAULT_ENABLED = True
    SETTINGS = (
        Setting("channel", "Channel", "channel", help="Empty = events channel."),
        Setting("delay", "Post … hours after the wipe", "int", 24, min=0, max=336),
        Setting("duration", "Poll open for (hours)", "int", 48, min=1, max=768),
        Setting("questions", "Questions", "textarea", DEFAULT_QUESTIONS,
                help="One poll per line: question | answer | answer … (2–10 answers, max. 5 polls)."),
    )

    @Plugin.listener()
    async def on_rce_wiped(self, next_wipe) -> None:
        self.data.set("due", self.now() + self.setting("delay") * 3600)

    async def post_polls(self) -> int:
        channel = self.channel(self.setting("channel"), self.bot.config.events_channel_id)
        if channel is None:
            self.log.warning("No channel for the wipe poll.")
            return 0
        posted = 0
        for question, answers in parse_questions(self.setting("questions")):
            poll = discord.Poll(question=question, duration=dt.timedelta(hours=self.setting("duration")))
            for answer in answers:
                poll.add_answer(text=answer)
            if await self.post(channel, poll=poll) is not None:
                posted += 1
        if posted:
            self.data.set("posted_total", int(self.data.get("posted_total", 0)) + posted)
            self.data.set("last_posted", self.now())
        return posted

    @tasks.loop(minutes=5)
    async def poll_loop(self) -> None:
        due = self.data.get("due")
        if due and self.now() >= due:
            self.data.delete("due")
            await self.post_polls()

    @app_commands.command(name="wipepoll", description="Post the wipe feedback polls now.")
    @app_commands.guild_only()
    @admin_only()
    async def wipepoll(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        n = await self.post_polls()
        await interaction.followup.send(f"🗳️ {n} poll(s) posted." if n else
                                        "⚠️ No poll posted – check the channel and the \"Send Polls\" permission.",
                                        ephemeral=True)

    def problems(self) -> list[str]:
        out = []
        if not parse_questions(self.setting("questions")):
            out.append("No valid question – use: question | answer | answer")
        if self.channel(self.setting("channel"), self.bot.config.events_channel_id) is None and self.bot.is_ready():
            out.append("Choose a channel (or set an events channel).")
        return out

    def status(self) -> dict:
        due = self.data.get("due")
        return {"Polls posted": int(self.data.get("posted_total", 0)),
                "Next polls": dt.datetime.fromtimestamp(due).strftime("%Y-%m-%d %H:%M") if due else "after the next wipe"}
