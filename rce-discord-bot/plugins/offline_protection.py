"""Offline protection zones: one zone per in-game team, placed by the team leader with quick chat, red while the
whole team has been offline for a while, green again the moment anyone of the team is back.

Placing: the team leader sends "Can I build here?" and then "Yes" (quick chat / emote wheel) within a few seconds.
Only the leader of a team can place (or move) its zone; members and players without a team get a short in-game note.

Status: red when every member has been offline for longer than "Offline time before red" (minutes); green as soon as
one member comes online (checked on every join, no waiting for the timer).

Zone commands – only what the Rust Console Edition community server guide "Custom zones" documents:
  zones.createcustomzone "Name" (x,y,z) rotation Sphere|Box size pvp npc_damage radiation building_damage building
  zones.editcustomzone "Name" Setting Value     (allowbuildingdamage 0/1, showarea 0/1, color (R,G,B),
                                                  showchatmessage 0/1, entermessage "…", leavemessage "…")
  zones.deletecustomzone "Name"
  server.printpos "ID"                          (prints the current position of the given target)
  relationshipmanager.logteamactions 1          (team log lines on – teams and leaders come from them)
Green = building damage allowed, a green ring; red = building damage off (offline raid protection), a red ring.
All commands are settings (defaults below) – an empty one is never sent.
"""
from __future__ import annotations

import re
import time

from discord.ext import tasks

from plugin_api import Plugin, Setting
from rce.rcon_client import RconError
from utils import sanitize_player_name

ASK = "d11_quick_chat_questions_slot_1"         # "Can I build here?"
YES = "d11_quick_chat_responses_slot_0"         # "Yes"
NUMBER = r"(-?\d+(?:\.\d+)?)"
# Reading a position from the reply: "(x, y, z)" like the guide writes positions, else the first three numbers
POSITION_PAREN = re.compile(r"\(\s*" + NUMBER + r"\s*,\s*" + NUMBER + r"\s*,\s*" + NUMBER + r"\s*\)")
POSITION_FALLBACK = re.compile(NUMBER + r"[\s,;]+" + NUMBER + r"[\s,;]+" + NUMBER)
# Documented commands (guide "Custom zones"). Create: a sphere around the position, PvP on, NPC damage on, no
# radiation, building damage allowed (normal, raidable while the team is online – green), building allowed; then the
# visible ring (shown within 100 m) and the chat message when entering.
CREATE_DEFAULT = """zones.createcustomzone "{zone}" ({x},{y},{z}) 0 Sphere {radius} 1 1 0 1 1
zones.editcustomzone "{zone}" showarea 1
zones.editcustomzone "{zone}" color (0,255,0)
zones.editcustomzone "{zone}" showchatmessage 1"""
RED_DEFAULT = """zones.editcustomzone "{zone}" allowbuildingdamage 0
zones.editcustomzone "{zone}" color (255,0,0)
zones.editcustomzone "{zone}" entermessage "Offline protection: this team is offline, their base can't be raided." """
GREEN_DEFAULT = """zones.editcustomzone "{zone}" allowbuildingdamage 1
zones.editcustomzone "{zone}" color (0,255,0)
zones.editcustomzone "{zone}" entermessage "This team is online – offline protection is off." """
REMOVE_DEFAULT = 'zones.deletecustomzone "{zone}"'
POSITION_DEFAULT = 'server.printpos "{name}"'
COMMAND_HELP = ("Placeholders: {zone} (zone name), {team} (team ID), {leader} (leader's name), {x} {y} {z} "
                "(position), {radius}. Several commands: one per line. Empty = nothing is sent.")


