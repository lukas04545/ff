"""Stream overlay: the server's name, players online and the next wipe as a live banner for TikTok LIVE Studio (or
OBS / Streamlabs) – like the server list entry in game.

  TikTok LIVE Studio → Add source → Link → paste the overlay address (shown on this plugin's card and with /overlay)

The page has a transparent background and updates itself every 15 seconds from /p/overlay/data.json (server name,
players, queue, online, next wipe – what the player website shows anyway, nothing private).
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

import discord
from aiohttp import web
from discord import app_commands

from plugin_api import Plugin, Setting
from utils import admin_only

ASSETS = Path(__file__).resolve().parent / "overlay_assets"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'none'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'")
HEX = re.compile(r"#[0-9a-fA-F]{6}")


class StreamOverlay(Plugin):
    NAME = "Stream overlay"
    ICON = "🎥"
    DESCRIPTION = ("A live banner for TikTok LIVE Studio (and OBS / Streamlabs): server name, players online and the "
                   "next wipe, updating by itself. Add it as a Link source – the address is on this card and with "
                   "/overlay.")
    DEFAULT_ENABLED = True
    SETTINGS = (
        Setting("title", "Name on the banner", "text", "", help="Empty = the server's name."),
        Setting("tags", "Line under the name", "text", "",
                help="E.g. “3X GATHER | 4.5K MAP | WEEKLY WIPES”. Empty = the next wipe (if there is one)."),
        Setting("show_wipe", "Count down to the next wipe", "bool", True,
                help="Shown on the right under the players when a wipe is planned."),
        Setting("accent", "Edge colour", "text", "#e5341c", help="Hex colour of the thin line on the left, e.g. #ff2e88."),
        Setting("bar_colour", "Banner colour", "text", "#1f2a14",
                help="Hex colour of the banner itself (the game's server list uses a dark green, #1f2a14)."),
    )

    # ------------------------------------------------------------ data

    def site_url(self) -> str:
        return (self.bot.config.web_public_url or "").rstrip("/")

    def overlay_url(self) -> str:
        return f"{self.site_url() or ''}/p/overlay/"

    def live(self) -> dict:
        info = getattr(self.bot, "server_info", None) or {}
        online = bool(self.bot.rcon.connected and info)
        wipe = self.next_wipe() if self.setting("show_wipe") else None
        return {"name": str(self.setting("title") or "").strip()[:60] or self.server_name,
                "tags": str(self.setting("tags") or "").strip()[:120],
                "online": online,
                "players": int(info.get("Players") or len(self.bot.online_players)) if online else 0,
                "max": int(info.get("MaxPlayers") or 0) if online else 0,
                "queued": int(info.get("Queued") or 0) if online else 0,
                "wipe": wipe if wipe and wipe > self.now() else None,
                "now": self.now()}

    def _colour(self, key: str, default: str) -> str:
        value = str(self.setting(key) or "").strip()
        return value if HEX.fullmatch(value) else default

    # ------------------------------------------------------------ web

    async def web_public(self, request, path: str):
        headers = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff"}
        if path in ("", "show") and request.method == "GET":
            d = self.live()
            page = (ASSETS / "overlay.html").read_text(encoding="utf-8")
            version = str(int(max((ASSETS / f).stat().st_mtime for f in ("overlay.js", "overlay.css"))))
            page = (page.replace("__V__", version).replace("__NAME__", html.escape(d["name"]))
                    .replace("__ACCENT__", self._colour("accent", "#e5341c"))
                    .replace("__BAR__", self._colour("bar_colour", "#1f2a14"))
                    .replace("__DATA__", html.escape(json.dumps(d), quote=True)))
            return web.Response(text=page, content_type="text/html", headers={**headers, "Cache-Control": "no-cache"})
        if path == "data.json" and request.method == "GET":
            return web.json_response(self.live(), headers={"Cache-Control": "no-store"})
        if path in ("overlay.js", "overlay.css") and request.method == "GET":
            kind = "text/javascript" if path.endswith(".js") else "text/css"
            return web.Response(body=(ASSETS / path).read_bytes(), content_type=kind,
                                headers={**headers, "Cache-Control": "public, max-age=3600"})
        raise web.HTTPNotFound(text="Not found.")

    # ------------------------------------------------------------ Discord / control panel

    @app_commands.command(name="overlay", description="The stream overlay's address for TikTok LIVE Studio (admins).")
    @admin_only()
    async def overlay_cmd(self, interaction: discord.Interaction) -> None:
        if not self.site_url():
            await interaction.response.send_message("The bot's web address isn't set up yet (WEB_PUBLIC_URL).",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(
            f"🎥 **Stream overlay:** {self.overlay_url()}\n"
            "TikTok LIVE Studio → **Add source → Link** → paste it, size **900 × 130**. It updates by itself.",
            ephemeral=True)

    def status(self) -> dict:
        return {"Overlay address": self.overlay_url() if self.site_url() else "set WEB_PUBLIC_URL first",
                "TikTok LIVE Studio": "Add source → Link, 900 × 130"}
