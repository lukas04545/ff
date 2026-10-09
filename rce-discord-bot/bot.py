"""Discord-Bot für Rust Console Edition (G-Portal) über WebRCON.

Start:  python bot.py   (oder start.bat / start.sh – richten alles automatisch ein)
"""
from __future__ import annotations

import itertools
import json
import logging
import logging.handlers
import os
import sys
import time
from collections import deque
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from config import Config, ConfigError, load_config
from logbuffer import memory_log
from rce import log_parser
from rce.rcon_client import RconClient, RconError, RconMessage
from storage import Storage
from utils import ChannelBuffer, NotAdmin

log = logging.getLogger("rce.bot")

# Exit-Code für Fehler, bei denen ein Neustart nichts bringt (start.bat/start.sh starten dann nicht neu)
EXIT_FATAL = 3
BASE_DIR = Path(__file__).resolve().parent

COGS = ("cogs.status", "cogs.feeds", "cogs.admin", "cogs.stats", "cogs.kits", "cogs.custom_kits", "cogs.tickets",
        "cogs.community", "cogs.moderation", "cogs.giveaways")

# Ereignisklasse -> Discord-Eventname (Listener heißen dann on_<name>)
EVENT_NAMES: dict[type, str] = {
    log_parser.ChatEvent: "rce_chat",
    log_parser.KillEvent: "rce_kill",
    log_parser.SuicideEvent: "rce_suicide",
    log_parser.RespawnEvent: "rce_respawn",
    log_parser.BanEvent: "rce_ban",
    log_parser.RoleEvent: "rce_role",
    log_parser.TeamEvent: "rce_team",
    log_parser.AdminActionEvent: "rce_admin_action",
    log_parser.ServerEvent: "rce_server_event",
    log_parser.SaveEvent: "rce_save",
}


class RceBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        # Nachrichteninhalt (privilegierter Intent, im Developer Portal aktivieren) brauchen
        # die Chat-Bridge Discord -> Spiel und die Support-Tickets (KI liest die Nachrichten).
        # Discord-Log (bearbeitete/gelöschte Nachrichten) braucht ihn ebenfalls.
        intents.message_content = bool(
            (config.chat_channel_id and config.chat_bridge_to_game) or config.ticket_category_id
            or config.discord_log_channel_id)
        # Server-Mitglieder-Intent (privilegiert): Willkommensnachricht, Auto-Rolle, Join/Leave-Log
        intents.members = bool(config.welcome_channel_id or config.auto_role_id or config.discord_log_channel_id)
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

        self.config = config
        self.db = Storage(config.database_path)
        self.rcon = RconClient(config.rcon_host, config.rcon_port, config.rcon_password)

        # Live-Zustand, gepflegt von cogs/status.py
        self.online_players: dict[str, dict] = {}
        self.server_info: dict | None = None

        self._feeds: dict[int, ChannelBuffer] = {}

        # Letzte Konsolenzeilen für das Webinterface (id, Zeitstempel, Text)
        self.console_buffer: deque[dict] = deque(maxlen=1000)
        self._console_ids = itertools.count(1)
        self.web = None  # web.server.WebInterface, falls aktiviert

    # ------------------------------------------------------------ Lebenszyklus

    async def setup_hook(self) -> None:
        self.rcon.add_message_listener(self._on_rcon_message)
        self.rcon.add_state_listener(self._on_rcon_state)

        for cog in COGS:
            await self.load_extension(cog)

        self.tree.on_error = self.on_app_command_error
        if self.config.guild_id:
            # Guild-Sync: Befehle sind sofort sichtbar (global kann bis zu 1 h dauern)
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("%d Slash-Commands synchronisiert.", len(synced))

        await self.rcon.start()

        if self.config.web_enabled:
            from web.server import WebInterface
            self.web = WebInterface(self)
            await self.web.start()

    async def on_ready(self) -> None:
        log.info("Eingeloggt als %s (ID %s).", self.user, self.user.id if self.user else "?")

    async def close(self) -> None:
        if self.web is not None:
            await self.web.stop()
        await self.rcon.close()
        for buffer in self._feeds.values():
            await buffer.flush()
        await super().close()
        self.db.close()

    # ------------------------------------------------------------ Feeds

    def feed(self, channel_id: int | None, *, code_block: bool = False) -> ChannelBuffer | None:
        """Gepufferter Ausgabekanal; None wenn der Channel nicht konfiguriert ist."""
        if not channel_id:
            return None
        if channel_id not in self._feeds:
            self._feeds[channel_id] = ChannelBuffer(self, channel_id, code_block=code_block)
        return self._feeds[channel_id]

    def discord_log(self, embed: discord.Embed) -> None:
        """Eintrag im Discord-Log-Channel (Mitglieder, Nachrichten, Moderation, Giveaways)."""
        feed = self.feed(self.config.discord_log_channel_id)
        if feed:
            if embed.timestamp is None:
                embed.timestamp = discord.utils.utcnow()
            feed.add_embed(embed)

    def audit(self, text: str) -> None:
        """Eintrag im Admin-Log-Channel (falls konfiguriert)."""
        feed = self.feed(self.config.admin_log_channel_id)
        if feed:
            feed.add_line(text)

    # ------------------------------------------------------------ RCON -> Events

    async def _on_rcon_message(self, msg: RconMessage) -> None:
        self.dispatch("rce_console", msg)
        now = time.time()
        for line in msg.text.splitlines():
            if line.strip():
                self.console_buffer.append({"id": next(self._console_ids), "ts": now, "text": line.strip()})

        # Fallback: Rust-PC-Format, falls ein Server Chat als JSON (Type "Chat") sendet.
        if msg.type == "Chat":
            try:
                data = json.loads(msg.text)
                text = str(data.get("Message", ""))
                self.dispatch("rce_chat", log_parser.ChatEvent(
                    "GLOBAL", str(data.get("Username", "?")), text, text))
                return
            except (json.JSONDecodeError, AttributeError):
                pass

        for line in msg.text.splitlines():
            event = log_parser.parse_line(line)
            if event is not None:
                self.dispatch(EVENT_NAMES[type(event)], event)

    async def _on_rcon_state(self, connected: bool) -> None:
        self.dispatch("rce_connection", connected)

    # ------------------------------------------------------------ Fehler

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, NotAdmin):
            text = "⛔ Dafür hast du keine Berechtigung (nur für konfigurierte Admin-Rollen/User)."
        elif isinstance(error, app_commands.NoPrivateMessage):
            text = "Dieser Befehl funktioniert nur auf dem Discord-Server, nicht per DM."
        elif isinstance(error, app_commands.MissingPermissions):
            text = "⛔ Dir fehlt die Discord-Berechtigung: " + ", ".join(error.missing_permissions)
        elif isinstance(error, app_commands.BotMissingPermissions):
            text = "⚠️ Dem Bot fehlt die Discord-Berechtigung: " + ", ".join(error.missing_permissions)
        elif isinstance(original, discord.Forbidden):
            text = "⚠️ Discord hat die Aktion verweigert – fehlen dem Bot Rechte oder steht seine Rolle zu tief?"
        elif isinstance(original, RconError):
            text = f"⚠️ {original}"
        else:
            log.error("Unerwarteter Fehler in /%s", interaction.command and interaction.command.name,
                      exc_info=original)
            text = "❌ Unerwarteter Fehler. Details stehen im Bot-Log."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


def setup_logging() -> None:
    # Windows-Konsole: UTF-8 erzwingen, sonst brechen Umlaute/Emojis in Logzeilen
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    (BASE_DIR / "logs").mkdir(exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        BASE_DIR / "logs" / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    console = logging.StreamHandler()
    for handler in (file_handler, console, memory_log):
        handler.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[console, file_handler, memory_log])
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def fatal(message: str) -> None:
    log.critical(message)
    sys.exit(EXIT_FATAL)


def main() -> None:
    # Arbeitsverzeichnis = Bot-Ordner, damit .env, Datenbank und Wissensbasis auch bei
    # Doppelklick oder Autostart gefunden werden.
    os.chdir(BASE_DIR)
    setup_logging()

    if not Path(".env").exists():
        fatal("Keine .env-Datei gefunden. Kopiere .env.example nach .env und trage deine Daten ein.")
    try:
        config = load_config()
    except ConfigError as exc:
        fatal(f"Konfigurationsfehler: {exc}")

    bot = RceBot(config)
    try:
        bot.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        fatal("Discord-Login fehlgeschlagen: DISCORD_TOKEN ist ungültig.")
    except discord.PrivilegedIntentsRequired:
        fatal("Ein privilegierter Intent ist im Developer Portal nicht aktiviert. Unter Bot -> Privileged "
              "Gateway Intents 'Message Content Intent' (Chat-Bridge, Tickets, Discord-Log) und 'Server Members "
              "Intent' (Willkommen, Auto-Rolle, Join/Leave-Log) einschalten – oder die Features in der .env leeren.")


if __name__ == "__main__":
    main()
