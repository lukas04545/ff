"""Trivia on Discord: a question with 4 answer buttons – the first right answer wins points.

  /trivia ask    a question now (staff) · automatically every … minutes in your channel (optional)
  /trivia top    who answered the most right

Everyone gets one try per question. Built-in Rust questions plus your own (one per line:
  Question | right answer | wrong 1 | wrong 2 | wrong 3).
"""
from __future__ import annotations

import random

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from utils import admin_only

BUILTIN = (
    ("Which tool gathers stone the fastest?", "Jackhammer", "Salvaged icepick", "Stone hatchet", "Rock"),
    ("What does a sleeping bag cost to craft?", "30 cloth", "50 wood", "20 metal fragments", "100 cloth"),
    ("Which monument has the blue keycard puzzle with a fuse at the start?", "Water treatment plant",
     "Gas station", "Lighthouse", "Supermarket"),
    ("What is the fuel for a furnace?", "Wood", "Low grade fuel", "Charcoal", "Sulfur"),
    ("How many C4 does it take to break a sheet metal door?", "1", "2", "3", "4"),
    ("What gives the most scrap when recycled?", "Tech trash", "Rope", "Metal pipe", "Tarp"),
    ("Which animal is the most dangerous to a fresh spawn?", "Bear", "Deer", "Chicken", "Boar"),
    ("What does the tool cupboard stop?", "Others building nearby", "Raids", "Decay entirely", "Animals"),
    ("Which ammo does the AK use?", "5.56 rifle ammo", "Pistol ammo", "12 gauge buckshot", "Arrows"),
    ("What is HQM short for?", "High quality metal", "Heavy quarry metal", "Hard quick metal", "High quantity mix"),
    ("Which item lets you see in the dark without a light?", "Night vision goggles", "Flashlight", "Torch",
     "Binoculars"),
    ("What do you get by smelting metal ore?", "Metal fragments", "HQM", "Scrap", "Sulfur"),
    ("How many players fit in a minicopter?", "2", "1", "4", "6"),
    ("What do you need to repair a building block?", "A hammer", "A wrench", "A hatchet", "A building plan"),
)


class AnswerButton(discord.ui.Button):
    def __init__(self, game: "TriviaRound", label: str, index: int) -> None:
        super().__init__(label=label[:80], style=discord.ButtonStyle.primary, custom_id=f"trivia:{game.qid}:{index}")
        self.game, self.index = game, index

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.game.answer(interaction, self.index)


class TriviaRound(discord.ui.View):
    def __init__(self, plugin: "Trivia", question: tuple, qid: int, seconds: int) -> None:
        super().__init__(timeout=seconds)
        self.plugin, self.qid = plugin, qid
        self.text, right, *wrong = question
        answers = [right, *wrong[:3]]
        random.shuffle(answers)
        self.answers, self.right = answers, answers.index(right)
        self.tried: set[int] = set()
        self.winner: int | None = None
        self.message = None
        for i, a in enumerate(answers):
            self.add_item(AnswerButton(self, a, i))

    def embed(self) -> discord.Embed:
        e = discord.Embed(title="❓ Trivia", description=f"**{self.text}**", colour=0x1ABC9C)
        if self.winner is not None:
            e.add_field(name="✅ Solved", value=f"<@{self.winner}> – **{self.answers[self.right]}**")
        elif self.is_finished():
            e.add_field(name="⌛ Time's up", value=f"The answer was **{self.answers[self.right]}**.")
        else:
            reward = self.plugin.setting("points")
            e.set_footer(text=f"First right answer wins{f' {reward} points' if reward else ''} – one try each.")
        return e

    async def answer(self, interaction: discord.Interaction, index: int) -> None:
        uid = interaction.user.id
        if self.winner is not None:
            await interaction.response.send_message("Already solved – next time!", ephemeral=True)
            return
        if uid in self.tried:
            await interaction.response.send_message("You had your try.", ephemeral=True)
            return
        self.tried.add(uid)
        if index != self.right:
            await interaction.response.send_message("❌ Wrong!", ephemeral=True)
            return
        self.winner = uid
        self.stop()
        prize = self.plugin.reward(uid)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=self.embed(), view=self)
        if prize:
            await interaction.followup.send(f"🎉 <@{uid}> got it and wins **{prize}**!",
                                            allowed_mentions=discord.AllowedMentions(users=[interaction.user]))

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(embed=self.embed(), view=self)
            except discord.HTTPException:
                pass


