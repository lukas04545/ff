"""Helpers: permissions, text sanitizing, buffered channel output."""
from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from bot import RceBot

log = logging.getLogger("rce.utils")

_RICH_TEXT_TAG = re.compile(r"<[^>]{1,40}>")


# --------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------
class NotAdmin(app_commands.CheckFailure):
    pass


def is_admin(bot: "RceBot", user: discord.abc.User) -> bool:
    cfg = bot.config
    if user.id in cfg.admin_user_ids:
        return True
    roles = getattr(user, "roles", [])  # only members (in the server) have roles
    return any(role.id in cfg.admin_role_ids for role in roles)


def admin_only():
    """Decorator for slash commands: configured roles/users only."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.client, interaction.user):  # type: ignore[arg-type]
            raise NotAdmin()
        return True

    return app_commands.check(predicate)


def player_choices(bot: "RceBot", current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete: online players first, then known players from the DB."""
    current_l = current.lower()
    names = [n for n in bot.online_players if current_l in n.lower()]
    for n in bot.db.search_names(current):
        if n not in names:
            names.append(n)
    return [app_commands.Choice(name=n[:100], value=n[:100]) for n in names[:25]]


# --------------------------------------------------------------------------
# Text sanitizing
# --------------------------------------------------------------------------
def strip_rich_text(text: str) -> str:
    """Strips Rust rich text tags like <color=red> from server names etc."""
    return _RICH_TEXT_TAG.sub("", text)


def safe_md(text: str) -> str:
    """Makes player text safe for Discord (no formatting, no @ pings)."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def sanitize_player_name(name: str) -> str:
    """Player name for RCON commands: no quotes/line breaks."""
    return name.replace('"', "").replace("\n", " ").replace("\r", " ").strip()


def sanitize_ingame_text(text: str, limit: int = 180) -> str:
    """Text for 'say': no rich text injection, no line breaks."""
    cleaned = re.sub(r"[<>\"\r\n]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit]


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes} min"


_DURATION_UNITS = {"d": 1440, "h": 60, "m": 1}


def parse_duration(text: str) -> int | None:
    """Duration like "90", "90m", "12h", "1d 12h" or "off"/"0" -> minutes. None = invalid."""
    text = text.strip().lower().replace(" ", "")
    if text in ("0", "off", "none", "no"):
        return 0
    if text.isdigit():
        return int(text)
    parts = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m(?:in)?)?", text)
    if not parts or not any(parts.groups()):
        return None
    return sum(int(v) * unit for v, unit in zip(parts.groups(), _DURATION_UNITS.values()) if v)


def format_minutes(minutes: int) -> str:
    """Cooldown for display: 0 -> "no cooldown", 1500 -> "1d 1h"."""
    return format_duration(minutes * 60) if minutes else "no cooldown"


# --------------------------------------------------------------------------
# Buffered channel output (spares Discord's rate limits)
# --------------------------------------------------------------------------
class ChannelBuffer:
    """Collects lines/embeds and sends them in batches every few seconds.

    Without batching, a full server with lots of kills would quickly hit Discord's
    rate limit (about 5 messages / 5 s per channel).
    """

    def __init__(self, bot: "RceBot", channel_id: int, *, interval: float = 2.0,
                 code_block: bool = False, max_items: int = 300) -> None:
        self.bot = bot
        self.channel_id = channel_id
        self.interval = interval
        self.code_block = code_block
        self._lines: deque[str] = deque(maxlen=max_items)
        self._embeds: deque[discord.Embed] = deque(maxlen=max_items)
        self._task: asyncio.Task | None = None
        self._warned = False

    def add_line(self, line: str) -> None:
        self._lines.append(line[:1800])
        self._ensure_task()

    def add_embed(self, embed: discord.Embed) -> None:
        self._embeds.append(embed)
        self._ensure_task()

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"feed-{self.channel_id}")

    async def _run(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            await asyncio.sleep(self.interval)  # collect first, then send as a batch
            await self.flush()
            if not (self._lines or self._embeds):
                return

    def _chunks(self) -> list[str]:
        wrapper = 8 if self.code_block else 0
        chunks: list[str] = []
        current = ""
        while self._lines:
            line = self._lines.popleft()
            if current and len(current) + len(line) + 1 + wrapper > 2000:
                chunks.append(current)
                current = ""
            current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)
        if self.code_block:
            chunks = [f"```\n{c.replace('```', 'ˋˋˋ')}\n```" for c in chunks]
        return chunks

    async def flush(self) -> None:
        channel = self.bot.get_channel(self.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            if not self._warned:
                log.error("Channel %s not found or not a text channel – discarding feed.",
                          self.channel_id)
                self._warned = True
            self._lines.clear()
            self._embeds.clear()
            return
        try:
            for chunk in self._chunks():
                await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
            while self._embeds:
                batch = [self._embeds.popleft() for _ in range(min(10, len(self._embeds)))]
                await channel.send(embeds=batch, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            if not self._warned:
                log.error("No permission to write in channel %s.", self.channel_id)
                self._warned = True
        except discord.HTTPException as exc:
            log.warning("Sending to channel %s failed: %s", self.channel_id, exc)
