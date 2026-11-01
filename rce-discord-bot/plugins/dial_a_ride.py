"""Dial-a-ride: a player uses a quick chat emote in game and a vehicle spawns right next to them – minicopter,
scrap transport helicopter, attack helicopter or a horse. Each ride has its own emote (empty = off), a cooldown per
player, an optional points price and an optional Discord role.

The spawn command is a setting (RCON-only Rust command, default `entity.spawnat {prefab} ({x},{y},{z})`), the same
way as the teleport command, so it can be adjusted for the server without a code change. Test it once with
/ride spawn before switching the emotes on.
Discord: /ride list · /ride spawn <vehicle> <player> (admins: test or gift a ride).
"""
from __future__ import annotations

import time

import discord
from discord import app_commands

from plugin_api import Plugin, Setting
from rce.rcon_client import RconError
from utils import admin_only, format_duration, player_choices

# vehicle key -> (label, icon, default emote, default prefab)
RIDES = {
    "minicopter": ("Minicopter", "🚁", "", "minicopter.entity"),
    "scrapheli": ("Scrap transport helicopter", "🛩️", "", "scraptransporthelicopter"),
    "attackheli": ("Attack helicopter", "🚀", "", "attackhelicopter.entity"),
    "horse": ("Horse", "🐎", "", "testridablehorse"),
}
SPAWN_DEFAULT = "entity.spawnat {prefab} ({x},{y},{z})"
OFFSET = 4.0                      # metres beside the player, so it doesn't spawn inside them


