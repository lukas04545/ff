"""Battle royale: players drop from the sky at random spots with a parachute and a pistol, a ring of radiation closes
in, the last one alive wins.

A round:
  1. An admin opens the lobby (/br start), players join on Discord (/br join – linked and online in-game).
  2. When the lobby closes, every player is killed once (optional, on by default) so they respawn with an empty
     inventory – their old gear stays in a bag where they died. As soon as one respawns the bot teleports them high
     above a random spot inside the zone and gives the start kit.
  3. The zone: a ring of radiation spheres just outside the safe circle (zones.createcustomzone … radiation 1) plus a
     visible border. Every few minutes (warned beforehand) it shrinks: the ring is deleted and created again, smaller,
     around a new centre inside the old circle. The guide has no command to resize a zone, so it's always delete +
     create. Players far outside – beyond the radiation ring – are warned and then killed (they read their position
     with server.printpos every few seconds).
  4. Every death of a dropped player counts (killfeed, suicide, radiation, the bot's kill) and so does leaving the
     server. The last one alive wins: announcement and optional points / kit.

Parachute already worn: give the start kit as an in-game kit (Kits page / kit manager) with the parachute in the
"Wear" container. Without a kit the items below are given with inventory.giveto – they land in the inventory and the
player has to put the parachute on.

Only documented commands are used (Custom zones guide, global.teleportpos, global.killplayer, server.printpos,
inventory.giveto, kit givetoplayer) and every one is an editable setting. A round interrupted by a bot restart is
ended on start and its zones are deleted.
"""
from __future__ import annotations

import math
import random
import re
import time

import discord
from discord import app_commands
from discord.ext import tasks

from plugin_api import Plugin, Setting
from rce.rcon_client import RconError
from utils import admin_only, player_choices, sanitize_player_name

COLOUR = discord.Colour(0xE67E22)
NUMBER = r"(-?\d+(?:\.\d+)?)"
POSITION = re.compile(r"\(\s*" + NUMBER + r"\s*,\s*" + NUMBER + r"\s*,\s*" + NUMBER + r"\s*\)")
SPACING = 0.8                  # ring spheres: centre distance as a share of their radius (they overlap)
RESPAWN_WAIT = 90              # seconds a killed player has to respawn before he's out

RING_DEFAULT = """zones.createcustomzone "{zone}" ({x},{y},{z}) 0 Sphere {radius} 1 1 1 1 1
zones.editcustomzone "{zone}" showarea 1
zones.editcustomzone "{zone}" color (255,90,0)
zones.editcustomzone "{zone}" showchatmessage 1
zones.editcustomzone "{zone}" entermessage "RADIATION – get back into the zone!" """
BORDER_DEFAULT = """zones.createcustomzone "{zone}" ({x},{y},{z}) 0 Sphere {radius} 1 1 0 1 1
zones.editcustomzone "{zone}" showarea 1
zones.editcustomzone "{zone}" color (0,170,255)"""
DELETE_DEFAULT = 'zones.deletecustomzone "{zone}"'
ITEMS_DEFAULT = "parachute 1\npistol.semiauto 1\nammo.pistol 40"


def ring_spheres(cx: float, cz: float, radius: float, min_sphere: int, max_spheres: int
                 ) -> tuple[int, list[tuple[float, float]]]:
    """Radiation spheres around the safe circle: (sphere radius, centres). Their inner edges touch the circle, so the
    radiation starts right where the zone ends and reaches 2 × sphere radius beyond it."""
    max_spheres = max(8, max_spheres)
    r = float(max(min_sphere, 1))
    need = 2 * math.pi * radius / (SPACING * (max_spheres - 2 * math.pi / SPACING))
    r = max(r, need)
    count = max(6, math.ceil(2 * math.pi * (radius + r) / (SPACING * r)))
    count = min(count, max_spheres)
    centres = [(cx + (radius + r) * math.cos(2 * math.pi * i / count), cz + (radius + r) * math.sin(2 * math.pi * i / count))
               for i in range(count)]
    return round(r), [(round(x, 1), round(z, 1)) for x, z in centres]