class Trivia(Plugin):
    NAME = "Trivia"
    ICON = "❓"
    DESCRIPTION = "Rust trivia on Discord with answer buttons – the first right answer wins points."
    SETTINGS = (
        Setting("channel", "Trivia channel", "channel"),
        Setting("every_minutes", "Ask automatically every … minutes", "int", 0, min=0, max=10080, help="0 = only /trivia ask."),
        Setting("seconds", "Time to answer (seconds)", "int", 60, min=10, max=600),
        Setting("points", "Points for the right answer", "int", 50, min=0, max=1_000_000),
        Setting("questions", "Your questions", "textarea", "",
                help="One per line: Question | right answer | wrong 1 | wrong 2 | wrong 3"),
        Setting("builtin", "Use the built-in Rust questions", "bool", True),
    )

    trivia = app_commands.Group(name="trivia", description="Trivia", guild_only=True)

    def questions(self) -> list[tuple]:
        out = list(BUILTIN) if self.setting("builtin") else []
        for line in str(self.setting("questions") or "").splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3 and all(parts[:3]):
                out.append(tuple(parts[:5]))
        return out

    def pick(self) -> tuple | None:
        qs = self.questions()
        if not qs:
            return None
        recent = list(self.data.get("recent", []))
        fresh = [q for q in qs if q[0] not in recent] or qs
        q = random.choice(fresh)
        self.data.set("recent", (recent + [q[0]])[-max(1, len(qs) // 2):])
        return q

    def reward(self, user_id: int) -> str:
        wins = dict(self.data.get("wins", {}))
        wins[str(user_id)] = wins.get(str(user_id), 0) + 1
        self.data.set("wins", wins)
        pts = self.setting("points")
        if pts and self.points_enabled and self.give_points(user_id, pts, "trivia") is not None:
            return self.fmt_points(pts)
        return ""

    async def ask(self, channel) -> TriviaRound | None:
        q = self.pick()
        if q is None or channel is None:
            return None
        qid = int(self.data.get("next", 0)) + 1
        self.data.set("next", qid)
        game = TriviaRound(self, q, qid, self.setting("seconds"))
        game.message = await self.post(channel, embed=game.embed(), view=game)
        return game

    @tasks.loop(minutes=1)
    async def auto_loop(self) -> None:
        every = self.setting("every_minutes")
        if every and self.now() - int(self.data.get("last", 0)) >= every * 60:
            self.data.set("last", self.now())
            await self.ask(self.channel(self.setting("channel")))

    @trivia.command(name="ask", description="Ask a trivia question now.")
    @admin_only()
    async def ask_cmd(self, interaction: discord.Interaction) -> None:
        ch = self.channel(self.setting("channel")) or interaction.channel
        game = await self.ask(ch)
        await interaction.response.send_message("❓ Asked." if game else "No questions (or I can't post there).",
                                                ephemeral=True)

    @trivia.command(name="top", description="Who answered the most right.")
    async def top_cmd(self, interaction: discord.Interaction) -> None:
        rows = sorted(self.data.get("wins", {}).items(), key=lambda x: -x[1])[:10]
        await interaction.response.send_message(
            "❓ **Trivia champions**\n" + "\n".join(f"{i}. <@{u}> – {n}" for i, (u, n) in enumerate(rows, 1))
            if rows else "Nobody has answered yet.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    def problems(self) -> list[str]:
        out = [] if self.questions() else ["No questions – switch on the built-in ones or add your own."]
        if self.setting("every_minutes") and not self.setting("channel"):
            out.append("Pick the trivia channel for automatic questions.")
        return out

    def status(self) -> dict:
        return {"Questions": len(self.questions()), "Answered right": sum(self.data.get("wins", {}).values())}
