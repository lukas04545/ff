"""TP Home: players save their home (e.g. at their bed) with one quick chat emote and teleport back with another.

In game:  "Set home" emote  -> the spot they stand on is saved (server.printpos)
          "Go home" emote   -> teleport there after a short delay (global.teleportpos), then a cooldown
Discord:  /home show · /home delete (their linked player) · /home clear <player> (admins)
"""
from __future__ import annotations

import asyncio
import time

import discord
from discord import app_commands

from plugin_api import Plugin, Setting
from support import teleports as tp
from utils import admin_only, format_duration, player_choices


class TpHome(Plugin):
    NAME = "TP Home"
    ICON = "🏠"
    DESCRIPTION = ("Players save their home with a quick chat emote and teleport back to it with another one – with a "
                   "delay and a cooldown. Like /home on PC servers.")
    SETTINGS = (
        Setting("set_emote", "Emote: set home", default="We need beds!",
                help="The quick chat emote that saves the spot the player stands on (type it as in game)."),
        Setting("go_emote", "Emote: go home", default="Let's go!",
                help="The quick chat emote that teleports the player home."),
        Setting("delay", "Delay before the teleport (seconds)", "int", 10, min=0, max=120,
                help="Stand still: they're told it's coming, then teleported."),
        Setting("cooldown", "Cooldown (minutes)", "int", 15, min=0, max=1440),
        Setting("linked_only", "Only linked players", "bool", False,
                help="Only players who linked their Discord account (/customkit link) can use it."),
        Setting("role", "Only players with this Discord role", "role",
                help="Empty = everyone (needs a linked account if set)."),
    )

    home = app_commands.Group(name="home", description="TP Home", guild_only=True)

    # ------------------------------------------------------------ Data

    def home_of(self, player: str) -> dict | None:
        return self.data.get(f"home:{player.lower()}")

    def _allowed(self, player: str) -> str | None:
        """Why this player may not use TP Home (None = may)."""
        role = self.setting("role")
        if self.setting("linked_only") or role:
            uid = self.linked_user(player)
            if uid is None:
                return "Link your account on Discord first (/customkit link)."
            if role:
                guild = self.guild
                member = guild.get_member(uid) if guild else None
                if member is None or not any(r.id == role for r in getattr(member, "roles", [])):
                    return "TP Home needs a Discord role you don't have."
        return None

    # ------------------------------------------------------------ In game

    @Plugin.listener()
    async def on_rce_chat(self, ev) -> None:
        if ev.channel == "SERVER":
            return
        if ev.raw == self.emote_raw(self.setting("set_emote")):
            await self.set_home(ev.player)
        elif ev.raw == self.emote_raw(self.setting("go_emote")):
            await self.go_home(ev.player)

    async def set_home(self, player: str) -> bool:
        if (why := self._allowed(player)) is not None:
            await self.say(f"{player}: {why}")
            return False
        pos = await self.position(player)
        if pos is None:
            await self.say(f"{player}: couldn't read your position – try again in a moment.")
            return False
        self.data.set(f"home:{player.lower()}", {"player": player, "x": pos[0], "y": pos[1], "z": pos[2],
                                                  "at": self.now()})
        await self.say(f"<color=#7CFC00>{player}</color>: home saved. Use the go-home emote to come back.")
        return True

    async def go_home(self, player: str) -> bool:
        if (why := self._allowed(player)) is not None:
            await self.say(f"{player}: {why}")
            return False
        home = self.home_of(player)
        if home is None:
            await self.say(f"{player}: no home yet – stand at your bed and use the set-home emote.")
            return False
        cooldown = self.setting("cooldown") * 60
        last = float(self.data.get(f"used:{player.lower()}", 0))
        if cooldown and time.time() - last < cooldown:
            await self.say(f"{player}: home again in {format_duration(int(cooldown - (time.time() - last)) + 1)}.")
            return False
        self.data.set(f"used:{player.lower()}", time.time())
        delay = self.setting("delay")
        if delay:
            await self.say(f"{player}: teleporting home in {delay} seconds …")
            await asyncio.sleep(delay)
        if self.online_name(player) is None:
            return False
        ok = await self.teleport(player, home["x"], home["y"], home["z"])
        if ok:
            self.data.set("teleports", int(self.data.get("teleports", 0)) + 1)
        return ok

    # ------------------------------------------------------------ Discord

    @home.command(name="show", description="Where your home is saved.")
    async def show_cmd(self, interaction: discord.Interaction) -> None:
        player = self.linked_player(interaction.user.id)
        home = self.home_of(player) if player else None
        if home is None:
            await interaction.response.send_message(
                f"No home saved. In game: stand at your bed and use **{self.setting('set_emote')}**; go home with "
                f"**{self.setting('go_emote')}**." + ("" if player else " (Link your player with /customkit link.)"),
                ephemeral=True)
            return
        await interaction.response.send_message(f"🏠 **{self.md(home['player'])}**'s home: "
                                                f"`{tp.fmt(home['x'], home['y'], home['z'])}` (saved <t:{home['at']}:R>)",
                                                ephemeral=True)

    @home.command(name="delete", description="Deletes your saved home.")
    async def delete_cmd(self, interaction: discord.Interaction) -> None:
        player = self.linked_player(interaction.user.id)
        ok = bool(player) and self.data.delete(f"home:{player.lower()}")
        await interaction.response.send_message("🗑️ Home deleted." if ok else "You have no saved home.", ephemeral=True)

    @home.command(name="clear", description="Deletes a player's home (admins).")
    @app_commands.describe(player="In-game name")
    @admin_only()
    async def clear_cmd(self, interaction: discord.Interaction, player: str) -> None:
        ok = self.data.delete(f"home:{player.strip().lower()}")
        await interaction.response.send_message("🗑️ Deleted." if ok else "That player has no home.", ephemeral=True)

    @clear_cmd.autocomplete("player")
    async def _player_ac(self, interaction, current: str):
        return player_choices(self.bot, current)

    # ------------------------------------------------------------ Panel

    def problems(self) -> list[str]:
        out = []
        for key in ("set_emote", "go_emote"):
            raw = self.emote_raw(self.setting(key))
            if raw is None:
                out.append(f"“{self.setting(key)}” is not a quick chat emote (Location emotes are teleports).")
            elif kit := self.emote_clash(raw):
                out.append(f"“{self.setting(key)}” also gives the kit {kit} – pick another emote or unbind it.")
        if self.emote_raw(self.setting("set_emote")) == self.emote_raw(self.setting("go_emote")):
            out.append("Set home and go home need two different emotes.")
        return out

    def status(self) -> dict:
        return {"Homes saved": len(self.data.items("home:")), "Teleports home": int(self.data.get("teleports", 0))}
