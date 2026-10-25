"""Player count channel: a channel whose NAME shows how many players are online, e.g. "🟢 Online: 12/50".

By default the bot creates a voice channel for it at the top of the server that nobody can join (it's only there to be
read). Or pick your own channel. Discord lets a bot rename a channel only twice every 10 minutes, so the name is
updated at most every "Update every … minutes" (at least 5) – and only when the text actually changed.
"""
from __future__ import annotations

import time

import discord
from discord.ext import tasks

from plugin_api import Plugin, Setting

PLACEHOLDERS = "{players} online, {max} slots, {queued} in the queue, {joining} joining, {server} = server name."


class PlayerCount(Plugin):
    NAME = "Player count channel"
    ICON = "📊"
    DESCRIPTION = ("A channel whose name shows how many players are online, e.g. \"🟢 Online: 12/50\" – the bot "
                   "creates a voice channel nobody can join at the top of your server, or you pick a channel.")
    SETTINGS = (
        Setting("channel", "Channel", "channel",
                help="Empty = the bot creates a voice channel for it (at the top, nobody can join). Switch the "
                     "plugin off and delete that channel if you don't want it any more."),
        Setting("online_name", "Name while the server is online", "text", "🟢 Online: {players}/{max}",
                help=PLACEHOLDERS),
        Setting("offline_name", "Name while the server is offline", "text", "🔴 Server offline"),
        Setting("minutes", "Update every … minutes", "int", 5, min=5, max=120,
                help="Discord allows only 2 renames per channel every 10 minutes – so 5 is the fastest."),
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_rename = 0.0
        self._problem = ""

    # ------------------------------------------------------------ Card

    def problems(self) -> list[str]:
        return [self._problem] if self._problem else []

    def status(self) -> dict:
        channel = self._channel()
        return {"Channel": f"#{channel.name}" if channel is not None else "–",
                "Shows": self.wanted_name()}

    # ------------------------------------------------------------ Name

    def wanted_name(self) -> str:
        info = self.bot.server_info or {}
        if not self.bot.rcon.connected or not info:
            return (str(self.setting("offline_name") or "").strip() or "🔴 Server offline")[:100]
        values = {"players": info.get("Players", len(self.bot.online_players)), "max": info.get("MaxPlayers", "?"),
                  "queued": info.get("Queued", 0), "joining": info.get("Joining", 0), "server": self.server_name}
        template = str(self.setting("online_name") or "").strip() or "🟢 Online: {players}/{max}"
        try:
            name = template.format(**values)
        except (KeyError, IndexError, ValueError):
            name = "🟢 Online: {players}/{max}".format(**values)
        return name[:100] or "Players"                          # Discord: 1–100 characters

    # ------------------------------------------------------------ Channel

    def _channel(self):
        chosen = self.setting("channel")
        if chosen:
            return self.bot.get_channel(int(chosen))
        created = self.data.get("created_channel")
        return self.bot.get_channel(int(created)) if created else None

    async def _create(self, name: str):
        guild = self.guild
        if guild is None:
            return None
        try:
            channel = await guild.create_voice_channel(
                name, position=0, reason="Player count channel (plugin)",
                overwrites={guild.default_role: discord.PermissionOverwrite(connect=False)})
        except discord.HTTPException as exc:
            self._problem = (f"Couldn't create the channel ({exc}). Give the bot the \"Manage Channels\" permission "
                             "or pick a channel in the settings.")
            return None
        self.data.set("created_channel", channel.id)
        self.audit(f"created the player count channel #{channel.name}")
        return channel

    async def refresh(self, *, force: bool = False) -> bool:
        """Renames the channel if its name is out of date (at most every "minutes"). True if it was renamed."""
        if not self.bot.is_ready():
            return False
        name = self.wanted_name()
        channel = self._channel()
        if channel is None:
            if self.setting("channel"):
                self._problem = "The chosen channel wasn't found – pick another one."
                return False
            channel = await self._create(name)
            if channel is None:
                return False
            self._last_rename = time.monotonic()
            self._problem = ""
            return True
        if channel.name == name:
            self._problem = ""
            return False
        if not force and time.monotonic() - self._last_rename < self.setting("minutes") * 60:
            return False
        try:
            await channel.edit(name=name, reason="Player count")
        except discord.HTTPException as exc:
            self._problem = f"Couldn't rename #{channel.name} ({exc}) – the bot needs \"Manage Channels\" on it."
            return False
        self._last_rename = time.monotonic()
        self._problem = ""
        return True

    async def on_settings_changed(self) -> None:
        self._last_rename = 0.0                                 # show a changed text at the next check

    @tasks.loop(seconds=60)
    async def tick(self) -> None:
        await self.refresh()
