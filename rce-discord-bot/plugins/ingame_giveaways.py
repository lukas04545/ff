"""In-game giveaways: announced in the game chat, players enter with a quick chat emote, winners get the prize in game.

Start one on Discord – /igiveaway start prize minutes winners – or let them run by themselves every few hours with a
prize picked from a list. Prizes: "kit:VIP" (a bot kit or in-game kit) or "item:rifle.ak 1" (item shortname and amount).
Entering: the entry emote (default "Hello") while the giveaway runs; only players online at the draw can win.
The entry emote can't give a kit while a giveaway runs.
"""
from __future__ import annotations

import random
import re
import time

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from rce.rcon_client import RconError
from utils import admin_only

PRIZE = re.compile(r"^\s*(kit|item)\s*:\s*([A-Za-z0-9 ._-]+?)\s*(\d+)?\s*$", re.I)
DEFAULT_PRIZES = """item:scrap 500
item:rifle.ak 1
item:supply.signal 1"""


def parse_prize(text: str) -> dict | None:
    """ "kit:VIP" -> {"kind": "kit", "name": "VIP"} · "item:rifle.ak 2" -> {"kind": "item", "name": …, "amount": 2}"""
    m = PRIZE.match(str(text or ""))
    if not m:
        return None
    kind, name = m[1].lower(), m[2].strip()
    if kind == "item":
        return {"kind": "item", "name": name.lower().replace(" ", ""), "amount": max(1, int(m[3] or 1))}
    return {"kind": "kit", "name": name}


def prize_text(p: dict) -> str:
    return f"the kit {p['name']}" if p["kind"] == "kit" else f"{p['amount']}× {p['name']}"


