"""RCE Companion: the players' Android app – raid alerts as phone notifications, server status and the website.

  /companion   on Discord: the app download and a one-time pairing link / code (15 minutes) for YOUR account

The app polls this server (every ~20 s while alerts are on) at /p/companion/feed with its own token and shows new
alerts as notifications. /p/companion/account gives the player's overview for the app's home screen (linked player,
kits with their claim timers, raid alerts, offline protection); /p/companion/claim claims a kit (same rules as
/customkit claim), /p/companion/alerts switches the raid alert DMs, /p/companion/rf sets the RF raid alarm frequency. Raid alerts (also the RF raid alarm) go to the app automatically; other plugins can call
push(user_id, title, text). Tokens are stored hashed; /companion again (or "Unpair" in the app) can end a pairing.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import secrets
import time
from urllib.parse import parse_qsl, urlencode

import aiohttp

import discord
from aiohttp import web
from discord import app_commands

from plugin_api import Plugin, Setting

CODE_TTL = 15 * 60
FEED_KEEP = 50
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Companion(Plugin):
    NAME = "Companion app"
    ICON = "📱"
    DESCRIPTION = ("RCE Companion – the players' Android app: raid alerts as phone notifications, the server's "
                   "status and the website. Players pair it with /companion.")
    DEFAULT_ENABLED = True
    SETTINGS = (
        Setting("max_devices", "Phones per player", "int", 3, min=1, max=10),
        Setting("chat_send", "Players can write in the game chat from the app", "bool", True,
                help="Shown in game as \"📱 Name: text\" (global chat). One message every 10 seconds per player."),
        Setting("map", "Team map in the app", "bool", True,
                help="Shows the player and their online team mates on a grid (asks the server for their positions, "
                     "at most every 20 seconds per team)."),
        Setting("map_auto", "Get the map picture from rcemaps.com", "bool", True,
                help="The bot asks rcemaps.com for the picture of your map (from its seed and size), keeps it and "
                     "shows it behind the grid in the app. A new seed after a wipe gets a new picture by itself."),
        Setting("map_seed", "Map seed", "text", "",
                help="Empty = the bot asks the game server (server.seed). Enter it here if the map stays empty – "
                     "it's the seed in your server's settings at your host."),
        Setting("map_terrain", "Custom terrain (optional)", "text", "",
                help="Only if your server starts with terrain / biome commands: write them like rcemaps.com does, "
                     "e.g. elevation_hill=0.8&oceanlevel=5 – otherwise the picture shows the stock map of the seed."),
        Setting("map_image", "Own map picture (optional)", "image",
                help="Replaces the picture from rcemaps.com: a square picture of your map whose edges are the "
                     "map's edges (e.g. a custom map). Upload a new one after a map wipe."),
        Setting("map_size", "Map size (metres)", "int", 4000, min=1000, max=8000,
                help="The world size of your map – used when the game server doesn't tell it (server.worldsize)."),
    )
    PREFS = {"team": True, "death": True, "events": False, "wipe": True}     # which notifications a player gets
    CHAT_KEEP, EVENT_KEEP, CHAT_EVERY, MAP_EVERY = 200, 30, 10, 20
    MAP_API = "https://rcemaps.com/api/v1/map.png"          # public API for bots (rcemaps.com/api), 512/1024 px PNG
    MAP_SIZES = (1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500)
    MAP_RETRY, MAP_ASK_EVERY = 600, 900                     # after a failed download / between asking the server

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        from web.ratelimit import RateLimiter
        self._reads = RateLimiter(40, 60)            # per phone: feed + account
        self._writes = RateLimiter(12, 60)           # per phone: claim, alerts, rf
        self._pair_tries = RateLimiter(10, 900)      # per visitor: wrong pairing codes
        self._bad_tokens = RateLimiter(30, 300)      # per visitor: requests with an unknown token
        from collections import deque
        self._chat: deque = deque(maxlen=self.CHAT_KEEP)        # the game chat the app shows (memory only)
        self._events: deque = deque(maxlen=self.EVENT_KEEP)     # cargo ship, helicopter, air drop …
        self._chat_n = 0
        self._last_sent: dict[int, float] = {}                  # user -> when they last wrote from the app
        self._map_cache: dict[int, tuple[float, list]] = {}     # team (or -user) -> (when, positions)
        self._world: tuple[float, int | None, int | None] = (0.0, None, None)   # (asked at, seed, size) from RCON
        self._map_tried: dict[str, float] = {}                  # picture name -> last download attempt
        self._map_job: asyncio.Task | None = None

    # ------------------------------------------------------------ pairing

    def site_url(self) -> str:
        return (self.bot.config.web_public_url or "").rstrip("/")

    def new_code(self, user_id: int) -> str:
        for key, row in list(self.data.items("code:").items()):        # old codes of this player go
            if row["uid"] == user_id or row["exp"] < self.now():
                self.data.delete(key)
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        self.data.set(f"code:{code}", {"uid": user_id, "exp": self.now() + CODE_TTL})
        return code

    def exchange(self, code: str, device: str = "") -> tuple[str, int] | None:
        """A pairing code (once, within 15 minutes) -> (token, user id)."""
        code = "".join(ch for ch in str(code or "").upper() if ch.isalnum())[:8]
        row = self.data.get(f"code:{code}") if code else None
        if not row:
            return None
        self.data.delete(f"code:{code}")
        if row["exp"] < self.now():
            return None
        mine = sorted(((k, v) for k, v in self.data.items("tok:").items() if v["uid"] == row["uid"]),
                      key=lambda kv: kv[1].get("n", 0))
        for key, _ in mine[:max(0, len(mine) - self.setting("max_devices") + 1)]:
            self.data.delete(key)                                      # oldest phones beyond the limit
        token = secrets.token_urlsafe(32)
        n = int(self.data.get("devn", 0)) + 1                           # pairing order (oldest goes first)
        self.data.set("devn", n)
        self.data.set(f"tok:{_hash(token)}", {"uid": row["uid"], "at": self.now(), "n": n,
                                              "device": str(device)[:40]})
        self.audit(f"Companion app paired for <@{row['uid']}>")
        return token, row["uid"]

    def user_of(self, token: str) -> int | None:
        row = self.data.get(f"tok:{_hash(token)}") if token else None
        return int(row["uid"]) if row else None

    def unpair(self, token: str) -> bool:
        return self.data.delete(f"tok:{_hash(token)}")

    def unpair_all(self, user_id: int) -> int:
        n = 0
        for key, row in list(self.data.items("tok:").items()):
            if row["uid"] == user_id:
                n += self.data.delete(key)
        return n

    def devices(self, user_id: int) -> int:
        return sum(1 for row in self.data.items("tok:").values() if row["uid"] == user_id)

    # ------------------------------------------------------------ alerts

    def push(self, user_id: int, title: str, text: str, *, kind: str = "info") -> int | None:
        """Queues a notification for the player's phones (only if they paired one). Returns its id."""
        if not self.devices(int(user_id)):
            return None
        feed = list(self.data.get(f"feed:{int(user_id)}", []))
        n = int(self.data.get("next", 0)) + 1
        self.data.set("next", n)
        feed.append({"id": n, "at": self.now(), "kind": kind, "title": str(title)[:100], "text": str(text)[:400]})
        self.data.set(f"feed:{int(user_id)}", feed[-FEED_KEEP:])
        return n

    def feed(self, user_id: int, since: int = 0) -> dict:
        info = getattr(self.bot, "server_info", None) or {}
        online = bool(self.bot.rcon.connected and info)
        player = self.linked_player(user_id)
        return {"server": {"name": self.server_name, "online": online,
                           "players": int(info.get("Players") or 0) if online else 0,
                           "max_players": int(info.get("MaxPlayers") or 0) if online else 0,
                           "next_wipe": self.next_wipe(), "website": self.site_url()},
                "player": player, "player_online": bool(player and self.online_name(player)),
                "alerts": [a for a in self.data.get(f"feed:{user_id}", []) if a["id"] > since]}

    # ------------------------------------------------------------ team, chat, events, map (like Rust+)

    def prefs(self, user_id: int) -> dict:
        saved = self.data.get(f"prefs:{int(user_id)}") or {}
        return {k: bool(saved.get(k, default)) for k, default in self.PREFS.items()}

    def set_prefs(self, user_id: int, changes: dict) -> dict:
        new = self.prefs(user_id)
        new.update({k: bool(v) for k, v in changes.items() if k in self.PREFS})
        self.data.set(f"prefs:{int(user_id)}", new)
        return new

    def paired_users(self) -> set[int]:
        return {int(row["uid"]) for row in self.data.items("tok:").values()}

    def team(self, player: str | None) -> dict | None:
        """The player's in-game team: leader and members with who is online and who is in the app's Discord."""
        found = self.bot.db.team_of(player) if player else None
        if found is None:
            return None
        row, members = found
        return {"id": row["team_id"], "leader": row["leader"],
                "members": [{"name": m, "online": self.online_name(m) is not None,
                             "leader": bool(row["leader"]) and m.lower() == str(row["leader"]).lower()}
                            for m in sorted(members, key=str.lower)]}

    def stats(self, player: str | None) -> dict | None:
        row = self.bot.db.player(player) if player else None
        if row is None:
            return None
        deaths = int(row["deaths"] or 0)
        return {"kills": int(row["kills"] or 0), "deaths": deaths,
                "kd": round(int(row["kills"] or 0) / deaths, 2) if deaths else float(row["kills"] or 0),
                "playtime": int(row["playtime_seconds"] or 0)}

    def chat(self, user_id: int, since: int = 0) -> list[dict]:
        """Global and server chat for everyone – team chat only for the members of that team."""
        player = self.linked_player(user_id)
        found = self.bot.db.team_of(player) if player else None
        mates = {m.lower() for m in found[1]} if found else ({player.lower()} if player else set())
        return [line for line in self._chat if line["id"] > since
                and (line["channel"] != "TEAM" or line["player"].lower() in mates)]

    def _chat_line(self, channel: str, player: str, text: str) -> None:
        self._chat_n += 1
        self._chat.append({"id": self._chat_n, "at": self.now(), "channel": channel, "player": player[:40],
                           "text": text[:300]})

    async def send_chat(self, user_id: int, text: str) -> dict:
        from utils import sanitize_ingame_text
        player = self.linked_player(user_id)
        if not self.setting("chat_send"):
            return {"ok": False, "message": "Writing from the app is switched off on this server."}
        if not player:
            return {"ok": False, "message": "Link your player first: /link on Discord."}
        text = sanitize_ingame_text(" ".join(str(text or "").split()), 140)
        if not text:
            return {"ok": False, "message": "Type a message first."}
        wait = self.CHAT_EVERY - (time.time() - self._last_sent.get(user_id, 0))
        if wait > 0:
            return {"ok": False, "message": f"Wait {int(wait) + 1} s before the next message."}
        self._last_sent[user_id] = time.time()
        if not await self.say(f"📱 {player}: {text}"):
            return {"ok": False, "message": "The game server isn't reachable right now."}
        self._chat_line("APP", player, text)
        return {"ok": True, "message": ""}

    async def positions(self, user_id: int) -> dict:
        """Where the player and their online team mates stand – only their own team, cached for 20 seconds."""
        if not self.setting("map"):
            return {"on": False, "size": int(self.setting("map_size")), "players": [], "image": None}
        _, version, size, no_picture = await self.map_picture()
        player = self.linked_player(user_id)
        found = self.bot.db.team_of(player) if player else None
        names = found[1] if found else ([player] if player else [])
        key = int(found[0]["team_id"]) if found else -int(user_id)
        cached = self._map_cache.get(key)
        if cached is None or time.time() - cached[0] > self.MAP_EVERY:
            spots = []
            for name in [n for n in names if self.online_name(n)][:8]:
                pos = await self.position(name)
                if pos is not None:
                    spots.append({"name": name, "x": round(pos[0], 1), "z": round(pos[2], 1)})
            cached = (time.time(), spots)
            self._map_cache[key] = cached
            for old in [k for k, v in self._map_cache.items() if time.time() - v[0] > 600]:
                self._map_cache.pop(old, None)
        online = [n for n in names if self.online_name(n)]
        why = ("" if cached[1] else "Link your player first: /link on the server's Discord." if not player else
               "You (and your team) aren't in game right now." if not online else
               "The game server didn't tell the positions – try again in a moment.")
        return {"on": True, "size": size, "at": int(cached[0]), "image": version, "why": why,
                "no_picture": no_picture, "credit": "Map: rcemaps.com" if version and version.startswith("rce-") else "",
                "players": [dict(sp, me=bool(player) and sp["name"].lower() == player.lower()) for sp in cached[1]]}

    # ---- the map picture: the owner's upload, else rcemaps.com (by seed and size)

    @staticmethod
    def _convar(reply: str) -> int | None:
        """The number in a console variable's answer: 'server.seed: "1234567"' -> 1234567."""
        m = re.search(r"[:=]\s*\"?(-?\d+)", str(reply or "")) or re.fullmatch(r"\s*\"?(\d+)\"?\s*", str(reply or ""))
        return int(m.group(1)) if m else None

    async def world(self) -> tuple[int | None, int]:
        """(seed, size) of the running map: the settings first, else what the game server says (asked every 15
        minutes at most)."""
        seed_text = str(self.setting("map_seed") or "").strip()
        seed = int(seed_text) if seed_text.isdigit() else None
        asked, got_seed, got_size = self._world
        if time.time() - asked > self.MAP_ASK_EVERY and self.bot.rcon.connected:
            got_seed = got_size = None
            for name in ("server.seed", "server.worldsize"):
                try:
                    value = self._convar(await self.bot.rcon.command(name, timeout=6))
                except Exception as exc:                       # no answer: the settings are used
                    self.log.debug("%s: %s", name, exc)
                    value = None
                if name == "server.seed":
                    got_seed = value if value is not None and 0 <= value <= 2147483647 else None
                else:
                    got_size = value if value is not None and 500 <= value <= 8000 else None
            self._world = (time.time(), got_seed, got_size)
        return (seed if seed is not None else got_seed), int(got_size or self.setting("map_size"))

    def _terrain(self) -> list[tuple[str, str]]:
        """The owner's terrain / biome commands as query parameters (names and values checked)."""
        pairs = parse_qsl(str(self.setting("map_terrain") or "").strip().lstrip("&?"), keep_blank_values=False)
        reserved = {"seed", "size", "clean", "quality", "q", "style", "key"}
        return [(k, v) for k, v in pairs if re.fullmatch(r"[a-z_]{2,40}", k) and k not in reserved
                and re.fullmatch(r"[A-Za-z0-9=,.\-]{1,120}", v)][:20]

    def _auto_path(self, seed: int, size: int):
        tag = hashlib.sha256(urlencode(self._terrain()).encode()).hexdigest()[:8]
        return self.manager.files_dir / self.plugin_id / f"rcemap_{seed}_{size}_{tag}.png"

    async def _download_map(self, seed: int, size: int, path) -> None:
        params = [("seed", str(seed)), ("size", str(size)), ("clean", "1"), ("quality", "quality1024"), *self._terrain()]
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
                async with session.get(self.MAP_API, params=params, headers={"User-Agent": "RCE-Discord-Bot"}) as resp:
                    data = await resp.read()
                    if resp.status != 200 or data[:4] != b"\x89PNG" or len(data) > 4_000_000:
                        self.log.warning("rcemaps.com: no map for seed %s size %s (HTTP %s: %s)", seed, size,
                                         resp.status, data[:200].decode("utf-8", "replace"))
                        return
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            self.log.warning("rcemaps.com isn't reachable: %s", exc)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        for old in path.parent.glob("rcemap_*.png"):             # one map at a time
            old.unlink(missing_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        self.log.info("Map picture for seed %s, size %s saved (rcemaps.com)", seed, size)

    async def map_picture(self) -> tuple[object | None, str | None, int, str]:
        """(file, version, world size, why there is none). The owner's upload wins; else the picture from
        rcemaps.com, downloaded once per seed in the background."""
        seed, size = await self.world() if self.manager is not None else (None, int(self.setting("map_size")))
        own = self.file("map_image")
        if own is not None:
            try:
                st = own.stat()
                return own, f"own-{int(st.st_mtime)}-{st.st_size}", size, ""
            except OSError:
                pass
        if not self.setting("map_auto"):
            return None, None, size, "No map picture on this server yet."
        if seed is None:
            return None, None, size, ("The server's map seed isn't known yet – its owner enters it in the control "
                                      "panel: Plugins → Companion app → Map seed.")
        if size not in self.MAP_SIZES:
            return None, None, size, f"There's no map picture for a map size of {size} m."
        path = self._auto_path(seed, size)
        if path.is_file():
            return path, f"rce-{seed}-{size}-{path.stem[-8:]}", size, ""
        if time.time() - self._map_tried.get(path.name, 0) > self.MAP_RETRY and (
                self._map_job is None or self._map_job.done()):
            self._map_tried[path.name] = time.time()
            self._map_job = asyncio.create_task(self._download_map(seed, size, path))
        return None, None, size, "The map picture is being fetched – tap Refresh in a moment."

    @Plugin.listener()
    async def on_rce_chat(self, ev) -> None:
        if ev.channel == "LOCAL" or (ev.channel == "SERVER" and ev.text.startswith("📱")):
            return                                   # local chat stays local; our own lines are in already
        self._chat_line(ev.channel, ev.player, ev.text)

    @Plugin.listener()
    async def on_rce_server_event(self, ev) -> None:
        self._events.append({"at": self.now(), "key": ev.key, "name": ev.name})
        for uid in self.paired_users():
            if self.prefs(uid)["events"]:
                self.push(uid, ev.name, f"Started on {self.server_name}.", kind="event")

    @Plugin.listener()
    async def on_rce_kill(self, ev) -> None:
        uid = self.linked_user(ev.victim) if ev.victim else None
        if uid is not None and self.prefs(uid)["death"]:
            self.push(uid, "💀 You were killed", f"{ev.victim} was killed by {ev.killer}.", kind="death")

    @Plugin.listener()
    async def on_rce_player_join(self, player) -> None:
        found = self.bot.db.team_of(player)
        for mate in (found[1] if found else []):
            uid = self.linked_user(mate)
            if uid is not None and mate.lower() != str(player).lower() and self.prefs(uid)["team"]:
                self.push(uid, "🟢 Team mate online", f"{player} joined {self.server_name}.", kind="team")

    @Plugin.listener()
    async def on_rce_wiped(self, *_args) -> None:
        for uid in self.paired_users():
            if self.prefs(uid)["wipe"]:
                self.push(uid, "🧹 Server wiped", f"{self.server_name} just wiped – fresh start!", kind="wipe")

    # ------------------------------------------------------------ the player's account (the app's home screen)

    async def member(self, user_id: int):
        guild = self.guild
        if guild is None:
            return None
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return None
        return member

    async def account(self, user_id: int) -> dict:
        player = self.linked_player(user_id)
        info = getattr(self.bot, "server_info", None) or {}
        up = bool(self.bot.rcon.connected and info)
        out: dict = {"server": self.server_name, "player": player, "website": self.site_url(),
                     "status": {"online": up, "players": int(info.get("Players") or 0) if up else 0,
                                "max_players": int(info.get("MaxPlayers") or 0) if up else 0,
                                "next_wipe": self.next_wipe(),
                                "queued": int(info.get("Queued") or 0) if up else 0,
                                "game_time": str(info.get("GameTime") or "") if up else "",
                                "map": str(info.get("Map") or "") if up else "",
                                "uptime": int(info.get("Uptime") or 0) if up else 0},
                     "player_online": bool(player and self.online_name(player)), "kits": [], "owned_kits": [],
                     "team": self.team(player), "stats": self.stats(player), "events": list(self._events)[-12:],
                     "prefs": self.prefs(user_id), "chat_send": bool(self.setting("chat_send")),
                     "map": bool(self.setting("map")),
                     "alerts": list(self.data.get(f"feed:{user_id}", []))[-15:]}
        custom = self.bot.get_cog("CustomKits")
        member = await self.member(user_id)
        if custom is not None and member is not None:
            try:
                for k in custom.kit_overview(member).get("claimable_kits", []):
                    out["kits"].append({"kit": k["kit"], "ready": k["can_claim_now"], "ready_at": k.get("ready_at"),
                                        "cooldown": k.get("cooldown"), "last": k.get("last_claimed"),
                                        "items": k.get("items", [])[:12]})
            except Exception:                               # one bad kit never breaks the overview
                self.log.exception("Companion: kit overview failed")
        accounts = self.bot.get_cog("PlayerAccounts")
        if accounts is not None:
            out["owned_kits"] = [k["name"] for k in accounts.my_kits(user_id)]
        raid = self.plugin("raid_alerts")
        if raid is not None:
            out["raid"] = {"dm": raid.data.get(f"sub:{user_id}") is not None, "rf": raid.rf_of(user_id),
                           "rf_on": bool(raid.setting("rf")),
                           "last": [a for a in self.data.get(f"feed:{user_id}", []) if a["kind"] == "raid"][-3:]}
        protection = self.plugin("offline_protection")
        if protection is not None and player:
            try:
                z = await protection.zone_status(player)
                zone = z.get("zone")
                out["protection"] = {"team": z.get("team"), "leader": z.get("is_leader"),
                                     "status": zone.get("status") if zone else None,
                                     "deleted_at": zone.get("deleted_at") if zone else None,
                                     "how": z.get("how_to_place_in_game")}
            except Exception:
                self.log.exception("Companion: protection status failed")
        return out

    async def claim(self, user_id: int, kit: str) -> dict:
        custom = self.bot.get_cog("CustomKits")
        member = await self.member(user_id)
        if custom is None or member is None:
            return {"ok": False, "message": "You aren't on this server's Discord (any more)."}
        outcome = await custom.claim_for(member, str(kit)[:60], via="the Companion app")
        return {"ok": outcome.ok, "message": outcome.message.replace("**", "").replace("`", ""),
                "ready_at": outcome.ready_at}

    # ------------------------------------------------------------ the app's address on this server

    def _limited(self, key: str, limiter) -> web.Response | None:
        wait = limiter.hit(key)
        if wait is None:
            return None
        return web.json_response({"error": "Too many requests – wait a moment."}, status=429,
                                 headers={"Retry-After": str(int(wait) + 1)})

    async def web_public(self, request, path: str):
        from web.server import WebInterface
        ip = WebInterface._client_ip(request)
        if path == "pair" and request.method == "GET":
            return self._pair_page(str(request.query.get("code", ""))[:12])
        if path == "exchange" and request.method == "POST":
            if self._pair_tries.retry_after(ip) is not None:
                return web.json_response({"error": "Too many wrong codes – wait 15 minutes."}, status=429)
            try:
                data = await request.json()
            except (ValueError, UnicodeDecodeError):
                data = {}
            got = self.exchange(str(data.get("code", "")), str(data.get("device", "")))
            if got is None:
                self._pair_tries.hit(ip)
                return web.json_response({"error": "This code is wrong or expired – get a new one with /companion."},
                                         status=400)
            token, uid = got
            return web.json_response({"token": token, "server": self.server_name,
                                      "player": self.linked_player(uid), "website": self.site_url()})
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        uid = self.user_of(token)
        if path in ("feed", "unpair", "account", "claim", "alerts", "rf", "chat", "map", "mapimage", "prefs") and uid is None:
            if (blocked := self._limited("bad:" + ip, self._bad_tokens)) is not None:
                return blocked                                   # guessing tokens
            return web.json_response({"error": "not paired"}, status=401)
        if uid is not None:
            limiter = self._writes if request.method == "POST" else self._reads
            if (blocked := self._limited(_hash(token), limiter)) is not None:
                return blocked
        if path == "account" and request.method == "GET":
            return web.json_response(await self.account(uid), headers={"Cache-Control": "no-store"})
        if path == "chat" and request.method == "GET":
            try:
                since = int(request.query.get("since", 0))
            except ValueError:
                since = 0
            return web.json_response({"lines": self.chat(uid, since), "can_send": bool(self.setting("chat_send"))},
                                     headers={"Cache-Control": "no-store"})
        if path == "mapimage" and request.method == "GET":
            picture = (await self.map_picture())[0] if self.setting("map") else None
            if picture is None or not picture.is_file():
                raise web.HTTPNotFound(text="No map picture.")
            return web.FileResponse(picture, headers={"Cache-Control": "private, max-age=86400"})
        if path == "map" and request.method == "GET":
            return web.json_response(await self.positions(uid), headers={"Cache-Control": "no-store"})
        if path in ("claim", "alerts", "rf", "chat", "prefs") and request.method == "POST":
            try:
                data = await request.json()
            except (ValueError, UnicodeDecodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            if path == "chat":
                return web.json_response(await self.send_chat(uid, str(data.get("text", ""))[:300]))
            if path == "prefs":
                return web.json_response({"ok": True, "prefs": self.set_prefs(uid, data)})
            if path == "claim":
                return web.json_response(await self.claim(uid, str(data.get("kit", ""))))
            raid = self.plugin("raid_alerts")
            if raid is None:
                return web.json_response({"ok": False, "message": "Raid alerts aren't on on this server."})
            if path == "alerts":
                if data.get("on"):
                    raid.data.set(f"sub:{uid}", uid)
                else:
                    raid.data.delete(f"sub:{uid}")
                return web.json_response({"ok": True, "message": "Raid alerts on." if data.get("on") else
                                          "Raid alerts off."})
            if data.get("release"):
                freq = raid.rf_of(uid)
                if freq is not None:
                    raid.data.delete(f"rf:{freq}")
                return web.json_response({"ok": True, "message": "RF frequency released."})
            if not self.linked_player(uid):
                return web.json_response({"ok": False, "message": "Link your player first: /link on Discord."})
            try:
                freq = int(data.get("frequency"))
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "message": "A frequency is 1 to 9999."})
            text = raid.claim_rf(uid, freq)
            return web.json_response({"ok": "taken" not in text and "1 to 9999" not in text,
                                      "message": text.replace("**", "")})
        if path == "feed" and request.method == "GET":
            try:
                since = int(request.query.get("since", 0))
            except ValueError:
                since = 0
            return web.json_response(self.feed(uid, since), headers={"Cache-Control": "no-store"})
        if path == "unpair" and request.method == "POST":
            self.unpair(token)
            return web.json_response({"ok": True})
        raise web.HTTPNotFound(text="Not found.")

    def _pair_page(self, code: str) -> web.Response:
        name = html.escape(self.server_name)
        site = self.site_url()
        link = f"rcecompanion://pair?url={site}&code={code}" if site and code else ""
        body = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                f"<title>{name} – Companion app</title><body style='background:#0b0e1a;color:#e8ecf8;"
                f"font-family:system-ui;max-width:460px;margin:0 auto;padding:24px'><h1>📱 {name}</h1>"
                f"<p><b>1.</b> <a style='color:#ff9a5a' href='{html.escape(site)}/app/rce-companion.apk'>Download the "
                f"RCE Companion app</a> and install it.</p>"
                + (f"<p><b>2.</b> <a style='display:inline-block;padding:12px 18px;background:#e5341c;color:#fff;"
                   f"border-radius:8px;text-decoration:none' href='{html.escape(link)}'>Open in the app</a></p>"
                   f"<p>Or type this code in the app: <b style='font-size:1.6rem;letter-spacing:.15em'>"
                   f"{html.escape(code)}</b><br><small>Server address: {html.escape(site)}</small></p>"
                   if link else "<p>Get your pairing code on our Discord with <b>/companion</b>.</p>")
                + "<p><small>The code works once, for 15 minutes.</small></p>")
        return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ Discord

    @app_commands.command(name="companion", description="Get the RCE Companion app – raid alerts on your phone.")
    @app_commands.guild_only()
    async def companion_cmd(self, interaction: discord.Interaction) -> None:
        if not self.linked_player(interaction.user.id):
            await interaction.response.send_message("Link your in-game player first: /link – then try again.",
                                                    ephemeral=True)
            return
        site = self.site_url()
        if not site:
            await interaction.response.send_message("The server's website address isn't set up yet – ask an admin.",
                                                    ephemeral=True)
            return
        code = self.new_code(interaction.user.id)
        paired = self.devices(interaction.user.id)
        await interaction.response.send_message(
            f"📱 **RCE Companion** – raid alerts as phone notifications.\n"
            f"**On your phone:** {site}/p/companion/pair?code={code}\n"
            f"(download + one tap to pair) – or type the code **{code}** in the app (server address: {site}).\n"
            f"The code works once, for 15 minutes."
            + (f"\n{paired} phone(s) paired already." if paired else ""), ephemeral=True)

    def status(self) -> dict:
        return {"Paired phones": len(self.data.items("tok:"))}
