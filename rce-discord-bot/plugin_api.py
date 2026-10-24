"""Everything a plugin needs – see PLUGINS.md for a guide and an example.

A plugin is ONE Python file in plugins/ (comes with the bot) or data/plugins/ (your own, uploaded in the web
interface → 🧩 Plugins). It contains one class derived from `Plugin`:

    from plugin_api import Plugin, Setting

    class Hello(Plugin):
        NAME = "Hello"
        DESCRIPTION = "Says hello to players who join."
        SETTINGS = (Setting("text", "Message", default="Welcome {player}!"),)

        @Plugin.listener()
        async def on_rce_player_join(self, player):
            await self.say(self.setting("text").format(player=player))

A plugin is a discord.py Cog: slash commands, listeners (on_rce_kill, on_rce_player_join, on_member_join …) and
tasks.loop work as usual. Loops are started and stopped automatically. Settings appear in the web interface by
themselves, `self.data` stores whatever the plugin wants to keep (survives restarts and updates).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from rce.rcon_client import RconError
from utils import safe_md, sanitize_ingame_text, strip_rich_text

if TYPE_CHECKING:
    from bot import RceBot
    from plugin_manager import PluginManager

# Setting kinds: text, textarea, int, bool, choice, channel (text channel), role, kit (custom or in-game kit name),
# image (uploaded on the Plugins page, read with self.file(key))
SETTING_KINDS = ("text", "textarea", "int", "bool", "choice", "channel", "role", "kit", "image")


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str = "text"
    default: Any = None
    help: str = ""
    min: int | None = None
    max: int | None = None
    choices: tuple[str, ...] = field(default_factory=tuple)

    def clean(self, raw: Any) -> Any:
        """Validated value for this setting, raises ValueError with a readable message."""
        if raw is None or (isinstance(raw, str) and not raw.strip() and self.kind not in ("text", "textarea")):
            return self.default
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if self.kind == "int":
            try:
                value = int(str(raw).strip())
            except ValueError:
                raise ValueError(f'"{self.label}" must be a number.') from None
            if self.min is not None and value < self.min:
                raise ValueError(f'"{self.label}" must be at least {self.min}.')
            if self.max is not None and value > self.max:
                raise ValueError(f'"{self.label}" must be at most {self.max}.')
            return value
        if self.kind in ("channel", "role"):
            text = str(raw).strip()
            if not text.isdigit():
                raise ValueError(f'"{self.label}" must be a Discord ID.')
            return int(text)
        text = str(raw)
        if self.kind == "choice":
            if text not in self.choices:
                raise ValueError(f'"{self.label}": allowed values are {", ".join(self.choices)}.')
            return text
        if self.kind != "textarea" and "\n" in text:
            raise ValueError(f'"{self.label}" must not contain line breaks.')
        if len(text) > 4000:
            raise ValueError(f'"{self.label}" is too long (max. 4000 characters).')
        return text.strip() if self.kind != "textarea" else text.strip("\n")

    def describe(self) -> dict:
        return {"key": self.key, "label": self.label, "kind": self.kind, "default": self.default, "help": self.help,
                "min": self.min, "max": self.max, "choices": list(self.choices)}


class PluginData:
    """Key/value storage of one plugin (JSON values, kept in the bot database)."""

    def __init__(self, db, plugin_id: str) -> None:
        self._db, self.plugin_id = db, plugin_id

    def get(self, key: str, default: Any = None) -> Any:
        return self._db.plugin_get(self.plugin_id, key, default)

    def set(self, key: str, value: Any) -> None:
        self._db.plugin_set(self.plugin_id, key, value)

    def delete(self, key: str) -> bool:
        return self._db.plugin_delete(self.plugin_id, key)

    def items(self, prefix: str = "") -> dict[str, Any]:
        return self._db.plugin_items(self.plugin_id, prefix)

    def clear(self, prefix: str = "") -> int:
        return self._db.plugin_clear(self.plugin_id, prefix)


class Plugin(commands.Cog):
    """Base class of every plugin. Override the class attributes and add listeners/commands/loops."""

    NAME = "Plugin"
    DESCRIPTION = ""
    VERSION = "1.0"
    ICON = "🧩"
    AUTHOR = ""
    SETTINGS: tuple[Setting, ...] = ()
    INTENTS: tuple[str, ...] = ()        # privileged intents it needs: "members", "message_content"
    DEFAULT_ENABLED = False              # built-in plugins: on without the owner switching it on
    TAB: str | None = None               # in the control panel menu by default (the owner can change the menu)
    PUBLIC_PATH: str | None = None       # address of its public page if not /p/<plugin id> (e.g. "/" for the site root)

    listener = commands.Cog.listener     # @Plugin.listener() reads nicer in plugin files

    def __init__(self, bot: "RceBot", plugin_id: str, manager: "PluginManager | None" = None) -> None:
        self.bot = bot
        self.plugin_id = plugin_id
        self.manager = manager
        self.data = PluginData(bot.db, plugin_id)
        self.log = logging.getLogger(f"rce.plugin.{plugin_id}")
        self._starter: asyncio.Task | None = None

    # ------------------------------------------------------------ Lifecycle (override on_load / on_unload)

    async def on_load(self) -> None:
        """Called when the plugin is switched on (or the bot starts with it on)."""

    async def on_unload(self) -> None:
        """Called when the plugin is switched off, reloaded or the bot stops."""

    async def on_settings_changed(self) -> None:
        """Called after its settings were saved in the web interface."""

    def problems(self) -> list[str]:
        """Setup problems shown in the web interface (e.g. "choose a role"). Empty = all good."""
        return []

    def status(self) -> dict[str, Any]:
        """A few numbers shown on the plugin's card in the web interface."""
        return {}

    def profile_fields(self, member: discord.abc.User) -> list[tuple[str, str]]:
        """Extra fields for /profile: [(name, value)]."""
        return []

    def _loops(self) -> list[tasks.Loop]:
        return [getattr(self, name) for name in dir(type(self))
                if isinstance(getattr(type(self), name, None), tasks.Loop)]

    async def cog_load(self) -> None:
        await self.on_load()
        loops = self._loops()
        if loops:
            async def start() -> None:
                await self.bot.wait_until_ready()
                for loop in loops:
                    if not loop.is_running():
                        loop.start()
            self._starter = asyncio.create_task(start(), name=f"plugin-{self.plugin_id}-loops")

    async def cog_unload(self) -> None:
        if self._starter is not None:
            self._starter.cancel()
        for loop in self._loops():
            loop.cancel()
        await self.on_unload()

    # ------------------------------------------------------------ Settings

    def file(self, key: str) -> Path | None:
        """The uploaded file of an "image" setting (None = nothing uploaded)."""
        return self.manager.file_path(self.plugin_id, key) if self.manager is not None else None

    def setting(self, key: str) -> Any:
        spec = next((s for s in self.SETTINGS if s.key == key), None)
        if spec is None:
            raise KeyError(f"Plugin {self.plugin_id} has no setting {key!r}")
        values = self.manager.settings_of(self.plugin_id) if self.manager is not None else {}
        return values.get(key, spec.default)

    # ------------------------------------------------------------ Discord helpers

    @property
    def guild(self) -> discord.Guild | None:
        cfg = self.bot.config
        return self.bot.get_guild(cfg.guild_id) if cfg.guild_id else (self.bot.guilds[0] if self.bot.guilds else None)

    def channel(self, channel_id: int | None, *fallbacks: int | None):
        """The first configured channel the bot can see."""
        for cid in (channel_id, *fallbacks):
            ch = self.bot.get_channel(cid) if cid else None
            if ch is not None:
                return ch
        return None

    async def post(self, channel, content: str | None = None, *, embed: discord.Embed | None = None,
                   ping_role: int | None = None, **kwargs):
        """Sends a message; pings only the given role. Returns the message or None."""
        if channel is None:
            return None
        if ping_role:
            content = f"<@&{ping_role}>" + (f" {content}" if content else "")
        try:
            return await channel.send(content=content, embed=embed, allowed_mentions=discord.AllowedMentions(
                roles=[discord.Object(ping_role)] if ping_role else [], users=False, everyone=False), **kwargs)
        except discord.HTTPException as exc:
            self.log.warning("Message to #%s failed: %s", getattr(channel, "id", "?"), exc)
            return None

    def audit(self, text: str) -> None:
        self.bot.audit(f"{self.ICON} {text}")

    # ------------------------------------------------------------ Game helpers

    def online_players(self) -> list[str]:
        return list(self.bot.online_players)

    def online_name(self, player: str | None) -> str | None:
        """Exact online name for a player (case-insensitive), None if offline."""
        if not player:
            return None
        return next((n for n in self.bot.online_players if n.lower() == player.lower()), None)

    def linked_user(self, player: str) -> int | None:
        """Discord user ID linked to an in-game player (/customkit link), or None."""
        return self.bot.db.player_owner(player)

    def linked_player(self, user_id: int) -> str | None:
        return self.bot.db.linked_player(user_id)

    async def say(self, text: str) -> bool:
        """In-game chat message from the server."""
        try:
            await self.bot.rcon.command(f"say {sanitize_ingame_text(text)}", expect_response=False)
            return True
        except RconError as exc:
            self.log.debug("say failed: %s", exc)
            return False

    async def give_kit(self, player: str, kit: str) -> bool:
        """Gives a custom kit (bot kit) – or, if there is none with that name, the in-game kit."""
        if not kit:
            return False
        custom = self.bot.get_cog("CustomKits")
        row = self.bot.db.custom_kit(kit)
        try:
            if row is not None and custom is not None:
                sent, total = await custom.deliver(row["name"], player)
                return sent > 0
            await self.bot.rcon.command(self.bot.config.kit_give_template.format(kit=kit, name=player),
                                        expect_response=False)
            return True
        except RconError as exc:
            self.log.warning("Kit %s for %s failed: %s", kit, player, exc)
            return False

    # ------------------------------------------------------------ Points

    @property
    def points_enabled(self) -> bool:
        return self.bot.config.economy_enabled

    def fmt_points(self, amount: int) -> str:
        return f"{amount:,}".replace(",", " ") + f" {self.bot.config.points_name}"

    def give_points(self, user_id: int, amount: int, reason: str) -> int | None:
        """Adds points (if points & shop are on). Returns the new balance."""
        if not self.points_enabled or amount <= 0:
            return None
        return self.bot.db.add_points(user_id, amount, f"{self.NAME}: {reason}"[:200])

    def take_points(self, user_id: int, amount: int, reason: str) -> int | None:
        """Takes points if the balance is high enough. New balance, or None if too few."""
        return self.bot.db.spend_points(user_id, amount, f"{self.NAME}: {reason}"[:200])

    # ------------------------------------------------------------ Other plugins & bot parts

    def plugin(self, plugin_id: str) -> "Plugin | None":
        """Another running plugin (None if it is off)."""
        return self.manager.running(plugin_id) if self.manager is not None else None

    def next_wipe(self) -> int | None:
        wipe = self.bot.get_cog("Wipe")
        return wipe.schedule()["next"] if wipe is not None else None

    @property
    def tz(self):
        wipe = self.bot.get_cog("Wipe")
        if wipe is not None:
            return wipe.tz
        from cogs.wipe import get_tz
        return get_tz(self.bot.config.wipe_timezone)

    @property
    def server_name(self) -> str:
        info = self.bot.server_info or {}
        return self.bot.config.server_display_name or strip_rich_text(str(info.get("Hostname") or "")) or "the server"

    @staticmethod
    def now() -> int:
        return int(time.time())

    @staticmethod
    def md(text: str) -> str:
        return safe_md(text)