class InGameGiveaways(Plugin):
    NAME = "In-game giveaways"
    ICON = "🎁"
    DESCRIPTION = ("Giveaways in the game chat: players enter with a quick chat emote, the bot draws the winners among "
                   "the players online and gives them the prize in game – started on Discord or every few hours.")
    SETTINGS = (
        Setting("entry_emote", "Entry emote", default="Hello",
                help="The quick chat emote players use to enter while a giveaway runs."),
        Setting("minutes", "How long a giveaway runs (minutes)", "int", 5, min=1, max=120),
        Setting("auto_every_hours", "Start one by itself every … hours", "int", 0, min=0, max=168,
                help="0 = only when an admin starts one (/igiveaway start)."),
        Setting("auto_prizes", "Prizes for automatic giveaways (one per line)", "textarea", DEFAULT_PRIZES,
                help="kit:VIP  or  item:rifle.ak 1 – one is picked at random."),
        Setting("auto_winners", "Winners of automatic giveaways", "int", 1, min=1, max=20),
        Setting("min_players", "Automatic giveaways only with at least … players online", "int", 3, min=1, max=500),
        Setting("channel", "Also announce winners on Discord in", "channel", help="Empty = only in game."),
    )

    igiveaway = app_commands.Group(name="igiveaway", description="In-game giveaways", guild_only=True)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.running: dict | None = None          # {"prize", "winners", "ends", "entries": set, "by", "reminded"}

    # ------------------------------------------------------------ the entry emote

    async def on_load(self) -> None:
        from support import emote_binds
        emote_binds.reserve(self.NAME, self.reserved_emotes)

    async def on_unload(self) -> None:
        from support import emote_binds
        emote_binds.release(self.NAME)

    def entry_raw(self) -> str | None:
        text = str(self.setting("entry_emote") or "").strip()
        return self.emote_raw(text) if text else None

    def reserved_emotes(self) -> set[str]:
        raw = self.entry_raw()
        return {raw} if raw and self.running else set()          # only while a giveaway runs

    @Plugin.listener()
    async def on_rce_chat(self, ev) -> None:
        if ev.channel == "SERVER" or self.running is None:
            return
        raw = self.entry_raw()
        if raw and ev.raw == raw:
            name = self.online_name(ev.player) or ev.player
            if name.lower() not in self.running["entries"]:
                self.running["entries"].add(name.lower())
                await self.say(f"🎁 {name} entered the giveaway ({len(self.running['entries'])} in)!")

    # ------------------------------------------------------------ start / draw

    async def start(self, prize: dict, winners: int, minutes: int, by: str = "") -> str | None:
        """Starts a giveaway. Returns a problem, or None."""
        if self.running is not None:
            return "A giveaway is running already."
        if not self.entry_raw():
            return "Set a valid entry emote first (Plugins → In-game giveaways)."
        self.running = {"prize": prize, "winners": max(1, winners), "ends": time.time() + minutes * 60,
                        "entries": set(), "by": by, "reminded": False}
        emote = str(self.setting("entry_emote")).strip()
        await self.say(f"🎁 <color=#FFD700>GIVEAWAY</color>: {prize_text(prize)} for {max(1, winners)} winner(s)! "
                       f"Use the quick chat “{emote}” in the next {minutes} min to enter.")
        self.audit(f"🎁 In-game giveaway started ({prize_text(prize)}, {winners} winner(s), {minutes} min"
                   + (f", by {by}" if by else "") + ")")
        return None

    async def draw(self) -> list[str]:
        """Ends the running giveaway: winners among the entrants still online get the prize."""
        g, self.running = self.running, None
        if g is None:
            return []
        online = {p.lower(): p for p in self.online_players()}
        pool = [online[e] for e in g["entries"] if e in online]
        winners = random.sample(pool, min(len(pool), g["winners"]))
        if not winners:
            await self.say("🎁 The giveaway ended – nobody entered (or nobody who entered is online).")
            return []
        given = []
        for name in winners:
            if await self.give(name, g["prize"]):
                given.append(name)
        await self.say(f"🎁 Giveaway winner{'s' if len(given) > 1 else ''}: <color=#7CFC00>{', '.join(given)}</color> "
                       f"– {prize_text(g['prize'])}!")
        if self.setting("channel"):
            await self.post(self.channel(self.setting("channel")),
                            f"🎁 In-game giveaway: **{self.md(', '.join(given))}** won {self.md(prize_text(g['prize']))}")
        self.data.set("given", int(self.data.get("given", 0)) + len(given))
        return given

    async def give(self, player: str, prize: dict) -> bool:
        if prize["kind"] == "kit":
            return await self.give_kit(player, prize["name"])
        try:
            await self.bot.rcon.command(self.bot.config.item_give_template.format(
                name=player, item=prize["name"], amount=prize["amount"]), expect_response=False)
            return True
        except RconError as exc:
            self.log.warning("Giveaway prize for %s failed: %s", player, exc)
            return False

    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        g = self.running
        if g is not None:
            left = g["ends"] - now
            if left <= 0:
                await self.draw()
            elif not g["reminded"] and left <= 60:
                g["reminded"] = True
                await self.say(f"🎁 1 minute left to enter the giveaway – quick chat “"
                               f"{str(self.setting('entry_emote')).strip()}”! ({len(g['entries'])} in)")
            return
        hours = int(self.setting("auto_every_hours") or 0)
        if not hours or len(self.online_players()) < int(self.setting("min_players") or 1):
            return
        if now - float(self.data.get("last_auto", 0)) < hours * 3600:
            return
        prizes = [p for p in (parse_prize(line) for line in str(self.setting("auto_prizes")).splitlines()) if p]
        if prizes:
            self.data.set("last_auto", now)
            await self.start(random.choice(prizes), int(self.setting("auto_winners") or 1),
                             int(self.setting("minutes") or 5), by="auto")

    @tasks.loop(seconds=15)
    async def giveaway_loop(self) -> None:
        if self.bot.rcon.connected:
            await self.tick()

    # ------------------------------------------------------------ Discord

    @igiveaway.command(name="start", description="Start a giveaway in the game chat.")
    @app_commands.describe(prize="kit:VIP  or  item:rifle.ak 1", winners="How many winners",
                           minutes="How long players can enter (default from the settings)")
    @admin_only()
    async def start_cmd(self, interaction: discord.Interaction, prize: str,
                        winners: app_commands.Range[int, 1, 20] = 1,
                        minutes: app_commands.Range[int, 1, 120] | None = None) -> None:
        p = parse_prize(prize)
        if p is None:
            await interaction.response.send_message("Prize: `kit:VIP` or `item:rifle.ak 1`.", ephemeral=True)
            return
        problem = await self.start(p, winners, minutes or int(self.setting("minutes") or 5), by=str(interaction.user))
        await interaction.response.send_message(f"❌ {problem}" if problem else
                                                f"🎁 Started: {self.md(prize_text(p))} for {winners} winner(s).",
                                                ephemeral=True)

    @igiveaway.command(name="end", description="Draw the running giveaway now.")
    @admin_only()
    async def end_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        given = await self.draw()
        await interaction.followup.send("🎁 " + (f"Winners: {self.md(', '.join(given))}" if given else
                                               "Ended – no winners."), ephemeral=True)

    # ------------------------------------------------------------ panel

    def problems(self) -> list[str]:
        out = []
        text = str(self.setting("entry_emote") or "").strip()
        if not text or not self.emote_raw(text):
            out.append(f"Entry emote: “{text}” is not a quick chat emote.")
        bad = [line for line in str(self.setting("auto_prizes")).splitlines() if line.strip() and not parse_prize(line)]
        if bad:
            out.append("Prizes not understood: " + ", ".join(bad[:3]) + " – write kit:NAME or item:shortname AMOUNT.")
        return out

    def status(self) -> dict:
        g = self.running
        return {"Running": (f"{prize_text(g['prize'])} – {len(g['entries'])} entered" if g else "no"),
                "Prizes given": int(self.data.get("given", 0))}
