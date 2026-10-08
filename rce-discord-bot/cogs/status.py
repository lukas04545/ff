"""Server-Status: Live-Embed, Bot-Aktivität, Spielerliste, Join/Leave-Erkennung.

Datenquellen (beide liefern auf RCE JSON):
  * "serverinfo"  -> Hostname, Spielerzahlen, Map, FPS, Uptime, ...
  * "playerlist"  -> DisplayName, Ping, ConnectedSeconds, Health (keine IDs!)

Joins/Leaves stehen auf der Console Edition nicht zuverlässig im Log. Sie werden
deshalb durch Vergleich zweier Spielerlisten erkannt (Verzögerung = Poll-Intervall).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from rce.rcon_client import RconError
from utils import format_duration, safe_md, strip_rich_text

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.status")

STATUS_MSG_KEY = "status_message_id"


def _parse_json(raw: str):
    # Manche Server-Antworten enthalten maskierte Zeilenumbrüche
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(raw.replace("\\n", "").strip())


class Status(commands.Cog):
    def __init__(self, bot: "RceBot") -> None:
        self.bot = bot
        self._synced = False           # erste Spielerliste nach (Re-)Connect: still übernehmen
        self._last_poll: float | None = None
        self._status_message: discord.Message | None = None
        self._parse_warned = False
        self._poll_lock = asyncio.Lock()  # verhindert doppelte Join/Leave-Meldungen

    async def cog_load(self) -> None:
        self.poll_players.change_interval(seconds=self.bot.config.playerlist_interval)
        self.update_status.change_interval(seconds=self.bot.config.status_interval)
        self.poll_players.start()
        self.update_status.start()

    async def cog_unload(self) -> None:
        self.poll_players.cancel()
        self.update_status.cancel()

    # ------------------------------------------------------------ Events

    @commands.Cog.listener()
    async def on_rce_connection(self, connected: bool) -> None:
        if connected:
            self._synced = False
            self._last_poll = None
            # Sofort aktualisieren statt auf das nächste Intervall zu warten
            self.bot.loop.create_task(self._refresh_now())
        else:
            self.bot.server_info = None
            await self._update_presence()

    async def _refresh_now(self) -> None:
        await self.bot.wait_until_ready()
        await self._poll_players_once()
        await self._update_status_once()

    # ------------------------------------------------------------ Spielerliste

    @tasks.loop(seconds=30)
    async def poll_players(self) -> None:
        await self._poll_players_once()

    async def _poll_players_once(self) -> None:
        async with self._poll_lock:
            await self._poll_players_locked()

    async def _poll_players_locked(self) -> None:
        if not self.bot.rcon.connected:
            return
        try:
            raw = await self.bot.rcon.command("playerlist")
            players = _parse_json(raw) if raw else []
        except RconError as exc:
            log.debug("playerlist fehlgeschlagen: %s", exc)
            return
        except json.JSONDecodeError:
            if not self._parse_warned:
                log.warning("Antwort auf 'playerlist' ist kein JSON: %r", raw[:200])
                self._parse_warned = True
            return

        current = {
            str(p.get("DisplayName") or p.get("Username") or "?"): p
            for p in players if isinstance(p, dict)
        }
        previous = self.bot.online_players
        now = time.monotonic()

        if self._synced:
            for name in current.keys() - previous.keys():
                self.bot.dispatch("rce_player_join", name)
            for name in previous.keys() - current.keys():
                self.bot.dispatch("rce_player_leave", name)
            # Spielzeit: vergangene Zeit seit dem letzten Poll gutschreiben (max. 2 Intervalle)
            if self._last_poll is not None and current:
                elapsed = min(now - self._last_poll, self.bot.config.playerlist_interval * 2)
                stayed = [n for n in current if n in previous]
                if stayed:
                    self.bot.db.add_playtime(stayed, int(elapsed))

        self.bot.online_players = current
        self._last_poll = now
        self._synced = True

    @poll_players.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ Status-Embed

    @tasks.loop(seconds=60)
    async def update_status(self) -> None:
        await self._update_status_once()

    @update_status.before_loop
    async def _before_status(self) -> None:
        await self.bot.wait_until_ready()

    async def _update_status_once(self) -> None:
        if self.bot.rcon.connected:
            try:
                raw = await self.bot.rcon.command("serverinfo")
                self.bot.server_info = _parse_json(raw)
            except (RconError, json.JSONDecodeError) as exc:
                log.debug("serverinfo fehlgeschlagen: %s", exc)
        await self._update_presence()
        await self._update_status_message()

    async def _update_presence(self) -> None:
        if not self.bot.is_ready():
            return
        info = self.bot.server_info
        if not self.bot.rcon.connected or not info:
            activity = discord.Activity(type=discord.ActivityType.watching, name="Server offline")
            status = discord.Status.dnd
        else:
            text = f"{info.get('Players', len(self.bot.online_players))}/{info.get('MaxPlayers', '?')} Spieler"
            if info.get("Queued"):
                text += f" (+{info['Queued']} Warteschlange)"
            activity = discord.Activity(type=discord.ActivityType.watching, name=text)
            status = discord.Status.online
        try:
            await self.bot.change_presence(activity=activity, status=status)
        except (discord.HTTPException, ConnectionError):
            pass

    def build_status_embed(self) -> discord.Embed:
        info = self.bot.server_info
        cfg = self.bot.config
        online = self.bot.rcon.connected and info is not None
        name = cfg.server_display_name or (strip_rich_text(str(info.get("Hostname", ""))) if info else "") \
            or "Rust Console Server"

        embed = discord.Embed(
            title=name[:256],
            colour=discord.Colour.green() if online else discord.Colour.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Status", value="🟢 Online" if online else "🔴 Offline / keine RCON-Verbindung")
        if online and info:
            players = f"**{info.get('Players', '?')}** / {info.get('MaxPlayers', '?')}"
            extra = [f"{info[k]} {label}" for k, label in (("Queued", "Warteschlange"), ("Joining", "verbinden"))
                     if info.get(k)]
            embed.add_field(name="Spieler", value=players + (f"\n({', '.join(extra)})" if extra else ""))
            embed.add_field(name="Map", value=str(info.get("Map", "?")))
            if "GameTime" in info:
                embed.add_field(name="Ingame-Zeit", value=str(info["GameTime"]))
            if "Uptime" in info:
                embed.add_field(name="Uptime", value=format_duration(float(info["Uptime"])))
            if "Framerate" in info:
                embed.add_field(name="Server-FPS", value=str(info["Framerate"]))

            names = sorted(self.bot.online_players, key=str.lower)
            if names:
                listing = ", ".join(safe_md(n) for n in names)
                if len(listing) > 1000:
                    listing = listing[:1000].rsplit(",", 1)[0] + " …"
                embed.add_field(name=f"Online ({len(names)})", value=listing, inline=False)
        embed.set_footer(text="Zuletzt aktualisiert")
        return embed

    async def _update_status_message(self) -> None:
        channel_id = self.bot.config.status_channel_id
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        embed = self.build_status_embed()
        try:
            if self._status_message is None:
                stored = self.bot.db.get_value(STATUS_MSG_KEY)
                if stored:
                    try:
                        self._status_message = await channel.fetch_message(int(stored))
                    except discord.NotFound:
                        self._status_message = None
            if self._status_message is not None:
                await self._status_message.edit(embed=embed)
                return
            self._status_message = await channel.send(embed=embed)
            self.bot.db.set_value(STATUS_MSG_KEY, str(self._status_message.id))
        except discord.NotFound:
            self._status_message = None  # Nachricht wurde gelöscht -> nächstes Mal neu senden
        except discord.HTTPException as exc:
            log.warning("Status-Nachricht konnte nicht aktualisiert werden: %s", exc)

    # ------------------------------------------------------------ Slash-Commands

    @app_commands.command(name="status", description="Zeigt den aktuellen Serverstatus.")
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._update_status_once()
        await interaction.followup.send(embed=self.build_status_embed())

    @app_commands.command(name="spieler", description="Listet alle Spieler, die gerade online sind.")
    async def players_cmd(self, interaction: discord.Interaction) -> None:
        if not self.bot.rcon.connected:
            await interaction.response.send_message("⚠️ Keine RCON-Verbindung zum Server.", ephemeral=True)
            return
        await interaction.response.defer()
        await self._poll_players_once()
        players = self.bot.online_players
        if not players:
            await interaction.followup.send("Niemand online.")
            return
        lines = []
        for name, data in sorted(players.items(), key=lambda kv: kv[0].lower()):
            connected = format_duration(float(data.get("ConnectedSeconds", 0) or 0))
            ping = data.get("Ping", "?")
            lines.append(f"• **{safe_md(name)}** – online seit {connected}, Ping {ping} ms")
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000].rsplit("\n", 1)[0] + "\n…"
        embed = discord.Embed(title=f"Online-Spieler ({len(players)})", description=text,
                              colour=discord.Colour.blurple())
        await interaction.followup.send(embed=embed)


async def setup(bot: "RceBot") -> None:
    await bot.add_cog(Status(bot))
