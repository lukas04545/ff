"""Battle royale: players drop from the sky at random spots with a parachute and a pistol, a ring of radiation closes
in, the last one alive wins.

A round:
  1. An admin opens the lobby (/br start), players join on Discord (/br join – linked and online in-game).
  2. When the lobby closes, every player is killed once (optional, on by default) so they respawn with an empty
     inventory – their old gear stays in a bag where they died. As soon as one respawns the bot teleports them high
     above a random spot inside the zone and gives the start kit.
  3. The zone: a ring of tall radiation boxes (or spheres) just outside the safe circle (zones.createcustomzone … Box
     (width,height,length) … radiation <strength>) plus a visible border. Every few minutes (warned beforehand) it shrinks: the ring is deleted and created again, smaller,
     around a new centre inside the old circle. The guide has no command to resize a zone, so it's always delete +
     create. Players far outside – beyond the radiation ring – are warned and then killed (they read their position
     with server.printpos every few seconds).
  4. Every death of a dropped player counts (killfeed, suicide, radiation, the bot's kill) and so does leaving the
     server. The last one alive wins: announcement and optional points / kit.

Markers: vending machines in the sky along the zone border (entity.spawn vendingmachine.deployed x,y,z – they show on
the map). The bot reads each machine's ID from the server's reply and deletes the old ones (by that ID) every time the
zone changes and when the round ends. If a reply has no ID the bot stops placing markers for that round, so nothing
it can't delete piles up. The panel's "Test a marker" button shows the server's replies to check the commands.

Test mode (panel, Server → Battle royale): a round without players – zone, radiation and markers only, shrinking only
when you press "Shrink zone now", no winner. Stop deletes everything.

Parachute already worn: give the start kit as an in-game kit (Kits page / kit manager) with the parachute in the
"Wear" container. Without a kit the items below are given with inventory.giveto – they land in the inventory and the
player has to put the parachute on.

Only documented commands are used (Custom zones guide, global.teleportpos, global.killplayer, server.printpos,
inventory.giveto, kit givetoplayer) and every one is an editable setting. A round interrupted by a bot restart is
ended on start and its zones are deleted.
"""
from __future__ import annotations

import asyncio
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
SPACING = 0.8                  # ring pieces: centre distance as a share of their size (they overlap)
RESPAWN_WAIT = 90              # seconds a killed player has to respawn before he's out

OLD_RING_DEFAULT = """zones.createcustomzone "{zone}" ({x},{y},{z}) 0 Sphere {radius} 1 1 1 1 1
zones.editcustomzone "{zone}" showarea 1
zones.editcustomzone "{zone}" color (255,90,0)
zones.editcustomzone "{zone}" showchatmessage 1
zones.editcustomzone "{zone}" entermessage "RADIATION – get back into the zone!" """
# Guide: zones.createcustomzone "Name" (x,y,z) rotation Sphere|Box size pvp npc_damage radiation building_damage building
# – a Box's size is (width,height,length), e.g. "Test" (500,35,90) 45 Box (225,150,150) 1 0 52 1 0
RING_DEFAULT = """zones.createcustomzone "{zone}" ({x},{y},{z}) {rotation} {shape} {size} 1 1 {radiation} 1 1
zones.editcustomzone "{zone}" showarea 1
zones.editcustomzone "{zone}" color (255,90,0)
zones.editcustomzone "{zone}" showchatmessage 1
zones.editcustomzone "{zone}" entermessage "RADIATION – get back into the zone!" """
BORDER_DEFAULT = """zones.createcustomzone "{zone}" ({x},{y},{z}) 0 Sphere {radius} 1 1 0 1 1
zones.editcustomzone "{zone}" showarea 1
zones.editcustomzone "{zone}" color (0,170,255)"""
DELETE_DEFAULT = 'zones.deletecustomzone "{zone}"'
MARKER_DEFAULT = "entity.spawn vendingmachine.deployed {x},{y},{z}"
MARKER_DELETE_DEFAULT = "entity.deleteentity {id}"
MARKER_ID_DEFAULT = r"(?<![\d.])(\d{5,})(?![\d.])"       # a whole number of 5+ digits (not a coordinate)
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


