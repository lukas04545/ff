"""Hilfsfunktionen: Berechtigungen, Text-Bereinigung, gepufferte Channel-Ausgabe."""
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
# Berechtigungen
# --------------------------------------------------------------------------
class NotAdmin(app_commands.CheckFailure):
    pass


def is_admin(bot: "RceBot", user: discord.abc.User) -> bool:
    cfg = bot.config
    if user.id in cfg.admin_user_ids:
        return True
    roles = getattr(user, "roles", [])  # nur Member (im Server) haben Rollen
    return any(role.id in cfg.admin_role_ids for role in roles)


def admin_only():
    """Decorator für Slash-Commands: nur konfigurierte Rollen/User."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.client, interaction.user):  # type: ignore[arg-type]
            raise NotAdmin()
        return True

    return app_commands.check(predicate)


# --------------------------------------------------------------------------
# Text-Bereinigung
# --------------------------------------------------------------------------
def strip_rich_text(text: str) -> str:
    """Entfernt Rust-Rich-Text-Tags wie <color=red> aus Servernamen o. Ä."""
    return _RICH_TEXT_TAG.sub("", text)


def safe_md(text: str) -> str:
    """Spielertext sicher für Discord (keine Formatierung, keine @-Pings)."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def sanitize_player_name(name: str) -> str:
    """Spielername für RCON-Befehle: keine Anführungszeichen/Zeilenumbrüche."""
    return name.replace('"', "").replace("\n", " ").replace("\r", " ").strip()


def sanitize_ingame_text(text: str, limit: int = 180) -> str:
    """Text für 'say': keine Rich-Text-Injection, keine Zeilenumbrüche."""
    cleaned = re.sub(r"[<>\"\r\n]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit]


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} T {hours} Std"
    if hours:
        return f"{hours} Std {minutes} Min"
    return f"{minutes} Min"


# --------------------------------------------------------------------------
# Gepufferte Channel-Ausgabe (schont Discords Rate-Limits)
# --------------------------------------------------------------------------
class ChannelBuffer:
    """Sammelt Zeilen/Embeds und sendet sie gebündelt alle paar Sekunden.

    Ohne Bündelung würde ein voller Server mit vielen Kills schnell in Discords
    Rate-Limit (ca. 5 Nachrichten / 5 s pro Channel) laufen.
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
            await asyncio.sleep(self.interval)  # erst sammeln, dann gebündelt senden
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
                log.error("Channel %s nicht gefunden oder kein Textchannel – Feed wird verworfen.",
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
                log.error("Keine Schreibrechte in Channel %s.", self.channel_id)
                self._warned = True
        except discord.HTTPException as exc:
            log.warning("Senden in Channel %s fehlgeschlagen: %s", self.channel_id, exc)
