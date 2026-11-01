"""Zones & portals.

Zones: custom zones made with the server's zone commands (Custom zones guide) at an admin's position – e.g. a
PvE/safe area around an event, a no-build area, a raid-free trade zone – with a ring and an enter message.
  /zone create <name> <radius> [pvp] [building_damage] [message]   at YOUR position (linked, online) or player:
  /zone delete <name> · /zone list
Portals: an entry spot and an exit spot. A player who uses the portal emote while standing within the radius of an
entry is teleported to its exit.
  /portal create <name> [radius]   entry = your position · /portal exit <name> = your position
  /portal delete <name> · /portal list
"""
from __future__ import annotations

import math
import re
import time

import discord
from discord import app_commands

from plugin_api import Plugin, Setting
from rce.rcon_client import RconError
from support import teleports as tp
from utils import admin_only, player_choices

ZONE_NAME = re.compile(r"^[A-Za-z0-9 _-]{2,30}$")
CREATE = """zones.createcustomzone "{zone}" ({x},{y},{z}) 0 Sphere {radius} {pvp} 1 0 {building} 1
zones.editcustomzone "{zone}" showarea 1
zones.editcustomzone "{zone}" color ({color})
zones.editcustomzone "{zone}" showchatmessage 1
zones.editcustomzone "{zone}" entermessage "{message}" """
DELETE = 'zones.deletecustomzone "{zone}"'


