"""Stream overlay: the server's name, players online and the next wipe as a live banner for TikTok LIVE Studio (or
OBS / Streamlabs) – like the server list entry in game, in your colours.

  TikTok LIVE Studio → Add source → Link → paste the overlay address (shown on this plugin's card and with /overlay)

The page has a transparent background and updates itself every 15 seconds from /p/overlay/data.json – the numbers and
the look: a changed setting shows on the stream within 15 seconds, without adding the source again. It only shows what
the player website shows anyway (name, players, queue, online, next wipe).
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
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'")
HEX = re.compile(r"#[0-9a-fA-F]{6}")
# setting -> (CSS variable, default)
COLOURS = {"bar_colour": ("--bar", "#1f2a14"), "accent": ("--accent", "#e5341c"), "name_colour": ("--name", "#ffffff"),
           "tags_colour": ("--tags", "#c9cbbd"), "count_colour": ("--count", "#ffffff"),
           "label_colour": ("--label", "#c9cbbd"), "icon_colour": ("--icon", "#e8e8e8"),
           "online_colour": ("--on", "#3ddc6a"), "offline_colour": ("--off", "#e5341c"),
           "progress_colour": ("--progress", "#3ddc6a")}
FONTS = ("game", "modern", "condensed", "mono")
ICONS = ("star", "logo", "none")
SHAPES = ("square", "rounded", "pill")


def _colour_setting(key: str, label: str, hint: str) -> Setting:
    return Setting(key, label, "text", COLOURS[key][1], help=f"Hex colour, e.g. {COLOURS[key][1]}. {hint}".strip())


class StreamOverlay(Plugin):
    NAME = "Stream overlay"
    ICON = "🎥"
    DESCRIPTION = ("A live banner for TikTok LIVE Studio (and OBS / Streamlabs): server name, players online and the "
                   "next wipe in your colours, updating by itself. Add it as a Link source – the address is on this "
                   "card and with /overlay. Changes here show on the stream within 15 seconds.")
    DEFAULT_ENABLED = True
    SETTINGS = (
        # ---- text
        Setting("title", "Name on the banner", "text", "", help="Empty = the server's name."),
        Setting("tags", "Line under the name", "text", "",
                help="E.g. “3X GATHER | 4.5K MAP | WEEKLY WIPES”. Empty = the next wipe (if there is one)."),
        Setting("messages", "More lines that take turns with it (one per line)", "textarea", "",
                help="E.g. “discord.gg/yourserver” or “Search 420 OFFICIAL in Community servers” – up to 8, shown "
                     "one after the other in the line under the name."),
        Setting("rotate_seconds", "Seconds per line", "int", 8, min=3, max=120),
        Setting("label", "Word under the player count", "text", "PLAYERS", help="E.g. PLAYERS, ONLINE, SURVIVORS."),
        # ---- what is shown
        Setting("icon", "Icon on the left", "choice", "star", choices=ICONS,
                help="star: like the game's server list · logo: the picture below (or the website's logo) · none."),
        Setting("logo", "Logo picture (for icon = logo)", "image",
                help="Square picture. Empty = the logo of the Player website."),
        Setting("show_players", "Show the player count", "bool", True),
        Setting("show_dot", "Show the online dot", "bool", True),
        Setting("show_queue", "Show the queue", "bool", True),
        Setting("show_wipe", "Count down to the next wipe", "bool", True,
                help="Under the player count (or in the line under the name when that's empty)."),
        Setting("show_progress", "Bar showing how full the server is", "bool", False,
                help="A thin bar along the bottom: players out of the maximum."),
        # ---- colours
        _colour_setting("bar_colour", "Banner colour", "The game's server list uses #1f2a14."),
        Setting("bar_opacity", "Banner opacity (%)", "int", 100, min=0, max=100,
                help="Lower it to let the stream shine through; 0 = text only."),
        _colour_setting("accent", "Edge colour", "The line on the left."),
        _colour_setting("name_colour", "Name colour", ""),
        _colour_setting("tags_colour", "Line colour", "The line under the name."),
        _colour_setting("count_colour", "Player count colour", ""),
        _colour_setting("label_colour", "Colour of the word under the count", ""),
        _colour_setting("icon_colour", "Star colour", ""),
        _colour_setting("online_colour", "Online dot colour", ""),
        _colour_setting("offline_colour", "Offline colour", "The dot and the word OFFLINE."),
        _colour_setting("progress_colour", "Fill bar colour", ""),
        # ---- shape and type
        Setting("font", "Font", "choice", "game", choices=FONTS,
                help="game: heavy like the server list · modern · condensed · mono."),
        Setting("uppercase", "Capital letters", "bool", True),
        Setting("shape", "Corners", "choice", "square", choices=SHAPES),
        Setting("edge_width", "Edge width", "int", 6, min=0, max=30, help="Pixels at 900 wide; 0 = no edge."),
        Setting("outline", "Thin outline in the edge colour", "bool", False),
        Setting("shadow", "Shadow under the banner", "bool", True),
    )

    # ------------------------------------------------------------ data

    def site_url(self) -> str:
        return (self.bot.config.web_public_url or "").rstrip("/")

    def overlay_url(self) -> str:
        return f"{self.site_url() or ''}/p/overlay/"

    def _colour(self, key: str) -> str:
        value = str(self.setting(key) or "").strip()
        return value if HEX.fullmatch(value) else COLOURS[key][1]

    def _choice(self, key: str, allowed: tuple[str, ...]) -> str:
        value = str(self.setting(key) or "")
        return value if value in allowed else allowed[0]

    def look(self) -> dict:
        """How the banner looks – sent with every refresh, so changes show on the stream by themselves."""
        messages = [m.strip()[:120] for m in str(self.setting("messages") or "").splitlines() if m.strip()][:8]
        icon = self._choice("icon", ICONS)
        logo = self.file("logo")
        site = self.plugin("website")
        logo_url = (f"logo?v={int(logo.stat().st_mtime)}" if logo is not None and logo.is_file() else
                    "logo?v=site" if site is not None else "")
        return {"colours": {var: self._colour(key) for key, (var, _d) in COLOURS.items()},
                "opacity": max(0, min(100, int(self.setting("bar_opacity") or 0))),
                "font": self._choice("font", FONTS), "upper": bool(self.setting("uppercase")),
                "shape": self._choice("shape", SHAPES), "edge": max(0, min(30, int(self.setting("edge_width") or 0))),
                "outline": bool(self.setting("outline")), "shadow": bool(self.setting("shadow")),
                "icon": icon if icon != "logo" or logo_url else "star", "logo": logo_url,
                "label": str(self.setting("label") or "").strip()[:20],
                "messages": messages, "rotate": max(3, min(120, int(self.setting("rotate_seconds") or 8))),
                "show": {k: bool(self.setting(f"show_{k}")) for k in ("players", "dot", "queue", "wipe", "progress")}}

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
                "now": self.now(), "look": self.look()}

    # ------------------------------------------------------------ web

    def _logo(self):
        own = self.file("logo")
        if own is not None and own.is_file():
            return own.read_bytes(), None
        site = self.plugin("website")
        if site is None:
            return None, None
        data, kind = site.picture("logo", 96)
        return data, kind

    async def web_public(self, request, path: str):
        headers = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff"}
        if path in ("", "show") and request.method == "GET":
            d = self.live()
            page = (ASSETS / "overlay.html").read_text(encoding="utf-8")
            version = str(int(max((ASSETS / f).stat().st_mtime for f in ("overlay.js", "overlay.css"))))
            style = ";".join(f"{var}:{value}" for var, value in d["look"]["colours"].items())
            page = (page.replace("__V__", version).replace("__NAME__", html.escape(d["name"]))
                    .replace("__STYLE__", style).replace("__DATA__", html.escape(json.dumps(d), quote=True)))
            return web.Response(text=page, content_type="text/html", headers={**headers, "Cache-Control": "no-cache"})
        if path == "data.json" and request.method == "GET":
            return web.json_response(self.live(), headers={"Cache-Control": "no-store"})
        if path in ("overlay.js", "overlay.css") and request.method == "GET":
            kind = "text/javascript" if path.endswith(".js") else "text/css"
            return web.Response(body=(ASSETS / path).read_bytes(), content_type=kind,
                                headers={**headers, "Cache-Control": "public, max-age=3600"})
        if path == "logo" and request.method == "GET":
            data, kind = self._logo()
            if not data:
                raise web.HTTPNotFound(text="No logo.")
            from web.item_images import image_type
            return web.Response(body=data, content_type=kind or image_type(data) or "image/png",
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
            "TikTok LIVE Studio → **Add source → Link** → paste it, size **900 × 130**. It updates by itself – "
            "change its look on the plugin card (Plugins → Stream overlay).",
            ephemeral=True)

    def status(self) -> dict:
        return {"Overlay address": self.overlay_url() if self.site_url() else "set WEB_PUBLIC_URL first",
                "TikTok LIVE Studio": "Add source → Link, 900 × 130"}