class DialARide(Plugin):
    NAME = "Dial-a-ride"
    ICON = "🚁"
    DESCRIPTION = ("Players call a ride with a quick chat emote: a minicopter, scrap helicopter, attack helicopter or "
                   "horse spawns right next to them – with a cooldown, an optional points price and role.")
    SETTINGS = (
        *(s for key, (label, _icon, emote, prefab) in RIDES.items() for s in (
            Setting(f"{key}_emote", f"{label}: emote", default=emote,
                    help="The quick chat emote that calls it (type it as in game, e.g. “I need a ride!”). "
                         "Empty = off."),
            Setting(f"{key}_price", f"{label}: points price", "int", 0, min=0, max=1_000_000,
                    help="0 = free. A price needs a linked account (/customkit link) and points & shop on."),
            Setting(f"{key}_prefab", f"{label}: entity name (experts)", default=prefab,
                    help="The entity the spawn command uses."),
        )),
        Setting("cooldown", "Cooldown per player (minutes)", "int", 30, min=0, max=10_080,
                help="Between two rides of the same player (any vehicle)."),
        Setting("linked_only", "Only linked players", "bool", False,
                help="Only players who linked their Discord account (/customkit link)."),
        Setting("role", "Only players with this Discord role", "role", help="Empty = everyone."),
        Setting("spawn_command", "Spawn command (experts)", default=SPAWN_DEFAULT,
                help="RCON command that spawns the vehicle. Placeholders: {prefab} {x} {y} {z} {name}."),
    )

    ride = app_commands.Group(name="ride", description="Dial-a-ride", guild_only=True)

    # ------------------------------------------------------------ Lookups

    def ride_for(self, raw: str | None) -> str | None:
        """The vehicle an emote calls (None = not a ride emote)."""
        if not raw:
            return None
        for key in RIDES:
            text = str(self.setting(f"{key}_emote") or "").strip()
            if text and self.emote_raw(text) == raw:
                return key
        return None

    def _allowed(self, player: str) -> str | None:
        role = self.setting("role")
        if self.setting("linked_only") or role:
            uid = self.linked_user(player)
            if uid is None:
                return "Link your account on Discord first (/customkit link)."
            if role:
                guild = self.guild
                member = guild.get_member(uid) if guild else None
                if member is None or not any(r.id == role for r in getattr(member, "roles", [])):
                    return "Dial-a-ride needs a Discord role you don't have."
        return None

    # ------------------------------------------------------------ In game

    @Plugin.listener()
    async def on_rce_chat(self, ev) -> None:
        if ev.channel == "SERVER":
            return
        key = self.ride_for(ev.raw)
        if key is not None:
            await self.call(ev.player, key)

    async def call(self, player: str, key: str, *, free: bool = False) -> bool:
        """Spawns the ride next to the player. free=True: an admin gift (no cooldown, no price, no role check)."""
        label, icon = RIDES[key][0], RIDES[key][1]
        name = self.online_name(player)
        if name is None:
            return False
        if not free:
            if (why := self._allowed(name)) is not None:
                await self.say(f"{name}: {why}")
                return False
            cooldown = self.setting("cooldown") * 60
            last = float(self.data.get(f"used:{name.lower()}", 0))
            if cooldown and time.time() - last < cooldown:
                await self.say(f"{name}: next ride in {format_duration(int(cooldown - (time.time() - last)) + 1)}.")
                return False
        price = 0 if free else int(self.setting(f"{key}_price") or 0)
        uid = None
        if price:
            uid = self.linked_user(name)
            if uid is None or not self.points_enabled:
                await self.say(f"{name}: a {label} costs {price} points – link your account on Discord first "
                               "(/customkit link).")
                return False
            if self.take_points(uid, price, f"{label} for {name}") is None:
                await self.say(f"{name}: a {label} costs {price} points – you don't have enough.")
                return False
        pos = await self.position(name)
        if pos is None:
            if uid is not None:
                self.give_points(uid, price, f"refund: {label} (no position)")
            await self.say(f"{name}: couldn't find where you are – try again in a moment.")
            return False
        x, y, z = pos
        command = str(self.setting("spawn_command") or SPAWN_DEFAULT)
        try:
            await self.bot.rcon.command(command.format(prefab=self.setting(f"{key}_prefab") or RIDES[key][3],
                                                       x=round(x + OFFSET, 2), y=round(y + 1, 2), z=round(z, 2),
                                                       name=name), expect_response=False)
        except (RconError, KeyError, IndexError, ValueError) as exc:
            self.log.warning("Ride %s for %s failed: %s", key, name, exc)
            if uid is not None:
                self.give_points(uid, price, f"refund: {label} (spawn failed)")
            await self.say(f"{name}: the {label} couldn't be spawned right now.")
            return False
        if not free:
            self.data.set(f"used:{name.lower()}", time.time())
        self.data.set(f"rides:{key}", int(self.data.get(f"rides:{key}", 0)) + 1)
        await self.say(f"<color=#7CFC00>{name}</color>: your {label} {icon} is here!"
                       + (f" (-{price} points)" if price else ""))
        feed = self.bot.feed(self.bot.config.admin_log_channel_id)
        if feed:
            feed.add_line(f"{icon} Dial-a-ride: **{self.md(name)}** got a {label}"
                          + (" (admin gift)" if free else f" ({price} points)" if price else ""))
        return True

    # ------------------------------------------------------------ Discord

    @ride.command(name="list", description="Which rides players can call, and how.")
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        lines = []
        for key, (label, icon, _e, _p) in RIDES.items():
            emote = str(self.setting(f"{key}_emote") or "").strip()
            if emote:
                price = int(self.setting(f"{key}_price") or 0)
                lines.append(f"{icon} **{label}** – emote **{self.md(emote)}**"
                             + (f" · {price} {self.bot.config.points_name}" if price else " · free"))
        cooldown = self.setting("cooldown")
        text = ("\n".join(lines) + (f"\nCooldown: {cooldown} min between rides." if cooldown else "")) if lines else \
            "No rides are switched on yet (Plugins → Dial-a-ride)."
        await interaction.response.send_message(text, ephemeral=True)

    @ride.command(name="spawn", description="Spawns a ride next to a player (admins – to test or as a gift).")
    @app_commands.describe(vehicle="Which ride", player="Online player")
    @app_commands.choices(vehicle=[app_commands.Choice(name=v[0], value=k) for k, v in RIDES.items()])
    @admin_only()
    async def spawn_cmd(self, interaction: discord.Interaction, vehicle: str, player: str) -> None:
        if self.online_name(player) is None:
            await interaction.response.send_message(f"**{self.md(player)}** isn't online.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok = await self.call(player, vehicle, free=True)
        await interaction.followup.send(
            f"{RIDES[vehicle][1]} {RIDES[vehicle][0]} spawned next to **{self.md(player)}**." if ok else
            "It didn't work – check the spawn command (Plugins → Dial-a-ride → experts) and the bot log.",
            ephemeral=True)

    @spawn_cmd.autocomplete("player")
    async def _player_ac(self, interaction, current: str):
        return player_choices(self.bot, current)

    # ------------------------------------------------------------ Panel

    def problems(self) -> list[str]:
        out, seen = [], {}
        for key, (label, *_rest) in RIDES.items():
            text = str(self.setting(f"{key}_emote") or "").strip()
            if not text:
                continue
            raw = self.emote_raw(text)
            if raw is None:
                out.append(f"{label}: “{text}” is not a quick chat emote (Location emotes are teleports).")
                continue
            if kit := self.emote_clash(raw):
                out.append(f"{label}: “{text}” also gives the kit {kit} – pick another emote or unbind it.")
            if raw in seen:
                out.append(f"{label} and {seen[raw]} use the same emote.")
            seen[raw] = label
        if not seen:
            out.append("No ride has an emote yet – set at least one below.")
        return out

    def status(self) -> dict:
        return {RIDES[k][0]: int(self.data.get(f"rides:{k}", 0)) for k in RIDES}