class Zones(Plugin):
    NAME = "Zones & portals"
    ICON = "🌀"
    DESCRIPTION = ("Admins create zones (PvE/safe/no-raid areas with a ring and enter message) and portals (stand "
                   "at the entry, use the portal emote, get teleported to the exit).")
    SETTINGS = (
        Setting("create_command", "Command: create zone", "textarea", CREATE,
                help="Placeholders: {zone} {x} {y} {z} {radius} {pvp} (1/0) {building} (building damage 1/0) "
                     "{color} {message}. Default from the Custom zones guide."),
        Setting("delete_command", "Command: delete zone", "text", DELETE),
        Setting("portal_emote", "Emote: use a portal", default="Go, go, go!",
                help="The quick chat emote players use while standing at a portal entry."),
        Setting("portal_cooldown", "Portal cooldown (seconds)", "int", 30, min=0, max=3600),
    )

    zone = app_commands.Group(name="zone", description="Custom zones (admins)", guild_only=True)
    portal = app_commands.Group(name="portal", description="Portals (admins)", guild_only=True)

    # ------------------------------------------------------------ Helpers

    def zones(self) -> dict:
        return dict(self.data.get("zones", {}))

    def portals(self) -> dict:
        return dict(self.data.get("portals", {}))

    async def _admin_pos(self, interaction: discord.Interaction, player: str | None):
        who = player or self.linked_player(interaction.user.id)
        if not who or self.online_name(who) is None:
            raise ValueError("Stand on the spot in game first (your linked player must be online), or give player:.")
        pos = await self.position(who)
        if pos is None:
            raise ValueError("Couldn't read the position from the server.")
        return pos

    async def _run(self, template: str, **values) -> list[str]:
        errors = []
        for line in str(template).splitlines():
            if not line.strip():
                continue
            try:
                await self.bot.rcon.command(line.strip().format(**values), expect_response=False)
            except (RconError, KeyError, IndexError, ValueError) as exc:
                errors.append(str(exc))
        return errors

    # ------------------------------------------------------------ Zones

    async def create_zone(self, name: str, pos, radius: int, *, pvp: bool, building: bool, message: str) -> list[str]:
        values = {"zone": f"zone_{name}", "x": round(pos[0], 1), "y": round(pos[1], 1), "z": round(pos[2], 1),
                  "radius": radius, "pvp": int(pvp), "building": int(building),
                  "color": "0,255,0" if not pvp else ("255,165,0" if building else "255,255,0"),
                  "message": message.replace('"', "'")[:120] or f"You entered {name}"}
        errors = await self._run(self.setting("create_command"), **values)
        zones = self.zones()
        zones[name.lower()] = {"name": name, "x": pos[0], "y": pos[1], "z": pos[2], "radius": radius, "pvp": pvp,
                               "building": building, "message": values["message"], "at": self.now()}
        self.data.set("zones", zones)
        return errors

    @zone.command(name="create", description="Create a zone at your position.")
    @app_commands.describe(name="Zone name", radius="Radius in metres", pvp="PvP allowed inside",
                           building_damage="Bases inside can be damaged (raided)", message="Shown when entering",
                           player="Whose position (default: your linked player)")
    @admin_only()
    async def zone_create(self, interaction: discord.Interaction, name: str,
                          radius: app_commands.Range[int, 5, 500] = 50, pvp: bool = False,
                          building_damage: bool = False, message: str = "", player: str | None = None) -> None:
        if not ZONE_NAME.match(name):
            await interaction.response.send_message("Name: 2–30 letters, numbers, spaces, - or _.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            pos = await self._admin_pos(interaction, player)
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        errors = await self.create_zone(name, pos, radius, pvp=pvp, building=building_damage, message=message)
        self.audit(f"{interaction.user} created zone **{self.md(name)}** at {tp.fmt(*pos)} r={radius}")
        await interaction.followup.send(f"🌀 Zone **{self.md(name)}** at `{tp.fmt(*pos)}`, {radius} m – PvP "
                                        f"{'on' if pvp else 'off'}, raiding {'on' if building_damage else 'off'}."
                                        + (f"\n⚠️ {'; '.join(errors)[:500]}" if errors else ""), ephemeral=True)

    @zone.command(name="delete", description="Delete a zone.")
    @admin_only()
    async def zone_delete(self, interaction: discord.Interaction, name: str) -> None:
        zones = self.zones()
        z = zones.pop(name.strip().lower(), None)
        if z is None:
            await interaction.response.send_message("No such zone.", ephemeral=True)
            return
        self.data.set("zones", zones)
        await self._run(self.setting("delete_command"), zone=f"zone_{z['name']}")
        self.audit(f"{interaction.user} deleted zone **{self.md(z['name'])}**")
        await interaction.response.send_message(f"🗑️ Zone **{self.md(z['name'])}** deleted.", ephemeral=True)

    @zone.command(name="list", description="All zones.")
    async def zone_list(self, interaction: discord.Interaction) -> None:
        lines = [f"🌀 **{self.md(z['name'])}** `{tp.fmt(z['x'], z['y'], z['z'])}` {z['radius']} m · PvP "
                 f"{'on' if z['pvp'] else 'off'} · raiding {'on' if z['building'] else 'off'}"
                 for z in self.zones().values()]
        lines += [f"🔵 portal **{self.md(p['name'])}**" + (" ✅" if p.get("exit") else " (no exit yet)")
                  for p in self.portals().values()]
        await interaction.response.send_message("\n".join(lines)[:1990] or "No zones or portals yet.", ephemeral=True)

    # ------------------------------------------------------------ Portals

    @portal.command(name="create", description="Create a portal: its entry is where you stand now.")
    @admin_only()
    async def portal_create(self, interaction: discord.Interaction, name: str,
                            radius: app_commands.Range[int, 2, 50] = 5, player: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            pos = await self._admin_pos(interaction, player)
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        portals = self.portals()
        old = portals.get(name.strip().lower(), {})
        portals[name.strip().lower()] = {"name": name.strip()[:30], "entry": list(pos), "radius": radius,
                                         "exit": old.get("exit")}
        self.data.set("portals", portals)
        await interaction.followup.send(f"🔵 Portal **{self.md(name)}** entry at `{tp.fmt(*pos)}` ({radius} m). Now "
                                        "stand at the exit and run `/portal exit`.", ephemeral=True)

    @portal.command(name="exit", description="Set a portal's exit to where you stand now.")
    @admin_only()
    async def portal_exit(self, interaction: discord.Interaction, name: str, player: str | None = None) -> None:
        portals = self.portals()
        p = portals.get(name.strip().lower())
        if p is None:
            await interaction.response.send_message("No such portal – /portal create first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            pos = await self._admin_pos(interaction, player)
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        p["exit"] = list(pos)
        self.data.set("portals", portals)
        await interaction.followup.send(f"✅ Portal **{self.md(p['name'])}** exit at `{tp.fmt(*pos)}`. Players stand at "
                                        f"the entry and use **{self.setting('portal_emote')}**.", ephemeral=True)

    @portal.command(name="delete", description="Delete a portal.")
    @admin_only()
    async def portal_delete(self, interaction: discord.Interaction, name: str) -> None:
        portals = self.portals()
        ok = portals.pop(name.strip().lower(), None) is not None
        self.data.set("portals", portals)
        await interaction.response.send_message("🗑️ Deleted." if ok else "No such portal.", ephemeral=True)

    @zone_create.autocomplete("player")
    @portal_create.autocomplete("player")
    @portal_exit.autocomplete("player")
    async def _player_ac(self, interaction, current: str):
        return player_choices(self.bot, current)

    @staticmethod
    def _near(pos, spot, radius: float) -> bool:
        return math.dist((pos[0], pos[2]), (spot[0], spot[2])) <= radius and abs(pos[1] - spot[1]) <= radius + 10

    async def use_portal(self, player: str) -> str | None:
        """The player used the portal emote: teleports if they stand at an entry. Returns the portal name."""
        last = float(self.data.get(f"used:{player.lower()}", 0))
        if time.time() - last < self.setting("portal_cooldown"):
            return None
        ready = [p for p in self.portals().values() if p.get("exit")]
        if not ready:
            return None
        pos = await self.position(player)
        if pos is None:
            return None
        for p in ready:
            if self._near(pos, p["entry"], p["radius"]):
                self.data.set(f"used:{player.lower()}", time.time())
                if await self.teleport(player, *p["exit"]):
                    self.data.set("portal_uses", int(self.data.get("portal_uses", 0)) + 1)
                    return p["name"]
        return None

    @Plugin.listener()
    async def on_rce_chat(self, ev) -> None:
        if ev.channel != "SERVER" and ev.raw == self.emote_raw(self.setting("portal_emote")):
            await self.use_portal(ev.player)

    # ------------------------------------------------------------ Panel

    def problems(self) -> list[str]:
        raw = self.emote_raw(self.setting("portal_emote"))
        if raw is None:
            return [f"“{self.setting('portal_emote')}” is not a quick chat emote."]
        if kit := self.emote_clash(raw):
            return [f"The portal emote also gives the kit {kit} – pick another emote or unbind it."]
        return []

    def status(self) -> dict:
        return {"Zones": len(self.zones()), "Portals": len(self.portals()),
                "Portal uses": int(self.data.get("portal_uses", 0))}
