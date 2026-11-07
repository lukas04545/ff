"""Random items: every few minutes online players get a random item – with an opt-out for players who don't want it.

Items: one per line  ITEM | MIN | MAX   (amount picked at random between MIN and MAX), e.g. "scrap | 50 | 200".
Who: one random online player, or everyone online (each their own random item).
Opt-out: players toggle it themselves with a quick chat emote (default "Sorry") or on Discord with
/randomitems toggle (linked account). Opted-out players are skipped; the toggle is answered in game.
"""
from __future__ import annotations

import random
import re

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from rce.rcon_client import RconError
from utils import admin_only

DEFAULT_ITEMS = """scrap | 50 | 200
wood | 1000 | 3000
stones | 500 | 2000
metal.fragments | 200 | 800
lowgradefuel | 50 | 150
syringe.medical | 2 | 5
bandage | 3 | 8"""
LINE = re.compile(r"^\s*([a-z0-9._-]+)\s*(?:\|\s*(\d+)\s*(?:\|\s*(\d+))?)?\s*$", re.I)


def parse_items(text: str) -> tuple[list[tuple[str, int, int]], list[str]]:
    items, errors = [], []
    for n, line in enumerate(str(text or "").splitlines(), 1):
        if not line.strip() or line.strip().startswith("#"):
            continue
        m = LINE.match(line)
        if not m:
            errors.append(f"Line {n}: write ITEM | MIN | MAX, e.g. scrap | 50 | 200")
            continue
        low = int(m[2] or 1)
        high = int(m[3] or low)
        items.append((m[1].lower(), max(1, min(low, high)), max(1, low, high)))
    return items, errors


class RandomItems(Plugin):
    NAME = "Random items"
    ICON = "🎲"
    DESCRIPTION = ("Every few minutes online players get a random item from your list – players who don't want it "
                   "switch it off themselves with an emote or on Discord.")
    SETTINGS = (
        Setting("every_minutes", "Every … minutes", "int", 30, min=5, max=1440),
        Setting("who", "Who gets something", "choice", "one", choices=("one", "everyone"),
                help="one = one random online player each time · everyone = every online player."),
        Setting("min_players", "Only with at least … players online", "int", 1, min=1, max=500),
        Setting("items", "Items (ITEM | MIN | MAX per line)", "textarea", DEFAULT_ITEMS,
                help="Item shortname, smallest and biggest amount – one is picked at random."),
        Setting("optout_emote", "Opt-out emote (toggles on/off)", default="Sorry",
                help="Players use this quick chat emote to stop (or start again) getting random items. Empty = only "
                     "on Discord (/randomitems toggle). The emote can't give a kit while this plugin is on."),
        Setting("announce", "Announce in game", "bool", True,
                help="\"🎲 Bob got 120 scrap from the random drop!\""),
    )

    randomitems = app_commands.Group(name="randomitems", description="Random items", guild_only=True)

    # ------------------------------------------------------------ lifecycle / emote

    async def on_load(self) -> None:
        from support import emote_binds
        emote_binds.reserve(self.NAME, self.reserved_emotes)

    async def on_unload(self) -> None:
        from support import emote_binds
        emote_binds.release(self.NAME)

    def reserved_emotes(self) -> set[str]:
        text = str(self.setting("optout_emote") or "").strip()
        raw = self.emote_raw(text) if text else None
        return {raw} if raw else set()

    def opted_out(self) -> set[str]:
        return set(self.data.get("optout", []))

    def toggle(self, player: str) -> bool:
        """Switches random items off/on for a player. Returns True if they get them now."""
        out = self.opted_out()
        key = player.lower()
        if key in out:
            out.discard(key)
        else:
            out.add(key)
        self.data.set("optout", sorted(out))
        return key not in out

    @Plugin.listener()
    async def on_rce_chat(self, ev) -> None:
        if ev.channel == "SERVER":
            return
        text = str(self.setting("optout_emote") or "").strip()
        raw = self.emote_raw(text) if text else None
        if raw and raw == ev.raw:
            name = self.online_name(ev.player) or ev.player
            on = self.toggle(name)
            await self.say(f"🎲 {name}: random items {'ON – you get drops again' if on else 'OFF – no more drops'} "
                           f"(use the emote again to switch).")

    # ------------------------------------------------------------ the drop

    async def drop(self) -> list[str]:
        """Gives the random item(s) now. Returns lines like "Bob: 120 scrap"."""
        items = parse_items(self.setting("items"))[0]
        players = [p for p in self.online_players() if p.lower() not in self.opted_out()]
        if not items or len(self.online_players()) < int(self.setting("min_players") or 1) or not players:
            return []
        lucky = players if self.setting("who") == "everyone" else [random.choice(players)]
        given = []
        for name in lucky:
            item, low, high = random.choice(items)
            amount = random.randint(low, high)
            try:
                await self.bot.rcon.command(self.bot.config.item_give_template.format(
                    name=name, item=item, amount=amount), expect_response=False)
            except RconError as exc:
                self.log.warning("Random item for %s failed: %s", name, exc)
                continue
            given.append(f"{name}: {amount} {item}")
        if given and self.setting("announce"):
            if len(given) == 1:
                who, what = given[0].split(": ", 1)
                await self.say(f"🎲 <color=#7CFC00>{who}</color> got {what} from the random drop!")
            else:
                await self.say(f"🎲 Random drop: {len(given)} players got a random item!")
        if given:
            self.data.set("drops", int(self.data.get("drops", 0)) + len(given))
        return given

    @tasks.loop(minutes=1)
    async def drop_loop(self) -> None:
        if not self.bot.rcon.connected:
            return
        last = float(self.data.get("last", 0))
        if self.now() - last >= int(self.setting("every_minutes") or 30) * 60:
            self.data.set("last", self.now())
            await self.drop()

    # ------------------------------------------------------------ Discord

    @randomitems.command(name="toggle", description="Switch random items off (or on again) for your player.")
    async def toggle_cmd(self, interaction: discord.Interaction) -> None:
        player = self.linked_player(interaction.user.id)
        if not player:
            await interaction.response.send_message("Link your in-game player first: /customkit link", ephemeral=True)
            return
        on = self.toggle(player)
        await interaction.response.send_message(
            f"🎲 Random items for **{self.md(player)}**: {'on' if on else 'off'}.", ephemeral=True)

    @randomitems.command(name="now", description="Drop random items right now (admins).")
    @admin_only()
    async def now_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        given = await self.drop()
        await interaction.followup.send("🎲 " + (", ".join(given) if given else "Nobody to give to right now."),
                                        ephemeral=True)

    # ------------------------------------------------------------ panel

    def problems(self) -> list[str]:
        out = parse_items(self.setting("items"))[1]
        if not parse_items(self.setting("items"))[0]:
            out.append("No items in the list.")
        text = str(self.setting("optout_emote") or "").strip()
        if text and not self.emote_raw(text):
            out.append(f"Opt-out emote: “{text}” is not a quick chat emote.")
        return out

    def status(self) -> dict:
        return {"Drops given": int(self.data.get("drops", 0)), "Players opted out": len(self.opted_out())}
