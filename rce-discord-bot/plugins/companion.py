"""RCE Companion: the players' Android app – raid alerts as phone notifications, server status and the website.

  /companion   on Discord: the app download and a one-time pairing link / code (15 minutes) for YOUR account

The app polls this server (every ~20 s while alerts are on) at /p/companion/feed with its own token and shows new
alerts as notifications. /p/companion/account gives the player's overview for the app's home screen (linked player,
kits with their claim timers, raid alerts, offline protection); /p/companion/claim claims a kit (same rules as
/customkit claim), /p/companion/alerts switches the raid alert DMs, /p/companion/rf sets the RF raid alarm frequency. Raid alerts (also the RF raid alarm) go to the app automatically; other plugins can call
push(user_id, title, text). Tokens are stored hashed; /companion again (or "Unpair" in the app) can end a pairing.
"""
from __future__ import annotations

import hashlib
import html
import json
import secrets
import time

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
    )

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        from web.ratelimit import RateLimiter
        self._reads = RateLimiter(40, 60)            # per phone: feed + account
        self._writes = RateLimiter(12, 60)           # per phone: claim, alerts, rf
        self._pair_tries = RateLimiter(10, 900)      # per visitor: wrong pairing codes
        self._bad_tokens = RateLimiter(30, 300)      # per visitor: requests with an unknown token

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
                                "next_wipe": self.next_wipe()},
                     "player_online": bool(player and self.online_name(player)), "kits": [], "owned_kits": []}
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
        if path in ("feed", "unpair", "account", "claim", "alerts", "rf") and uid is None:
            if (blocked := self._limited("bad:" + ip, self._bad_tokens)) is not None:
                return blocked                                   # guessing tokens
            return web.json_response({"error": "not paired"}, status=401)
        if uid is not None:
            limiter = self._writes if request.method == "POST" else self._reads
            if (blocked := self._limited(_hash(token), limiter)) is not None:
                return blocked
        if path == "account" and request.method == "GET":
            return web.json_response(await self.account(uid), headers={"Cache-Control": "no-store"})
        if path in ("claim", "alerts", "rf") and request.method == "POST":
            try:
                data = await request.json()
            except (ValueError, UnicodeDecodeError):
                data = {}
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