def next_circle(cx: float, cz: float, radius: float, percent: int, min_radius: int,
                rng: random.Random) -> tuple[float, float, float]:
    """The next, smaller safe circle – always completely inside the current one."""
    new = max(float(min_radius), radius * percent / 100)
    if new >= radius:
        return cx, cz, radius
    room = radius - new
    angle, dist = rng.uniform(0, 2 * math.pi), room * math.sqrt(rng.random())
    return round(cx + dist * math.cos(angle), 1), round(cz + dist * math.sin(angle), 1), round(new, 1)


def random_spot(cx: float, cz: float, radius: float, rng: random.Random) -> tuple[float, float]:
    """A random point inside the circle (evenly spread, not crowded in the middle)."""
    angle, dist = rng.uniform(0, 2 * math.pi), radius * math.sqrt(rng.random())
    return round(cx + dist * math.cos(angle), 1), round(cz + dist * math.sin(angle), 1)


def whole_map_radius(cx: float, cz: float, map_size: int) -> float:
    """A circle around (cx, cz) that holds the whole map – out to its farthest corner (plus 2 %)."""
    half = map_size / 2
    far = max(math.hypot(x - cx, z - cz) for x in (-half, half) for z in (-half, half))
    return round(far * 1.02, 1)


def drop_spot(cx: float, cz: float, radius: float, map_size: int, margin_percent: int,
              rng: random.Random) -> tuple[float, float]:
    """A random spot inside the zone and on the map – away from the map's edge (the sea around the island)."""
    limit = map_size / 2 * (1 - margin_percent / 100)
    for _ in range(200):
        x, z = random_spot(cx, cz, radius, rng)
        if abs(x) <= limit and abs(z) <= limit:
            return x, z
    return round(max(-limit, min(limit, cx)), 1), round(max(-limit, min(limit, cz)), 1)


def parse_items(text: str) -> list[tuple[str, int]]:
    out = []
    for line in str(text or "").splitlines():
        parts = line.split()
        if 1 <= len(parts) <= 2 and re.fullmatch(r"[a-z0-9._-]{2,64}", parts[0]) \
                and (len(parts) == 1 or parts[1].isdigit()):
            amount = int(parts[1]) if len(parts) > 1 else 1
            out.append((parts[0], max(1, min(amount, 10000))))
    return out


