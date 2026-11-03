"""Dial-a-ride: a player calls a vehicle with an emote combo in game and it spawns right next to them – minicopter,
scrap transport helicopter, attack helicopter or a horse. A combo is the start emote (default "Let's go!") and then,
within a few seconds, the ride's own emote (e.g. "OK" = minicopter, "No" = scrap heli). Empty start emote = a single
emote calls the ride. Each ride has its own emote (empty = off), an optional points price and Discord role; there's
one cooldown per player. Helicopters come with low grade fuel (default 100). The combo emotes are kept for
Dial-a-ride while it's on: they can't be bound to kits (support/emote_binds.reserve).

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

# vehicle key -> (label, icon, default emote (the combo's second one), default prefab, takes fuel)
RIDES = {
    "minicopter": ("Minicopter", "🚁", "OK", "minicopter.entity", True),
    "scrapheli": ("Scrap transport helicopter", "🛩️", "No", "scraptransporthelicopter", True),
    "attackheli": ("Attack helicopter", "🚀", "Yes", "attackhelicopter.entity", True),
    "horse": ("Horse", "🐎", "Thanks", "testridablehorse", False),
}
START_DEFAULT = "Let's go!"
FUEL_ITEM = "lowgradefuel"
SPAWN_DEFAULT = "entity.spawnat {prefab} ({x},{y},{z})"
OFFSET = 4.0                      # metres beside the player, so it doesn't spawn inside them


class DialARide(Plugin):
    NAME = "Dial-a-ride"
    ICON = "🚁"
    DESCRIPTION = ("Players call a ride with an emote combo (\"Let's go!\" then e.g. \"OK\"): a minicopter, scrap "
                   "helicopter, attack helicopter or horse spawns right next to them, helicopters with fuel – with a "
                   "cooldown, an optional points price and Discord role per ride. The combo emotes can't give kits.")
    SETTINGS = (
        Setting("start_emote", "Start emote (first of every combo)", default=START_DEFAULT,
                help="Players use this emote first, then the ride's emote. Empty = the ride's emote alone calls it."),
        Setting("combo_seconds", "Seconds for the second emote", "int", 15, min=3, max=120,
                help="How long after the start emote the ride's emote counts."),
        *(s for key, (label, _icon, emote, prefab, _fuel) in RIDES.items() for s in (
            Setting(f"{key}_emote", f"{label}: emote", default=emote,
                    help="The ride's emote (the second of the combo, type it as in game, e.g. “OK”). Empty = off."),
            Setting(f"{key}_role", f"{label}: only players with this Discord role", "role",
                    help="Empty = everyone (or the role for all rides below)."),
            Setting(f"{key}_price", f"{label}: points price", "int", 0, min=0, max=1_000_000,
                    help="0 = free. A price needs a linked account (/customkit link) and points & shop on."),
            Setting(f"{key}_prefab", f"{label}: entity name (experts)", default=prefab,
                    help="The entity the spawn command uses."),
        )),
        Setting("fuel", "Low grade fuel with every helicopter", "int", 100, min=0, max=1000,
                help="Given to the player with the minicopter, scrap or attack helicopter. 0 = none."),
        Setting("cooldown", "Cooldown per player (minutes)", "int", 30, min=0, max=10_080,
                help="Between two rides of the same player (any vehicle)."),
        Setting("linked_only", "Only linked players", "bool", False,
                help="Only players who linked their Discord account (/customkit link)."),
        Setting("role", "Only players with this Discord role (all rides)", "role", help="Empty = everyone."),
        Setting("spawn_command", "Spawn command (experts)", default=SPAWN_DEFAULT,
                help="RCON command that spawns the vehicle. Placeholders: {prefab} {x} {y} {z} {name}."),
    )

    ride = app_commands.Group(name="ride", description="Dial-a-ride", guild_only=True)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._started: dict[str, float] = {}           # player (lower case) -> when they used the start emote

    async def on_load(self) -> None:
        from support import emote_binds
        emote_binds.reserve(self.NAME, self.reserved_emotes)

    async def on_unload(self) -> None:
        from support import emote_binds
        emote_binds.release(self.NAME)

    def reserved_emotes(self) -> set[str]:
        """Every emote of the combos – kept for Dial-a-ride (no kits on them)."""
        texts = [self.setting("start_emote")] + [self.setting(f"{k}_emote") for k in RIDES]
        return {raw for t in texts if str(t or "").strip() and (raw := self.emote_raw(str(t).strip()))}

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

    async def _allowed(self, player: str, key: str) -> str | None:
        roles = [r for r in (self.setting("role"), self.setting(f"{key}_role")) if r]
        if self.setting("linked_only") or roles:
            uid = self.linked_user(player)
            if uid is None:
                return "Link your account on Discord first (/customkit link)."
            if roles:
                guild = self.guild
                member = guild.get_member(uid) if guild else None
                if member is None and guild is not None:
                    try:
                        member = await guild.fetch_member(uid)
                    except discord.HTTPException:
                        member = None
                have = {r.id for r in getattr(member, "roles", [])} if member is not None else set()
                if not all(r in have for r in roles):
                    return f"the {RIDES[key][0]} needs a Discord role you don't have."
        return None

    # ------------------------------------------------------------ In game

    @Plugin.listener()
    async def on_rce_chat(self, ev) -> None:
        if ev.channel == "SERVER":
            return
        await self.on_emote(ev.player, ev.raw)

    async def on_emote(self, player: str, raw: str, now: float | None = None) -> str | None:
        """Combo logic. Returns the ride it called (or None)."""
        now = time.time() if now is None else now
        start = self.emote_raw(str(self.setting("start_emote") or "").strip()) \
            if str(self.setting("start_emote") or "").strip() else None
        who = (player or "").lower()
        key = self.ride_for(raw)
        if start is None:                                  # no combo: the ride's emote alone
            if key is not None:
                await self.call(player, key)
            return key
        if raw == start:
            self._started[who] = now
            for name in [n for n, t in self._started.items() if now - t > 600]:
                self._started.pop(name, None)
            return None
        began = self._started.get(who)
        if key is None or began is None or now - began > int(self.setting("combo_seconds") or 15):
            return None
        self._started.pop(who, None)
        await self.call(player, key)
        return key

    async def call(self, player: str, key: str, *, free: bool = False) -> bool:
        """Spawns the ride next to the player. free=True: an admin gift (no cooldown, no price, no role check)."""
        label, icon, fuel_ride = RIDES[key][0], RIDES[key][1], RIDES[key][4]
        name = self.online_name(player)
        if name is None:
            return False
        if not free:
            if (why := await self._allowed(name, key)) is not None:
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
        fuel = int(self.setting("fuel") or 0) if fuel_ride else 0
        if fuel:
            try:
                await self.bot.rcon.command(self.bot.config.item_give_template.format(
                    name=name, item=FUEL_ITEM, amount=fuel), expect_response=False)
            except (RconError, KeyError, IndexError, ValueError) as exc:
                self.log.warning("Fuel for %s's %s: %s", name, key, exc)
                fuel = 0
        if not free:
            self.data.set(f"used:{name.lower()}", time.time())
        self.data.set(f"rides:{key}", int(self.data.get(f"rides:{key}", 0)) + 1)
        await self.say(f"<color=#7CFC00>{name}</color>: your {label} {icon} is here!"
                       + (f" +{fuel} low grade fuel" if fuel else "") + (f" (-{price} points)" if price else ""))
        feed = self.bot.feed(self.bot.config.admin_log_channel_id)
        if feed:
            feed.add_line(f"{icon} Dial-a-ride: **{self.md(name)}** got a {label}"
                          + (" (admin gift)" if free else f" ({price} points)" if price else ""))
        return True

    # ------------------------------------------------------------ Discord

    @ride.command(name="list", description="Which rides players can call, and how.")
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        lines = []
        start = str(self.setting("start_emote") or "").strip()
        for key, (label, icon, _e, _p, _f) in RIDES.items():
            emote = str(self.setting(f"{key}_emote") or "").strip()
            if emote:
                price = int(self.setting(f"{key}_price") or 0)
                combo = f"**{self.md(start)}** then **{self.md(emote)}**" if start else f"**{self.md(emote)}**"
                lines.append(f"{icon} **{label}** – {combo}"
                             + (f" · {price} {self.bot.config.points_name}" if price else " · free")
                             + (f" · <@&{self.setting(key + '_role')}> only" if self.setting(f"{key}_role") else ""))
        cooldown = self.setting("cooldown")
        text = ("\n".join(lines) + (f"\nCooldown: {cooldown} min between rides." if cooldown else "")
                + (f"\nThe second emote within {self.setting('combo_seconds')} s." if start else "")) if lines else \
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
        start_text = str(self.setting("start_emote") or "").strip()
        start = self.emote_raw(start_text) if start_text else None
        if start_text and start is None:
            out.append(f"Start emote: “{start_text}” is not a quick chat emote (Location emotes are teleports).")
        if start and (kit := self.emote_clash(start)):
            out.append(f"Start emote: the kit {kit} is bound to it too – it won't be given while Dial-a-ride is on "
                       "(unbind it).")
        for key, (label, *_rest) in RIDES.items():
            text = str(self.setting(f"{key}_emote") or "").strip()
            if not text:
                continue
            raw = self.emote_raw(text)
            if raw is None:
                out.append(f"{label}: “{text}” is not a quick chat emote (Location emotes are teleports).")
                continue
            if kit := self.emote_clash(raw):
                out.append(f"{label}: the kit {kit} is bound to “{text}” too – it won't be given while Dial-a-ride "
                           "is on (unbind it).")
            if start and raw == start:
                out.append(f"{label}: its emote is the start emote – pick another one.")
            if raw in seen:
                out.append(f"{label} and {seen[raw]} use the same emote.")
            seen[raw] = label
        if not seen:
            out.append("No ride has an emote yet – set at least one below.")
        return out

    def status(self) -> dict:
        return {RIDES[k][0]: int(self.data.get(f"rides:{k}", 0)) for k in RIDES}