class OfflineProtection(Plugin):
    NAME = "Offline protection zones"
    ICON = "🛡️"
    DESCRIPTION = ("One protection zone per team, placed by the team leader with the quick chats \"Can I build "
                   "here?\" + \"Yes\". Red while the whole team is offline longer than the set time, green the "
                   "moment a member is back. Runs the zone commands you enter (from the Custom zones guide).")
    SETTINGS = (
        Setting("offline_minutes", "Offline time before red (minutes)", "int", 30, min=1, max=10080,
                help="How long every member of a team must be offline before the zone turns red."),
        Setting("confirm_seconds", "Seconds to answer \"Yes\"", "int", 20, min=5, max=120,
                help="Time between \"Can I build here?\" and \"Yes\" for the leader to place the zone."),
        Setting("radius", "Zone radius in metres ({radius})", "int", 30, min=1, max=100000,
                help="Size of the sphere around the leader's position (zones.createcustomzone, Zone Size)."),
        Setting("zone_prefix", "Zone name prefix ({zone})", "text", "team_",
                help="Zone names are this + the team ID, e.g. team_123."),
        Setting("position_command", "Command that returns a player's position", "text", POSITION_DEFAULT,
                help="Run for the leader when placing ({name} = the leader). Its reply must contain the x, y, z "
                     "position. Default: server.printpos (guide: prints the current position of the target ID)."),
        Setting("position_pattern", "Position in the reply (regular expression)", "text", "",
                help="Optional: a regular expression with the groups (?P<x>…) (?P<y>…) (?P<z>…). Empty = the "
                     "first (x, y, z) in the reply, else the first three numbers."),
        Setting("create_command", "Command(s): create the zone", "textarea", CREATE_DEFAULT,
                help="Default: zones.createcustomzone – name, position, rotation 0, Sphere, radius, PvP 1, NPC "
                     "damage 1, radiation 0, building damage 1, building 1 (see the guide). " + COMMAND_HELP),
        Setting("remove_command", "Command(s): remove a zone", "textarea", REMOVE_DEFAULT,
                help="Run when a leader moves the zone or the team is gone. " + COMMAND_HELP),
        Setting("red_command", "Command(s): status red (team offline)", "textarea", RED_DEFAULT,
                help="Default: building damage off, red ring, enter message (zones.editcustomzone). "
                     + COMMAND_HELP),
        Setting("green_command", "Command(s): status green (team online)", "textarea", GREEN_DEFAULT,
                help="Default: building damage on, green ring, enter message (zones.editcustomzone). "
                     + COMMAND_HELP),
        Setting("team_logs", "Make sure the server logs team actions", "bool", True,
                help="Runs relationshipmanager.logteamactions 1 when the plugin starts – teams and leaders are "
                     "read from these log lines (create, join, leave, new leader). It's on by default on servers."),
        Setting("tell_leader", "Tell the leader in-game what happened", "bool", True),
        Setting("channel", "Discord channel for status changes (optional)", "channel", None),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._asked: dict[str, float] = {}        # player (lower) -> when he asked "Can I build here?"

    # ------------------------------------------------------------ Setup / card

    async def on_load(self) -> None:
        self._team_logs_done = False

    async def _ensure_team_logs(self) -> None:
        if self.setting("team_logs") and not getattr(self, "_team_logs_done", False):
            try:
                await self.bot.rcon.command("relationshipmanager.logteamactions 1", expect_response=False)
                self._team_logs_done = True
            except RconError as exc:
                self.log.debug("logteamactions: %s", exc)

    def problems(self) -> list[str]:
        missing = [label for key, label in (
            ("position_command", "a command that returns a player's position"),
            ("create_command", "the create zone command"),
            ("red_command", "the command for status red"),
            ("green_command", "the command for status green"))
            if not str(self.setting(key) or "").strip()]
        if not missing:
            pattern = str(self.setting("position_pattern") or "").strip()
            try:
                if pattern and not {"x", "y", "z"} <= set(re.compile(pattern).groupindex):
                    return ["The position pattern needs the groups (?P<x>…), (?P<y>…) and (?P<z>…)."]
            except re.error as exc:
                return [f"The position pattern is not a valid regular expression: {exc}"]
            return []
        return ["Not sent to the server – enter " + ", ".join(missing) + " (commands: see the Custom zones "
                "guide of the Rust Console Edition community server docs)."]

    def status(self) -> dict:
        zones = self.zones()
        return {"Zones": len(zones), "Red (team offline)": sum(1 for z in zones.values() if z["state"] == "red"),
                "Green": sum(1 for z in zones.values() if z["state"] == "green")}

    # ------------------------------------------------------------ Zones (kept in self.data)

    def zones(self) -> dict[int, dict]:
        return {int(k.split(":", 1)[1]): v for k, v in self.data.items("zone:").items()}

    def zone(self, team_id: int) -> dict | None:
        return self.data.get(f"zone:{team_id}")

    def _save(self, team_id: int, zone: dict) -> None:
        self.data.set(f"zone:{team_id}", zone)

    def _vars(self, zone: dict) -> dict:
        leader = sanitize_player_name(zone["leader"])        # no quotes: it goes into "…" in commands
        return {"zone": zone["name"], "team": zone["team"], "leader": leader, "name": leader,
                "x": zone["x"], "y": zone["y"], "z": zone["z"], "radius": self.setting("radius")}

    async def _run(self, key: str, values: dict) -> bool:
        """Runs the command lines of a setting with the placeholders filled in. False if empty or failed."""
        lines = [line.strip() for line in str(self.setting(key) or "").splitlines() if line.strip()]
        if not lines:
            return False
        ok = True
        for line in lines:
            try:
                command = line.format(**values)
            except (KeyError, IndexError, ValueError) as exc:
                self.log.warning("%s: bad placeholder in %r: %s", key, line, exc)
                return False
            try:
                await self.bot.rcon.command(command, expect_response=False)
            except RconError as exc:
                self.log.warning("%s failed (%s): %s", key, command, exc)
                ok = False
        return ok

    async def _tell(self, player: str, text: str) -> None:
        if self.setting("tell_leader"):
            await self.say(f"{player}: {text}")

    async def _announce(self, text: str) -> None:
        channel = self.channel(self.setting("channel"))
        if channel is not None:
            await self.post(channel, text)

    # ------------------------------------------------------------ Placing with quick chat

    @Plugin.listener()
    async def on_rce_chat(self, ev) -> None:
        raw = str(getattr(ev, "raw", "") or "").strip()
        player = str(getattr(ev, "player", "") or "")
        if not player or getattr(ev, "channel", "") == "SERVER":
            return
        key, now = player.lower(), time.monotonic()
        if raw == ASK:
            self._asked[key] = now
        elif raw == YES:
            asked = self._asked.pop(key, None)
            if asked is not None and now - asked <= self.setting("confirm_seconds"):
                await self.place(player)

    async def place(self, player: str) -> str:
        """The player asked to place his team's zone at his position. Returns what happened (also told in-game)."""
        team = self.bot.db.team_of(player)
        if team is None:
            msg = "you need to be in a team to place a protection zone."
        elif (team[0]["leader"] or "").lower() != player.lower():
            msg = "only your team leader can place the protection zone."
        else:
            msg = await self._place_for_leader(player, int(team[0]["team_id"]), team[1])
        await self._tell(player, msg)
        return msg

    async def _place_for_leader(self, leader: str, team_id: int, members: list[str]) -> str:
        if not all(str(self.setting(k) or "").strip() for k in ("position_command", "create_command")):
            return "protection zones aren't set up yet – tell an admin."
        position = await self._position(leader)
        if position is None:
            return "couldn't read your position – try again in a moment."
        old = self.zone(team_id)
        if old is not None:
            await self._run("remove_command", self._vars(old))
        x, y, z = position
        zone = {"team": team_id, "name": f"{self.setting('zone_prefix')}{team_id}", "leader": leader,
                "x": x, "y": y, "z": z, "state": "", "offline_since": None, "placed": int(time.time())}
        if not await self._run("create_command", self._vars(zone)):
            return "the zone couldn't be created – tell an admin."
        self._save(team_id, zone)
        self.audit(f"{leader} placed the offline protection zone of team {team_id} at {x:.0f} {y:.0f} {z:.0f}")
        await self._update(team_id)
        moved = " (moved)" if old is not None else ""
        return f"your team's protection zone is placed here{moved}."

    async def _position(self, player: str) -> tuple[float, float, float] | None:
        name = sanitize_player_name(player)
        try:
            reply = await self.bot.rcon.command(str(self.setting("position_command")).format(name=name))
        except (RconError, KeyError, IndexError, ValueError) as exc:
            self.log.warning("Position of %s: %s", player, exc)
            return None
        pattern = str(self.setting("position_pattern") or "").strip()
        try:
            m = re.search(pattern, reply or "") if pattern else \
                (POSITION_PAREN.search(reply or "") or POSITION_FALLBACK.search(reply or ""))
        except re.error:
            return None
        if m is None:
            return None
        try:
            if pattern:
                return float(m["x"]), float(m["y"]), float(m["z"])
            return float(m[1]), float(m[2]), float(m[3])
        except (IndexError, ValueError):
            return None

    # ------------------------------------------------------------ Red / green

    def _online(self, members: list[str], joined: str = "", left: str = "") -> bool:
        online = {n.lower() for n in self.bot.online_players} | ({joined.lower()} if joined else set())
        online.discard(left.lower())
        return any(m.lower() in online for m in members)

    async def _update(self, team_id: int, now: float | None = None, *, joined: str = "", left: str = "") -> None:
        """Sets the zone's status: green if a member is online, red once all were offline long enough."""
        zone = self.zone(team_id)
        if zone is None:
            return
        now = time.time() if now is None else now
        team = self.bot.db.team_by_id(team_id)
        if team is None or not team[1]:                     # the team is gone: its zone goes too
            await self._run("remove_command", self._vars(zone))
            self.data.delete(f"zone:{team_id}")
            self.audit(f"Offline protection zone of team {team_id} removed (the team doesn't exist any more)")
            return
        zone["leader"] = team[0]["leader"] or zone["leader"]
        if self._online(team[1], joined, left):
            zone["offline_since"] = None
            if zone["state"] != "green":
                zone["state"] = "green"
                await self._run("green_command", self._vars(zone))
                await self._announce(f"🟢 Team **{self.md(zone['leader'])}** is online – zone "
                                     f"`{zone['name']}` green.")
        else:
            if zone["offline_since"] is None:
                zone["offline_since"] = now                  # from now on (the bot can only count what it saw)
            if zone["state"] != "red" and now - zone["offline_since"] >= self.setting("offline_minutes") * 60:
                zone["state"] = "red"
                await self._run("red_command", self._vars(zone))
                await self._announce(f"🔴 Team **{self.md(zone['leader'])}** has been offline for "
                                     f"{self.setting('offline_minutes')} min – zone `{zone['name']}` red.")
            elif not zone["state"]:
                zone["state"] = "green"                       # just placed, timer running
        self._save(team_id, zone)

    @Plugin.listener()
    async def on_rce_player_join(self, name: str) -> None:
        team = self.bot.db.team_of(name)
        if team is not None and self.zone(int(team[0]["team_id"])) is not None:
            await self._update(int(team[0]["team_id"]), joined=name)      # green right away

    @Plugin.listener()
    async def on_rce_player_leave(self, name: str) -> None:
        team = self.bot.db.team_of(name)
        if team is not None and self.zone(int(team[0]["team_id"])) is not None:
            await self._update(int(team[0]["team_id"]), left=name)        # last one out: the timer starts

    @tasks.loop(seconds=20)
    async def clock(self) -> None:
        await self._ensure_team_logs()                       # once, as soon as RCON is up
        for team_id in list(self.zones()):
            try:
                await self._update(team_id)
            except Exception:
                self.log.exception("Offline protection zone of team %s", team_id)