class BattleRoyale(Plugin):
    NAME = "Battle royale"
    ICON = "🪂"
    DESCRIPTION = ("Rounds of battle royale on your server: players drop from the sky at random spots with a "
                   "parachute and a pistol, a ring of radiation shrinks the zone, the last one alive wins.")
    DEFAULT_ENABLED = False
    SETTINGS = (
        Setting("map_size", "Map size (metres)", "int", 3500, min=500, max=8000,
                help="Your map's size (the world goes from −size/2 to +size/2). Used for the start zone."),
        Setting("centre", "Start zone centre (x,z)", "text", "",
                help="Empty = the middle of the map (0,0). E.g. 250,-400."),
        Setting("start_radius", "Start zone radius (metres)", "int", 0, min=0, max=12000,
                help="0 = the whole island: the circle reaches the map's farthest corner, so the whole map starts "
                     "safe and the radiation ring begins outside it."),
        Setting("drop_margin", "Keep drops away from the map edge (%)", "int", 12, min=0, max=45,
                help="Players land on the map but not in this outer share of it – the sea around the island."),
        Setting("drop_height", "Drop height (y)", "int", 250, min=50, max=1500,
                help="How high players appear above the map. 200–300 gives time to open the parachute."),
        Setting("kit", "Start kit (in-game kit – parachute in Wear)", "kit", "",
                help="Best: an in-game kit with the parachute in the Wear container (then it's already on) and the "
                     "pistol in the Belt. Empty = the items below with inventory.giveto (they land in the "
                     "inventory)."),
        Setting("items", "Start items without a kit", "textarea", ITEMS_DEFAULT,
                help="One per line: shortname amount."),
        Setting("lobby_seconds", "Lobby open for (seconds)", "int", 120, min=15, max=3600),
        Setting("min_players", "Minimum players", "int", 2, min=2, max=200),
        Setting("kill_at_start", "Kill players at the start (empty inventory)", "bool", True,
                help="Everyone respawns with nothing, then drops with the start kit. Off = they keep their gear."),
        Setting("shrink_minutes", "Zone shrinks every … minutes", "int", 3, min=1, max=60),
        Setting("shrink_percent", "New zone size (% of the old)", "int", 60, min=10, max=95),
        Setting("min_radius", "Smallest zone (metres)", "int", 40, min=5, max=2000),
        Setting("warn_seconds", "Warn before shrinking (seconds)", "int", 45, min=0, max=600),
        Setting("ring_min_sphere", "Radiation sphere radius at least (metres)", "int", 120, min=20, max=2000,
                help="The radiation ring is made of overlapping spheres; bigger zones use bigger spheres."),
        Setting("ring_max_spheres", "Radiation spheres at most", "int", 40, min=8, max=120,
                help="More spheres = a smoother edge but more commands each time the zone shrinks."),
        Setting("zone_y", "Height of the zone centres (y)", "int", 30, min=-200, max=500,
                help="About ground level, so the spheres cover the ground."),
        Setting("show_border", "Show the zone border", "bool", True,
                help="A zone without radiation the size of the safe circle, with a visible ring (blue)."),
        Setting("far_kill_seconds", "Kill players beyond the radiation after (seconds)", "int", 15, min=0,
                max=600, help="Players farther out than the radiation ring are warned and killed after this "
                              "long. 0 = never (only the radiation)."),
        Setting("check_seconds", "Check positions every … seconds", "int", 10, min=3, max=120),
        Setting("win_points", "Points for the winner", "int", 0, min=0, max=10_000_000),
        Setting("kill_points", "Points per kill", "int", 0, min=0, max=10_000_000),
        Setting("win_kit", "Kit for the winner", "kit", "", help="Optional."),
        Setting("zone_prefix", "Zone name prefix", "text", "br_"),
        Setting("ring_command", "Command(s): radiation sphere", "textarea", RING_DEFAULT,
                help="Placeholders {zone} {x} {y} {z} {radius}. Default: zones.createcustomzone with radiation 1 "
                     "(PvP 1, NPC damage 1, radiation 1, building damage 1, building 1)."),
        Setting("border_command", "Command(s): zone border", "textarea", BORDER_DEFAULT),
        Setting("delete_command", "Command: delete a zone", "text", DELETE_DEFAULT),
        Setting("teleport_command", "Command: teleport", "text", 'global.teleportpos ({x},{y},{z}) "{name}"'),
        Setting("kill_command", "Command: kill a player", "text", 'global.killplayer "{name}"'),
        Setting("position_command", "Command: a player's position", "text", 'server.printpos "{name}"'),
        Setting("channel", "Announcement channel", "channel", help="Empty = events channel."),
    )

    br = app_commands.Group(name="br", description="Battle royale", guild_only=True)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.rng = random.Random()
        self.game: dict = self._idle()

    @staticmethod
    def _idle() -> dict:
        return {"state": "idle", "players": {}}

    # ------------------------------------------------------------ Setup

    async def on_load(self) -> None:
        left = self.data.get("zones", [])
        if left:                                                # a round was cut off by a restart
            await self._delete_zones(left)
            self.data.delete("zones")
            self.audit(f"Battle royale round ended by a bot restart – {len(left)} zone(s) deleted")

    def problems(self) -> list[str]:
        out = []
        if self.setting("centre") and self._centre_setting() is None:
            out.append("Start zone centre: write it as x,z – e.g. 250,-400.")
        if not self.setting("kit") and not parse_items(self.setting("items")):
            out.append("No start kit and no start items – players would drop with nothing.")
        if not str(self.setting("ring_command") or "").strip():
            out.append("The radiation sphere command is empty – there would be no radiation.")
        return out

    def status(self) -> dict:
        g = self.game
        alive = [p for p in g["players"].values() if p["alive"]]
        return {"Round": g["state"], "Players": len(g["players"]), "Alive": len(alive),
                "Zone radius": round(g.get("radius", 0)) if g["state"] == "running" else "–",
                "Rounds played": self.data.get("rounds", 0)}

    def _centre_setting(self) -> tuple[float, float] | None:
        text = str(self.setting("centre") or "").strip()
        if not text:
            return 0.0, 0.0
        m = re.fullmatch(r"\s*" + NUMBER + r"\s*[,; ]\s*" + NUMBER + r"\s*", text)
        return (float(m[1]), float(m[2])) if m else None

    # ------------------------------------------------------------ Commands

    async def _run(self, template: str, **values) -> bool:
        ok = True
        for line in (ln.strip() for ln in str(template or "").splitlines() if ln.strip()):
            try:
                await self.bot.rcon.command(line.format(**values), expect_response=False)
            except (KeyError, IndexError, ValueError) as exc:
                self.log.warning("Bad placeholder in %r: %s", line, exc)
                return False
            except RconError as exc:
                self.log.warning("Command failed (%s): %s", line, exc)
                ok = False
        return ok

    async def _announce(self, text: str, *, ingame: bool = True, embed: bool = True) -> None:
        if ingame:
            await self.say(f"<color=#f39c12>[Battle royale]</color> {text}")
        if embed:
            channel = self.channel(self.setting("channel"), self.bot.config.events_channel_id)
            if channel is not None:
                await self.post(channel, embed=discord.Embed(description=f"🪂 {self.md(text)}", colour=COLOUR))

    async def _position(self, name: str) -> tuple[float, float, float] | None:
        try:
            reply = await self.bot.rcon.command(str(self.setting("position_command")).format(
                name=sanitize_player_name(name)))
        except (RconError, KeyError, IndexError, ValueError):
            return None
        m = POSITION.search(reply or "")
        return (float(m[1]), float(m[2]), float(m[3])) if m else None

    # ------------------------------------------------------------ Lobby

    def open_lobby(self, seconds: int | None = None) -> tuple[bool, str]:
        if self.game["state"] != "idle":
            return False, f"A round is already {self.game['state']} – /br stop ends it."
        if self.problems():
            return False, "Not set up: " + " ".join(self.problems())
        secs = seconds or self.setting("lobby_seconds")
        self.game = {"state": "lobby", "players": {}, "lobby_ends": time.time() + secs}
        return True, f"Lobby open for {secs} seconds – join with /br join (linked account, online in-game)."

    def join(self, name: str) -> tuple[bool, str]:
        if self.game["state"] != "lobby":
            return False, "There's no lobby open right now."
        online = self.online_name(name)
        if online is None:
            return False, f"{name} must be online in-game to join."
        key = online.lower()
        if key in self.game["players"]:
            return False, f"{online} is already in."
        self.game["players"][key] = {"name": online, "alive": True, "dropped": False, "killed_at": None,
                                     "far_since": None, "kills": 0}
        return True, f"{online} joined ({len(self.game['players'])} player(s))."

    def leave(self, name: str) -> bool:
        if self.game["state"] == "lobby":
            return self.game["players"].pop(name.lower(), None) is not None
        return False

    # ------------------------------------------------------------ Round

    async def start_round(self) -> str:
        g, players = self.game, self.game["players"]
        if len(players) < self.setting("min_players"):
            self.game = self._idle()
            msg = f"Not enough players ({len(players)}/{self.setting('min_players')}) – round cancelled."
            await self._announce(msg)
            return msg
        cx, cz = self._centre_setting() or (0.0, 0.0)
        radius = float(self.setting("start_radius") or whole_map_radius(cx, cz, self.setting("map_size")))
        g.update(state="running", cx=cx, cz=cz, radius=radius, started=time.time(),
                 next_shrink=time.time() + self.setting("shrink_minutes") * 60, warned=False, last_check=0.0)
        await self._build_zone()
        names = ", ".join(p["name"] for p in players.values())
        await self._announce(f"{len(players)} players: {names}. Dropping in – open your parachute! The zone shrinks "
                             f"every {self.setting('shrink_minutes')} min.")
        for p in players.values():
            if self.setting("kill_at_start"):
                p["killed_at"] = time.time()
                await self._run(self.setting("kill_command"), name=sanitize_player_name(p["name"]))
            else:
                await self.drop(p["name"])
        self.data.set("rounds", int(self.data.get("rounds", 0)) + 1)
        self.audit(f"Battle royale started with {len(players)} players")
        return f"Round started with {len(players)} players."

    async def drop(self, name: str) -> bool:
        """Teleports the player high above a random spot inside the zone and gives the start kit."""
        p = self.game["players"].get(name.lower())
        if p is None or p["dropped"] or not p["alive"]:
            return False
        g = self.game
        x, z = drop_spot(g["cx"], g["cz"], g["radius"] * 0.9, self.setting("map_size"), self.setting("drop_margin"),
                         self.rng)
        safe = sanitize_player_name(p["name"])
        await self._run(self.setting("teleport_command"), x=x, y=self.setting("drop_height"), z=z, name=safe)
        if self.setting("kit"):
            await self.give_kit(p["name"], self.setting("kit"))
        else:
            for item, amount in parse_items(self.setting("items")):
                await self._run(self.bot.config.item_give_template, name=safe, item=item, amount=amount)
        p["dropped"], p["dropped_at"] = True, time.time()
        return True

    async def _delete_zones(self, names: list[str]) -> None:
        for zone in names:
            await self._run(self.setting("delete_command"), zone=zone)

    async def _build_zone(self) -> None:
        """(Re)creates the radiation ring and the border around the current circle."""
        g = self.game
        await self._delete_zones(g.get("zones", []))
        prefix, y = self.setting("zone_prefix"), self.setting("zone_y")
        sphere, centres = ring_spheres(g["cx"], g["cz"], g["radius"], self.setting("ring_min_sphere"),
                                       self.setting("ring_max_spheres"))
        zones = []
        for i, (x, z) in enumerate(centres):
            zone = f"{prefix}rad_{i}"
            await self._run(self.setting("ring_command"), zone=zone, x=x, y=y, z=z, radius=sphere)
            zones.append(zone)
        if self.setting("show_border"):
            zone = f"{prefix}border"
            await self._run(self.setting("border_command"), zone=zone, x=g["cx"], y=y, z=g["cz"],
                            radius=round(g["radius"]))
            zones.append(zone)
        g["zones"], g["sphere"] = zones, sphere
        self.data.set("zones", zones)                          # for the cleanup after a restart

    async def shrink(self) -> None:
        g = self.game
        g["cx"], g["cz"], g["radius"] = next_circle(g["cx"], g["cz"], g["radius"], self.setting("shrink_percent"),
                                                    self.setting("min_radius"), self.rng)
        await self._build_zone()
        alive = sum(1 for p in g["players"].values() if p["alive"])
        await self._announce(f"The zone shrank to {round(g['radius'])} m around {round(g['cx'])}, {round(g['cz'])}. "
                             f"{alive} alive.", embed=False)

    def _far(self, x: float, z: float) -> bool:
        g = self.game
        return math.hypot(x - g["cx"], z - g["cz"]) > g["radius"] + 2 * g.get("sphere", 0)

    async def eliminate(self, name: str, why: str, killer: str | None = None) -> None:
        g = self.game
        p = g["players"].get(name.lower())
        if g["state"] != "running" or p is None or not p["alive"] or not p["dropped"]:
            return
        p["alive"] = False
        k = g["players"].get((killer or "").lower())
        if k is not None and k is not p and k["alive"]:
            k["kills"] += 1
            uid = self.linked_user(k["name"])
            if uid and self.setting("kill_points"):
                self.give_points(uid, self.setting("kill_points"), f"battle royale kill on {p['name']}")
        alive = [q for q in g["players"].values() if q["alive"]]
        await self._announce(f"{p['name']} is out ({why}). {len(alive)} left.", embed=False)
        await self._check_winner()

    async def _check_winner(self) -> None:
        g = self.game
        alive = [p for p in g["players"].values() if p["alive"]]
        if g["state"] != "running" or len(alive) > 1:
            return
        if alive:
            win = alive[0]
            await self._announce(f"🏆 {win['name']} wins the battle royale with {win['kills']} kill(s)!")
            uid = self.linked_user(win["name"])
            if uid and self.setting("win_points"):
                self.give_points(uid, self.setting("win_points"), "battle royale win")
            if self.setting("win_kit"):
                await self.give_kit(win["name"], self.setting("win_kit"))
            wins = int(self.data.get(f"wins:{win['name'].lower()}", 0)) + 1
            self.data.set(f"wins:{win['name'].lower()}", wins)
            self.audit(f"Battle royale won by **{self.md(win['name'])}**")
        else:
            await self._announce("Nobody survived – no winner this round.")
        await self.end_round()

    async def end_round(self) -> None:
        await self._delete_zones(self.game.get("zones", []))
        self.data.delete("zones")
        self.game = self._idle()

    # ------------------------------------------------------------ Clock

    @tasks.loop(seconds=3)
    async def clock(self) -> None:
        g, now = self.game, time.time()
        if g["state"] == "lobby" and now >= g["lobby_ends"]:
            await self.start_round()
            return
        if g["state"] != "running":
            return
        for p in list(g["players"].values()):                 # killed at the start but never respawned
            if p["alive"] and not p["dropped"] and p["killed_at"] and now - p["killed_at"] > RESPAWN_WAIT:
                p["alive"] = False
                await self._announce(f"{p['name']} didn't respawn in time – out.", embed=False)
        await self._check_winner()
        if self.game["state"] != "running":
            return
        warn = self.setting("warn_seconds")
        if warn and not g["warned"] and now >= g["next_shrink"] - warn and g["radius"] > self.setting("min_radius"):
            g["warned"] = True
            await self._announce(f"The zone shrinks in {warn} seconds!", embed=False)
        if now >= g["next_shrink"]:
            g["next_shrink"], g["warned"] = now + self.setting("shrink_minutes") * 60, False
            if g["radius"] > self.setting("min_radius"):
                await self.shrink()
        if now - g["last_check"] >= self.setting("check_seconds"):
            g["last_check"] = now
            await self._check_positions(now)

    async def _check_positions(self, now: float) -> None:
        grace = self.setting("far_kill_seconds")
        if not grace:
            return
        for p in [q for q in self.game["players"].values() if q["alive"] and q["dropped"]]:
            if now - p.get("dropped_at", now) < 60:             # still falling / landing
                continue
            pos = await self._position(p["name"])
            if pos is None:
                continue
            if not self._far(pos[0], pos[2]):
                p["far_since"] = None
                continue
            if p["far_since"] is None:
                p["far_since"] = now
                await self.say(f"{p['name']}: you're far outside the zone – get back in {grace} seconds or die!")
            elif now - p["far_since"] >= grace:
                await self._run(self.setting("kill_command"), name=sanitize_player_name(p["name"]))
                await self.eliminate(p["name"], "left the zone")

    # ------------------------------------------------------------ Game events

    @Plugin.listener()
    async def on_rce_respawn(self, ev) -> None:
        p = self.game["players"].get(str(getattr(ev, "player", "")).lower())
        if self.game["state"] == "running" and p is not None and p["alive"] and not p["dropped"]:
            await self.drop(p["name"])

    @Plugin.listener()
    async def on_rce_kill(self, ev) -> None:
        if self.game["state"] == "running" and ev.victim.lower() in self.game["players"]:
            how = f"killed by {ev.killer}" if ev.is_pvp else f"died ({ev.killer})"
            await self.eliminate(ev.victim, how, ev.killer if ev.is_pvp else None)

    @Plugin.listener()
    async def on_rce_suicide(self, ev) -> None:
        await self.eliminate(ev.player, "suicide")

    @Plugin.listener()
    async def on_rce_player_leave(self, name: str) -> None:
        if self.game["state"] == "lobby":
            self.leave(name)
        else:
            await self.eliminate(name, "left the server")

    # ------------------------------------------------------------ Discord

    def _embed(self) -> discord.Embed:
        g = self.game
        e = discord.Embed(title="🪂 Battle royale", colour=COLOUR)
        if g["state"] == "idle":
            e.description = "No round right now."
        elif g["state"] == "lobby":
            names = ", ".join(self.md(p["name"]) for p in g["players"].values())
            e.description = f"Lobby open – closes <t:{int(g['lobby_ends'])}:R>. Join with `/br join`.\n{names or 'Nobody yet.'}"
        else:
            alive = [p for p in g["players"].values() if p["alive"]]
            e.description = (f"Running – zone {round(g['radius'])} m around {round(g['cx'])}, {round(g['cz'])}, "
                             f"next shrink <t:{int(g['next_shrink'])}:R>.\n**Alive ({len(alive)}):** "
                             + ", ".join(f"{self.md(p['name'])} ({p['kills']})" for p in alive))
        return e

    @br.command(name="join", description="Join the battle royale lobby (your linked player must be online)")
    async def join_cmd(self, interaction: discord.Interaction) -> None:
        player = self.linked_player(interaction.user.id)
        if player is None:
            await interaction.response.send_message("Link your player first with /customkit link.", ephemeral=True)
            return
        ok, msg = self.join(player)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + self.md(msg), ephemeral=not ok)
        if ok:
            await self.say(f"{player} joined the battle royale ({len(self.game['players'])} in the lobby).")

    @br.command(name="leave", description="Leave the battle royale lobby")
    async def leave_cmd(self, interaction: discord.Interaction) -> None:
        player = self.linked_player(interaction.user.id) or ""
        ok = self.leave(player)
        await interaction.response.send_message("You left the lobby." if ok else "You're not in an open lobby.",
                                                ephemeral=True)

    @br.command(name="status", description="The battle royale: lobby, players alive, zone")
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=self._embed(), ephemeral=True)

    @br.command(name="start", description="Open a battle royale lobby (admins)")
    @app_commands.describe(seconds="How long the lobby stays open (default: the setting)")
    @admin_only()
    async def start_cmd(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 15, 3600] | None = None
                        ) -> None:
        ok, msg = self.open_lobby(seconds)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + msg, ephemeral=not ok)
        if ok:
            await self._announce(f"Battle royale lobby open! Join on Discord with /br join – starts in "
                                 f"{seconds or self.setting('lobby_seconds')} seconds."
                                 + (" You'll be killed once at the start and drop with a fresh kit – store "
                                    "your gear first!" if self.setting("kill_at_start") else ""))

    @br.command(name="add", description="Add an online player to the lobby (admins)")
    @admin_only()
    async def add_cmd(self, interaction: discord.Interaction, player: str) -> None:
        ok, msg = self.join(player)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + self.md(msg), ephemeral=True)

    @add_cmd.autocomplete("player")
    async def _player_ac(self, interaction: discord.Interaction, current: str):
        return player_choices(self.bot, current)

    @br.command(name="remove", description="Take a player out of the round (admins)")
    @admin_only()
    async def remove_cmd(self, interaction: discord.Interaction, player: str) -> None:
        if self.game["state"] == "lobby":
            ok = self.leave(player)
        else:
            p = self.game["players"].get(player.lower())
            ok = p is not None and p["alive"]
            if ok:
                p["dropped"] = True
                await self.eliminate(player, "removed by an admin")
        await interaction.response.send_message("Done." if ok else "Not in the round.", ephemeral=True)

    @br.command(name="stop", description="End the battle royale round and delete its zones (admins)")
    @admin_only()
    async def stop_cmd(self, interaction: discord.Interaction) -> None:
        if self.game["state"] == "idle":
            await interaction.response.send_message("No round running.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.end_round()
        await self._announce("The battle royale was stopped by an admin.")
        await interaction.followup.send("Round ended, zones deleted.", ephemeral=True)