def ring_boxes(cx: float, cz: float, radius: float, min_width: int, max_boxes: int
               ) -> tuple[int, list[tuple[float, float, float]]]:
    """Square radiation boxes around the safe circle: (box width, [(x, z, rotation)]). Each box's inner face lies on
    the circle (turned to face the centre), so the radiation starts right where the zone ends and reaches one box
    width beyond it; neighbours overlap."""
    max_boxes = max(8, max_boxes)
    w = max(float(max(min_width, 1)), 2 * math.pi * radius / (SPACING * max_boxes - math.pi))
    count = max(6, min(max_boxes, math.ceil(2 * math.pi * (radius + w / 2) / (SPACING * w))))
    out = []
    for i in range(count):
        a = 2 * math.pi * i / count
        x, z = cx + (radius + w / 2) * math.cos(a), cz + (radius + w / 2) * math.sin(a)
        out.append((round(x, 1), round(z, 1), round((90 - math.degrees(a)) % 360, 1)))   # yaw: face the centre
    return round(w), out


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
        Setting("map_size", "Map size (metres)", "int", 4500, min=500, max=8000,
                help="Your server's world size (the world goes from −size/2 to +size/2). Used for the start zone "
                     "and the drop spots."),
        Setting("centre", "Start zone centre (x,z)", "text", "",
                help="Empty = the middle of the map (0,0). E.g. 250,-400."),
        Setting("start_radius", "Start zone radius (metres)", "int", 0, min=0, max=12000,
                help="0 = the whole island: the circle reaches the map's farthest corner, so the whole map starts "
                     "safe and the radiation ring begins outside it."),
        Setting("drop_margin", "Keep drops away from the map edge (%)", "int", 12, min=0, max=45,
                help="Players land on the map but not in this outer share of it – the sea around the island."),
        Setting("equip_seconds", "Time to put the parachute on before the drop (seconds)", "int", 15, min=0,
                max=120, help="After respawning players get the start kit first and this long to put the parachute "
                              "on (in-game message + a 5-second warning), then they're teleported into the air. "
                              "0 = straight away."),
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
        Setting("ring_shape", "Radiation zone shape", "choice", "Box", choices=("Box", "Sphere"),
                help="Box (default): tall boxes – the radiation reaches from below the ground up to Box height, so "
                     "nobody gets past on a hill or in the air. Sphere: round zones (only as high as they are wide)."),
        Setting("box_height", "Box height (metres)", "int", 600, min=50, max=3000,
                help="How tall the radiation boxes are, centred on the zone height below."),
        Setting("radiation", "Radiation strength", "int", 50, min=1, max=1000,
                help="The radiation value of the zones (the guide's example uses 52). Higher = faster damage – try a "
                     "round and adjust."),
        Setting("ring_min_sphere", "Radiation pieces: size at least (metres)", "int", 120, min=20, max=2000,
                help="Box width / sphere radius. The ring is made of overlapping pieces; bigger zones use bigger "
                     "pieces."),
        Setting("ring_max_spheres", "Radiation pieces at most", "int", 40, min=8, max=120,
                help="More pieces = a smoother edge but more commands each time the zone shrinks."),
        Setting("zone_y", "Height of the zone centres (y)", "int", 100, min=-500, max=1000,
                help="The middle of the boxes (Box height reaches half of it up and down from here) – keep it "
                     "around ground level so they cover the whole island."),
        Setting("markers", "Vending machines on the zone border (in the sky)", "bool", True,
                help="Marks the zone edge with vending machines high up (they show on the map). Old ones are deleted "
                     "every time the zone changes and at the end of the round. Check the commands once with "
                     "\"Test a marker\" (Server → Battle royale)."),
        Setting("marker_count", "Vending machines around the border", "int", 16, min=3, max=64),
        Setting("marker_height", "Vending machine height (y)", "int", 200, min=0, max=1500),
        Setting("marker_command", "Command: spawn a vending machine", "text", MARKER_DEFAULT,
                help="{x} {y} {z}. Guide (Entity spawning): entity.spawn <entity> <x,y,z>."),
        Setting("marker_id_pattern", "The machine's ID in the server's reply (regular expression)", "text",
                MARKER_ID_DEFAULT, help="The first group is the ID. Default: the first whole number with 5+ digits."),
        Setting("marker_delete_command", "Command: delete a vending machine", "text", MARKER_DELETE_DEFAULT,
                help="{id} = the ID from the spawn reply."),
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
        Setting("ring_command", "Command(s): radiation zone", "textarea", RING_DEFAULT,
                help="Placeholders {zone} {x} {y} {z} {rotation} {shape} {size} – Box: (width,height,length), Sphere: "
                     "the radius – {radius} {width} {height} {radiation}. Default: zones.createcustomzone with PvP 1, "
                     "NPC damage 1, radiation {radiation}, building damage 1, building 1."),
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
        self._waiting: dict[str, asyncio.Task] = {}             # player (lower) -> the drop after the equip timer

    @staticmethod
    def _idle() -> dict:
        return {"state": "idle", "players": {}}

    # ------------------------------------------------------------ Setup

    async def on_load(self) -> None:
        self.marker_test: dict | None = None
        markers = self.data.get("markers", [])
        if markers:
            await self._delete_markers(markers)
            self.data.delete("markers")
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
        state = "test round" if g.get("test") else g["state"]
        return {"Round": state, "Players": len(g["players"]), "Alive": len(alive),
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

    async def start_test(self) -> tuple[bool, str]:
        """Panel: a round without players – zone, radiation and markers only; shrinks only with shrink_now()."""
        if self.game["state"] != "idle":
            return False, f"A round is already {self.game['state']} – stop it first."
        cx, cz = self._centre_setting() or (0.0, 0.0)
        radius = float(self.setting("start_radius") or whole_map_radius(cx, cz, self.setting("map_size")))
        self.game = {"state": "running", "test": True, "players": {}, "cx": cx, "cz": cz, "radius": radius,
                     "started": time.time(), "next_shrink": None, "warned": False, "last_check": 0.0}
        await self._build_zone()
        self.audit("Battle royale test round started (no players)")
        return True, f"Test round running – zone {round(radius)} m. Shrink it with the button, stop it when done."

    async def shrink_now(self) -> tuple[bool, str]:
        g = self.game
        if g["state"] != "running":
            return False, "No round running."
        if g["radius"] <= self.setting("min_radius"):
            return False, f"The zone is already at its smallest ({self.setting('min_radius')} m)."
        await self.shrink()
        if not g.get("test"):
            g["next_shrink"], g["warned"] = time.time() + self.setting("shrink_minutes") * 60, False
        return True, f"Zone shrank to {round(g['radius'])} m."

    def panel_state(self) -> dict:
        """For the panel's Battle royale page."""
        g = self.game
        return {"state": g["state"], "test": bool(g.get("test")),
                "zone": {"x": g.get("cx"), "z": g.get("cz"), "radius": g.get("radius"), "band": g.get("band"),
                         "zones": len(g.get("zones", [])), "markers": len(g.get("markers", []))}
                if g["state"] == "running" else None,
                "markers_broken": g.get("markers_broken"), "next_shrink": g.get("next_shrink"),
                "lobby_ends": g.get("lobby_ends"),
                "players": [{"name": p["name"], "alive": p["alive"], "dropped": p["dropped"], "kills": p["kills"]}
                            for p in g["players"].values()],
                "marker_test": getattr(self, "marker_test", None)}

    async def drop(self, name: str) -> bool:
        """Gives the start kit, then – after the equip timer – teleports the player high above a random spot inside
        the zone."""
        key = name.lower()
        p = self.game["players"].get(key)
        if p is None or p["dropped"] or not p["alive"]:
            return False
        old = self._waiting.pop(key, None)
        if old is not None:                                    # died while waiting: start over with a new kit
            old.cancel()
        p["killed_at"] = None                                  # respawned: no longer waiting for that
        safe = sanitize_player_name(p["name"])
        if self.setting("kit"):
            await self.give_kit(p["name"], self.setting("kit"))
        else:
            for item, amount in parse_items(self.setting("items")):
                await self._run(self.bot.config.item_give_template, name=safe, item=item, amount=amount)
        wait = self.setting("equip_seconds")
        if not wait:
            await self._teleport(p)
            return True
        await self.say(f"{p['name']}: put your parachute on – you drop into the zone in {wait} seconds!")
        self._waiting[key] = asyncio.create_task(self._drop_later(key, wait, self.game.get("started")),
                                                 name=f"br-drop-{key}")
        return True

    async def _drop_later(self, key: str, wait: int, round_started) -> None:
        try:
            if wait > 5:
                await asyncio.sleep(wait - 5)
                p = self.game["players"].get(key)
                if p is not None and p["alive"] and not p["dropped"]:
                    await self.say(f"{p['name']}: dropping in 5 seconds – parachute on!")
                wait = 5
            await asyncio.sleep(wait)
            p = self.game["players"].get(key)
            if self.game.get("started") == round_started and p is not None and p["alive"] and not p["dropped"]:
                await self._teleport(p)
        finally:
            if self._waiting.get(key) is asyncio.current_task():
                self._waiting.pop(key, None)

    async def _teleport(self, p: dict) -> None:
        g = self.game
        x, z = drop_spot(g["cx"], g["cz"], g["radius"] * 0.9, self.setting("map_size"), self.setting("drop_margin"),
                         self.rng)
        await self._run(self.setting("teleport_command"), x=x, y=self.setting("drop_height"), z=z,
                        name=sanitize_player_name(p["name"]))
        p["dropped"], p["dropped_at"] = True, time.time()

    async def _delete_zones(self, names: list[str]) -> None:
        for zone in names:
            await self._run(self.setting("delete_command"), zone=zone)

    async def _build_zone(self) -> None:
        """(Re)creates the radiation ring and the border around the current circle."""
        g = self.game
        await self._delete_zones(g.get("zones", []))
        prefix, y = self.setting("zone_prefix"), self.setting("zone_y")
        template = str(self.setting("ring_command") or "")
        if template.strip() == OLD_RING_DEFAULT.strip():       # saved before boxes existed: the new default
            template = RING_DEFAULT
        common = {"y": y, "radiation": self.setting("radiation"), "height": self.setting("box_height")}
        zones = []
        if self.setting("ring_shape") == "Sphere":
            size, centres = ring_spheres(g["cx"], g["cz"], g["radius"], self.setting("ring_min_sphere"),
                                         self.setting("ring_max_spheres"))
            pieces = [(x, z, 0, "Sphere", size) for x, z in centres]
            band = 2 * size
        else:
            width, boxes = ring_boxes(g["cx"], g["cz"], g["radius"], self.setting("ring_min_sphere"),
                                      self.setting("ring_max_spheres"))
            pieces = [(x, z, rot, "Box", f"({width},{self.setting('box_height')},{width})") for x, z, rot in boxes]
            size, band = width, width
        for i, (x, z, rot, shape, dims) in enumerate(pieces):
            zone = f"{prefix}rad_{i}"
            await self._run(template, zone=zone, x=x, z=z, rotation=rot, shape=shape, size=dims,
                            radius=size if shape == "Sphere" else round(size / 2), width=size, **common)
            zones.append(zone)
        if self.setting("show_border"):
            zone = f"{prefix}border"
            await self._run(self.setting("border_command"), zone=zone, x=g["cx"], y=y, z=g["cz"],
                            radius=round(g["radius"]))
            zones.append(zone)
        g["zones"], g["band"] = zones, band
        self.data.set("zones", zones)                          # for the cleanup after a restart
        await self._place_markers()

    # ------------------------------------------------------------ Vending machine markers

    def _marker_id(self, reply: str) -> str | None:
        try:
            m = re.search(str(self.setting("marker_id_pattern") or MARKER_ID_DEFAULT), reply or "")
        except re.error:
            return None
        if m is None:
            return None
        return m.group(1) if m.groups() else m.group(0)

    async def _spawn_marker(self, x: float, y: float, z: float) -> tuple[str, str, str | None]:
        """(command, the server's reply, ID or None)."""
        try:
            command = str(self.setting("marker_command")).format(x=x, y=y, z=z)
        except (KeyError, IndexError, ValueError):
            return "", "bad placeholder in the spawn command", None
        try:
            reply = await self.bot.rcon.command(command)
        except RconError as exc:
            return command, f"error: {exc}", None
        return command, reply or "", self._marker_id(reply or "")

    async def _delete_markers(self, ids: list[str]) -> None:
        for marker in ids:
            await self._run(self.setting("marker_delete_command"), id=marker)

    async def _place_markers(self) -> None:
        """Deletes the last border's vending machines and puts new ones around the current one, high up."""
        g = self.game
        await self._delete_markers(g.get("markers", []))
        g["markers"] = []
        self.data.set("markers", [])
        if not self.setting("markers") or g.get("markers_broken"):
            return
        n, y = self.setting("marker_count"), self.setting("marker_height")
        for i in range(n):
            a = 2 * math.pi * i / n
            x, z = round(g["cx"] + g["radius"] * math.cos(a), 1), round(g["cz"] + g["radius"] * math.sin(a), 1)
            command, reply, marker = await self._spawn_marker(x, y, z)
            if marker is None:                                 # can't delete what we can't name: stop placing
                g["markers_broken"] = f"{command} → {reply[:200] or '(no reply)'}"
                self.log.warning("Battle royale markers stopped – no ID in the reply to %r: %r", command, reply)
                self.audit("Battle royale: vending machine markers stopped – the server's reply had no ID (check "
                           "with \"Test a marker\" on the Battle royale page)")
                break
            g["markers"].append(marker)
        self.data.set("markers", g["markers"])                  # for the cleanup after a restart

    async def test_marker(self) -> dict:
        """Panel button: spawns one vending machine in the sky above the map centre, deletes it again and returns
        the server's replies – to check the commands before a round."""
        cx, cz = self._centre_setting() or (0.0, 0.0)
        command, reply, marker = await self._spawn_marker(cx, self.setting("marker_height"), cz)
        result = {"spawn_command": command, "spawn_reply": reply, "id": marker, "delete_command": "",
                  "delete_reply": "", "time": int(time.time())}
        if marker is not None:
            try:
                result["delete_command"] = str(self.setting("marker_delete_command")).format(id=marker)
                result["delete_reply"] = await self.bot.rcon.command(result["delete_command"]) or ""
            except (KeyError, IndexError, ValueError):
                result["delete_reply"] = "bad placeholder in the delete command"
            except RconError as exc:
                result["delete_reply"] = f"error: {exc}"
        self.marker_test = result
        return result

    async def shrink(self) -> None:
        g = self.game
        g["cx"], g["cz"], g["radius"] = next_circle(g["cx"], g["cz"], g["radius"], self.setting("shrink_percent"),
                                                    self.setting("min_radius"), self.rng)
        await self._build_zone()
        if g.get("test"):                                      # test round: no announcements
            return
        alive = sum(1 for p in g["players"].values() if p["alive"])
        await self._announce(f"The zone shrank to {round(g['radius'])} m around {round(g['cx'])}, {round(g['cz'])}. "
                             f"{alive} alive.", embed=False)

    def _far(self, x: float, z: float) -> bool:
        g = self.game
        return math.hypot(x - g["cx"], z - g["cz"]) > g["radius"] + g.get("band", 0)

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
        if g["state"] != "running" or len(alive) > 1 or g.get("test"):
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
        for task in self._waiting.values():
            task.cancel()
        self._waiting.clear()
        await self._delete_zones(self.game.get("zones", []))
        self.data.delete("zones")
        await self._delete_markers(self.game.get("markers", []))
        self.data.delete("markers")
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
        if g.get("test"):                                      # test round: shrinks only with the button
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

